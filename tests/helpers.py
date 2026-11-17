"""构造测试数据的小工具（纯函数，不是 fixture）。"""

import itertools
from datetime import datetime

import db
import repo
from models import TZ_SHANGHAI, MessageCommand
from schemas import TaskRecognition

SENT_AT = datetime(2026, 11, 16, 10, 0, tzinfo=TZ_SHANGHAI)
DEADLINE = datetime(2026, 11, 20, 18, 0, tzinfo=TZ_SHANGHAI)

_seq = itertools.count(1)


def make_cmd(**overrides) -> MessageCommand:
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


def make_recognition(tasks=None, deadline=DEADLINE, **overrides) -> TaskRecognition:
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
    # 模拟 detector 里的 deadline.apply_to_tasks 回填
    for task in result.tasks:
        task.deadline_at = deadline
        task.deadline_status = "resolved"
        task.deadline_rule = "本-week-4|default-end"
    return result


def save_one(**cmd_overrides):
    """落一条候选并返回它。"""
    return repo.save_candidates(make_recognition(), make_cmd(**cmd_overrides))[0]


def make_task(deadline_at=None, owners=("ou_zhangsan",), creator="ou_sender",
              title="提交报告", status=None):
    """直接造一条正式任务（建单流程走的是真代码，不是手写 INSERT）。

    每次调用换一个 message_id：候选表有 (message_id, seq) 唯一约束，
    用同一个 message_id 造第二条会被幂等逻辑挡掉，测试就会莫名其妙少一条数据。
    """
    n = next(_seq)
    rec = make_recognition(
        tasks=[{
            "title": title,
            "deliverable": "交付物",
            "deadline_text": "明天",
            "assignee_mention_ids": list(owners),
            "missing_fields": [],
        }],
        deadline=deadline_at or DEADLINE,
    )
    cand = repo.save_candidates(rec, make_cmd(message_id=f"om_test_{n}"))[0]
    task = repo.confirm_candidate(cand.uid, operator_open_id=creator)

    if status and status != task.status:
        # 测试数据准备：跳过状态机直接落状态（状态机本身在 Day 10 单测里验）
        from sqlalchemy import update

        from models_db import Task

        with db.session_scope() as s:
            s.execute(
                update(Task).where(Task.uid == task.uid).values(status=status)
                .execution_options(synchronize_session=False)
            )
        task = repo.get_task(task.uid)
    return task
