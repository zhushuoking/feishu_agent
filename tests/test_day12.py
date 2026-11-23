"""Day 12 单测：日报生成 + 发件箱 + Worker。

三条主线：
1. 该提醒谁、提醒什么（分堆规则，尤其是「逾期压过今天到期」）
2. 同一天重跑不能重复发（dedupe_key 幂等）
3. 发不出去要能重试，太晚了要主动放弃（发件箱的真正价值）

所有用例都把「现在」写死成 NOW，不依赖机器时间。
定时任务如果测试必须等到明天 9 点，那等于没法测。
"""

from datetime import datetime, timedelta

import cards
import notify
import repo
import worker
from models import TZ_SHANGHAI
from models_db import (
    NOTIFY_FAILED, NOTIFY_PENDING, NOTIFY_SENT, NOTIFY_SKIPPED,
    TASK_DONE, TASK_TODO, Task,
)
from tests.helpers import make_task

NOW = datetime(2026, 11, 17, 9, 5, tzinfo=TZ_SHANGHAI)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ_SHANGHAI)


def _buttons(card) -> list:
    out = []
    for element in card["elements"]:
        if element.get("tag") == "action":
            out.extend(element["actions"])
    return out


def _text(card) -> str:
    chunks = []
    for element in card["elements"]:
        if "text" in element:
            chunks.append(element["text"].get("content", ""))
        if element.get("tag") == "note":
            chunks.extend(e.get("content", "") for e in element.get("elements", []))
    return "\n".join(chunks)


# ---------------------------------------------------------------- 分堆规则

def test_day_bounds():
    start, tomorrow, day_after = notify.day_bounds(NOW)
    assert start == at(17, 0)
    assert tomorrow == at(18, 0)
    assert day_after == at(19, 0)


def test_overdue_wins_over_today(database):
    """今天 8 点截止、现在 9:05 —— 是逾期，不是「今天到期」。
    说成今天到期会让人以为还有一整天。"""
    task = make_task(deadline_at=at(17, 8))
    assert notify.categorize(task, NOW) == "overdue"


def test_today_and_tomorrow(database):
    today = make_task(deadline_at=at(17, 14))
    tomorrow = make_task(deadline_at=at(18, 18))
    midnight = make_task(deadline_at=at(18, 0))  # 明天 00:00 属于明天

    assert notify.categorize(today, NOW) == "today"
    assert notify.categorize(tomorrow, NOW) == "tomorrow"
    assert notify.categorize(midnight, NOW) == "tomorrow"


def test_far_future_is_ignored(database):
    task = make_task(deadline_at=at(20, 18))
    assert notify.categorize(task, NOW) is None


def test_terminal_is_ignored(database):
    task = make_task(deadline_at=at(17, 8), status=TASK_DONE)
    assert notify.categorize(task, NOW) is None


def test_no_deadline_is_ignored():
    """没有截止时间的任务不进日报 —— 提醒的前提是「有时间要到了」。"""
    task = Task(uid="x" * 8, chat_id="oc_1", title="随便", status=TASK_TODO,
                deadline_at=None)
    assert notify.categorize(task, NOW) is None


def test_plan_groups_by_owner(database):
    """一条任务两个负责人 → 两个人各收一份，不是一份发给两个人。"""
    make_task(deadline_at=at(17, 14), owners=("ou_a", "ou_b"))
    digests = notify.plan_digests(NOW)

    assert {d.open_id for d in digests} == {"ou_a", "ou_b"}
    assert all(d.total == 1 for d in digests)
    assert digests[0].dedupe_key.startswith("DIGEST:2026-11-17:")


def test_plan_sorts_by_deadline(database):
    make_task(deadline_at=at(17, 15), title="晚的")
    make_task(deadline_at=at(16, 9), title="早的")
    digest = notify.plan_digests(NOW)[0]

    overdue = digest.sections["overdue"]
    assert [t.title for t in overdue] == ["早的"]


def test_plan_returns_empty_when_nothing_due(database):
    make_task(deadline_at=at(25, 18))
    assert notify.plan_digests(NOW) == []


def test_digest_key_is_per_day_per_person():
    assert notify.digest_key("2026-11-17", "ou_a") == "DIGEST:2026-11-17:ou_a"
    assert (notify.digest_key("2026-11-17", "ou_a")
            != notify.digest_key("2026-11-18", "ou_a"))


# ---------------------------------------------------------------- 时间闸门

def test_should_plan_only_after_nine():
    assert notify.should_plan(at(17, 8, 59)) is False
    assert notify.should_plan(at(17, 9, 0)) is True
    assert notify.should_plan(at(17, 23, 30)) is True


def test_quiet_hours_cross_midnight():
    assert notify.in_quiet_hours(at(17, 22, 0)) is True
    assert notify.in_quiet_hours(at(17, 23, 59)) is True
    assert notify.in_quiet_hours(at(17, 2, 0)) is True
    assert notify.in_quiet_hours(at(17, 7, 59)) is True
    assert notify.in_quiet_hours(at(17, 8, 0)) is False
    assert notify.in_quiet_hours(at(17, 21, 59)) is False


# ---------------------------------------------------------------- 发件箱

def test_enqueue_is_idempotent(database):
    assert repo.enqueue_notification("K1", "ou_a", "{}") is True
    assert repo.enqueue_notification("K1", "ou_a", "{}") is False
    assert len(repo.list_notifications()) == 1


def test_enqueue_same_person_different_days(database):
    assert repo.enqueue_notification("DIGEST:2026-11-17:ou_a", "ou_a", "{}")
    assert repo.enqueue_notification("DIGEST:2026-11-18:ou_a", "ou_a", "{}")
    assert len(repo.list_notifications()) == 2


def test_claim_only_returns_due(database):
    repo.enqueue_notification("K1", "ou_a", "{}", scheduled_at=NOW - timedelta(minutes=1))
    repo.enqueue_notification("K2", "ou_b", "{}", scheduled_at=NOW + timedelta(hours=1))

    rows = repo.claim_pending_notifications(NOW)
    assert [r.dedupe_key for r in rows] == ["K1"]


# ---------------------------------------------------------------- Worker 一轮

def test_tick_enqueues_once_per_day(database):
    make_task(deadline_at=at(17, 14))

    first = worker.tick(NOW, sender=lambda row: True)
    second = worker.tick(NOW, sender=lambda row: True)

    assert first["enqueued"] == 1
    assert second["enqueued"] == 0  # dedupe_key 挡掉了
    assert len(repo.list_notifications()) == 1


def test_tick_before_nine_does_nothing(database):
    make_task(deadline_at=at(17, 14))
    stats = worker.tick(at(17, 8, 30), sender=lambda row: True)
    assert stats["digests"] == 0
    assert repo.list_notifications() == []


def test_tick_force_plan_bypasses_hour_gate(database):
    make_task(deadline_at=at(17, 14))
    stats = worker.tick(at(17, 8, 30), force_plan=True, sender=lambda row: True)
    assert stats["digests"] == 1


# ---------------------------------------------------------------- 发送与重试

def test_drain_marks_sent(database):
    repo.enqueue_notification("K1", "ou_a", "{}", scheduled_at=NOW)
    stats = worker.drain_outbox(NOW, sender=lambda row: True)

    assert stats["sent"] == 1
    assert repo.list_notifications()[0].status == NOTIFY_SENT


def test_drain_retries_then_gives_up(database):
    repo.enqueue_notification("K1", "ou_a", "{}", scheduled_at=NOW)

    for _ in range(3):
        worker.drain_outbox(NOW, sender=lambda row: False)

    row = repo.list_notifications()[0]
    assert row.attempts == 3
    assert row.status == NOTIFY_FAILED
    assert "失败" in row.last_error


def test_failed_before_max_attempts_stays_pending(database):
    repo.enqueue_notification("K1", "ou_a", "{}", scheduled_at=NOW)
    worker.drain_outbox(NOW, sender=lambda row: False)

    row = repo.list_notifications()[0]
    assert row.attempts == 1
    assert row.status == NOTIFY_PENDING  # 还会再试


def test_drain_skips_too_late(database):
    """早上 9 点的日报，下午 4 点才轮到 —— 别发了。"""
    repo.enqueue_notification("K1", "ou_a", "{}",
                              scheduled_at=NOW)
    stats = worker.drain_outbox(NOW + timedelta(hours=7), sender=lambda row: True)

    assert stats["skipped"] == 1
    assert repo.list_notifications()[0].status == NOTIFY_SKIPPED


def test_drain_respects_quiet_hours(database):
    """免打扰期间不发，但也不丢 —— 08:00 之后还在时限内就发掉。"""
    repo.enqueue_notification("K1", "ou_a", "{}", scheduled_at=at(18, 7, 50))

    stats = worker.drain_outbox(at(18, 7, 55), sender=lambda row: True)
    assert stats["quiet"] == 1
    assert repo.list_notifications()[0].status == NOTIFY_PENDING

    # 08:00 之后下一轮自己捡起来，不需要人工干预
    stats = worker.drain_outbox(at(18, 8, 5), sender=lambda row: True)
    assert stats["sent"] == 1


def test_stale_digest_is_dropped_next_morning(database):
    """跨夜的日报要放弃：早上 9 点的「今天要交」，第二天早上 8 点才发没有意义。
    免打扰是「延后」，补发时限是「放弃」，两个机制各管一段。"""
    repo.enqueue_notification("K1", "ou_a", "{}", scheduled_at=at(17, 9, 5))
    stats = worker.drain_outbox(at(18, 8, 30), sender=lambda row: True)

    assert stats["skipped"] == 1
    assert repo.list_notifications()[0].status == NOTIFY_SKIPPED


def test_drain_ignores_not_due(database):
    repo.enqueue_notification("K1", "ou_a", "{}", scheduled_at=NOW + timedelta(hours=1))
    assert worker.drain_outbox(NOW, sender=lambda row: True)["sent"] == 0


def test_sender_exception_is_contained(database):
    """发送函数抛异常也只算一次失败，不能让整轮 Worker 挂掉。"""
    def boom(row):
        raise RuntimeError("网络炸了")

    repo.enqueue_notification("K1", "ou_a", "{}", scheduled_at=NOW)
    stats = worker.drain_outbox(NOW, sender=boom)
    assert stats["failed"] == 1


# ---------------------------------------------------------------- 卡片

def test_digest_card_structure(database):
    make_task(deadline_at=at(17, 14), title="今天要交的")
    make_task(deadline_at=at(16, 9), title="昨天该交的")
    make_task(deadline_at=at(18, 18), title="明天的")

    digest = notify.plan_digests(NOW)[0]
    card = cards.build_digest_card(digest, NOW)
    text = _text(card)

    assert card["header"]["template"] == "red"  # 有逾期 → 红
    assert "今日任务" in card["header"]["title"]["content"]
    assert "已逾期" in text and "今天到期" in text and "明天到期" in text
    assert "昨天该交的" in text

    buttons = _buttons(card)
    assert len(buttons) == 3
    for button in buttons:
        assert button["value"]["tid"]
        assert button["value"]["ver"] == 1


def test_digest_card_truncates_long_lists(database):
    for i in range(6):
        make_task(deadline_at=at(17, 14 + i), title=f"任务{i}")

    digest = notify.plan_digests(NOW)[0]
    card = cards.build_digest_card(digest, NOW)

    assert "还有 2 条" in _text(card)


def test_pending_acceptance_is_excluded_from_owner_digest(database):
    """负责人已提交完成后，从催办日报移除。"""
    task = make_task(deadline_at=at(17, 14))
    repo.transition_task(task.uid, "start", operator_open_id="ou_zhangsan")
    repo.transition_task(task.uid, "submit", operator_open_id="ou_zhangsan")

    assert notify.plan_digests(NOW) == []


# ---------------------------------------------------------------- 端到端

def test_end_to_end_digest_delivery(database):
    """造 3 条任务 → 跑一轮 Worker → 发件箱里一条、状态 sent、内容带标题。"""
    make_task(deadline_at=at(17, 14), title="周报")
    make_task(deadline_at=at(16, 9), title="整改报告")
    make_task(deadline_at=at(20, 18), title="下周的事")  # 不进日报

    delivered = []

    def fake_send(row):
        delivered.append(row)
        return True

    stats = worker.tick(NOW, sender=fake_send)

    assert stats["digests"] == 1
    assert stats["sent"] == 1
    assert len(delivered) == 1
    assert "周报" in delivered[0].payload
    assert "整改报告" in delivered[0].payload
    assert "下周的事" not in delivered[0].payload
    assert repo.count_notifications_by_status() == {NOTIFY_SENT: 1}
