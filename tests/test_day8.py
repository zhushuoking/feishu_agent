"""Day 8 单测：数据落地。

每个测试用独立的临时库文件，互不污染。
重点验证四类容易出事的地方：
1. 幂等（重复投递不会插重）
2. 时区往返不丢（SQLite 的经典坑）
3. 候选 → 任务的转换与状态日志
4. 外键真的生效（SQLite 默认是关的）
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

import db
import repo
from models import TZ_SHANGHAI, MessageCommand
from models_db import (
    CAND_CONFIRMED, CAND_EXPIRED, CAND_PENDING, CAND_REJECTED,
    TASK_CANCELLED, TASK_DONE, TASK_TODO, Task, TaskAssignee,
)
from schemas import TaskRecognition

SENT_AT = datetime(2026, 11, 16, 10, 0, tzinfo=TZ_SHANGHAI)
DEADLINE = datetime(2026, 11, 20, 18, 0, tzinfo=TZ_SHANGHAI)


@pytest.fixture()
def database(tmp_path):
    path = str(tmp_path / "test.db")
    db.configure(path)
    db.init_db()
    yield path
    db.get_engine().dispose()


def _cmd(**overrides) -> MessageCommand:
    data = dict(
        event_id="evt_001",
        message_id="om_001",
        chat_id="oc_001",
        chat_type="group",
        sender_open_id="ou_sender",
        sender_type="user",
        text_raw="@_user_1 本周五前提交整改报告",
        text="@张三 本周五前提交整改报告",
        sent_at=SENT_AT,
    )
    data.update(overrides)
    return MessageCommand(**data)


def _recognition(tasks=None, **overrides) -> TaskRecognition:
    payload = {
        "is_task_candidate": True,
        "candidate_type": "task",
        "confidence": 0.91,
        "tasks": tasks if tasks is not None else [
            {
                "title": "提交供应商整改报告",
                "deliverable": "供应商整改报告",
                "deadline_text": "本周五前",
                "assignee_mention_ids": ["ou_zhangsan"],
                "missing_fields": [],
                "evidence": "本周五前提交整改报告",
            }
        ],
        "clarification_needed": False,
        "clarification_question": None,
    }
    payload.update(overrides)
    result = TaskRecognition.model_validate(payload)
    # 模拟 detector 之后的回填
    for task in result.tasks:
        task.deadline_at = DEADLINE
        task.deadline_status = "resolved"
        task.deadline_rule = "本-week-4|default-end"
    return result


# ---------------------------------------------------------------- 落库

def test_save_creates_pending_candidates(database):
    created = repo.save_candidates(_recognition(), _cmd())
    assert len(created) == 1

    cand = repo.get_candidate(created[0].uid)
    assert cand is not None
    assert cand.status == CAND_PENDING
    assert cand.title == "提交供应商整改报告"
    assert cand.chat_id == "oc_001"
    assert cand.seq == 1
    assert cand.expires_at > db.now()


def test_multi_task_message_gets_sequence_numbers(database):
    result = _recognition(tasks=[
        {"title": "提交采购清单", "deliverable": "清单", "deadline_text": "下周二"},
        {"title": "提交试验计划", "deliverable": "计划", "deadline_text": "下周二"},
    ])
    created = repo.save_candidates(result, _cmd())
    assert [c.seq for c in created] == [1, 2]


def test_save_is_idempotent(database):
    """同一条消息重复识别，不能产生重复候选。"""
    cmd = _cmd()
    repo.save_candidates(_recognition(), cmd)
    repo.save_candidates(_recognition(), cmd)

    assert len(repo.list_candidates(status=CAND_PENDING)) == 1


def test_uid_is_not_sequential(database):
    created = repo.save_candidates(_recognition(tasks=[
        {"title": "任务A", "deadline_text": "明天"},
        {"title": "任务B", "deadline_text": "明天"},
    ]), _cmd())
    assert created[0].uid != created[1].uid
    assert all(len(c.uid) == 32 for c in created)


def test_no_tasks_saves_nothing(database):
    result = _recognition(is_task_candidate=False, candidate_type="unrelated", tasks=[])
    assert repo.save_candidates(result, _cmd()) == []


# ---------------------------------------------------------------- 时区与 JSON

def test_timezone_survives_roundtrip(database):
    """SQLite 存 DATETIME 会丢时区，这里必须保证读回来还是 +08:00。"""
    uid = repo.save_candidates(_recognition(), _cmd())[0].uid

    cand = repo.get_candidate(uid)
    assert cand.deadline_at.tzinfo is not None
    assert cand.deadline_at == DEADLINE
    assert cand.deadline_at.utcoffset() == timedelta(hours=8)
    assert cand.message_sent_at == SENT_AT


def test_json_fields_roundtrip(database):
    result = _recognition(tasks=[{
        "title": "提交整改报告",
        "assignee_mention_ids": ["ou_a", "ou_b"],
        "missing_fields": ["acceptance_criteria"],
        "deadline_text": "本周五前",
    }])
    uid = repo.save_candidates(result, _cmd())[0].uid

    cand = repo.get_candidate(uid)
    assert cand.assignee_open_ids == ["ou_a", "ou_b"]
    assert cand.missing_fields == ["acceptance_criteria"]


# ---------------------------------------------------------------- 过期

def test_expire_marks_overdue_candidates(database):
    # ttl 用负数：时间精度是「秒」，用 0 的话可能和 now() 落在同一秒，比较不过
    repo.save_candidates(_recognition(), _cmd(), ttl_hours=-2)
    assert repo.count_candidates_by_status().get(CAND_PENDING) == 1

    assert repo.expire_candidates() == 1
    assert repo.count_candidates_by_status().get(CAND_EXPIRED) == 1


def test_expire_skips_candidates_still_alive(database):
    repo.save_candidates(_recognition(), _cmd(), ttl_hours=72)
    assert repo.expire_candidates() == 0


# ---------------------------------------------------------------- 确认建单

def test_confirm_creates_task_with_log(database):
    cand_uid = repo.save_candidates(_recognition(), _cmd())[0].uid

    task = repo.confirm_candidate(cand_uid, operator_open_id="ou_manager")
    assert task is not None
    assert task.status == TASK_TODO
    assert task.version == 1
    assert task.owner_open_ids == ["ou_zhangsan"]
    assert task.deadline_at == DEADLINE

    with db.session_scope() as s:
        from models_db import TaskStatusLog
        from sqlalchemy import select
        logs = s.scalars(select(TaskStatusLog).where(TaskStatusLog.task_id == task.id)).all()
    assert len(logs) == 1
    assert logs[0].from_status is None
    assert logs[0].to_status == TASK_TODO
    assert logs[0].operator_open_id == "ou_manager"

    assert repo.get_candidate(cand_uid).status == CAND_CONFIRMED


def test_confirm_twice_returns_none(database):
    cand_uid = repo.save_candidates(_recognition(), _cmd())[0].uid
    assert repo.confirm_candidate(cand_uid) is not None
    assert repo.confirm_candidate(cand_uid) is None
    assert repo.count_tasks() == 1


def test_confirm_can_override_assignees(database):
    cand_uid = repo.save_candidates(_recognition(), _cmd())[0].uid
    task = repo.confirm_candidate(cand_uid, assignee_open_ids=["ou_lisi"])
    assert task.owner_open_ids == ["ou_lisi"]


def test_rejected_candidate_cannot_be_confirmed(database):
    cand_uid = repo.save_candidates(_recognition(), _cmd())[0].uid
    assert repo.reject_candidate(cand_uid, "ou_manager") is True
    assert repo.get_candidate(cand_uid).status == CAND_REJECTED
    assert repo.confirm_candidate(cand_uid) is None


# ---------------------------------------------------------------- 派生与约束

def test_is_overdue_is_derived(database):
    """逾期不落库，靠 deadline_at 和状态算出来。"""
    cand_uid = repo.save_candidates(_recognition(), _cmd())[0].uid
    task = repo.confirm_candidate(cand_uid)

    with db.session_scope() as s:
        row = s.get(Task, task.id)
        row.deadline_at = db.now() - timedelta(days=1)
    assert repo.get_task(task.uid).is_overdue is True

    with db.session_scope() as s:
        row = s.get(Task, task.id)
        row.status = TASK_DONE
    assert repo.get_task(task.uid).is_overdue is False


def test_no_deadline_is_never_overdue(database):
    result = _recognition()
    for task in result.tasks:
        task.deadline_at = None
        task.deadline_status = "missing"
    cand_uid = repo.save_candidates(result, _cmd())[0].uid
    task = repo.confirm_candidate(cand_uid)
    assert task.is_overdue is False


def test_foreign_key_is_enforced(database):
    """SQLite 默认不校验外键，db.py 里每条连接 PRAGMA 打开，这里验证真的开了。"""
    with pytest.raises(IntegrityError):
        with db.session_scope() as s:
            s.add(TaskAssignee(task_id=99999, open_id="ou_ghost"))


def test_cancelled_task_not_overdue(database):
    cand_uid = repo.save_candidates(_recognition(), _cmd())[0].uid
    task = repo.confirm_candidate(cand_uid)
    with db.session_scope() as s:
        row = s.get(Task, task.id)
        row.status = TASK_CANCELLED
        row.deadline_at = db.now() - timedelta(hours=1)
    assert repo.get_task(task.uid).is_overdue is False
