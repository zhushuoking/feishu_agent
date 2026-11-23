"""问句和计划不能把真实任务推进到待验收。"""

import pytest

import feedback
import repo
from models_db import TASK_IN_PROGRESS
from tests.helpers import make_cmd, make_task


@pytest.mark.parametrize("text", [
    "做完了吗。", "搞定了没有", "提交了没！", "是不是已经完成了",
    "是否已经提交了", "完成了？报告在哪", "如果完成了就发我",
    "等到做完了再通知我", "预计明天就完成了", "计划明天开始做",
])
def test_non_statement_does_not_change_task(database, text):
    task = make_task(status=TASK_IN_PROGRESS)
    candidate = repo.get_candidate(task.candidate_uid)
    cmd = make_cmd(parent_id=candidate.message_id,
                   sender_open_id=task.owner_open_ids[0], text=text, text_raw=text)
    assert feedback.classify(text) is None
    assert not feedback.apply_feedback(cmd).ok
    assert repo.get_task(task.uid).status == TASK_IN_PROGRESS


@pytest.mark.parametrize("text, action", [
    ("做完了，报告放在共享盘了", "submit"),
    ("这边卡住了，供应商还没回复", "block"),
    ("这个不用做了", "cancel"),
    ("开始做了", "start"),
    ("验收通过。", "accept"),
    ("验收不通过：缺少测试报告", "reject"),
])
def test_explicit_statement_still_works(text, action):
    reading = feedback.classify(text)
    assert reading and reading.action == action
