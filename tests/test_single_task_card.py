import json

import pytest

import card_action
import card_sync
import cards
import repo
import worker
import db
from models_db import TASK_BLOCKED, TASK_PENDING_ACCEPTANCE
from tests.helpers import make_task, save_one

OWNER = "ou_zhangsan"
CREATOR = "ou_sender"


def register(task, message_id, user):
    repo.register_task_card(task.uid, message_id, user, viewer_open_id=user, version=task.version)


def test_confirm_receipt_reuses_the_confirmed_card(database, monkeypatch):
    import feishu_api

    cand = save_one()
    updates = []
    monkeypatch.setattr(feishu_api, "send_card_to_user", lambda *args, **kwargs: pytest.fail("duplicate card"))
    monkeypatch.setattr(feishu_api, "update_card", lambda mid, card: updates.append(mid) or True)
    monkeypatch.setattr(card_action, "_reply_task_created_async", feishu_api.reply_task_created)
    assert card_action.process(cards.ACTION_CONFIRM, cand.uid, CREATOR, message_id="om_confirmation").ok
    assert updates == ["om_confirmation"]
    task, = repo.list_tasks()
    row, = repo.list_live_task_cards(task.uid)
    assert row.message_id == "om_confirmation"


def test_events_update_original_and_use_text_notifications(database, monkeypatch):
    import feishu_api

    task = make_task()
    register(task, "om_creator", CREATOR)
    repo.transition_task(task.uid, "submit", OWNER, note="材料已完成 https://example.test/report")
    row, = repo.list_notifications()
    updates, texts = [], []
    monkeypatch.setattr(feishu_api, "send_card_to_user", lambda *args, **kwargs: pytest.fail("duplicate card"))
    monkeypatch.setattr(feishu_api, "update_card", lambda mid, card: updates.append((mid, card)) or True)
    monkeypatch.setattr(feishu_api, "send_text_to_user", lambda user, text, **kwargs: texts.append(text) or "om_text")
    assert worker._default_sender(row)
    assert updates[0][0] == "om_creator"
    assert "材料已完成" in json.dumps(updates[0][1], ensure_ascii=False)
    assert texts == [f"{task.title}｜待验收"]
    assert len(repo.list_live_task_cards(task.uid)) == 1


def test_existing_duplicate_private_cards_are_removed(database):
    task = make_task()
    register(task, "om_original", CREATOR)
    register(task, "om_duplicate", CREATOR)
    register(task, "om_owner", OWNER)
    removed = []
    result = card_sync.sync_task(task.uid, updater=lambda *args: True,
                                deleter=lambda mid: removed.append(mid) or True)
    assert result["deleted"] == 1
    assert removed == ["om_duplicate"]
    assert {row.message_id for row in repo.list_live_task_cards(task.uid)} == {"om_original", "om_owner"}


def test_failed_card_update_does_not_send_another_card(database, monkeypatch):
    import feishu_api

    task = make_task()
    register(task, "om_original", CREATOR)
    monkeypatch.setattr(feishu_api, "update_card", lambda *args: False)
    monkeypatch.setattr(feishu_api, "send_card_to_user", lambda *args, **kwargs: pytest.fail("duplicate card"))
    assert feishu_api.reply_task_created("om_source", task) is None
    row, = repo.list_notifications()
    assert row.kind == "task_created"
    assert repo.get_primary_task_card(task.uid, CREATOR).message_id == "om_original"


def test_duplicate_is_kept_when_primary_card_cannot_be_verified(database):
    task = make_task()
    register(task, "om_original", CREATOR)
    register(task, "om_duplicate", CREATOR)
    result = card_sync.sync_task(task.uid, updater=lambda *args: False,
                                deleter=lambda *args: pytest.fail("primary card unavailable"))
    assert result["failed"] >= 1
    assert len(repo.list_live_task_cards(task.uid)) == 2


def test_expired_duplicate_is_reduced_to_a_merged_notice(database):
    task = make_task()
    register(task, "om_original", CREATOR)
    register(task, "om_expired", CREATOR)
    updates = []
    result = card_sync.sync_task(task.uid, updater=lambda mid, card: updates.append((mid, card)) or True,
                                deleter=lambda mid: False)
    assert result["merged"] == 1
    assert len(repo.list_live_task_cards(task.uid)) == 1
    notice = next(card for mid, card in updates if mid == "om_expired")
    assert notice["header"]["title"]["content"] == "任务卡已合并"
    assert not any(element.get("tag") == "action" for element in notice["elements"])


def test_repeated_submission_updates_completion_note_without_new_notification(database):
    task = make_task()
    assert repo.transition_task(task.uid, "submit", OWNER, note="报告初稿").ok
    before = len(repo.list_notifications())
    result = repo.transition_task(task.uid, "submit", OWNER, note="报告终稿 https://example.test/final")
    assert result.ok and result.task.status == TASK_PENDING_ACCEPTANCE
    assert "报告终稿" in result.task.completion_note
    assert len(repo.list_notifications()) == before
    unchanged = repo.transition_task(task.uid, "submit", OWNER, note=result.task.completion_note)
    assert unchanged.task.version == result.task.version


def test_queued_old_state_notification_is_skipped_after_new_state(database):
    task = make_task()
    repo.transition_task(task.uid, "submit", OWNER, note="初次提交")
    old_event, = repo.list_notifications()
    repo.transition_task(task.uid, "reject", CREATOR, note="补充测试数据")
    assert not worker._refresh_reminder(old_event, db.now())


def test_blocked_task_can_submit_for_acceptance(database):
    task = make_task(status=TASK_BLOCKED)
    result = repo.transition_task(task.uid, "submit", OWNER, note="阻塞已解决，交付完成")
    assert result.ok and result.task.status == TASK_PENDING_ACCEPTANCE


@pytest.mark.parametrize("action", ["cancel", "reject"])
def test_reason_is_required_and_logged_for_destructive_actions(database, action):
    task = make_task(status=TASK_PENDING_ACCEPTANCE)
    assert not repo.transition_task(task.uid, action, CREATOR).ok
    result = repo.transition_task(task.uid, action, CREATOR, note="缺少测试数据")
    assert result.ok
    assert result.task.review_note == "缺少测试数据"
    assert repo.list_task_logs(task.uid)[-1].reason == "缺少测试数据"
