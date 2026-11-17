"""Day 4 单测：截止时间解析。

覆盖三类必须钉死的行为：
1. 相对时间换算对不对（今天/明天/本周X/下周X/月底/N天后）
2. 基准是不是消息时间（不是 now()）—— 用一个远期 base 验证
3. 算不出来的时候有没有老老实实返回 unresolved

所有断言都用固定 base，禁止依赖 datetime.now()，否则测试会随日期漂。
"""

from datetime import datetime

import pytest

import deadline
from models import TZ_SHANGHAI
from schemas import ExtractedTask

# 2026-11-13 是周五
BASE = datetime(2026, 11, 13, 10, 0, tzinfo=TZ_SHANGHAI)


def parse(text, base=BASE):
    return deadline.parse_deadline(text, base)


# ---------------------------------------------------------------- 能算出来的

def test_off_work_today():
    d = parse("今天下班前")
    assert d.ok
    assert d.deadline_at == datetime(2026, 11, 13, 18, 0, tzinfo=TZ_SHANGHAI)
    assert d.assumed is False          # 下班 18:00 是明确口径，不算假设
    assert d.precision == "minute"


def test_tomorrow_noon():
    d = parse("明天中午12点前")
    assert d.deadline_at == datetime(2026, 11, 14, 12, 0, tzinfo=TZ_SHANGHAI)


def test_this_friday():
    d = parse("本周五前")
    assert d.deadline_at == datetime(2026, 11, 13, 18, 0, tzinfo=TZ_SHANGHAI)
    assert d.assumed is True           # 只到日，18:00 是系统补的
    assert d.precision == "day"


def test_next_tuesday():
    d = parse("下周二")
    assert d.deadline_at == datetime(2026, 11, 17, 18, 0, tzinfo=TZ_SHANGHAI)


def test_friday_with_explicit_clock():
    d = parse("周五下午3点前")
    assert d.deadline_at == datetime(2026, 11, 13, 15, 0, tzinfo=TZ_SHANGHAI)
    # precision 是"原话粒度"：只说了 3 点没说 3 点几分 → hour
    assert d.precision == "hour"
    assert d.assumed is False


def test_tonight_8pm():
    d = parse("今晚8点前")
    assert d.deadline_at == datetime(2026, 11, 13, 20, 0, tzinfo=TZ_SHANGHAI)


def test_bare_hour_defaults_to_afternoon():
    """'3点前' 没说上下午，按 15:00 算，但必须标记 assumed。"""
    d = parse("3点前")
    assert d.deadline_at == datetime(2026, 11, 13, 15, 0, tzinfo=TZ_SHANGHAI)
    assert d.assumed is True


def test_relative_days():
    d = parse("三天后")
    assert d.deadline_at == datetime(2026, 11, 16, 18, 0, tzinfo=TZ_SHANGHAI)


def test_relative_weeks():
    d = parse("两周后")
    assert d.deadline_at == datetime(2026, 11, 27, 18, 0, tzinfo=TZ_SHANGHAI)


def test_month_end():
    d = parse("月底前")
    assert d.deadline_at == datetime(2026, 11, 30, 23, 59, 59, tzinfo=TZ_SHANGHAI)
    assert d.precision == "day"


def test_next_month_end():
    d = parse("下个月底")
    assert d.deadline_at == datetime(2026, 12, 31, 23, 59, 59, tzinfo=TZ_SHANGHAI)


def test_cn_number_month_end():
    d = parse("十二月底")
    assert d.deadline_at == datetime(2026, 12, 31, 23, 59, 59, tzinfo=TZ_SHANGHAI)


def test_explicit_month_day():
    d = parse("11月20日")
    assert d.deadline_at == datetime(2026, 11, 20, 18, 0, tzinfo=TZ_SHANGHAI)


def test_slash_date():
    d = parse("12-01")
    assert d.deadline_at == datetime(2026, 12, 1, 18, 0, tzinfo=TZ_SHANGHAI)


def test_this_week_within():
    """'本周内' 是区间语义 → 周日 23:59:59。"""
    d = parse("本周内")
    assert d.deadline_at == datetime(2026, 11, 15, 23, 59, 59, tzinfo=TZ_SHANGHAI)


def test_next_week_within():
    d = parse("下周内")
    assert d.deadline_at == datetime(2026, 11, 22, 23, 59, 59, tzinfo=TZ_SHANGHAI)


def test_weekend():
    d = parse("周末前")
    assert d.deadline_at == datetime(2026, 11, 15, 23, 59, 59, tzinfo=TZ_SHANGHAI)


# ---------------------------------------------------------------- 基准必须是消息时间

def test_base_is_message_time_not_now():
    """同一个说法，base 不同结果必须不同。这条挂了说明有人在代码里偷用了 now()。"""
    early = parse("本周五前", datetime(2025, 1, 1, 9, 0, tzinfo=TZ_SHANGHAI))  # 2025-01-01 周三
    assert early.deadline_at == datetime(2025, 1, 3, 18, 0, tzinfo=TZ_SHANGHAI)

    late = parse("本周五前", datetime(2026, 11, 13, 10, 0, tzinfo=TZ_SHANGHAI))
    assert late.deadline_at == datetime(2026, 11, 13, 18, 0, tzinfo=TZ_SHANGHAI)
    assert early.deadline_at != late.deadline_at


def test_off_work_after_hours_rolls_to_next_workday():
    """周五 19:00 说'下班前' → 今天已过点，顺延到下一个工作日（周一）并标记假设。"""
    d = parse("下班前", datetime(2026, 11, 13, 19, 0, tzinfo=TZ_SHANGHAI))
    assert d.deadline_at == datetime(2026, 11, 16, 18, 0, tzinfo=TZ_SHANGHAI)
    assert d.assumed is True
    assert "rolled" in d.rule


# ---------------------------------------------------------------- 算不出来就认输

def test_empty_text_is_missing():
    d = parse("")
    assert d.status == deadline.STATUS_MISSING
    assert d.deadline_at is None


def test_vague_text_is_unresolved():
    d = parse("尽快")
    assert d.status == deadline.STATUS_UNRESOLVED
    assert d.deadline_at is None
    assert "模糊" in d.reason


def test_past_week_is_unresolved():
    d = parse("上周五前")
    assert d.status == deadline.STATUS_UNRESOLVED
    assert d.deadline_at is None


def test_yesterday_is_unresolved():
    d = parse("昨天")
    assert d.status == deadline.STATUS_UNRESOLVED


def test_deadline_already_passed_is_unresolved():
    """周五 20:44 说'今天下班前' —— 已经不可能，必须报 unresolved 而不是给个假时间。"""
    d = parse("今天下班前", datetime(2026, 11, 13, 20, 44, tzinfo=TZ_SHANGHAI))
    assert d.status == deadline.STATUS_UNRESOLVED
    assert d.deadline_at is None
    assert "早于消息时间" in d.reason


def test_unknown_phrase_is_unresolved():
    d = parse("等通知")
    assert d.status == deadline.STATUS_UNRESOLVED


# ---------------------------------------------------------------- 接入任务候选

def test_apply_resolved_clears_missing_deadline():
    task = ExtractedTask(title="提交整改报告", deadline_text="本周五前",
                         missing_fields=["deadline"])
    deadline.apply_to_tasks([task], BASE)

    assert task.deadline_at == datetime(2026, 11, 13, 18, 0, tzinfo=TZ_SHANGHAI)
    assert task.deadline_status == "resolved"
    assert "deadline" not in task.missing_fields
    assert task.deadline_rule


def test_apply_unresolved_marks_missing_deadline():
    task = ExtractedTask(title="提交整改报告", deadline_text="尽快")
    deadline.apply_to_tasks([task], BASE)

    assert task.deadline_at is None
    assert task.deadline_status == "unresolved"
    assert "deadline" in task.missing_fields


def test_apply_no_text_marks_missing():
    task = ExtractedTask(title="提交整改报告")
    deadline.apply_to_tasks([task], BASE)

    assert task.deadline_status == "missing"
    assert "deadline" in task.missing_fields


@pytest.mark.parametrize("text,expected_hour", [
    ("今天下班前", 18),
    ("明天中午前", 12),
    ("上午10点", 10),
    ("下午4点半", 16),
])
def test_time_of_day_table(text, expected_hour):
    d = parse(text)
    assert d.ok, d.reason
    assert d.deadline_at.hour == expected_hour
