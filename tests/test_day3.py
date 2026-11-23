"""Day 3 单测：Schema 校验 + 防「模型伪造成员」的后置校验。

不调真实模型，用构造的 dict 模拟模型输出。
"""

import pytest
from pydantic import ValidationError

import detector
from models import Mention
from schemas import TaskRecognition


def _base_payload(**overrides):
    payload = {
        "is_task_candidate": True,
        "candidate_type": "task",
        "confidence": 0.93,
        "tasks": [
            {
                "title": "提交供应商整改报告",
                "description": None,
                "assignee_mention_ids": [],
                "deliverable": "供应商整改报告",
                "deadline_text": "本周五前",
                "acceptance_criteria": None,
                "priority": "normal",
                "missing_fields": [],
                "evidence": "请本周五前提交供应商整改报告",
            }
        ],
        "clarification_needed": False,
        "clarification_question": None,
    }
    payload.update(overrides)
    return payload


def test_valid_payload_passes():
    result = TaskRecognition.model_validate(_base_payload())
    assert result.tasks[0].deadline_text == "本周五前"
    assert result.confidence == 0.93


def test_confidence_out_of_range_rejected():
    with pytest.raises(ValidationError):
        TaskRecognition.model_validate(_base_payload(confidence=1.7))


def test_more_than_10_tasks_rejected():
    task = _base_payload()["tasks"][0]
    with pytest.raises(ValidationError):
        TaskRecognition.model_validate(_base_payload(tasks=[task] * 11))


def test_not_candidate_drops_tasks():
    result = TaskRecognition.model_validate(
        _base_payload(is_task_candidate=False, candidate_type="unrelated")
    )
    assert result.tasks == []


def test_candidate_without_tasks_becomes_unclear():
    result = TaskRecognition.model_validate(_base_payload(tasks=[]))
    assert result.is_task_candidate is False
    assert result.candidate_type == "unclear"


def test_blank_title_rejected():
    payload = _base_payload()
    payload["tasks"][0]["title"] = "   "
    with pytest.raises(ValidationError):
        TaskRecognition.model_validate(payload)


def test_hallucinated_assignee_is_dropped():
    """模型编了一个不存在的 open_id，必须被丢掉并标记为缺负责人。"""
    payload = _base_payload()
    payload["tasks"][0]["assignee_mention_ids"] = ["ou_fake_hallucination"]
    result = TaskRecognition.model_validate(payload)

    mentions = [Mention(key="@_user_1", name="张三", open_id="ou_zhangsan")]
    result = detector._validate_mentions(result, mentions)

    assert result.tasks[0].assignee_mention_ids == []
    assert "assignee" in result.tasks[0].missing_fields


def test_valid_assignee_is_kept_and_missing_cleared():
    payload = _base_payload()
    payload["tasks"][0]["assignee_mention_ids"] = ["ou_zhangsan"]
    payload["tasks"][0]["missing_fields"] = ["assignee"]
    result = TaskRecognition.model_validate(payload)

    mentions = [Mention(key="@_user_1", name="张三", open_id="ou_zhangsan")]
    result = detector._validate_mentions(result, mentions)

    assert result.tasks[0].assignee_mention_ids == ["ou_zhangsan"]
    assert "assignee" not in result.tasks[0].missing_fields


def test_no_mention_marks_assignee_missing():
    result = TaskRecognition.model_validate(_base_payload())
    result = detector._validate_mentions(result, [])
    assert "assignee" in result.tasks[0].missing_fields


def test_high_confidence_requires_complete_fields():
    result = TaskRecognition.model_validate(_base_payload())
    assert result.high_confidence() is True

    payload = _base_payload()
    payload["tasks"][0]["missing_fields"] = ["deadline"]
    assert TaskRecognition.model_validate(payload).high_confidence() is False


@pytest.mark.parametrize("source,evidence,expected", [
    ("今天晚上七点前交给我论文ppt", None, "normal"),
    ("今天晚上七点前交给我论文ppt", "请紧急提交", "normal"),
    ("请加急提交论文ppt", "请加急提交论文ppt", "urgent"),
    ("这是一项紧急任务，请交论文ppt", "紧急任务", "urgent"),
    ("请优先处理论文ppt", "优先处理", "urgent"),
    ("这个不紧急，明天交论文ppt", "不紧急", "normal"),
    ("无需加急处理论文ppt", "无需加急处理论文ppt", "normal"),
    ("请提交紧急预案培训PPT", "紧急预案培训PPT", "normal"),
    ("请加急提交论文ppt，不要拖延", "请加急提交论文ppt，不要拖延", "urgent"),
])
def test_detect_requires_source_evidence_for_urgent_priority(monkeypatch, source, evidence, expected):
    from tests.helpers import make_cmd

    payload = _base_payload()
    payload["tasks"][0].update(priority="urgent", priority_evidence=evidence, evidence=source)
    monkeypatch.setattr(detector.llm, "chat_json", lambda *args: payload)
    result = detector.detect(make_cmd(text=source, text_raw=source))
    assert result.tasks[0].priority == expected
    if expected == "normal":
        assert result.tasks[0].priority_evidence is None


def test_urgency_is_not_inherited_by_another_task(monkeypatch):
    from tests.helpers import make_cmd

    source = "请加急提交报告，另请明天提交PPT"
    payload = _base_payload()
    first = payload["tasks"][0]
    first.update(priority="urgent", priority_evidence="请加急提交报告", evidence="请加急提交报告")
    payload["tasks"].append(dict(first, title="提交PPT", evidence="明天提交PPT"))
    monkeypatch.setattr(detector.llm, "chat_json", lambda *args: payload)
    result = detector.detect(make_cmd(text=source, text_raw=source))
    assert [task.priority for task in result.tasks] == ["urgent", "normal"]
