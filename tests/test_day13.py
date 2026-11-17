"""Day 13 单测：状态变更的即时通知。

要钉住的行为只有四条：
1. 谁动了任务，该知道的人立刻能收到（提交验收 → 发起人）
2. 不该通知的不通知（开始处理不打扰任何人）
3. 同一次变更只发一次（回调重推、用户连点都不会重复轰炸）
4. 一个任务多个负责人时，人人有份，但发起人=负责人时不发两份
"""

import events
import repo
from models_db import TASK_IN_PROGRESS
from tests.helpers import make_task

CREATOR = "ou_sender"      # make_cmd 里的 sender_open_id
OWNER = "ou_zhangsan"      # 候选里的负责人
OWNER2 = "ou_lisi"


def _notifications():
    return repo.list_notifications(limit=50)


def test_submit_notifies_creator(task):
    """负责人提交验收 → 发起人必须立刻知道，否则任务就停在那儿了。"""
    repo.transition_task(task.uid, "start", operator_open_id=OWNER)
    repo.transition_task(task.uid, "submit", operator_open_id=OWNER)

    rows = [r for r in _notifications() if r.kind == "task_event"]
    assert len(rows) == 1
    assert rows[0].receive_id == CREATOR
    assert "提交验收" in rows[0].dedupe_key or "submit" in rows[0].dedupe_key


def test_start_is_silent(task):
    """开始处理不通知任何人 —— 他自己点的，通知他自己纯属噪音。

    这一条是产品判断不是技术判断：通知一多到需要忽略，就全都没用了。
    """
    repo.transition_task(task.uid, "start", operator_open_id=OWNER)
    assert _notifications() == []


def test_accept_notifies_owner(task):
    repo.transition_task(task.uid, "start", operator_open_id=OWNER)
    repo.transition_task(task.uid, "submit", operator_open_id=OWNER)
    repo.transition_task(task.uid, "accept", operator_open_id=CREATOR)

    targets = [r.receive_id for r in _notifications() if r.kind == "task_event"]
    assert targets == [CREATOR, OWNER]  # 先通知发起人验收，再通知负责人结果


def test_reject_notifies_owner_with_reason(task):
    repo.transition_task(task.uid, "start", operator_open_id=OWNER)
    repo.transition_task(task.uid, "submit", operator_open_id=OWNER)
    repo.transition_task(task.uid, "reject", operator_open_id=CREATOR,
                         note="交付物不完整")

    latest = [r for r in _notifications() if r.kind == "task_event"][-1]
    assert latest.receive_id == OWNER
    assert "打回" in latest.payload


def test_block_notifies_creator(task):
    repo.transition_task(task.uid, "start", operator_open_id=OWNER)
    repo.transition_task(task.uid, "block", operator_open_id=OWNER)

    rows = [r for r in _notifications() if r.kind == "task_event"]
    assert [r.receive_id for r in rows] == [CREATOR]


def test_cancel_notifies_owner(task):
    repo.transition_task(task.uid, "cancel", operator_open_id=CREATOR)

    rows = [r for r in _notifications() if r.kind == "task_event"]
    assert [r.receive_id for r in rows] == [OWNER]


def test_same_change_enqueued_once(task):
    """飞书重推、用户连点都不该让发起人被通知两次。"""
    current = repo.get_task(task.uid)
    result = repo.TransitionResult(True, task=current, message="")

    assert events.on_transition(result, "submit", TASK_IN_PROGRESS, OWNER) == 1
    assert events.on_transition(result, "submit", TASK_IN_PROGRESS, OWNER) == 0
    assert len(_notifications()) == 1


def test_each_owner_gets_own_notification(database):
    """两个负责人就要两通知：去重键里带 open_id，否则第二个会被唯一约束吞掉。"""
    task = make_task(owners=(OWNER, OWNER2))
    repo.transition_task(task.uid, "cancel", operator_open_id=CREATOR)

    rows = [r for r in _notifications() if r.kind == "task_event"]
    assert sorted(r.receive_id for r in rows) == sorted([OWNER, OWNER2])
    assert len({r.dedupe_key for r in rows}) == 2


def test_creator_is_also_owner_gets_one(database):
    """自己给自己派活、自己提交验收：不该收到「你提交了验收」这种通知。

    收件人等于操作者，说明这条消息的唯一作用是提醒他自己刚点过的按钮。
    """
    task = make_task(owners=(CREATOR,), creator=CREATOR)
    repo.transition_task(task.uid, "start", operator_open_id=CREATOR)
    repo.transition_task(task.uid, "submit", operator_open_id=CREATOR)

    assert [r for r in _notifications() if r.kind == "task_event"] == []


def test_self_action_does_not_notify_self(database):
    """打回也一样：自己打回自己的任务，不用再通知自己一遍。"""
    from sqlalchemy import update

    from models_db import Task

    task = make_task(owners=(OWNER,))
    # 把发起人改成和负责人同一个人（make_task 的 creator 只是操作者，不改落库的发起人）
    import db as _db
    with _db.session_scope() as s:
        s.execute(update(Task).where(Task.uid == task.uid)
                  .values(creator_open_id=OWNER)
                  .execution_options(synchronize_session=False))

    repo.transition_task(task.uid, "start", operator_open_id=OWNER)
    repo.transition_task(task.uid, "submit", operator_open_id=OWNER)
    assert [r for r in _notifications() if r.kind == "task_event"] == []

    repo.transition_task(task.uid, "reject", operator_open_id=OWNER)
    assert [r for r in _notifications() if r.kind == "task_event"] == []


def test_other_person_action_still_notifies(database):
    """别人动任务时照常通知 —— 上面的过滤不能把正常通知一起干掉。"""
    task = make_task(owners=(OWNER,), creator=CREATOR)
    repo.transition_task(task.uid, "start", operator_open_id=OWNER)
    repo.transition_task(task.uid, "submit", operator_open_id=OWNER)
    repo.transition_task(task.uid, "reject", operator_open_id=CREATOR)

    rows = [r for r in _notifications() if r.kind == "task_event"]
    assert [r.receive_id for r in rows] == [CREATOR, OWNER]


def test_card_has_operator_and_next_step(task):
    repo.transition_task(task.uid, "start", operator_open_id=OWNER)
    repo.transition_task(task.uid, "submit", operator_open_id=OWNER)

    payload = [r for r in _notifications() if r.kind == "task_event"][0].payload
    assert OWNER in payload          # 卡片里 @ 了操作人
    assert "等你验收" in payload      # 收件人知道下一步该干嘛
    assert '"act"' in payload        # 带按钮，点一下就能处理
    assert '"accept"' in payload     # 发起人看到的是验收按钮


def test_no_creator_does_not_crash(database):
    """任务没有发起人（脏数据）时不能抛异常，只是没人可通知。"""
    task = make_task(owners=(OWNER,))
    # 直接把 creator 置空，模拟历史数据
    import db as _db
    from sqlalchemy import update
    from models_db import Task

    with _db.session_scope() as s:
        s.execute(update(Task).where(Task.uid == task.uid).values(creator_open_id=None)
                  .execution_options(synchronize_session=False))

    result = repo.transition_task(task.uid, "start", operator_open_id=OWNER)
    assert result.ok
    assert repo.transition_task(task.uid, "submit", operator_open_id=OWNER).ok
    assert [r for r in _notifications() if r.kind == "task_event"] == []


def test_keys_are_unique_across_versions(task):
    """提交 → 打回 → 再提交，是三次不同的事件，不能被去重吃掉。"""
    repo.transition_task(task.uid, "start", operator_open_id=OWNER)
    repo.transition_task(task.uid, "submit", operator_open_id=OWNER)
    repo.transition_task(task.uid, "reject", operator_open_id=CREATOR)
    repo.transition_task(task.uid, "submit", operator_open_id=OWNER)

    submit_keys = [
        r.dedupe_key for r in _notifications()
        if r.kind == "task_event" and ":submit:" in r.dedupe_key
    ]
    assert len(submit_keys) == 2
    assert submit_keys[0] != submit_keys[1]
