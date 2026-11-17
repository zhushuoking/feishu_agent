"""Day 12：每日提醒的生成逻辑（对应设计文档 12.x）。

为什么要把「生成」和「发送」拆开：

生成是纯计算（扫库 → 分组 → 拼卡片），发送要走网络、会被限流、会失败。
混在一起，一次抖动就丢掉一整天的提醒，而且没法重发。

拆成三层：
    notify.plan_digests(now)   算清楚「该发给谁、发什么」（纯函数，now 可注入）
    repo.enqueue_notification  草稿落进发件箱（dedupe_key 唯一 → 天然幂等）
    worker.drain_outbox        发件箱 → 真实发送（失败重试、超时放弃）

三条设计取舍，都是被现实逼出来的：

1. 【一人一条，不按任务发】一个人今天有 5 条任务到期，发 5 条消息就是刷屏。
   聚合成一条，他才会看完。这个和「确认卡片」不一样：确认卡片是按事的，
   提醒是按人的。

2. 【日报只走私聊，不回落群聊】确认卡片私聊失败会回落到群里（怕任务丢），
   但日报是「你个人今天要做什么」，发到群里既打扰别人也没人认领。
   宁可记一条发送失败，也不往群里扔。

3. 【时间必须从外面传进来】`now` 是参数不是全局。定时任务的测试难点是
   「不能等到明天早上 9 点」，把时间变成参数，测试里随便造假时间。
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional

import db
import repo
from models import TZ_SHANGHAI
from models_db import TASK_TERMINAL, Task

# 日报几点发（北京时间）。08 点前入队会提前发出去，所以要卡这个点。
SEND_HOUR = int(os.getenv("DIGEST_SEND_HOUR", "9"))

# 免打扰时段：[22:00, 次日 08:00)。这段时间不发任何主动消息。
QUIET_START_HOUR = int(os.getenv("QUIET_START_HOUR", "22"))
QUIET_END_HOUR = int(os.getenv("QUIET_END_HOUR", "8"))

# 补发时限：早上 9 点的日报，如果下午 4 点才有机会发，就别发了。
# 过期提醒比没有提醒更烦人 —— 收到一条「今天要交」的推送，而今天快过完了。
MAX_DELAY_HOURS = int(os.getenv("DIGEST_MAX_DELAY_HOURS", "6"))

CAT_OVERDUE = "overdue"
CAT_TODAY = "today"
CAT_TOMORROW = "tomorrow"

SECTION_LABELS = {
    CAT_OVERDUE: "已逾期",
    CAT_TODAY: "今天到期",
    CAT_TOMORROW: "明天到期",
}


@dataclass
class Digest:
    """一个人的一天：按截止时间分三堆的任务。"""

    open_id: str
    date: str                                    # 2026-11-17，也是去重键的一部分
    sections: Dict[str, List[Task]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.sections.values())

    @property
    def has_overdue(self) -> bool:
        return bool(self.sections.get(CAT_OVERDUE))

    def tasks(self) -> List[Task]:
        out: List[Task] = []
        for cat in (CAT_OVERDUE, CAT_TODAY, CAT_TOMORROW):
            out.extend(self.sections.get(cat, []))
        return out

    @property
    def dedupe_key(self) -> str:
        return digest_key(self.date, self.open_id)


def digest_key(date: str, open_id: str) -> str:
    """去重键：一天一人一条。

    为什么不带 chat_id：一个人在三个群里都有任务，应该收到一条汇总，
    而不是三条各说各的。聚合的维度是「人」，不是「群」。
    """
    return f"DIGEST:{date}:{open_id}"


def day_bounds(now: datetime):
    """返回 (今天 00:00, 明天 00:00, 后天 00:00)，都是带时区的。"""
    today = now.astimezone(TZ_SHANGHAI).date()
    start = datetime.combine(today, time(0, 0), tzinfo=TZ_SHANGHAI)
    return start, start + timedelta(days=1), start + timedelta(days=2)


def categorize(task: Task, now: datetime) -> Optional[str]:
    """这条任务该归到哪一堆。返回 None 表示今天不用提它。

    逾期优先于「今天到期」：一条今天 10:00 截止、现在 11:00 的任务，
    既逾期又属于今天。归到逾期才对 —— 说「今天到期」会让人以为还有时间。
    """
    if task.status in TASK_TERMINAL:
        return None
    if task.deadline_at is None:
        return None

    deadline = task.deadline_at
    if deadline.tzinfo is None:  # 防御：从库里捞出来理论上是 aware
        deadline = deadline.replace(tzinfo=TZ_SHANGHAI)

    if deadline < now:
        return CAT_OVERDUE

    today_start, tomorrow_start, day_after = day_bounds(now)
    if deadline < tomorrow_start:
        return CAT_TODAY
    if deadline < day_after:
        return CAT_TOMORROW
    return None


def plan_digests(now: Optional[datetime] = None) -> List[Digest]:
    """扫一遍库，算出每个负责人今天该收到什么。

    纯函数：只读库、不写库、不发消息。这样单测里灌几条任务就能验全部规则。
    """
    now = now or db.now()
    date_str = now.astimezone(TZ_SHANGHAI).strftime("%Y-%m-%d")

    buckets: Dict[str, Dict[str, List[Task]]] = {}
    for task in repo.list_active_tasks_with_deadline():
        cat = categorize(task, now)
        if cat is None:
            continue
        # 一个任务多个负责人 → 每人一份。dict.fromkeys 去重且保序，
        # 防止同一个人被写进同一条任务两次（脏数据）导致日报里重复出现。
        for open_id in dict.fromkeys(task.owner_open_ids):
            if not open_id:
                continue
            buckets.setdefault(open_id, {}).setdefault(cat, []).append(task)

    digests: List[Digest] = []
    for open_id, sections in buckets.items():
        for cat in sections:
            # 同一堆里按截止时间排，最早的排最前 —— 逾期最久的先看到
            sections[cat].sort(key=lambda t: t.deadline_at)
        digest = Digest(open_id=open_id, date=date_str, sections=sections)
        if digest.total:
            digests.append(digest)

    # 逾期多的人排前面：越需要被搭理的人越先被生成
    digests.sort(key=lambda d: (-len(d.sections.get(CAT_OVERDUE, [])), d.open_id))
    return digests


def in_quiet_hours(now: datetime) -> bool:
    """免打扰：[22:00, 次日 08:00)。跨零点，不能用简单的 a < h < b。"""
    hour = now.astimezone(TZ_SHANGHAI).hour
    if QUIET_START_HOUR <= QUIET_END_HOUR:      # 同一天内的区间
        return QUIET_START_HOUR <= hour < QUIET_END_HOUR
    return hour >= QUIET_START_HOUR or hour < QUIET_END_HOUR


def should_plan(now: Optional[datetime] = None) -> bool:
    """现在该不该生成当天的日报。

    只做一件事：09:00 之前不生成。不加这个判断，Worker 半夜启动就会
    立刻把当天的日报推出去（dedupe_key 是当天的，一入队就发）。
    """
    now = now or db.now()
    return now.astimezone(TZ_SHANGHAI).hour >= SEND_HOUR


def is_too_late(scheduled_at: datetime, now: datetime,
                max_delay_hours: int = MAX_DELAY_HOURS) -> bool:
    """计划发送时间过去太久，就别补了。

    早上 9 点的「今天要交」，下午 5 点才发出去等于骂人。
    """
    return (now - scheduled_at) > timedelta(hours=max_delay_hours)
