"""Day 13 下半单测：从群里一句话读出状态反馈。

钉住四件事：
1. 一句话能被正确读成动作（做完了 / 卡住了 / 不做了）
2. 反话不能读错（还没做完、做完了吗 都不是「完成」）
3. 只有回复原消息才认，凭空一句「做完了」不改任何东西
4. 状态机不放过、权限不够、一句话多条任务 —— 都要给出人能看懂的回话
"""

import feedback
import repo
from models_db import TASK_DONE, TASK_IN_PROGRESS, TASK_TODO
from tests.helpers import make_cmd, make_task

CREATOR = "ou_sender"     # make_task 落库的发起人就是派活那条消息的发送者
OWNER = "ou_zhangsan"
STRANGER = "ou_stranger"


def _reply_to(task, text, sender=OWNER):
    """造一条「回复派活消息」的反馈。"""
    cand = repo.get_candidate(task.candidate_uid)
    return make_cmd(
        message_id="om_reply_%s" % task.uid[:6],
        parent_id=cand.message_id,
        sender_open_id=sender,
        text=text,
        text_raw=text,
    )


# ------------------------------------------------------------------ 读句子

def test_done_reads_as_submit():
    reading = feedback.classify("做完了，报告放在共享盘了")
    assert reading and reading.action == "submit"


def test_blocked_reads_as_block():
    reading = feedback.classify("这边卡住了，供应商还没回复")
    assert reading and reading.action == "block"


def test_cancel_reads_as_cancel():
    reading = feedback.classify("这个不用做了")
    assert reading and reading.action == "cancel"


def test_negation_is_not_done():
    """「还没做完」不是「做完了」。读反了会把任务推进到待验收。"""
    assert feedback.classify("还没做完") is None
    assert feedback.classify("没有完成") is None
    assert feedback.classify("未提交") is None


def test_question_is_not_statement():
    assert feedback.classify("做完了吗") is None
    assert feedback.classify("这个完成了吗？") is None


def test_chitchat_is_nothing():
    assert feedback.classify("好的收到") is None
    assert feedback.classify("") is None


# ------------------------------------------------------------------ 落到任务

def test_owner_reply_completes_task(database):
    task = make_task(owners=(OWNER,), status=TASK_IN_PROGRESS)
    applied = feedback.apply_feedback(_reply_to(task, "做完了，报告在共享盘"))

    assert applied.ok
    assert applied.text
    assert repo.get_task(task.uid).status == "PENDING_ACCEPTANCE"


def test_reply_text_tells_what_happened(database):
    task = make_task(owners=(OWNER,), status=TASK_IN_PROGRESS)
    applied = feedback.apply_feedback(_reply_to(task, "搞定了"))

    assert "待验收" in applied.text
    assert "提交报告" in applied.text


def test_without_reply_changes_nothing(database):
    """群里凭空一句「做完了」不认 —— 同时有五件事在跑，猜错就改错。"""
    task = make_task(owners=(OWNER,), status=TASK_IN_PROGRESS)
    cmd = make_cmd(sender_open_id=OWNER, text="做完了", text_raw="做完了")

    applied = feedback.apply_feedback(cmd)
    assert not applied.ok
    assert applied.text == ""
    assert repo.get_task(task.uid).status == TASK_IN_PROGRESS


def test_reply_to_other_message_changes_nothing(database):
    task = make_task(owners=(OWNER,), status=TASK_IN_PROGRESS)
    cmd = make_cmd(message_id="om_x", parent_id="om_not_a_task_msg",
                   sender_open_id=OWNER, text="做完了", text_raw="做完了")

    applied = feedback.apply_feedback(cmd)
    assert not applied.ok
    assert applied.text == ""
    assert repo.get_task(task.uid).status == TASK_IN_PROGRESS


def test_stranger_cannot_change_status(database):
    """权限不够要给回话，不能静默失败 —— 否则用户以为系统没看见。"""
    task = make_task(owners=(OWNER,), status=TASK_IN_PROGRESS)
    applied = feedback.apply_feedback(_reply_to(task, "做完了", sender=STRANGER))

    assert not applied.ok
    assert "权限" in applied.text
    assert repo.get_task(task.uid).status == TASK_IN_PROGRESS


def test_todo_can_submit_completion_per_design(database):
    """设计 §6.3 允许待开始任务直接提交验收。"""
    task = make_task(owners=(OWNER,), status=TASK_TODO)
    applied = feedback.apply_feedback(_reply_to(task, "做完了"))

    assert applied.ok
    assert applied.text
    assert repo.get_task(task.uid).status == "PENDING_ACCEPTANCE"


def test_finished_task_is_not_touched(database):
    task = make_task(owners=(OWNER,), status=TASK_DONE)
    applied = feedback.apply_feedback(_reply_to(task, "做完了"))

    assert not applied.ok
    assert "已经结束" in applied.text
    assert repo.get_task(task.uid).status == TASK_DONE


def test_two_tasks_in_one_message_is_ambiguous(database):
    """一条消息派了两件事，回复一句「做完了」—— 不猜，让人去点卡片。"""
    task = make_task(owners=(OWNER,), status=TASK_IN_PROGRESS)
    second = make_task(owners=(OWNER,), status=TASK_IN_PROGRESS)
    # 把第二条的候选改成指向同一条派活消息
    import db as _db
    from sqlalchemy import update
    from models_db import TaskCandidate

    first_cand = repo.get_candidate(task.candidate_uid)
    with _db.session_scope() as s:
        s.execute(update(TaskCandidate).where(TaskCandidate.uid == second.candidate_uid)
                  # seq 也要错开：(message_id, seq) 是唯一约束，两条都写 seq=1 会撞
                  .values(message_id=first_cand.message_id, seq=2)
                  .execution_options(synchronize_session=False))

    cmd = make_cmd(message_id="om_r2", parent_id=first_cand.message_id,
                   sender_open_id=OWNER, text="做完了", text_raw="做完了")
    applied = feedback.apply_feedback(cmd)

    assert not applied.ok
    assert "别让我猜" in applied.text
    assert repo.get_task(task.uid).status == TASK_IN_PROGRESS
    assert repo.get_task(second.uid).status == TASK_IN_PROGRESS


def test_feedback_enqueues_notification(database):
    """反馈改完状态也要走通知：不然发起人还是不知道可以验收了。"""
    task = make_task(owners=(OWNER,), status=TASK_IN_PROGRESS)
    feedback.apply_feedback(_reply_to(task, "做完了"))

    rows = [r for r in repo.list_notifications(limit=20) if r.kind == "task_event"]
    assert [r.receive_id for r in rows] == [CREATOR]
