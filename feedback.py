"""Day 13 下半：从群里一句话读出「任务现在怎么样了」。

为什么要这一层：

现在改状态只有两条路 —— 点卡片按钮，或者命令行 taskctl。
但真实现场是这样的：负责人在群里回一句「做完了」，然后人就走了。
系统看不见这句话，任务停在「进行中」，发起人也不知道该验收了。

跟进系统最怕的不是功能少，是「人以为系统知道」。
所以这一层只干一件事：把回复里的一句话，翻译成一个状态动作。

【刻意不做的两件事】

1. 不认「@机器人 做完了」这种不带回复的消息。
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
from models import MessageCommand
from models_db import TASK_TERMINAL, Task, TaskCandidate

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
_QUESTION = re.compile(r"[?？]\s*$|\s*吗\s*$")

# 否定：「还没做完」「没卡住」。关键词前面三个字里出现否定词就整条作废。
# 只往前看，所以「不做了」这种自带否定的说法不会被自己挡掉。
_NEG_TAIL = re.compile(r"(还没|没有|没|未|不|别)$")


@dataclass
class Reading:
    """一句话的解读结果。"""

    action: str
    keyword: str
    reason: str


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
    if _QUESTION.search(body):
        return None

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
    """从回复关系里找出这句话说的是哪几条任务。

    只认回复：回复的那条消息 = 当初派活的那条消息 → 沿候选找到正式任务。
    """
    ids = [x for x in (cmd.parent_id, cmd.root_id) if x]
    if not ids:
        return []

    with db.session_scope() as s:
        cand_uids = list(s.scalars(
            select(TaskCandidate.uid).where(TaskCandidate.message_id.in_(ids))
        ).all())
        if not cand_uids:
            return []
        task_uids = list(s.scalars(
            select(Task.uid).where(Task.candidate_uid.in_(cand_uids))
        ).all())

    tasks = [repo.get_task(uid) for uid in task_uids]
    return [t for t in tasks if t is not None]


def _would_succeed(task: Task, action: str, open_id: str) -> bool:
    """预演一次：状态机放行 + 权限放行。不改任何东西。"""
    _dst, check = state_machine.resolve_action(task.status, action)
    if not check.ok:
        return False
    return permissions.can(task, open_id, action).ok


def apply_feedback(cmd: MessageCommand) -> Applied:
    """把一句话落到任务上。返回给群里回什么。

    多层防护，每层都对应一个真实的错法：
    - 不是状态反馈       → 当作没看见，继续走任务识别
    - 没回复任何消息     → 不猜
    - 回复的消息下没任务 → 不猜
    - 任务都已结束       → 告诉他不用改了
    - 一句话对应多条任务 → 只处理唯一能成的那条，否则让他点卡片
    """
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
        note=cmd.text[:200],
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
