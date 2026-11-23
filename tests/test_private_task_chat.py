"""任务入口集中到机器人私聊，所有外部发送均替换为本地记录。"""

import json
from types import SimpleNamespace

import pytest

import cards
import db
import feedback
import repo
import worker
from models_db import TASK_DONE, TASK_IN_PROGRESS, TASK_PENDING_ACCEPTANCE
from tests.helpers import make_cmd, make_task, save_one

OWNER = "ou_zhangsan"
CREATOR = "ou_sender"


def private_reply(message_id, text, sender=OWNER):
    return make_cmd(chat_type="p2p", chat_id="oc_private", sender_open_id=sender,
                    parent_id=message_id, root_id=message_id, text=text, text_raw=text)


def register_private_card(task, message_id, receiver):
    repo.register_task_card(task.uid, message_id, receiver, viewer_open_id=receiver,
                            version=task.version)


def test_private_reply_targets_the_replied_card_among_multiple_tasks(database):
    first = make_task(status=TASK_IN_PROGRESS)
    second = make_task(status=TASK_IN_PROGRESS)
    register_private_card(first, "om_first", OWNER)
    register_private_card(second, "om_second", OWNER)
    result = feedback.apply_feedback(private_reply("om_second", "做完了"))
    assert result.ok
    assert repo.get_task(first.uid).status == TASK_IN_PROGRESS
    assert repo.get_task(second.uid).status == TASK_PENDING_ACCEPTANCE


def test_creator_accepts_by_replying_in_private_chat(database):
    task = make_task(status=TASK_PENDING_ACCEPTANCE)
    register_private_card(task, "om_review", CREATOR)
    result = feedback.apply_feedback(private_reply("om_review", "ok验收通过", CREATOR))
    assert result.ok
    assert repo.get_task(task.uid).status == TASK_DONE


@pytest.mark.parametrize("case", ["other_recipient", "disabled_group", "no_reply"])
def test_private_feedback_requires_card_recipient_and_enabled_group(database, monkeypatch, case):
    task = make_task(status=TASK_IN_PROGRESS)
    register_private_card(task, "om_task", CREATOR if case == "other_recipient" else OWNER)
    cmd = private_reply("om_task", "做完了")
    if case == "disabled_group":
        monkeypatch.setenv("FEISHU_DISABLED_CHAT_IDS", task.chat_id)
    if case == "no_reply":
        cmd.parent_id = cmd.root_id = None
    assert not feedback.apply_feedback(cmd).ok
    assert repo.get_task(task.uid).status == TASK_IN_PROGRESS


@pytest.mark.parametrize("text", ["验收通过了吗", "还没验收通过", "验收通过？",
                                  "不能验收通过", "不是验收不通过", "验收通过后再提交"])
def test_acceptance_question_or_negation_does_not_approve(text):
    assert feedback.classify(text) is None


def test_candidate_sends_to_private_chat_and_failed_delivery_is_queued(database, monkeypatch):
    import feishu_api

    cand = save_one()
    attempts = []
    monkeypatch.setattr(feishu_api, "send_card_to_user",
                        lambda receiver, card, **kwargs: attempts.append((receiver, kwargs)) or None)
    monkeypatch.setattr(feishu_api, "send_card_to_chat", lambda *args: pytest.fail("unexpected group send"))
    assert feishu_api.push_confirm_card(cand, cards.build_candidate_card(cand)) is None
    row, = repo.list_notifications()
    assert row.kind == "candidate_confirm"
    assert row.receive_id_type == "open_id"
    assert row.receive_id == CREATOR
    assert attempts[0][0] == CREATOR
    assert attempts[0][1]["message_key"] == row.dedupe_key
    assert repo.reject_candidate(cand.uid, CREATOR)
    assert not worker._refresh_reminder(row, db.now())


def test_task_delivery_failure_is_queued_for_each_owner(database, monkeypatch):
    import feishu_api

    task = make_task(owners=(OWNER, "ou_lisi"))
    monkeypatch.setattr(feishu_api, "send_card_to_user", lambda *args, **kwargs: None)
    monkeypatch.setattr(feishu_api, "send_card_to_chat", lambda *args: pytest.fail("unexpected group send"))
    assert feishu_api.push_task_card(task) is None
    rows = repo.list_notifications()
    assert {row.receive_id for row in rows} == {OWNER, "ou_lisi"}
    assert all(row.kind == "task_assignment" and row.receive_id_type == "open_id" for row in rows)
    assert repo.transition_task(task.uid, "start", OWNER).ok
    row = rows[0]
    assert worker._refresh_reminder(row, db.now())
    assert "进行中" in row.payload
    assert row.task_version == repo.get_task(task.uid).version


@pytest.mark.parametrize("chat_type", ["p2p", "group"])
def test_feedback_entry_acknowledges_in_private_chat(database, monkeypatch, chat_type):
    import main

    task = make_task(status=TASK_IN_PROGRESS)
    register_private_card(task, "om_task", OWNER)
    cmd = private_reply("om_task", "做完了")
    if chat_type == "group":
        cmd.chat_type = "group"
        cmd.chat_id = task.chat_id
        cmd.parent_id = cmd.root_id = repo.get_candidate(task.candidate_uid).message_id
    monkeypatch.setattr(main.models, "parse_event", lambda *args: cmd)
    monkeypatch.setattr(main.store, "is_new_event", lambda *args: True)
    monkeypatch.setattr(main.detector, "detect", lambda *args: pytest.fail("unexpected model call"))
    messages = []
    monkeypatch.setattr(main.feishu_api, "send_text_to_user",
                        lambda receiver, text: messages.append((receiver, text)))
    monkeypatch.setattr(main.feishu_api, "send_text_to_chat", lambda *args: pytest.fail("unexpected group send"))
    main.on_message(SimpleNamespace(event=None, header=None))
    assert repo.get_task(task.uid).status == TASK_PENDING_ACCEPTANCE
    receiver, text = messages[0]
    assert receiver == OWNER
    assert "待验收" in text


def test_worker_retries_with_same_message_key_and_registers_private_card(database, monkeypatch):
    import feishu_api

    task = make_task()
    key = f"task_assignment:{task.uid}:{OWNER}"
    repo.enqueue_notification(key, OWNER, json.dumps(cards.build_task_card(task, OWNER)),
                              kind="task_assignment", task_uid=task.uid, task_version=task.version)
    row, = repo.list_notifications()
    calls = []
    monkeypatch.setattr(feishu_api, "send_card_to_user",
                        lambda receiver, card, **kwargs: calls.append((receiver, kwargs)) or "om_retry")
    assert worker._default_sender(row)
    assert calls == [(OWNER, {"message_key": f"TASK:{task.uid}:{OWNER}"})]
    registered, = repo.list_live_task_cards(task.uid)
    assert registered.viewer_open_id == OWNER
    assert registered.message_id == "om_retry"
