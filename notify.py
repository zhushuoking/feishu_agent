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


# ---------------------------------------------------------------- 到期前 24h / 逾期（设计文档 12.3）

# 设计文档 12.3 的「默认提醒策略」要求 9 类通知，之前只做了「每日汇总」和
# 部分状态变更事件，这两类是缺得最明显、也最影响体验的：
#
# | 业务事件 | 接收人 | 通道 | 去重／限制 |
# |---|---|---|---|
# | 到期前 24 小时 | 主负责人 | 单聊 | 每任务每期限一次 |
# | 逾期 | 负责人、发起人 | 单聊 | 默认首次逾期及之后每 2 个工作日一次 |
#
# 和日报一样走「算 / 存 / 发」三层：这两个 plan_* 只算不写不送，Worker 负责
# 入队，drain_outbox 负责投递。理由和 Day 12 完全一样，不重复写。

KIND_DUE = "due_24h"
KIND_OVERDUE = "overdue"

DUE_WINDOW_HOURS = 24
OVERDUE_INTERVAL_WORKDAYS = int(os.getenv("OVERDUE_INTERVAL_WORKDAYS", "2"))


def _flag(name: str) -> bool:
    """每次调用都读环境变量，不在 import 时定死 —— 测试用 monkeypatch 关它。"""
    return os.getenv(name, "1").strip() != "0"


def due_reminders_enabled() -> bool:
    return _flag("DUE_REMINDER_ENABLED")


def overdue_reminders_enabled() -> bool:
    return _flag("OVERDUE_REMINDER_ENABLED")


@dataclass
class Reminder:
    """一条待入队的提醒。task 是已加载关系的 ORM 对象，卡片在 Worker 里渲染。"""

    kind: str
    task: Task
    receive_id: str
    dedupe_key: str
    hint: str


def _aware(dt) -> Optional[datetime]:
    """库里读出来的理论上是 aware，防御一下 naive。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ_SHANGHAI)
    return dt.astimezone(TZ_SHANGHAI)


def _deadline_stamp(task) -> str:
    dt = _aware(task.deadline_at)
    return dt.strftime("%Y%m%d%H%M") if dt else "none"


def due_key(task, recipient: str) -> str:
    """设计文档 12.4 给的键：DUE_24H:{task_id}:{deadline_at}:{recipient_id}

    带上 deadline_at 是关键：负责人说「延到周五」改了截止时间，那就是一个新期限，
    应该重新提醒一次。只带 task_id 的话改期后就再也不提醒了。
    """
    return f"DUE_24H:{task.uid}:{_deadline_stamp(task)}:{recipient}"


def overdue_key(task, day: str, recipient: str) -> str:
    """文档没给逾期的键，自己定一个。

    带日期而不是「第几次」：同一天 Worker 跑 60 轮也只会有一条（唯一约束），
    换一天才可能再发，配合 2 工作日节流正好。
    """
    return f"OVERDUE:{task.uid}:{day}:{recipient}"


def is_workday(day) -> bool:
    """周一到周五。

    【坑】这里没接法定节假日表。国庆调休、春节这种日子会照常发提醒，
    首期先忍着。真要接，得维护一张 holiday 表或一个可配置的 JSON，
    别在代码里硬编码 2026 年的节假日 —— 明年就错了。
    """
    return day.weekday() < 5


def workdays_between(start, end) -> int:
    """(start, end] 之间的工作日天数，左开右闭。"""
    if end <= start:
        return 0
    count, cur = 0, start
    while cur < end:
        cur += timedelta(days=1)
        if is_workday(cur):
            count += 1
    return count


def should_remind_overdue(now: datetime, last_sent=None,
                          interval_workdays: Optional[int] = None) -> bool:
    """这次 tick 要不要给这条任务发逾期提醒。

    首次（从没发过）→ 发。之后要求「距上次发出跨过 N 个工作日」。
    非工作日直接不发：周末催人只会让人把机器人的通知关掉。
    """
    interval = OVERDUE_INTERVAL_WORKDAYS if interval_workdays is None else interval_workdays
    today = now.astimezone(TZ_SHANGHAI).date()
    if not is_workday(today):
        return False
    if last_sent is None:
        return True
    last_day = _aware(last_sent).date()
    return workdays_between(last_day, today) >= interval


def _recipients(task, include_creator: bool) -> List[str]:
    ids = list(task.owner_open_ids)
    if include_creator:
        ids.append(task.creator_open_id)
    return [x for x in dict.fromkeys(ids) if x]


def plan_due_reminders(now: Optional[datetime] = None,
                       horizon_hours: int = DUE_WINDOW_HOURS) -> List[Reminder]:
    """算「到期前 24 小时」该发给谁。纯函数，now 可注入。

    窗口是 (now, now+24h]，不含已经过点的任务 —— 那些归逾期提醒管，
    两边都发就成了一条任务连收两条，噪音。
    """
    now = now or db.now()
    upper = now + timedelta(hours=horizon_hours)
    out: List[Reminder] = []

    for task in repo.list_active_tasks_with_deadline():
        deadline = _aware(task.deadline_at)
        if deadline is None or not (now < deadline <= upper):
            continue

        hours_left = max(0, int((deadline - now).total_seconds() // 3600))
        clock = deadline.strftime("%m-%d %H:%M")
        left = (f"还有 {hours_left} 小时" if hours_left else "马上就到")
        hint = f"距离截止{left}（{clock}），记得收尾"

        for open_id in _recipients(task, include_creator=False):
            out.append(Reminder(KIND_DUE, task, open_id,
                                due_key(task, open_id), hint))
    return out


def plan_overdue_reminders(now: Optional[datetime] = None,
                           last_sent: Optional[Dict[str, datetime]] = None,
                           interval_workdays: Optional[int] = None) -> List[Reminder]:
    """算「逾期」该发给谁。纯函数：上次发送时间由调用方传进来。

    为什么 last_sent 不由这里查库：查了库就不纯了，单测得先造一堆发件箱记录
    才能验「2 个工作日节流」。现在传一个 dict 就能测，边界清清楚楚。
    """
    now = now or db.now()
    today = now.astimezone(TZ_SHANGHAI)
    if not is_workday(today.date()):
        return []
    day_str = today.strftime("%Y-%m-%d")
    last_sent = last_sent or {}
    out: List[Reminder] = []

    for task in repo.list_active_tasks_with_deadline():
        deadline = _aware(task.deadline_at)
        if deadline is None or deadline >= now:
            continue
        if not should_remind_overdue(now, last_sent.get(task.uid), interval_workdays):
            continue

        days = max(0, (now - deadline).days)
        hint = (f"任务已逾期 {days} 天（截止 {deadline:%m-%d %H:%M}），"
                f"请尽快处理，或回复说明情况")
        for open_id in _recipients(task, include_creator=True):
            out.append(Reminder(KIND_OVERDUE, task, open_id,
                                overdue_key(task, day_str, open_id), hint))
    return out
