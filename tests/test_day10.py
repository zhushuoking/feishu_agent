"""Day 10 单测：状态机 + 权限 + 乐观锁。

三条主线：
1. 不该跳的状态跳不过去（TODO 不能直接 DONE）
2. 不该做的动作做不了（负责人不能自己验收自己的活）
3. 两个人同时改，只有一个成功（靠 version 乐观锁）
"""

import db
import permissions
import repo
import state_machine
from models_db import (
    TASK_BLOCKED, TASK_CANCELLED, TASK_DONE, TASK_IN_PROGRESS,
    TASK_PENDING_ACCEPTANCE, TASK_TODO,
)
from tests.helpers import make_cmd, make_recognition

CREATOR = "ou_sender"      # make_cmd 里的 sender_open_id
OWNER = "ou_zhangsan"      # 候选里的负责人
STRANGER = "ou_stranger"

# task fixture 在 tests/conftest.py 里，Day 11 的卡片用例共用同一条任务


def _go(task, *actions, operator=OWNER):
    """连着走几步，每步都断言成功，返回最后一步的结果。"""
    result = None
    for action in actions:
        result = repo.transition_task(task.uid, action, operator_open_id=operator)
        assert result.ok, f"{action} 应该成功，实际：{result.message}"
    return result


# ---------------------------------------------------------------- 状态机

def test_owner_can_start(task):
    result = repo.transition_task(task.uid, "start", operator_open_id=OWNER)
    assert result.ok
    assert result.task.status == TASK_IN_PROGRESS
    assert result.task.version == 2


def test_cannot_skip_to_done(task):
    """TODO 直接 DONE = 没人开工、没验收，状态机必须挡住。"""
    result = repo.transition_task(task.uid, "accept", operator_open_id=CREATOR)
    assert not result.ok
    assert "不能执行" in result.message
    assert "待验收" in result.message  # 顺手提示这个动作该在什么时候用
    assert repo.get_task(task.uid).status == TASK_TODO


def test_terminal_task_is_frozen(task):
    _go(task, "start", "submit", operator=OWNER)
    repo.transition_task(task.uid, "accept", operator_open_id=CREATOR)

    again = repo.transition_task(task.uid, "start", operator_open_id=OWNER)
    assert not again.ok
    assert "终态" in again.message


def test_block_and_unblock(task):
    _go(task, "start")
    blocked = repo.transition_task(task.uid, "block", operator_open_id=OWNER)
    assert blocked.task.status == TASK_BLOCKED

    unblocked = repo.transition_task(task.uid, "unblock", operator_open_id=OWNER)
    assert unblocked.task.status == TASK_IN_PROGRESS


def test_reject_sends_task_back(task):
    _go(task, "start", "submit")
    rejected = repo.transition_task(task.uid, "reject", operator_open_id=CREATOR,
                                    note="交付物不完整")
    assert rejected.task.status == TASK_IN_PROGRESS
    assert rejected.task.version == 4


def test_completed_at_only_set_on_done(task):
    _go(task, "start", "submit")
    assert repo.get_task(task.uid).completed_at is None

    done = repo.transition_task(task.uid, "accept", operator_open_id=CREATOR)
    assert done.task.completed_at is not None


def test_next_actions_order(task):
    assert state_machine.next_actions(TASK_TODO) == ["start", "block", "submit", "cancel"]
    assert state_machine.next_actions(TASK_PENDING_ACCEPTANCE) == ["accept", "reject", "cancel"]
    assert state_machine.next_actions(TASK_DONE) == []


# ---------------------------------------------------------------- 权限

def test_owner_cannot_accept_own_work(task):
    """自己干完自己验收 = 没有验收。这条是权限矩阵的核心意义。"""
    _go(task, "start", "submit")
    result = repo.transition_task(task.uid, "accept", operator_open_id=OWNER)

    assert not result.ok
    assert "发起人" in result.message
    assert repo.get_task(task.uid).status == TASK_PENDING_ACCEPTANCE


def test_stranger_cannot_do_anything(task):
    result = repo.transition_task(task.uid, "start", operator_open_id=STRANGER)
    assert not result.ok
    assert "无权" in result.message


def test_creator_cannot_start_someone_elses_task(task):
    """发起人只管验收和取消，不能替负责人干活（否则进度是假的）。"""
    result = repo.transition_task(task.uid, "start", operator_open_id=CREATOR)
    assert not result.ok
    assert "负责人" in result.message


def test_only_creator_can_cancel(task):
    denied = repo.transition_task(task.uid, "cancel", operator_open_id=OWNER)
    assert not denied.ok

    ok = repo.transition_task(task.uid, "cancel", operator_open_id=CREATOR, note="测试取消")
    assert ok.ok
    assert ok.task.status == TASK_CANCELLED


def test_technical_admin_cannot_do_business_actions(task, monkeypatch):
    monkeypatch.setenv("AGENT_ADMIN_OPEN_IDS", f"{STRANGER},ou_another")
    result = repo.transition_task(task.uid, "start", operator_open_id=STRANGER)
    assert not result.ok


def test_empty_operator_is_rejected(task):
    result = repo.transition_task(task.uid, "start", operator_open_id="")
    assert not result.ok


def test_legacy_task_without_owner_requires_assignment(task):
    """旧任务缺少负责人时无法代行负责人操作。"""
    from models_db import Task

    with db.session_scope() as s:
        legacy_task = s.get(Task, task.id)
        legacy_task.assignees.clear()
    task = repo.get_task(task.uid)

    assert task.owner_open_ids == []
    started = repo.transition_task(task.uid, "start", operator_open_id=CREATOR)
    assert not started.ok
    assert not permissions.can(repo.get_task(task.uid), CREATOR, "block")


def test_can_reports_role(task):
    assert "负责人" in permissions.describe_roles(task, OWNER)
    assert "发起人" in permissions.describe_roles(task, CREATOR)
    assert permissions.describe_roles(task, STRANGER) == "与该任务无关"


def test_owner_cannot_self_accept_even_when_creator(database):
    """负责人兼任发起人时仍由其他验收人处理。"""
    result = make_recognition(tasks=[{
        "title": "整理周报", "deliverable": "周报",
        "deadline_text": "明天", "assignee_mention_ids": [CREATOR], "missing_fields": [],
    }])
    cand = repo.save_candidates(result, make_cmd())[0]
    task = repo.confirm_candidate(cand.uid, operator_open_id=CREATOR)

    assert task.creator_open_id == CREATOR
    assert task.owner_open_ids == [CREATOR]

    _go(task, "start", "submit", operator=CREATOR)
    done = repo.transition_task(task.uid, "accept", operator_open_id=CREATOR)
    assert not done.ok
    assert "不能自行验收" in done.message
    assert repo.get_task(task.uid).status == TASK_PENDING_ACCEPTANCE


# ---------------------------------------------------------------- 乐观锁

def test_version_conflict_is_detected(task):
    """卡片上拿到的是 v1，点下去的时候别人已经改到 v2 了 —— 必须拒绝。"""
    current = repo.get_task(task.uid)
    assert current.version == 1

    first = repo.transition_task(task.uid, "start", operator_open_id=OWNER,
                                expected_version=1)
    assert first.ok

    stale = repo.transition_task(task.uid, "block", operator_open_id=OWNER,
                                expected_version=1)  # 还拿着旧版本
    assert not stale.ok
    assert stale.conflict is True
    assert "刷新" in stale.message


def test_version_increments_each_time(task):
    first = repo.transition_task(task.uid, "start", operator_open_id=OWNER)
    assert first.task.version == 2

    _go(task, "block", "unblock")
    assert repo.get_task(task.uid).version == 4


# ---------------------------------------------------------------- 流水与边界

def test_every_transition_writes_log(task):
    _go(task, "start", "block", "unblock", "submit")
    repo.transition_task(task.uid, "accept", operator_open_id=CREATOR)

    logs = repo.list_task_logs(task.uid)
    # 建单那一条 + 5 次流转
    assert len(logs) == 6
    assert [log.to_status for log in logs] == [
        TASK_TODO, TASK_IN_PROGRESS, TASK_BLOCKED,
        TASK_IN_PROGRESS, TASK_PENDING_ACCEPTANCE, TASK_DONE,
    ]
    assert logs[-1].operator_open_id == CREATOR


def test_unknown_action(task):
    result = repo.transition_task(task.uid, "explode", operator_open_id=OWNER)
    assert not result.ok
    assert "未知动作" in result.message


def test_unknown_task(database):
    result = repo.transition_task("nope" * 8, "start", operator_open_id=OWNER)
    assert not result.ok
    assert "不存在" in result.message


def test_resolve_action_reports_reason(task):
    to_status, check = state_machine.resolve_action(TASK_TODO, "unblock")
    assert to_status is None
    assert not check.ok
    assert "不能执行" in check.reason
