"""Day 16 单测：任务 ↔ 飞书日历同步。

要钉住的四件事：
1. 日程占用是 [截止前 15min, 截止]，timestamp 是秒级字符串，提醒语义对
2. 幂等：重复点确认不建第二个日程，失败重试复用同一个 idempotency_key
3. 状态分流：accept 改标题、cancel 删日程、其它动作一次接口都不打
4. 失败留痕但不冒到业务层（日历挂了，任务和卡片照常工作）
"""

import pytest

import calendar_sync
import repo
from models_db import CAL_CREATED, CAL_DELETED, CAL_FAILED, CAL_SKIPPED, TASK_CANCELLED, TASK_TODO
from tests.helpers import DEADLINE, make_task

CREATOR = "ou_sender"
OWNER = "ou_zhangsan"
OTHER = "ou_lisi"
CAL_ID = "feishu.cn_fixture4"


@pytest.fixture(autouse=True)
def _calendar_env(monkeypatch):
    """别让开发机上的 .env 把测试的时间窗口改掉。"""
    monkeypatch.delenv("FEISHU_CALENDAR_ID", raising=False)
    monkeypatch.setenv("CALENDAR_SYNC_MODE", "application")
    monkeypatch.setenv("CALENDAR_LEAD_MINUTES", "15")
    monkeypatch.setenv("CALENDAR_REMINDER_MINUTES", "0")


class FakeApi:
    """假的日历接口。记下每次调用，按需失败。

    跟 card_sync 的 FakeUpdater 一个套路：把「打接口」当参数注入，
    单测不用 mock httpx、不用真凭证、也不用起线程。
    """

    def __init__(self, calendar_id=CAL_ID, fail=()):
        self.calendar_id = calendar_id
        self.fail = set(fail)
        self.created = []
        self.attendees = []
        self.updated = []
        self.deleted = []
        self._n = 0

    def primary_calendar_id(self):
        return self.calendar_id

    def create_event(self, calendar_id, body, key):
        self.created.append((calendar_id, body, key))
        if "create" in self.fail:
            return None
        self._n += 1
        return f"evt_{self._n}_0"

    def add_attendees(self, calendar_id, event_id, open_ids):
        self.attendees.append((calendar_id, event_id, list(open_ids)))
        return "attendees" not in self.fail

    def update_event(self, calendar_id, event_id, body):
        self.updated.append((calendar_id, event_id, body))
        return "update" not in self.fail

    def delete_event(self, calendar_id, event_id):
        self.deleted.append((calendar_id, event_id))
        return "delete" not in self.fail


def _advance(task_uid, action):
    task = repo.get_task(task_uid)
    if action == "accept":
        assert repo.transition_task(task_uid, "start", OWNER).ok
        assert repo.transition_task(task_uid, "submit", OWNER).ok
        assert repo.transition_task(task_uid, "accept", CREATOR).ok
    elif task.status != TASK_CANCELLED:
        assert repo.transition_task(task_uid, "cancel", CREATOR, note="测试取消").ok


def _set_deadline(task_uid, value):
    """直接改库里的截止时间，跳过状态机（状态机在 Day 10 单测里验过）。"""
    from sqlalchemy import update

    import db
    from models_db import Task

    with db.session_scope() as s:
        s.execute(
            update(Task).where(Task.uid == task_uid)
            .values(deadline_at=value)
            .execution_options(synchronize_session=False)
        )


# ------------------------------------------------------------------ 请求体

def test_event_window_is_15_minutes_before_deadline(database):
    task = make_task(owners=(OWNER,))
    body = calendar_sync.build_event_body(task)
    end = int(DEADLINE.timestamp())
    assert body["end_time"]["timestamp"] == str(end)
    assert body["start_time"]["timestamp"] == str(end - 15 * 60)


def test_timestamps_are_strings_with_timezone(database):
    body = calendar_sync.build_event_body(make_task())
    assert isinstance(body["start_time"]["timestamp"], str)
    assert isinstance(body["end_time"]["timestamp"], str)
    assert body["start_time"]["timezone"] == "Asia/Shanghai"
    assert body["end_time"]["timezone"] == "Asia/Shanghai"


def test_event_body_carries_task_context(database):
    task = make_task(owners=(OWNER,), title="提交组会 PPT")
    body = calendar_sync.build_event_body(task)
    assert body["summary"] == "提交组会 PPT"
    text = body["description"]
    for piece in (OWNER, CREATOR, "交付物", task.uid, task.chat_id, "截止时间"):
        assert piece in text


def test_event_body_has_source_message_id(database):
    body = calendar_sync.build_event_body(
        make_task(), source_message_id="om_original_001")
    assert "om_original_001" in body["description"]


def test_reminder_is_relative_to_start_not_deadline(database):
    """开始时间已经是截止前 15 分钟，minutes=0 才落在那一下。"""
    body = calendar_sync.build_event_body(make_task())
    assert body["reminders"] == [{"minutes": 0}]


def test_reminder_can_be_disabled(database, monkeypatch):
    monkeypatch.setenv("CALENDAR_REMINDER_MINUTES", "-1")
    body = calendar_sync.build_event_body(make_task())
    assert "reminders" not in body


def test_event_body_protects_from_attendee_edits(database):
    body = calendar_sync.build_event_body(make_task())
    assert body["free_busy_status"] == "busy"
    assert body["attendee_ability"] == "can_see_others"
    assert body["need_notification"] is False


def test_completed_body_has_prefix_and_result(database):
    body = calendar_sync.build_event_body(make_task(), completed=True)
    assert body["summary"].startswith("已完成｜")
    assert "验收结果：已完成" in body["description"]


def test_no_deadline_returns_none(database):
    """「尽快 / 抽空」被判成 unresolved，这种任务不能硬建出 1970 年的日程。"""
    task = make_task()
    _set_deadline(task.uid, None)
    assert calendar_sync.build_event_body(repo.get_task(task.uid)) is None


# ------------------------------------------------------------------ 建单同步

def test_create_writes_mapping_and_adds_attendees(database):
    task = make_task(owners=(OWNER, OTHER))
    api = FakeApi()
    stats = calendar_sync.sync_created(task.uid, api)

    assert stats["created"] == 1
    assert len(api.created) == 1
    row = repo.get_calendar_event(task.uid)
    assert row.status == CAL_CREATED
    assert row.event_id == "evt_1_0"
    assert row.calendar_id == CAL_ID
    assert api.attendees[0][2] == [OWNER, OTHER, CREATOR]


def test_creator_who_is_also_owner_is_not_added_twice(database):
    task = make_task(owners=(CREATOR,))
    api = FakeApi()
    calendar_sync.sync_created(task.uid, api)
    assert api.attendees[0][2] == [CREATOR]


def test_repeated_create_does_not_build_second_event(database):
    task = make_task()
    api = FakeApi()
    calendar_sync.sync_created(task.uid, api)
    stats = calendar_sync.sync_created(task.uid, api)
    assert stats["skipped"] == 1
    assert stats["created"] == 0
    assert len(api.created) == 1
    assert len(api.attendees) == 1


def test_idempotency_key_is_stable_across_failed_retry(database):
    task = make_task()
    api = FakeApi(fail={"create"})
    first = calendar_sync.sync_created(task.uid, api)
    assert first["failed"] == 1
    assert repo.get_calendar_event(task.uid).status == CAL_FAILED

    api.fail.clear()
    second = calendar_sync.sync_created(task.uid, api)
    assert second["created"] == 1
    assert api.created[0][2] == api.created[1][2]
    assert 32 <= len(api.created[0][2]) <= 128
    assert repo.get_calendar_event(task.uid).status == CAL_CREATED


def test_attendee_failure_keeps_event_created(database):
    """日程已经建好，加参与人失败不能把状态打回 failed，否则重试会以为没建。"""
    task = make_task()
    api = FakeApi(fail={"attendees"})
    stats = calendar_sync.sync_created(task.uid, api)
    assert stats["created"] == 1
    assert stats["attendees"] == 0
    row = repo.get_calendar_event(task.uid)
    assert row.status == CAL_CREATED
    assert "参与人" in row.last_error
    assert row.attendees_synced == 0


def test_no_deadline_is_skipped_without_calling_api(database):
    task = make_task()
    _set_deadline(task.uid, None)
    api = FakeApi()
    stats = calendar_sync.sync_created(task.uid, api)
    assert stats["skipped"] == 1
    assert api.created == []
    assert repo.get_calendar_event(task.uid).status == CAL_SKIPPED


def test_missing_calendar_id_is_logged_as_failure(database):
    task = make_task()
    api = FakeApi(calendar_id="")
    stats = calendar_sync.sync_created(task.uid, api)
    assert stats["failed"] == 1
    assert api.created == []
    assert repo.get_calendar_event(task.uid).status == CAL_FAILED


# ------------------------------------------------------------------ 状态同步

def test_accept_patches_summary_and_description(database):
    task = make_task(owners=(OWNER,))
    api = FakeApi()
    calendar_sync.sync_created(task.uid, api)
    _advance(task.uid, "accept")
    stats = calendar_sync.sync_transition(task.uid, "accept", api)

    assert stats["updated"] == 1
    assert len(api.updated) == 1
    body = api.updated[0][2]
    assert body["summary"].startswith("已完成｜")
    assert "验收结果：已完成" in body["description"]
    assert repo.get_calendar_event(task.uid).status == CAL_CREATED


def test_cancel_deletes_event(database):
    task = make_task()
    api = FakeApi()
    calendar_sync.sync_created(task.uid, api)
    _advance(task.uid, "cancel")
    stats = calendar_sync.sync_transition(task.uid, "cancel", api)

    assert stats["deleted"] == 1
    assert len(api.deleted) == 1
    assert repo.get_calendar_event(task.uid).status == CAL_DELETED


def test_cancelled_task_does_not_get_rebuilt(database):
    task = make_task()
    api = FakeApi()
    calendar_sync.sync_created(task.uid, api)
    _advance(task.uid, "cancel")
    calendar_sync.sync_transition(task.uid, "cancel", api)
    stats = calendar_sync.sync_created(task.uid, api)
    assert stats["skipped"] == 1
    assert len(api.created) == 1


def test_stale_action_does_not_override_current_task(database):
    task = make_task()
    api = FakeApi()
    calendar_sync.sync_created(task.uid, api)
    for action in ("start", "block", "unblock", "submit", "reject", "cancel", "accept"):
        assert calendar_sync.sync_transition(task.uid, action, api)["skipped"] == 1
    assert api.updated == []
    assert api.deleted == []


def test_transition_without_mapping_repairs_missing_event(database):
    task = make_task()
    api = FakeApi()
    stats = calendar_sync.sync_transition(task.uid, "accept", api)
    assert stats["created"] == 1
    assert api.updated == []


def test_delete_failure_keeps_created_status(database):
    task = make_task()
    api = FakeApi()
    calendar_sync.sync_created(task.uid, api)
    _advance(task.uid, "cancel")
    api.fail.add("delete")
    stats = calendar_sync.sync_transition(task.uid, "cancel", api)
    assert stats["failed"] == 1
    assert repo.get_calendar_event(task.uid).status == CAL_CREATED


def test_delete_failure_can_be_retried(database):
    """删失败后状态保持 created，下一轮才能真的删掉，不会永远卡着。"""
    task = make_task()
    api = FakeApi()
    calendar_sync.sync_created(task.uid, api)
    _advance(task.uid, "cancel")
    api.fail.add("delete")
    calendar_sync.sync_transition(task.uid, "cancel", api)

    api.fail.clear()
    stats = calendar_sync.sync_transition(task.uid, "cancel", api)
    assert stats["deleted"] == 1
    assert repo.get_calendar_event(task.uid).status == CAL_DELETED


def test_async_entry_respects_disabled_switch(database, monkeypatch):
    """conftest 已经把开关关了：入口必须一个接口都不打。"""
    calls = []
    monkeypatch.setattr(calendar_sync, "sync_created",
                        lambda uid: calls.append(uid))
    calendar_sync.on_created_async("abc12345")
    assert calls == []


# ------------------------------------------------------------------ 改期（预留）

def test_reschedule_patches_start_and_end_together(database):
    task = make_task()
    api = FakeApi()
    calendar_sync.sync_created(task.uid, api)

    from datetime import timedelta

    new_deadline = DEADLINE + timedelta(days=2)
    _set_deadline(task.uid, new_deadline)

    stats = calendar_sync.sync_deadline_changed(task.uid, api)
    assert stats["updated"] == 1
    patch = api.updated[0][2]
    assert "start_time" in patch and "end_time" in patch
    assert patch["end_time"]["timestamp"] == str(int(new_deadline.timestamp()))
    assert repo.get_calendar_event(task.uid).deadline_at == new_deadline


def test_reschedule_skips_when_deadline_unchanged(database):
    task = make_task()
    api = FakeApi()
    calendar_sync.sync_created(task.uid, api)
    stats = calendar_sync.sync_deadline_changed(task.uid, api)
    assert stats["skipped"] == 1
    assert api.updated == []


# ------------------------------------------------------------------ 补偿、并发和旧数据


def test_attendee_failure_is_retried_without_recreating_event(database):
    task = make_task()
    api = FakeApi(fail={"attendees"})
    calendar_sync.sync_task(task.uid, api)
    api.fail.clear()
    stats = calendar_sync.sync_task(task.uid, api)
    assert stats["attendees"] == 2
    assert len(api.created) == 1
    assert repo.get_calendar_event(task.uid).attendees_synced == 1
    assert repo.get_calendar_event(task.uid).last_error is None


def test_status_changes_update_calendar_and_completion_stops_reminders(database):
    task = make_task()
    api = FakeApi()
    calendar_sync.sync_task(task.uid, api)
    for action, user, label in (
        ("start", OWNER, "进行中"),
        ("block", OWNER, "阻塞"),
        ("unblock", OWNER, "进行中"),
        ("submit", OWNER, "待验收"),
        ("reject", CREATOR, "进行中"),
        ("submit", OWNER, "待验收"),
        ("accept", CREATOR, "已完成"),
    ):
        assert repo.transition_task(task.uid, action, user, note="测试状态变更").ok
        calendar_sync.sync_transition(task.uid, action, api)
        assert api.updated[-1][2]["summary"].startswith(label + "｜")
        assert label in api.updated[-1][2]["description"]
    assert api.updated[-1][2]["reminders"] == []
    assert api.updated[-1][2]["free_busy_status"] == "free"
    assert repo.get_calendar_event(task.uid).synced_version == repo.get_task(task.uid).version


def test_worker_retries_failure_after_backoff_and_recovers_after_reconfigure(database, monkeypatch):
    import db
    monkeypatch.setenv("CALENDAR_SYNC_ENABLED", "1")
    task = make_task()
    api = FakeApi(fail={"create"})
    calendar_sync.sync_task(task.uid, api)
    row = repo.get_calendar_event(task.uid)
    assert row.fail_count == 1
    assert row.retry_at > db.now()
    assert calendar_sync.reconcile(api=api)["checked"] == 0
    api.fail.clear()
    db.configure(database)
    stats = calendar_sync.reconcile(now=row.retry_at, api=api)
    assert stats["checked"] == 1
    assert stats["failed"] == 0
    assert api.created[0][2] == api.created[1][2]
    assert repo.get_calendar_event(task.uid).retry_at is None
    assert repo.get_calendar_event(task.uid).fail_count == 0
    assert calendar_sync.reconcile(now=row.retry_at, api=api)["checked"] == 0


def test_worker_repairs_missing_event_and_honors_disabled_chat(database, monkeypatch):
    monkeypatch.setenv("CALENDAR_SYNC_ENABLED", "1")
    task = make_task()
    api = FakeApi()
    monkeypatch.setenv("FEISHU_DISABLED_CHAT_IDS", task.chat_id)
    assert calendar_sync.reconcile(api=api)["checked"] == 0
    monkeypatch.setenv("FEISHU_DISABLED_CHAT_IDS", "")
    assert calendar_sync.reconcile(api=api)["checked"] == 1
    assert len(api.created) == 1


def test_worker_dry_run_has_no_api_or_database_writes(database, monkeypatch):
    monkeypatch.setenv("CALENDAR_SYNC_ENABLED", "1")
    task = make_task()
    api = FakeApi()
    assert calendar_sync.reconcile(api=api, dry_run=True)["checked"] == 1
    assert repo.get_calendar_event(task.uid) is None
    assert api.created == []


def test_cancel_before_creation_does_not_create_an_event(database):
    task = make_task()
    _advance(task.uid, "cancel")
    api = FakeApi()
    calendar_sync.sync_created(task.uid, api)
    assert api.created == []
    assert api.attendees == []
    assert repo.get_calendar_event(task.uid).status == CAL_DELETED


def test_cancel_during_creation_deletes_event_before_inviting(database):
    task = make_task()
    api = FakeApi()
    original = api.create_event
    def create(calendar_id, body, key):
        _advance(task.uid, "cancel")
        return original(calendar_id, body, key)
    api.create_event = create
    stats = calendar_sync.sync_created(task.uid, api)
    assert stats["deleted"] == 1
    assert len(api.deleted) == 1
    assert api.attendees == []
    assert repo.get_calendar_event(task.uid).status == CAL_DELETED


def test_uncertain_create_is_recovered_then_cancelled(database):
    task = make_task()
    api = FakeApi(fail={"create"})
    calendar_sync.sync_task(task.uid, api)
    _advance(task.uid, "cancel")
    api.fail.clear()
    calendar_sync.sync_task(task.uid, api)
    assert api.created[0][1:] == api.created[1][1:]
    assert len(api.deleted) == 1
    assert api.attendees == []
    assert repo.get_calendar_event(task.uid).status == CAL_DELETED


def test_failed_create_then_deadline_change_replays_original_and_patches_latest(database):
    from datetime import timedelta
    task = make_task()
    api = FakeApi(fail={"create"})
    calendar_sync.sync_task(task.uid, api)
    _set_deadline(task.uid, DEADLINE + timedelta(days=1))
    api.fail.clear()
    calendar_sync.sync_task(task.uid, api)
    assert api.created[0][1:] == api.created[1][1:]
    assert api.updated[-1][2]["end_time"]["timestamp"] == str(int((DEADLINE + timedelta(days=1)).timestamp()))


def test_calendar_configuration_change_does_not_move_pending_creation(database, monkeypatch):
    task = make_task()
    api = FakeApi(fail={"create"})
    calendar_sync.sync_task(task.uid, api)
    monkeypatch.setenv("FEISHU_CALENDAR_ID", "another_calendar")
    api.fail.clear()
    calendar_sync.sync_task(task.uid, api)
    assert {call[0] for call in api.created} == {CAL_ID}


def test_removing_then_restoring_deadline_uses_new_event_key(database):
    task = make_task()
    api = FakeApi()
    calendar_sync.sync_task(task.uid, api)
    original_key = api.created[0][2]
    _set_deadline(task.uid, None)
    calendar_sync.sync_task(task.uid, api)
    assert len(api.deleted) == 1
    assert repo.get_calendar_event(task.uid).status == CAL_SKIPPED
    _set_deadline(task.uid, DEADLINE)
    calendar_sync.sync_task(task.uid, api)
    assert len(api.created) == 2
    assert api.created[-1][2] != original_key


def test_api_exception_keeps_task_and_schedules_retry(database):
    task = make_task()
    api = FakeApi()
    def broken(*args):
        raise OSError("connection lost")
    api.create_event = broken
    assert calendar_sync.sync_task(task.uid, api)["failed"] == 1
    assert repo.get_task(task.uid).status == TASK_TODO
    row = repo.get_calendar_event(task.uid)
    assert "connection lost" in row.last_error
    assert row.retry_at is not None


def test_cross_process_lock_prevents_parallel_calls(database):
    import sqlite3
    task = make_task()
    api = FakeApi()
    with sqlite3.connect(database + ".calendar-lock.db") as lock:
        lock.execute("BEGIN IMMEDIATE")
        assert calendar_sync.sync_task(task.uid, api)["skipped"] == 1
    assert api.created == []
    assert calendar_sync.sync_task(task.uid, api)["created"] == 1


def test_description_escapes_html_and_renders_chinese_status(database):
    from sqlalchemy import update
    import db
    from models_db import Task
    task = make_task()
    with db.session_scope() as session:
        session.execute(update(Task).where(Task.uid == task.uid).values(description="<b>内容</b> & 测试"))
    body = calendar_sync.build_event_body(repo.get_task(task.uid))
    assert "&lt;b&gt;内容&lt;/b&gt; &amp; 测试" in body["description"]
    assert "当前状态：待开始" in body["description"]
    assert "<br>" in body["description"]


def test_worker_tick_runs_calendar_compensation(database, monkeypatch):
    import worker
    calls = []
    monkeypatch.setattr(calendar_sync, "reconcile", lambda now, dry_run: calls.append(dry_run) or {"checked": 2, "failed": 1})
    stats = worker.tick(dry_run=True)
    assert calls == [True]
    assert stats["calendar_checked"] == 2
    assert stats["calendar_failed"] == 1


def test_old_database_calendar_columns_are_upgraded_without_data_loss(database):
    import db
    from sqlalchemy import text
    task = make_task()
    calendar_sync.sync_task(task.uid, FakeApi())
    with db.get_engine().begin() as conn:
        for column in ("synced_version", "retry_at", "request_body"):
            conn.execute(text(f"ALTER TABLE task_calendar_event DROP COLUMN {column}"))
    db.init_db()
    db.init_db()
    row = repo.get_calendar_event(task.uid)
    assert row.event_id == "evt_1_0"
    assert row.synced_version == 0
    api = FakeApi()
    assert calendar_sync.sync_task(task.uid, api)["updated"] == 1
    assert api.created == []
