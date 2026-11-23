from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.helpers import make_cmd


@pytest.mark.parametrize("allowed,disabled,chat_type,expected", [
    ("oc_test", "", "group", True),
    ("oc_other", "", "group", False),
    ("oc_test", "oc_test", "group", False),
    ("", "", "group", False),
    ("oc_test", "", "p2p", False),
])
def test_message_entry_filters_before_database_and_model(
    monkeypatch, allowed, disabled, chat_type, expected,
):
    import main

    monkeypatch.setenv("FEISHU_ALLOWED_CHAT_IDS", allowed)
    monkeypatch.setenv("FEISHU_DISABLED_CHAT_IDS", disabled)
    monkeypatch.setattr(main, "STATS", dict.fromkeys(main.STATS, 0))
    cmd = make_cmd(chat_id="oc_test", chat_type=chat_type)
    monkeypatch.setattr(main.models, "parse_event", Mock(return_value=cmd))
    record = Mock(return_value=True)
    feedback = Mock(return_value=SimpleNamespace(text=""))
    detect = Mock(return_value=SimpleNamespace(
        candidate_type="none", confidence=0.9, is_task_candidate=False,
        clarification_question=None, tasks=[],
    ))
    save = Mock(return_value=[])
    monkeypatch.setattr(main.store, "is_new_event", record)
    monkeypatch.setattr(main.store, "count_events", lambda: 0)
    monkeypatch.setattr(main.feedback, "apply_feedback", feedback)
    monkeypatch.setattr(main.detector, "detect", detect)
    monkeypatch.setattr(main.repo, "save_candidates", save)
    monkeypatch.setattr(main.feishu_api, "send_text_to_user", Mock())

    main.on_message(SimpleNamespace(event=None, header=None))

    assert record.call_count == feedback.call_count == int(expected or chat_type == "p2p")
    assert detect.call_count == save.call_count == int(expected)
