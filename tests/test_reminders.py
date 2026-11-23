"""Day 15 单测：到期前 24 小时 + 逾期提醒（设计文档 12.3 / 12.4）。

钉住六件事：
1. 到期窗口只认 (now, now+24h]，已经过点和不着急的都不发
2. 去重键带 deadline_at —— 改期之后要能重新提醒
3. 逾期收件人是负责人 + 发起人，去重；到期提醒只给负责人
4. 「每 2 个工作日一次」靠已发送记录节流，入队没发出去的不算
5. 周末不催；跨周末算工作日不算自然日
6. Worker 串起来后一天只入队一次，卡片上带得出状态按钮
"""

import json
from datetime import datetime, timedelta

import pytest

import db
import notify
import repo
import worker
from models import TZ_SHANGHAI
from models_db import NOTIFY_SENT, TASK_DONE, TASK_PENDING_ACCEPTANCE
from tests.helpers import make_cmd, make_recognition, make_task

OWNER = "ou_zhangsan"
OWNER2 = "ou_lisi"
CREATOR = "ou_sender"       # make_task 落库的发言人，跟 owners 无关

# 2026-11-20 是周五，工作日，10:00 不在免打扰时段
NOW = datetime(2026, 11, 20, 10, 0, tzinfo=TZ_SHANGHAI)


def _info_candidate(monkeypatch, *, fields=("assignee",), age=60, sender=CREATOR,
                    deadline_status="resolved", ttl_hours=72):
    monkeypatch.setenv("INFO_REMINDER_ENABLED", "1")
    monkeypatch.setattr(notify, "should_plan", lambda now: False)
    monkeypatch.setattr(db, "now", lambda: NOW - timedelta(minutes=age))
    recognition = make_recognition()
    recognition.tasks[0].missing_fields = list(fields)
    recognition.tasks[0].deadline_status = deadline_status
    return repo.save_candidates(
        recognition, make_cmd(sender_open_id=sender), ttl_hours=ttl_hours,
    )[0]


@pytest.mark.parametrize("fields,deadline_status", [
    (("assignee",), "resolved"), ((), "unresolved"), ((), "missing"),
])
def test_info_reminder_is_private_actionable_and_sent_once(
    database, monkeypatch, fields, deadline_status,
):
    cand = _info_candidate(monkeypatch, fields=fields, deadline_status=deadline_status)
    rows = []
    sender = lambda row: rows.append(row) or True

    assert worker.tick(NOW, dry_run=True, sender=sender)["info"] == 1
    assert repo.claim_pending_notifications(NOW) == []
    assert worker.tick(NOW, sender=sender)["info"] == 1
    assert worker.tick(NOW + timedelta(minutes=1), sender=sender)["info"] == 0

    row, = rows
    assert row.kind == "needs_info"
    assert row.dedupe_key == f"NEEDS_INFO:{cand.uid}"
    assert row.receive_id_type == "open_id"
    assert row.receive_id == CREATOR
    card = json.loads(row.payload)
    values = [button["value"] for element in card["elements"]
              for button in element.get("actions", [])]
    assert {value["act"] for value in values} == {"confirm", "reject"}
    assert {value["cid"] for value in values} == {cand.uid}


@pytest.mark.parametrize("case", ["young", "complete", "no_sender", "expired", "confirmed",
                                  "rejected", "disabled", "quiet"])
def test_no_info_reminder_when_candidate_is_ineligible(database, monkeypatch, case):
    cand = _info_candidate(
        monkeypatch, fields=() if case == "complete" else ("deliverable",),
        age=59 if case == "young" else 60, sender="" if case == "no_sender" else CREATOR,
        ttl_hours=1 if case == "expired" else 72,
    )
    if case == "confirmed":
        assert repo.confirm_candidate(cand.uid, operator_open_id=CREATOR)
    if case == "rejected":
        assert repo.reject_candidate(cand.uid, operator_open_id=CREATOR)
    if case == "disabled":
        monkeypatch.setenv("INFO_REMINDER_ENABLED", "0")

    now = NOW.replace(hour=23) if case == "quiet" else NOW
    assert worker.tick(now, sender=lambda row: pytest.fail(row.kind))["info"] == 0
    if case == "quiet":
        after_quiet = (NOW + timedelta(days=1)).replace(hour=8)
        assert worker.tick(after_quiet, sender=lambda row: True)["sent"] == 1


def test_retry_skips_candidate_already_processed(database, monkeypatch):
    cand = _info_candidate(monkeypatch)
    assert worker.tick(NOW, sender=lambda row: False)["failed"] == 1
    assert repo.reject_candidate(cand.uid, operator_open_id=CREATOR)

    sender = lambda row: pytest.fail("processed candidate sent again")
    assert worker.tick(NOW, dry_run=True, sender=sender)["skipped"] == 1
    assert len(repo.claim_pending_notifications(NOW)) == 1
    assert worker.tick(NOW, sender=sender)["skipped"] == 1
    assert repo.claim_pending_notifications(NOW) == []


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ_SHANGHAI)


def _enable(monkeypatch):
    monkeypatch.setenv("DUE_REMINDER_ENABLED", "1")
    monkeypatch.setenv("OVERDUE_REMINDER_ENABLED", "1")


# ---------------------------------------------------------------- 到期前 24 小时

def test_due_window_picks_the_right_task(database):
    inside = make_task(deadline_at=at(21, 9), owners=(OWNER,))       # +23h
    make_task(deadline_at=at(21, 11), owners=(OWNER,))                # +25h，太早
    make_task(deadline_at=at(20, 8), owners=(OWNER,))                 # 已过点

    rows = notify.plan_due_reminders(NOW)

    assert len(rows) == 1
    assert rows[0].task.uid == inside.uid
    assert rows[0].receive_id == OWNER
    assert rows[0].kind == "due_24h"
    assert "距离截止" in rows[0].hint


def test_due_key_changes_when_deadline_changes(database):
    """「延到周五」改了截止时间 = 新期限，必须能再提醒一次。"""
    task = make_task(deadline_at=at(21, 9), owners=(OWNER,))
    before = notify.due_key(task, OWNER)
    assert before.startswith("DUE_24H:")

    from sqlalchemy import update

    from db import session_scope
    from models_db import Task

    with session_scope() as s:
        s.execute(update(Task).where(Task.uid == task.uid)
                  .values(deadline_at=at(23, 18))
                  .execution_options(synchronize_session=False))
    task = repo.get_task(task.uid)
    assert notify.due_key(task, OWNER) != before


def test_due_reminder_goes_only_to_owners(database):
    make_task(deadline_at=at(21, 9), owners=(OWNER, OWNER2))
    rows = notify.plan_due_reminders(NOW)
    assert [r.receive_id for r in rows] == [OWNER, OWNER2]


def test_terminal_task_is_not_reminded(database):
    make_task(deadline_at=at(21, 9), owners=(OWNER,), status=TASK_DONE)
    assert notify.plan_due_reminders(NOW) == []
    assert notify.plan_overdue_reminders(NOW) == []


@pytest.mark.parametrize("deadline", [at(20, 9), at(20, 18)])
def test_pending_acceptance_has_no_owner_reminders(database, deadline):
    make_task(deadline_at=deadline, status=TASK_PENDING_ACCEPTANCE)
    assert notify.plan_due_reminders(NOW) == []
    assert notify.plan_overdue_reminders(NOW) == []
    assert notify.plan_digests(NOW) == []


@pytest.mark.parametrize("kind", ["due_24h", "overdue"])
def test_already_queued_reminder_is_skipped_after_submission(database, kind):
    deadline = at(20, 18) if kind == "due_24h" else at(20, 9)
    task = make_task(deadline_at=deadline)
    reminders = notify.plan_due_reminders(NOW) if kind == "due_24h" else notify.plan_overdue_reminders(NOW)
    worker._enqueue_reminders(reminders, NOW, False, "test")
    assert repo.transition_task(task.uid, "start", OWNER).ok
    assert repo.transition_task(task.uid, "submit", OWNER).ok
    # 验收通知由状态流转产生，正常投递；普通催办应全部跳过。
    sent = []
    result = worker.drain_outbox(NOW + timedelta(minutes=1), sender=lambda row: sent.append(row.kind) or True)
    assert result["skipped"] == len(reminders)
    assert all(item == "task_event" for item in sent)


def test_queued_digest_is_refreshed_after_submission(database):
    submitted = make_task(deadline_at=at(20, 18), title="pending report")
    make_task(deadline_at=at(20, 18), title="active report")
    digest, = notify.plan_digests(NOW)
    import cards
    repo.enqueue_notification(digest.dedupe_key, OWNER,
                              cards.card_to_content(cards.build_digest_card(digest, NOW)), scheduled_at=NOW)
    assert repo.transition_task(submitted.uid, "start", OWNER).ok
    assert repo.transition_task(submitted.uid, "submit", OWNER).ok
    sent = []
    worker.drain_outbox(NOW + timedelta(minutes=1), sender=lambda row: sent.append(row) or True)
    daily, = [row for row in sent if row.kind == "digest"]
    assert "active report" in daily.payload
    assert "pending report" not in daily.payload


def test_task_without_deadline_is_not_reminded(database):
    from sqlalchemy import update

    from db import session_scope
    from models_db import Task

    task = make_task(deadline_at=at(21, 9), owners=(OWNER,))
    with session_scope() as s:
        s.execute(update(Task).where(Task.uid == task.uid)
                  .values(deadline_at=None)
                  .execution_options(synchronize_session=False))
    assert notify.plan_due_reminders(NOW) == []


# ---------------------------------------------------------------- 工作日

def test_weekend_is_not_workday():
    assert notify.is_workday(at(20, 10).date()) is True      # 周五
    assert notify.is_workday(at(21, 10).date()) is False     # 周六
    assert notify.is_workday(at(22, 10).date()) is False     # 周日
    assert notify.is_workday(at(23, 10).date()) is True      # 周一


def test_workdays_between_skips_weekend():
    """周五 → 周一只有 1 个工作日，按自然日算会错成 3。"""
    assert notify.workdays_between(at(20, 10).date(), at(23, 10).date()) == 1
    assert notify.workdays_between(at(18, 10).date(), at(20, 10).date()) == 2


def test_should_remind_throttle(database):
    assert notify.should_remind_overdue(NOW, None) is True        # 首次
    assert notify.should_remind_overdue(NOW, at(19, 10)) is False  # 隔 1 个工作日
    assert notify.should_remind_overdue(NOW, at(18, 10)) is True   # 隔 2 个
    assert notify.should_remind_overdue(at(21, 10), None) is False  # 周六不催


# ---------------------------------------------------------------- 逾期

def test_overdue_notifies_owner_and_creator(database):
    make_task(deadline_at=at(20, 8), owners=(OWNER,))
    rows = notify.plan_overdue_reminders(NOW)

    assert [r.receive_id for r in rows] == [OWNER, CREATOR]
    assert all(r.kind == "overdue" for r in rows)
    assert all(":2026-11-20:" in r.dedupe_key for r in rows)
    assert "已逾期" in rows[0].hint


def test_overdue_dedupes_when_owner_is_creator(database):
    """自己给自己派活，别给同一个人发两条。"""
    make_task(deadline_at=at(20, 8), owners=(CREATOR,))
    rows = notify.plan_overdue_reminders(NOW)
    assert [r.receive_id for r in rows] == [CREATOR]


def test_overdue_throttles_after_last_sent(database):
    task = make_task(deadline_at=at(20, 8), owners=(OWNER,))
    assert notify.plan_overdue_reminders(NOW) != []
    assert notify.plan_overdue_reminders(NOW, {task.uid: at(19, 10)}) == []
    assert notify.plan_overdue_reminders(NOW, {task.uid: at(18, 10)}) != []


def test_only_sent_reminders_count_for_throttle(database):
    """入队了但没发出去的不能算提醒过 —— 否则人永远收不到。"""
    task = make_task(deadline_at=at(20, 8), owners=(OWNER,))
    repo.enqueue_notification(
        f"OVERDUE:{task.uid}:2026-11-19:{OWNER}", OWNER, "{}",
        kind="overdue", task_uid=task.uid,
    )
    assert repo.last_overdue_sent_times() == {}

    row = repo.list_notifications()[0]
    repo.mark_notification_sent(row.uid)
    assert repo.last_overdue_sent_times()[task.uid] is not None


def test_overdue_skips_weekend(database):
    make_task(deadline_at=at(20, 8), owners=(OWNER,))
    assert notify.plan_overdue_reminders(at(21, 10)) == []


# ---------------------------------------------------------------- Worker 串起来

def test_tick_enqueues_both_kinds(database, monkeypatch):
    _enable(monkeypatch)
    make_task(deadline_at=at(21, 9), owners=(OWNER,))     # 到期前
    make_task(deadline_at=at(20, 8), owners=(OWNER,))     # 已逾期

    stats = worker.tick(NOW, sender=lambda row: True)

    assert stats["due"] == 1
    assert stats["overdue"] == 2
    rows = repo.list_notifications(limit=50)
    kinds = [r.kind for r in rows]
    assert "due_24h" in kinds and kinds.count("overdue") == 2
    assert all(r.task_uid for r in rows if r.kind in ("due_24h", "overdue"))


def test_tick_dry_run_does_not_write(database, monkeypatch):
    _enable(monkeypatch)
    make_task(deadline_at=at(21, 9), owners=(OWNER,))
    worker.tick(NOW, dry_run=True, sender=lambda row: True)
    assert repo.list_notifications() == []


def test_same_day_tick_does_not_duplicate(database, monkeypatch):
    """Worker 每 60 秒一轮，同一天绝不能重复推。"""
    _enable(monkeypatch)
    make_task(deadline_at=at(21, 9), owners=(OWNER,))
    make_task(deadline_at=at(20, 8), owners=(OWNER,))

    first = worker.tick(NOW, sender=lambda row: True)
    second = worker.tick(NOW, sender=lambda row: True)

    assert first["due"] == 1 and first["overdue"] == 2
    assert second["due"] == 0 and second["overdue"] == 0


def test_reminder_cards_are_actionable(database, monkeypatch):
    """提醒卡不是纯通知：要带状态按钮，看到就能点。"""
    _enable(monkeypatch)
    make_task(deadline_at=at(21, 9), owners=(OWNER,))
    worker.tick(NOW, sender=lambda row: True)

    row = next(r for r in repo.list_notifications(limit=20)
               if r.kind == "due_24h")
    card = json.loads(row.payload)
    assert card["config"]["update_multi"] is True
    assert any(e.get("tag") == "action" for e in card["elements"])
    assert row.status == NOTIFY_SENT


def test_flags_are_read_at_call_time(database, monkeypatch):
    monkeypatch.setenv("DUE_REMINDER_ENABLED", "0")
    monkeypatch.setenv("OVERDUE_REMINDER_ENABLED", "0")
    assert notify.due_reminders_enabled() is False
    assert notify.overdue_reminders_enabled() is False
    _enable(monkeypatch)
    assert notify.due_reminders_enabled() is True
