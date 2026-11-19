"""Day 13 上半：状态变更的即时通知（对应设计文档 12.x）。

为什么要有它：
点卡片按钮只会更新「点的那个人眼前那张卡」。负责人提交验收之后，发起人
什么都不会收到 —— 他得自己想起来去群里翻。这不是提醒，这是碰运气。
一个跟进系统如果靠人记着去看，那它就没在跟进。

和日报的分工（别混）：

| | 触发方式 | 收件人 | 去重键 |
|---|---|---|---|
| 日报 | 每天 09:00 按时间扫 | 有任务到期的负责人 | `DIGEST:{日期}:{open_id}` |
| 事件通知 | 有人点了按钮 | 该知道这件事的人 | `EVENT:{任务}:{动作}:{版本}:{open_id}` |

两者共用同一个发件箱、同一套重试和放弃规则 —— 这就是当初把「算/存/发」
拆开的好处：加一种通知只需要写「算」这一段。

收件人规则（按动作定，不按状态定 —— 状态是结果，动作才是原因）：

| 动作 | 告诉谁 | 为什么 |
|---|---|---|
| 提交验收 | 发起人 | 轮到他验收了，不告诉他任务就停在这儿 |
| 报阻塞 | 发起人 | 阻塞往往要发起人去协调资源 |
| 解除阻塞 | 发起人 | 他刚被告知「卡住了」，通了得说一声 |
| 验收通过 | 负责人 | 他要知道自己这活儿结没结 |
| 验收打回 | 负责人 | 要返工，必须立刻知道，且要看到打回原因 |
| 取消 | 负责人 | 别再做了 |
| 开始处理 | 不发 | 他自己点的，通知他自己没意义 |

刻意不发的那几种，是为了压制噪音。通知一旦多到需要忽略，就全都没用了。
"""

import os
import threading
from typing import List

import cards
import db
import repo

# 动作 → 给收件人看的一句话。用第二人称写，读的人才知道该干什么。
SUMMARIES = {
    "submit": "提交了交付物，等你验收",
    "accept": "验收通过，任务已完成",
    "reject": "打回了验收，需要继续处理",
    "block": "把任务标记为阻塞",
    # 解除阻塞也要说一声：发起人上一秒刚被告知「卡住了」，
    # 不告诉他通了，他的认知就永远停在「卡住了」，下次还得来问。
    "unblock": "解除了阻塞，可以继续了",
    "cancel": "取消了任务",
}

# 这些动作的收件人是「发起人」，其余是「负责人」。
# 单独抽出来是因为 recipients() 和语义都靠它，散在两处就会漏。
CREATOR_ACTIONS = frozenset({"submit", "block", "unblock"})

# 只有这些动作会触发通知。加动作就往这里加。
NOTIFY_ACTIONS = frozenset(SUMMARIES)


def event_key(task_uid: str, action: str, version: int, open_id: str) -> str:
    """幂等键：一次状态变更、对一个人，只发一次。

    为什么必须带 open_id：一个任务可能有多个负责人，一条变更要发好几份。
    不带 open_id 的话，第二个负责人会被唯一约束直接吞掉。

    为什么带 version：同一个人可能被连续通知两次（提交 → 打回 → 再提交），
    版本号变了就是新事件，不该被去重。
    """
    return f"EVENT:{task_uid}:{action}:{version}:{open_id}"


def recipients(task, action: str, operator_open_id: str = "") -> List[str]:
    """这条变更该告诉谁。空值过滤 + 去重保序 + 不通知操作者本人。"""
    if action in CREATOR_ACTIONS:
        targets = [task.creator_open_id]
    else:
        targets = list(task.owner_open_ids)
    # 自己刚点的按钮，再通知自己一遍纯属噪音。
    # 典型场景：自己给自己派活，然后自己提交验收 —— 收件人和操作者是同一个。
    return [x for x in dict.fromkeys(targets)
            if x and x != operator_open_id]


def on_transition(result, action: str, from_status: str,
                  operator_open_id: str = "") -> int:
    """状态流转成功后调用：生成通知卡并入队。返回入队条数。

    只写发件箱，不发消息 —— 发送是 Worker 的事（或调用方自己触发 flush）。
    这样在卡片回调里调用也不会因为网络慢而超过飞书 3 秒限制。
    """
    if not result or not getattr(result, "ok", False) or not result.task:
        return 0
    if action not in NOTIFY_ACTIONS:
        return 0

    task = result.task
    targets = recipients(task, action, operator_open_id)
    if not targets:
        return 0

    who = cards.at(operator_open_id) if operator_open_id else "有人"
    hint = f"{who} {SUMMARIES[action]}"

    current = db.now()
    queued = 0
    for open_id in targets:
        # 每个人的卡按他自己的权限渲染，私聊是一对一，可以这么做
        card = cards.build_task_card(task, open_id, hint=hint)
        key = event_key(task.uid, action, task.version, open_id)
        if repo.enqueue_notification(
            key, open_id, cards.card_to_content(card),
            kind="task_event", scheduled_at=current,
        ):
            queued += 1
            print(f"[NOTIFY] 入队 {action} → {open_id[:12]}… key={key}")

    return queued


def flush_async() -> None:
    """后台线程把发件箱里的东西发出去。

    为什么要异步：这段代码跑在卡片回调里，飞书等 3 秒就重推，
    发消息要走网络，一慢就会重复推送。
    为什么要线程而不是丢给 Worker：Worker 一分钟才跑一轮，等一分钟
    才知道「有人提交了验收」，体验上就是没有通知。
    """
    if os.getenv("EVENT_FLUSH_IMMEDIATE", "1").strip() == "0":
        return

    def _run() -> None:
        try:
            import worker
            stats = worker.drain_outbox()
            if stats.get("sent") or stats.get("failed"):
                print(f"[NOTIFY] 即时发送：成功 {stats.get('sent', 0)} "
                      f"失败 {stats.get('failed', 0)}")
        except Exception as exc:  # 通知失败不能影响业务
            print(f"[NOTIFY] 即时发送异常：{type(exc).__name__}: {exc}")

    threading.Thread(target=_run, name="notify-flush", daemon=True).start()
