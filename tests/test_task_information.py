from datetime import datetime, timedelta
import uuid

import pytest
from sqlalchemy import update

import card_action
import cards
import db
import feedback
import notify
import repo
import worker
from models import TZ_SHANGHAI
from models_db import TASK_BLOCKED, TASK_NEEDS_INFO, TASK_TODO, Task, TaskCandidate
from tests.helpers import make_cmd, make_task, save_one

OWNER = "ou_zhangsan"
CREATOR = "ou_sender"


def incomplete_task(deliverable=None, deadline_at=None):
    cand = save_one(message_id="om_info_" + uuid.uuid4().hex)
    with db.session_scope() as session:
        session.execute(update(TaskCandidate).where(TaskCandidate.uid == cand.uid).values(
            deliverable=deliverable, deadline_at=deadline_at,
            deadline_status="resolved" if deadline_at else "missing",
        ))
    return repo.confirm_candidate(cand.uid, CREATOR)


def test_confirmation_marks_missing_information_and_never_overdue(database):
    task = incomplete_task(deadline_at=db.now() - timedelta(days=1))
    assert task.status == TASK_NEEDS_INFO
    assert task.missing_info == ["交付物"]
    assert not task.is_overdue
    assert not repo.transition_task(task.uid, "start", OWNER).ok
    assert "待补充" in str(cards.build_task_card(task, OWNER))


def test_authorized_owner_can_complete_information_in_two_steps(database):
    task = incomplete_task()
    first = repo.supplement_task(task.uid, OWNER, deliverable="测试报告")
    assert first.ok and first.task.status == TASK_NEEDS_INFO
    deadline = (db.now() + timedelta(days=2)).replace(microsecond=0)
    second = repo.supplement_task(task.uid, OWNER, deadline_at=deadline, deadline_text="后天")
    assert second.ok and second.task.status == TASK_TODO
    assert second.task.deliverable == "测试报告"
    assert second.task.deadline_at == deadline
    assert second.task.version == task.version + 2
    assert repo.list_task_logs(task.uid)[-1].operator_open_id == OWNER


@pytest.mark.parametrize("case", ["stranger", "admin", "disabled_group", "old_version", "past_date"])
def test_supplementation_validates_authority_version_and_date(database, monkeypatch, case):
    task = incomplete_task()
    operator = OWNER
    args = dict(deliverable="报告", deadline_at=db.now() + timedelta(days=2))
    if case in ("stranger", "admin"):
        operator = "ou_other"
    if case == "admin":
        monkeypatch.setenv("AGENT_ADMIN_OPEN_IDS", operator)
    if case == "disabled_group":
        monkeypatch.setenv("FEISHU_DISABLED_CHAT_IDS", task.chat_id)
    if case == "old_version":
        args["expected_version"] = task.version - 1
    if case == "past_date":
        args["deadline_at"] = db.now() - timedelta(minutes=1)
    assert not repo.supplement_task(task.uid, operator, **args).ok
    assert repo.get_task(task.uid).status == TASK_NEEDS_INFO
    assert repo.get_task(task.uid).version == task.version


def test_supplementation_cannot_silently_change_a_complete_task(database):
    task = incomplete_task()
    assert repo.supplement_task(task.uid, CREATOR, deliverable="报告",
                                deadline_at=db.now() + timedelta(days=1)).ok
    assert not repo.supplement_task(task.uid, CREATOR, deliverable="新的要求").ok


def test_private_reply_completes_information_without_model(database):
    task = incomplete_task()
    repo.register_task_card(task.uid, "om_info_card", OWNER, viewer_open_id=OWNER)
    now = db.now()
    cmd = make_cmd(chat_type="p2p", chat_id="oc_private", sender_open_id=OWNER,
                   parent_id="om_info_card", root_id="om_info_card", sent_at=now,
                   text="补充信息\n交付物：测试报告\n截止时间：明天18点前")
    result = feedback.apply_feedback(cmd)
    assert result.ok
    assert result.task.status == TASK_TODO
    assert result.task.deliverable == "测试报告"
    assert "截止时间按" in result.text


def test_ambiguous_supplement_time_is_not_written(database):
    task = incomplete_task()
    repo.register_task_card(task.uid, "om_info_card", OWNER, viewer_open_id=OWNER)
    cmd = make_cmd(chat_type="p2p", sender_open_id=OWNER, parent_id="om_info_card",
                   text="补充信息\n交付物：报告\n截止时间：尽快")
    assert not feedback.apply_feedback(cmd).ok
    assert repo.get_task(task.uid).deliverable is None


def test_supplement_button_prompts_for_fields_without_creating_another_card(database):
    task = incomplete_task()
    outcome = card_action.process("supplement", tid=task.uid, operator_open_id=OWNER, ver=task.version)
    assert outcome.toast_type == "info"
    assert repo.get_task(task.uid).version == task.version


def test_pending_information_is_excluded_from_normal_reminders(database):
    now = db.now()
    task = incomplete_task(deadline_at=now + timedelta(hours=1))
    assert notify.categorize(task, now) is None
    assert notify.plan_due_reminders(now) == []
    assert notify.plan_overdue_reminders(now + timedelta(days=2)) == []


def test_task_info_reminder_uses_two_workday_interval_and_rechecks_state(database, monkeypatch):
    task = incomplete_task()
    created = datetime(2026, 11, 19, 9, tzinfo=TZ_SHANGHAI)
    now = datetime(2026, 11, 23, 10, tzinfo=TZ_SHANGHAI)
    with db.session_scope() as session:
        session.execute(update(Task).where(Task.uid == task.uid).values(created_at=created))
    reminders = notify.plan_task_info_reminders(now)
    assert {item.receive_id for item in reminders} == {OWNER, CREATOR}
    assert notify.plan_task_info_reminders(now, {task.uid: datetime(2026, 11, 20, 10, tzinfo=TZ_SHANGHAI)}) == []
    monkeypatch.setenv("TASK_INFO_REMINDER_ENABLED", "1")
    worker._enqueue_reminders(reminders, now, False, "测试")
    row = repo.list_notifications()[0]
    assert worker._refresh_reminder(row, now)
    assert repo.supplement_task(task.uid, CREATOR, deliverable="报告",
                                deadline_at=db.now() + timedelta(days=2)).ok
    assert not worker._refresh_reminder(row, now)


def test_old_incomplete_task_migration_is_idempotent_and_audited(database):
    task = incomplete_task()
    with db.session_scope() as session:
        session.execute(update(Task).where(Task.uid == task.uid).values(status=TASK_TODO))
    db.init_db()
    migrated = repo.get_task(task.uid)
    assert migrated.status == TASK_NEEDS_INFO
    assert migrated.version == task.version + 1
    log_count = len(repo.list_task_logs(task.uid))
    db.init_db()
    assert len(repo.list_task_logs(task.uid)) == log_count


def test_disabled_group_has_no_reminders_or_task_actions(database, monkeypatch):
    task = incomplete_task()
    monkeypatch.setenv("FEISHU_DISABLED_CHAT_IDS", task.chat_id)
    assert notify.plan_task_info_reminders(db.now() + timedelta(days=5)) == []
    assert not repo.transition_task(task.uid, "cancel", CREATOR, note="测试取消").ok


def test_quiet_hours_do_not_consume_notification_retry_window():
    start = datetime(2026, 11, 23, 21, tzinfo=TZ_SHANGHAI)
    after_quiet = datetime(2026, 11, 24, 8, tzinfo=TZ_SHANGHAI)
    assert not notify.is_too_late(start, after_quiet)
    assert not notify.is_too_late(start.replace(hour=23), after_quiet)
    assert notify.is_too_late(start, after_quiet.replace(hour=14))


def test_daily_digest_is_planned_only_on_workdays():
    assert not notify.should_plan(datetime(2026, 11, 21, 10, tzinfo=TZ_SHANGHAI))
    assert notify.should_plan(datetime(2026, 11, 23, 10, tzinfo=TZ_SHANGHAI))


def test_todo_task_can_report_a_blocker(database):
    task = make_task()
    result = repo.transition_task(task.uid, "block", OWNER, note="等待测试资源")
    assert result.ok and result.task.status == TASK_BLOCKED


def test_reminder_throttle_uses_actual_latest_delivery_time(database, monkeypatch):
    task = make_task()
    first = datetime(2026, 11, 20, 10, tzinfo=TZ_SHANGHAI)
    later = first + timedelta(days=3)
    for key in ("first-created", "second-created"):
        repo.enqueue_notification(key, OWNER, "{}", kind="overdue", task_uid=task.uid)
    rows = {row.dedupe_key: row for row in repo.list_notifications()}
    monkeypatch.setattr(db, "now", lambda: later)
    repo.mark_notification_sent(rows["first-created"].uid)
    monkeypatch.setattr(db, "now", lambda: first)
    repo.mark_notification_sent(rows["second-created"].uid)
    assert repo.last_overdue_sent_times()[task.uid] == later


def test_card_sync_selects_enabled_groups_before_applying_limit(database):
    old = make_task()
    with db.session_scope() as session:
        session.execute(update(Task).where(Task.uid == old.uid).values(chat_id="oc_disabled"))
    active = make_task()
    for task, mid in ((old, "om_disabled"), (active, "om_active")):
        repo.register_task_card(task.uid, mid, OWNER, viewer_open_id=OWNER, version=0)
    assert repo.tasks_needing_card_sync(limit=1) == [active.uid]


def test_supplemented_task_can_replan_a_previously_skipped_unsent_reminder(database):
    now = db.now()
    task = incomplete_task(deadline_at=now + timedelta(hours=3))
    key = notify.due_key(task, OWNER)
    repo.enqueue_notification(key, OWNER, "{}", kind=notify.KIND_DUE,
                              task_uid=task.uid, task_version=task.version)
    old, = repo.list_notifications()
    repo.mark_notification_skipped(old.uid, "任务状态或提醒条件已改变")
    assert repo.supplement_task(task.uid, CREATOR, deliverable="报告").ok
    ready = repo.get_task(task.uid)
    assert repo.enqueue_notification(key, OWNER, "{}", kind=notify.KIND_DUE,
                                     task_uid=task.uid, task_version=ready.version)
    assert not repo.enqueue_notification(key, OWNER, "{}", kind=notify.KIND_DUE,
                                         task_uid=task.uid, task_version=ready.version)
    assert sum(row.dedupe_key == key for row in repo.list_notifications()) == 1
    repo.mark_notification_sent(old.uid)
    assert not repo.enqueue_notification(key, OWNER, "{}", kind=notify.KIND_DUE,
                                         task_uid=task.uid, task_version=ready.version)


@pytest.mark.parametrize("kind", [notify.KIND_DUE, notify.KIND_OVERDUE])
def test_previously_queued_reminder_is_stopped_when_group_is_disabled(database, monkeypatch, kind):
    task = make_task()
    repo.enqueue_notification("test-disabled-reminder", OWNER, "{}", kind=kind, task_uid=task.uid)
    row, = repo.list_notifications()
    monkeypatch.setenv("FEISHU_DISABLED_CHAT_IDS", task.chat_id)
    assert not worker._refresh_reminder(row, db.now())


def test_candidate_info_planning_and_retry_honor_group_disable(database, monkeypatch):
    cand = save_one()
    with db.session_scope() as session:
        session.execute(update(TaskCandidate).where(TaskCandidate.uid == cand.uid).values(missing_fields=["deliverable"]))
    now = db.now()
    cand = repo.get_candidate(cand.uid)
    assert notify.candidate_needs_info(cand, now)
    assert cand.uid in {row.uid for row in repo.list_candidates_for_info_reminder(now, now + timedelta(hours=1))}
    monkeypatch.setenv("FEISHU_DISABLED_CHAT_IDS", cand.chat_id)
    assert not notify.candidate_needs_info(cand, now)
    assert repo.list_candidates_for_info_reminder(now, now + timedelta(hours=1)) == []
