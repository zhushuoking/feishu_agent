"""Day 4：截止时间解析（deadline_text -> deadline_at）。

为什么必须自己算，不让模型算：
1. 模型算日期必然出错，而且同一句话两次结果可能不一样，不可测
2. 设计文档 8.3 明确要求模型只抄原文（deadline_text），换算在服务端做
3. 换成具体时间的规则要能写进单测、能回归、能被人复核

三条硬规矩：
1. 【基准】只能是消息的 sent_at，绝不能用 now()。
   消息延迟投递、补录历史消息时，now() 会让所有相对时间整体偏移。
2. 【算不出就给不了】解析不出来一律 unresolved，进人工澄清。
   绝不返回一个"看起来合理"的假时间 —— 假时间比没时间危险得多。
3. 【假设要留痕】只精确到日的（"周五"），一律取当日 18:00（下班时间）
   并标记 assumed=True，让上层知道这是系统假设，不是用户原话。

口径约定（有歧义的地方在这里一次性定死）：
- "下班前" = 当日 18:00，"上班前" = 当日 09:00
- "本周内/下周内" = 该周周日 23:59:59（"内"是区间语义）
- "月底/本月底" = 该月最后一天 23:59:59
- "周五前" = 周五 18:00（口语里"周五前"普遍指周五下班前，
  真要卡周四会写成"周四前"）
- "3点前"没说明上午下午 → 按 15:00 算，并标记 assumed
- 解析结果早于消息时间 → unresolved（"昨天前""本周一"这种），
  不猜"你其实想说的是下周一"
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import List, Optional

from models import TZ_SHANGHAI

# ---- 口径常量（要改口径就改这里，别散落在正则里）----
WORKDAY_END = time(18, 0)
WORKDAY_START = time(9, 0)
NOON = time(12, 0)
EVENING = time(21, 0)
BOUNDARY_END = time(23, 59, 59)

STATUS_RESOLVED = "resolved"
STATUS_UNRESOLVED = "unresolved"
STATUS_MISSING = "missing"

# 模糊表述：不是时间，是情绪。必须转人工。
_VAGUE = re.compile(r"尽快|越快越好|asap|抽空|有空|近期|最近|这两天有空|再说|看情况|方便的时候", re.I)
# 过去表述：直接判死，不猜
_PAST = re.compile(r"(上|前)(个)?(周|星期|礼拜)|昨天|前天|去年|上个?月|前天")

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_NUM = r"\d+|[一二两三四五六七八九十]+"
_WEEKDAY = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
    "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6,
}
_FULLWIDTH = str.maketrans("０１２３４５６７８９：", "0123456789:")


def cn_to_int(value: str) -> Optional[int]:
    """中文数字转 int，支持 一 ~ 九十九。转不出来返回 None。"""
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if "十" in s:
        head, _, tail = s.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    if len(s) == 1:
        return _CN_DIGITS.get(s)
    return None


def normalize(text: str) -> str:
    """统一说法：全角转半角、去空格、之前->前、号->日。"""
    s = (text or "").translate(_FULLWIDTH)
    s = re.sub(r"\s+", "", s)
    s = s.replace("之前", "前").replace("以前", "前")
    s = s.replace("号", "日")
    return s


@dataclass(frozen=True)
class Deadline:
    """一次解析的完整结果。"""

    raw: str
    status: str                       # resolved / unresolved / missing
    deadline_at: Optional[datetime]   # 只有 resolved 才有值
    rule: str = ""                    # 命中的规则串，排错用
    precision: str = "day"            # minute / hour / day
    assumed: bool = False             # 是否含系统默认假设
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_RESOLVED

    def describe(self) -> str:
        if self.ok:
            flag = " (默认)" if self.assumed else ""
            return f"{self.deadline_at:%Y-%m-%d %H:%M}{flag} <- {self.raw} [{self.rule}]"
        if self.status == STATUS_MISSING:
            return "（原话没给时间）"
        return f"无法解析：{self.raw} -> {self.reason}"


@dataclass
class _DateHit:
    date: date
    rule: str
    boundary: bool = False   # True = 区间语义，用 23:59:59
    assumed: bool = False
    past: bool = False       # 解析到了但早于基准


@dataclass
class _TimeHit:
    value: time
    rule: str
    precision: str = "minute"
    assumed: bool = False


# ---------------------------------------------------------------- 日期

def _last_day_of_month(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def _add_months(d: date, months: int) -> date:
    total = (d.year * 12 + d.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(d.day, _last_day_of_month(year, month).day))


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _match_date(s: str, base: datetime) -> Optional[_DateHit]:
    d0 = base.date()

    if _PAST.search(s):
        return _DateHit(d0, "past-marker", past=True)

    # 1) N 天后 / N 天内（"半天后" 按今天算，粒度为天不硬凑小时）
    m = re.search(rf"({_CN_NUM}|半)\s*个?\s*天\s*(?:后|以后|之内|内)", s)
    if m:
        n = 0 if m.group(1) == "半" else cn_to_int(m.group(1))
        if n is None:
            return None
        return _DateHit(d0 + timedelta(days=n), "rel-days")

    # 2) N 周后 / N 周内
    m = re.search(rf"({_CN_NUM}|半)\s*个?\s*(?:周|星期|礼拜)\s*(?:后|以后|之内|内)", s)
    if m:
        n = 3 if m.group(1) == "半" else cn_to_int(m.group(1))
        if n is None:
            return None
        return _DateHit(d0 + timedelta(days=7 * n), "rel-weeks")

    # 3) N 个月后
    m = re.search(rf"({_CN_NUM})\s*个?\s*月\s*(?:后|以后|之内|内)", s)
    if m:
        n = cn_to_int(m.group(1))
        return None if n is None else _DateHit(_add_months(d0, n), "rel-months")

    # 4) 今天 / 明天 / 后天（长词优先）
    for word, delta in (
        ("大后天", 3), ("后天", 2), ("明儿", 1), ("明晚", 1), ("明早", 1), ("明天", 1),
        ("明日", 1), ("今晚", 0), ("今儿", 0), ("今天", 0), ("今日", 0),
    ):
        if word in s:
            return _DateHit(d0 + timedelta(days=delta), f"rel-{word}")

    # 5) 下下/下/本/这 + 周X
    m = re.search(rf"(下下|下|本|这|这个|本个|上个?)\s*(?:周|星期|礼拜)\s*([一二三四五六日天1-7])", s)
    if m:
        which, wd = m.group(1), _WEEKDAY[m.group(2)]
        if which.startswith("上"):
            return _DateHit(d0, "past-week", past=True)
        weeks = {"下下": 2, "下": 1}.get(which, 0)
        target = _monday_of(d0) + timedelta(days=7 * weeks + wd)
        return _DateHit(target, f"{which}-week-{wd}", past=(weeks == 0 and target < d0))

    # 6) 单独的周X → 最近的那个（含今天）
    m = re.search(r"(?:周|星期|礼拜)\s*([一二三四五六日天1-7])", s)
    if m:
        wd = _WEEKDAY[m.group(1)]
        return _DateHit(d0 + timedelta(days=(wd - d0.weekday()) % 7), f"nearest-weekday-{wd}")

    # 7) 本/下 + 周（不带具体星期）→ 该周周日，区间语义
    m = re.search(r"(下下|下|本|这|这个)\s*(?:周|星期|礼拜)(?:末|内|前|结束前|底)?", s)
    if m:
        which = m.group(1)
        weeks = {"下下": 2, "下": 1}.get(which, 0)
        return _DateHit(
            _monday_of(d0) + timedelta(days=7 * weeks + 6),
            f"{which}-week-end", boundary=True,
        )

    # 8) 裸的"周末" → 本周日（必须放在 7 之后，否则"下周末"会被当成本周日）
    if re.search(r"(?:周|星期|礼拜)末", s):
        return _DateHit(_monday_of(d0) + timedelta(days=6), "weekend", boundary=True)

    # 9) 月底 / 下个月底 / 12月底
    m = re.search(rf"(?:(下下|下|本|这|这个)\s*个?\s*)?(\d{{1,2}}|[一二三四五六七八九十]+)?\s*月\s*(?:底|末)", s)
    if m:
        prefix, num = m.group(1), m.group(2)
        months = {"下下": 2, "下": 1}.get(prefix or "", 0)
        if num:
            month = cn_to_int(num)
            if month is None or not 1 <= month <= 12:
                return None
            year = d0.year + (1 if months else 0)
            if not months:
                # 只写了月份：已经过去的月份顺延到明年
                cand = _last_day_of_month(year, month)
                if cand < d0:
                    cand = _last_day_of_month(year + 1, month)
                    return _DateHit(cand, "month-end", boundary=True, assumed=True)
            return _DateHit(_last_day_of_month(year, month), "month-end", boundary=True)
        target_month = _add_months(date(d0.year, d0.month, 1), months)
        return _DateHit(
            _last_day_of_month(target_month.year, target_month.month),
            f"{prefix or ''}-month-end", boundary=True,
        )

    # 10) 只写月份（"12月" / "下个月"）
    m = re.search(r"(下下|下个|下|本|这个)?\s*(\d{1,2}|[一二三四五六七八九十]+)?\s*个?\s*月"
                  r"(?![\d一二三四五六七八九十])", s)
    if m and (m.group(1) or m.group(2)):
        prefix, num = m.group(1), m.group(2)
        months = {"下下": 2, "下": 1}.get(prefix or "", 0)
        if num:
            month = cn_to_int(num)
            if month is None or not 1 <= month <= 12:
                return None
            year = d0.year + (1 if months else 0)
            cand = _last_day_of_month(year, month)
            if cand < d0 and not months:
                # 只写了月份且已经过去 → 顺延到明年，标记假设
                return _DateHit(_last_day_of_month(year + 1, month),
                                "month-end", boundary=True, assumed=True)
            return _DateHit(cand, "month-end", boundary=True)
        target_month = _add_months(date(d0.year, d0.month, 1), months)
        return _DateHit(
            _last_day_of_month(target_month.year, target_month.month),
            f"{prefix}-month-end", boundary=True,
        )

    # 11) 带年份：2026-11-20 / 2026年11月20日
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        try:
            return _DateHit(date(int(m.group(1)), int(m.group(2)), int(m.group(3))), "iso-date")
        except ValueError:
            return None

    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?", s)
    if m:
        try:
            return _DateHit(date(int(m.group(1)), int(m.group(2)), int(m.group(3))), "cn-year-date")
        except ValueError:
            return None

    # 12) 具体日期：11月20日 / 十一月二十日
    m = re.search(rf"({_CN_NUM})\s*月\s*({_CN_NUM})\s*日?", s)
    if m:
        month, day = cn_to_int(m.group(1)), cn_to_int(m.group(2))
        if not month or not day or not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        try:
            cand = date(d0.year, month, day)
        except ValueError:
            return None
        if cand < d0:
            try:
                cand = date(d0.year + 1, month, day)
            except ValueError:
                return None
            return _DateHit(cand, "month-day", assumed=True)
        return _DateHit(cand, "month-day")

    # 13) 11-20 / 11/20
    m = re.search(r"(?<!\d)(\d{1,2})[-/](\d{1,2})(?!\d)", s)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            try:
                cand = date(d0.year, month, day)
            except ValueError:
                return None
            if cand < d0:
                cand = date(d0.year + 1, month, day)
                return _DateHit(cand, "slash-date", assumed=True)
            return _DateHit(cand, "slash-date")

    # 14) 年底
    if re.search(r"年(底|末|前)", s):
        return _DateHit(date(d0.year, 12, 31), "year-end", boundary=True)

    return None


# ---------------------------------------------------------------- 时刻

_EXPLICIT_CLOCK = re.compile(rf"(\d{{1,2}}):(\d{{2}})")
_CLOCK = re.compile(
    rf"(上午|早上|早晨|一早|凌晨|中午|午间|下午|傍晚|晚上|今晚|明晚|夜里)?"
    rf"\s*({_CN_NUM})\s*[点时]\s*(半|(\d{{1,2}})\s*分?)?"
)


def _match_time(s: str) -> Optional[_TimeHit]:
    # 下班 / 上班：最明确的办公语义，优先
    if re.search(r"下班", s):
        return _TimeHit(WORKDAY_END, "off-work")
    if re.search(r"上班", s):
        return _TimeHit(WORKDAY_START, "on-work")

    m = _EXPLICIT_CLOCK.search(s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return _TimeHit(time(h, mi), "clock")

    m = _CLOCK.search(s)
    if m:
        part = m.group(1) or ""
        hour = cn_to_int(m.group(2))
        if hour is None or not 0 <= hour <= 24:
            return None
        minute = 30 if m.group(3) == "半" else (int(m.group(4)) if m.group(4) else 0)
        assumed = False
        if part in ("下午", "傍晚") and hour < 12:
            hour += 12
        elif part in ("晚上", "今晚", "明晚", "夜里") and hour < 12:
            hour += 12
        elif not part and 1 <= hour <= 6:
            # "3点前" 没说上下午 → 按下午算，标记假设
            hour += 12
            assumed = True
        if hour > 23 or minute > 59:
            return None
        precision = "minute" if (minute or m.group(3)) else "hour"
        return _TimeHit(time(hour, minute), f"clock-cn-{part or 'none'}", precision, assumed)

    # 只给了时段
    if re.search(r"中午|午间", s):
        return _TimeHit(NOON, "daypart-noon", "hour")
    if re.search(r"上午|早上|早晨|一早", s):
        return _TimeHit(NOON, "daypart-morning", "hour", assumed=True)
    if re.search(r"下午", s):
        return _TimeHit(WORKDAY_END, "daypart-afternoon", "hour", assumed=True)
    if re.search(r"傍晚", s):
        return _TimeHit(WORKDAY_END, "daypart-evening", "hour", assumed=True)
    if re.search(r"晚上|今晚|明晚|夜里", s):
        return _TimeHit(EVENING, "daypart-night", "hour", assumed=True)

    return None


# ---------------------------------------------------------------- 主入口

def _roll_to_next_workday(d: date) -> date:
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def _ensure_tz(base: Optional[datetime]) -> datetime:
    if base is None:
        return datetime.now(tz=TZ_SHANGHAI)
    if base.tzinfo is None:
        return base.replace(tzinfo=TZ_SHANGHAI)
    return base.astimezone(TZ_SHANGHAI)


def parse_deadline(text: str, base: datetime) -> Deadline:
    """把一句时间表述解析成带时区的绝对时间。

    base 必须是【消息发送时间】（MessageCommand.sent_at），不是 now()。
    """
    raw = (text or "").strip()
    if not raw:
        return Deadline(raw=raw, status=STATUS_MISSING, deadline_at=None,
                        reason="原话里没有时间表述")

    base = _ensure_tz(base)
    s = normalize(raw)

    if _VAGUE.search(s):
        return Deadline(raw=raw, status=STATUS_UNRESOLVED, deadline_at=None,
                        rule="vague",
                        reason="表述模糊（尽快/近期/抽空），无法换算，需人工指定")

    dh = _match_date(s, base)
    th = _match_time(s)

    if dh and dh.past:
        return Deadline(raw=raw, status=STATUS_UNRESOLVED, deadline_at=None,
                        rule=dh.rule,
                        reason="指向过去的时间，不能当截止时间，需人工确认")

    if not dh and not th:
        return Deadline(raw=raw, status=STATUS_UNRESOLVED, deadline_at=None,
                        rule="no-pattern",
                        reason="没有匹配到任何已知时间句式")

    if dh:
        d = dh.date
        if th:
            t, precision = th.value, th.precision
            assumed = th.assumed or dh.assumed
        else:
            t = BOUNDARY_END if dh.boundary else WORKDAY_END
            precision = "day"
            assumed = True  # 只到日 → 时间是系统补的
        rule = f"{dh.rule}|{th.rule if th else 'default-end'}"
    else:
        # 只有时刻没有日期
        d, t = base.date(), th.value
        precision, assumed, rule = th.precision, th.assumed, th.rule

    dt = datetime.combine(d, t, tzinfo=TZ_SHANGHAI)

    if dt < base and not dh:
        # "下班前" 但已经过点了 → 顺延到下一个工作日，并留痕
        nd = _roll_to_next_workday(d)
        dt = datetime.combine(nd, t, tzinfo=TZ_SHANGHAI)
        assumed = True
        rule += "|rolled-next-workday"

    if dt < base:
        return Deadline(raw=raw, status=STATUS_UNRESOLVED, deadline_at=None,
                        rule=rule,
                        reason=f"解析为 {dt:%Y-%m-%d %H:%M}，早于消息时间 "
                               f"{base:%Y-%m-%d %H:%M}")

    return Deadline(raw=raw, status=STATUS_RESOLVED, deadline_at=dt,
                    rule=rule, precision=precision, assumed=assumed)


def apply_to_tasks(tasks: List[object], base: Optional[datetime]) -> List[object]:
    """把一批任务候选的 deadline_text 解析成 deadline_at，并回写缺失字段。

    只改内存里的对象，不碰数据库。base 为空时用 now()（仅调试场景）。
    """
    base_dt = _ensure_tz(base)
    for task in tasks:
        result = parse_deadline(getattr(task, "deadline_text", None) or "", base_dt)

        task.deadline_at = result.deadline_at
        task.deadline_status = result.status
        task.deadline_rule = result.rule or None

        missing = list(getattr(task, "missing_fields", []) or [])
        if result.ok:
            missing = [f for f in missing if f != "deadline"]
        elif "deadline" not in missing:
            missing.append("deadline")
        task.missing_fields = missing
    return tasks
