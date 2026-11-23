"""Day 13 下半：从群里一句话读出「任务现在怎么样了」。

为什么要这一层：

现在改状态只有两条路 —— 点卡片按钮，或者命令行 taskctl。
但真实现场是这样的：负责人在群里回一句「做完了」，然后人就走了。
系统看不见这句话，任务停在「进行中」，发起人也不知道该验收了。

跟进系统最怕的不是功能少，是「人以为系统知道」。
所以这一层只干一件事：把回复里的一句话，翻译成一个状态动作。

【刻意不做的两件事】

1. 处理回复原派活消息或私聊任务卡的反馈。
   群里同时有五件事在跑，猜错一次就改错一条任务。
   代价不对等：让人多点一下「回复」，比改错一条任务便宜得多。

2. 不猜「大概是想说完成了」。
   规则命中不了就当没看见，继续走原来的任务识别流程。
   宁可漏，不可错 —— 状态被悄悄改错，比没改更难查。
"""

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from sqlalchemy import select

import db
import permissions
import repo
import state_machine
import deadline
from models import MessageCommand
from models_db import TASK_TERMINAL, Task, TaskCandidate, TaskCard

# 动作 → 关键词。列表顺序就是优先级。
# 为什么「完成」排最前：「已经做完了，不用再等审批」这种句子会同时命中
# 「完成」和「取消」，结论应该取那个「事情已经结束」的。
_RULES: List[Tuple[str, Tuple[str, ...]]] = [
    ("submit", ("做完了", "完成了", "已完成", "搞定了", "弄完了", "交付了",
                "提交了", "已提交", "已经提交", "发你了", "已发出",
                "收尾了", "写完了", "改完了", "处理完了", "处理好了",
                "解决完了", "已经解决", "已解决")),
    ("block", ("卡住", "被卡", "阻塞", "堵住", "推不动", "推进不了",
               "没法继续", "做不下去", "等审批", "等确认", "等回复")),
    ("cancel", ("取消了", "不做了", "别做了", "不用做了", "作废", "这个不做")),
    ("start", ("开始做", "刚开始", "在做", "正在做", "开工", "着手")),
]

# 问句不是结论。「做完了吗」「搞定没有？」都不是在说「已经做完」。
_QUESTION = re.compile(r"[?？]|(?:吗|么|没有|没|了吗|了没)[。.!！\s]*$")

# 条件、计划和转述不能作为操作者已经执行动作的依据。
_NON_ASSERTION = re.compile(r"如果|假如|要是|等到|一旦|计划|打算|准备|预计|争取|希望|请问|是不是|是否")

# 否定：「还没做完」「没卡住」。关键词前面三个字里出现否定词就整条作废。
# 只往前看，所以「不做了」这种自带否定的说法不会被自己挡掉。
_NEG_TAIL = re.compile(r"(还没|没有|没|未|不|别)$")


@dataclass
class Reading:
    """一句话的解读结果。"""

    action: str
    keyword: str
    reason: str
    note: Optional[str] = None


@dataclass
class Applied:
    """把一句话落到任务上的结果。text 是准备回给群里的那句话。"""

    ok: bool
    text: str = ""
    task: Optional[Task] = None


def classify(text: str) -> Optional[Reading]:
    """一句话 → 动作。识别不出来返回 None（不是报错，是「这不是状态反馈」）。"""
    body = (text or "").strip()
    if not body:
        return None
    if _QUESTION.search(body) or _NON_ASSERTION.search(body):
        return None

    review_text = re.sub(r"^(?:ok|好的?)[，,\s]*", "", body, flags=re.IGNORECASE).rstrip("。.!！ ")
    review = re.fullmatch(r"(验收通过|验收合格|验收不通过|验收打回|打回重做|取消任务)(?:[：:，,]\s*(.+))?", review_text)
    review_action = {
        "验收通过": "accept", "验收合格": "accept",
        "验收不通过": "reject", "验收打回": "reject", "打回重做": "reject",
        "取消任务": "cancel",
    }.get(review.group(1) if review else "")
    if review_action:
        return Reading(review_action, review.group(1), "明确的处理结论", note=review.group(2) or "")

    for action, words in _RULES:
        for keyword in words:
            start = body.find(keyword)
            if start < 0:
                continue
            if _NEG_TAIL.search(body[max(0, start - 3):start]):
                continue
            return Reading(action, keyword, "命中「%s」" % keyword)
    return None


def resolve_tasks(cmd: MessageCommand) -> List[Task]:
    """优先关联直接回复的任务卡，再查根消息；私聊按收件人校验。"""
    ids = list(dict.fromkeys(x for x in (cmd.parent_id, cmd.root_id) if x))
    for message_id in ids:
        with db.session_scope() as session:
            if cmd.chat_type == "p2p":
                task_uids = list(session.scalars(
                    select(Task.uid).join(TaskCard, TaskCard.task_id == Task.id).where(
                        TaskCard.message_id == message_id,
                        TaskCard.receive_id_type == "open_id",
                        TaskCard.receive_id == cmd.sender_open_id,
                        TaskCard.viewer_open_id == cmd.sender_open_id,
                    )
                ).all())
            else:
                task_uids = list(session.scalars(
                    select(Task.uid).join(TaskCandidate, Task.candidate_uid == TaskCandidate.uid).where(
                        TaskCandidate.message_id == message_id, Task.chat_id == cmd.chat_id,
                    )
                ).all())
        tasks = [repo.get_task(uid) for uid in dict.fromkeys(task_uids)]
        tasks = [task for task in tasks if task and permissions.chat_enabled(task.chat_id)]
        if tasks:
            return tasks
    return []


def _would_succeed(task: Task, action: str, open_id: str) -> bool:
    """预演一次：状态机放行 + 权限放行。不改任何东西。"""
    _dst, check = state_machine.resolve_action(task.status, action)
    if not check.ok:
        return False
    return permissions.can(task, open_id, action).ok


def apply_feedback(cmd: MessageCommand) -> Applied:
    """把一句话落到任务上。返回发送给操作者私聊的处理结果。

    多层防护，每层都对应一个真实的错法：
    - 不是状态反馈       → 当作没看见，继续走任务识别
    - 没回复任何消息     → 不猜
    - 回复的消息下没任务 → 不猜
    - 任务都已结束       → 告诉他不用改了
    - 一句话对应多条任务 → 只处理唯一能成的那条，否则让他点卡片
    """
    if (cmd.text or "").strip().splitlines()[:1] == ["补充信息"]:
        return _apply_supplement(cmd)
    reading = classify(cmd.text)
    if not reading:
        return Applied(False)

    tasks = resolve_tasks(cmd)
    if not tasks:
        return Applied(False)

    active = [t for t in tasks if t.status not in TASK_TERMINAL]
    if not active:
        return Applied(False, "这条消息下的任务都已经结束了，不用再改状态。")

    # 一句话可能对应多条任务（一条消息里派了三件事）。这时不猜，
    # 先用状态机和权限各预演一遍，只处理「唯一能成」的那条。
    possible = [t for t in active
                if _would_succeed(t, reading.action, cmd.sender_open_id)]
    if not possible:
        titles = "、".join("「%s」" % t.title for t in active[:3])
        return Applied(
            False,
            "没找到能执行「%s」的任务（%s）。可能是状态不对，或者你没有这个权限。"
            % (state_machine.ACTION_LABELS.get(reading.action, reading.action), titles),
        )
    if len(possible) > 1:
        titles = "、".join("「%s」" % t.title for t in possible[:3])
        return Applied(
            False,
            "这条消息下有 %d 条任务都能执行「%s」（%s），请在任务卡上点按钮，别让我猜。"
            % (len(possible),
               state_machine.ACTION_LABELS.get(reading.action, reading.action),
               titles),
        )

    target = possible[0]
    result = repo.transition_task(
        target.uid, reading.action, operator_open_id=cmd.sender_open_id,
        note=(reading.note or "") if reading.action in ("reject", "cancel") else cmd.text[:2000],
    )
    if not result.ok:
        return Applied(False, "状态没改成：%s" % result.message)

    fresh = result.task or target
    label = state_machine.STATUS_LABELS.get(fresh.status, fresh.status)
    return Applied(
        True,
        "已记录：「%s」→ %s（v%d）" % (fresh.title, label, fresh.version),
        fresh,
    )


def _apply_supplement(cmd: MessageCommand) -> Applied:
    tasks = resolve_tasks(cmd)
    if len(tasks) != 1:
        return Applied(False, "请回复需要补充的那张任务卡，再填写交付物和截止时间。")
    fields = {}
    for line in cmd.text.strip().splitlines()[1:]:
        if not line.strip():
            continue
        match = re.fullmatch(r"\s*(交付物|截止时间)\s*[：:]\s*(.+?)\s*", line)
        if not match or match.group(1) in fields:
            return Applied(False, "请每行填写一项，格式为“交付物：具体成果”或“截止时间：明天18点前”。")
        fields[match.group(1)] = match.group(2)
    parsed = None
    if "截止时间" in fields:
        parsed = deadline.parse_deadline(fields["截止时间"], cmd.sent_at or db.now())
        if not parsed.ok:
            return Applied(False, "截止时间还不明确，请填写具体日期和时间后重试。")
    result = repo.supplement_task(
        tasks[0].uid, cmd.sender_open_id, deliverable=fields.get("交付物"),
        deadline_at=parsed.deadline_at if parsed else None,
        deadline_text=parsed.raw if parsed else None, note=cmd.text[:2000],
    )
    text = result.message
    if result.ok and parsed:
        text += f"；截止时间按 {parsed.deadline_at:%Y-%m-%d %H:%M} 记录"
        if parsed.assumed:
            text += "（含默认时间口径）"
    return Applied(result.ok, text, result.task)
