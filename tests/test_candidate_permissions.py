import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest

import card_action
import db
import repo
import show_db
from models_db import TaskCandidate
from tests.helpers import save_one


@pytest.fixture(autouse=True)
def candidate_managers(monkeypatch):
    monkeypatch.setenv("FEISHU_CHAT_MANAGER_BINDINGS", "oc_001:ou_mgr")


@pytest.mark.parametrize("action", ["confirm", "reject"])
@pytest.mark.parametrize("case", ["assignee", "foreign_manager", "no_identity", "disabled", "unlisted"])
def test_denial_preserves_shared_card_and_candidate(database, monkeypatch, action, case):
    cand = save_one()
    operator = cand.sender_open_id
    if case == "assignee":
        operator = cand.assignee_open_ids[0]
    elif case == "foreign_manager":
        monkeypatch.setenv("FEISHU_CHAT_MANAGER_BINDINGS", "oc_other:ou_mgr")
        operator = "ou_mgr"
    elif case == "no_identity":
        operator = ""
    elif case == "disabled":
        monkeypatch.setenv("FEISHU_DISABLED_CHAT_IDS", cand.chat_id)
    else:
        monkeypatch.setenv("FEISHU_ALLOWED_CHAT_IDS", "oc_other")

    outcome = card_action.process(action, cand.uid, operator)
    assert not outcome.ok
    assert outcome.card is None
    assert cand.title not in outcome.toast_content
    assert repo.get_candidate(cand.uid).status == "pending"
    assert repo.count_tasks() == 0


@pytest.mark.parametrize("action", ["confirm", "reject"])
@pytest.mark.parametrize("legacy", [False, True])
def test_expires_at_boundary_without_worker(database, monkeypatch, action, legacy):
    cand = save_one()
    if legacy:
        with db.session_scope() as s:
            s.get(TaskCandidate, cand.id).expires_at = None
    monkeypatch.setattr(db, "now", lambda: cand.created_at + timedelta(hours=72))
    outcome = card_action.process(action, cand.uid, cand.sender_open_id)
    assert not outcome.ok and "过期" in outcome.toast_content
    assert repo.count_tasks() == 0
    assert repo.expire_candidates() == 1


@pytest.mark.parametrize(("field", "value"), [
    ("title", " "), ("sender_open_id", ""), ("assignee_open_ids", []),
    ("assignee_open_ids", ["@_user_1"]), ("missing_fields", ["task_content"]),
])
def test_incomplete_candidate_can_be_ignored_but_not_confirmed(database, field, value):
    cand = save_one()
    with db.session_scope() as s:
        setattr(s.get(TaskCandidate, cand.id), field, value)
    with pytest.raises(repo.CandidateActionError, match="补充"):
        repo.confirm_candidate(cand.uid, "ou_mgr")
    assert repo.get_candidate(cand.uid).status == "pending"
    assert repo.count_tasks() == 0
    assert repo.reject_candidate(cand.uid, "ou_mgr")


def test_simultaneous_authorized_confirms_create_one_task(database):
    cand = save_one()
    barrier = Barrier(2)

    def confirm(operator):
        barrier.wait(timeout=5)
        return repo.confirm_candidate(cand.uid, operator)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(confirm, [cand.sender_open_id, "ou_mgr"]))
    assert sum(task is not None for task in results) == repo.count_tasks() == 1


def test_cli_requires_verified_identity(database, monkeypatch):
    cand = save_one()
    monkeypatch.setattr(sys, "argv", ["show_db.py", "--confirm", cand.uid])
    monkeypatch.setattr(show_db, "load_dotenv", lambda: None)
    monkeypatch.delenv("FEISHU_USER_ACCESS_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="身份|用户|TOKEN"):
        show_db.main()
    monkeypatch.setattr(show_db, "_verified_operator", lambda: "ou_stranger")
    with pytest.raises(SystemExit, match="无权"):
        show_db.main()
    assert repo.get_candidate(cand.uid).status == "pending"
    monkeypatch.setattr(show_db, "_verified_operator", lambda: cand.sender_open_id)
    show_db.main()
    assert repo.count_tasks() == 1
