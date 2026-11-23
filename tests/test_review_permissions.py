import pytest
from sqlalchemy import text

import cards
import db
import events
import permissions
import repo
from tests.helpers import save_one


@pytest.mark.parametrize("binding,allowed", [("oc_001:ou_pm", True), ("oc_other:ou_pm", False)])
def test_manager_authority_is_scoped_to_task_chat(task, monkeypatch, binding, allowed):
    monkeypatch.setenv("FEISHU_CHAT_MANAGER_BINDINGS", binding)
    for action in ("submit", "unblock", "accept", "reject", "cancel"):
        assert permissions.can(task, "ou_pm", action).ok is allowed
    assert not permissions.can(task, "ou_pm", "start").ok


def test_designated_reviewer_can_accept_and_receives_card(database):
    cand = save_one()
    task = repo.confirm_candidate(cand.uid, cand.sender_open_id, reviewer_open_id="ou_reviewer")
    assert not permissions.can(task, "ou_reviewer", "cancel")
    assert repo.transition_task(task.uid, "start", "ou_zhangsan").ok
    assert repo.transition_task(task.uid, "submit", "ou_zhangsan").ok
    pending = repo.get_task(task.uid)
    assert events.recipients(pending, "submit", "ou_zhangsan") == ["ou_sender", "ou_reviewer"]
    card = cards.build_task_card(pending, "ou_reviewer")
    actions = [button["value"]["act"] for element in card["elements"]
               for button in element.get("actions", [])]
    assert set(actions) == {"accept", "reject"}
    assert repo.transition_task(task.uid, "accept", "ou_reviewer").ok
    assert repo.list_task_logs(task.uid)[-1].operator_open_id == "ou_reviewer"


def test_default_reviewer_and_manager_self_review_prohibition(database, monkeypatch):
    monkeypatch.setenv("FEISHU_CHAT_REVIEWER_BINDINGS", "oc_other:ou_other,oc_001:ou_reviewer")
    monkeypatch.setenv("FEISHU_CHAT_MANAGER_BINDINGS", "oc_001:ou_zhangsan,oc_001:ou_pm")
    cand = save_one()
    task = repo.confirm_candidate(cand.uid, cand.sender_open_id)
    assert task.effective_reviewer_open_id == "ou_reviewer"
    assert not permissions.can(task, "ou_zhangsan", "accept")
    assert not permissions.can(task, "ou_zhangsan", "reject")
    assert permissions.can(task, "ou_pm", "accept")
    assert set(events.recipients(task, "block", "ou_zhangsan")) == {"ou_sender", "ou_pm"}


def test_legacy_database_upgrade_preserves_tasks_and_is_repeatable(task):
    # 模拟升级前的数据库缺列，然后按正常启动入口升级两次。
    with db.get_engine().begin() as conn:
        conn.execute(text("ALTER TABLE task DROP COLUMN reviewer_open_id"))
    db.init_db()
    db.init_db()
    restored = repo.get_task(task.uid)
    assert restored.reviewer_open_id is None
    assert restored.effective_reviewer_open_id == task.creator_open_id
    assert permissions.can(restored, task.creator_open_id, "accept")


def test_reviewer_is_persisted_and_rejected_on_invalid_input(database, monkeypatch):
    cand = save_one()
    with pytest.raises(repo.CandidateActionError):
        repo.confirm_candidate(cand.uid, cand.sender_open_id, reviewer_open_id="")
    assert repo.get_candidate(cand.uid).status == "pending"
    task = repo.confirm_candidate(cand.uid, cand.sender_open_id)
    monkeypatch.setenv("FEISHU_CHAT_REVIEWER_BINDINGS", "oc_001:ou_new")
    assert repo.get_task(task.uid).effective_reviewer_open_id == cand.sender_open_id
    assert events.recipients(task, "submit", "ou_zhangsan") == [cand.sender_open_id]
