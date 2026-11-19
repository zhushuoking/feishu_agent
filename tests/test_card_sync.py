"""Day 14 单测：旧卡片同步。

要钉住的四件事：
1. 发出去的卡片能被登记（没 message_id 的不记，重复登记不报错）
2. 状态一变，落后的卡都刷成最新，已经最新的不白刷
3. 私聊卡按原来那个人的视角重渲染，群卡保持公共视角（不能串）
4. 刷不动（14 天过期、被撤回）不重试到天长地久，攒够三次就退休
"""

import json

import card_sync
import cards
import repo
import state_machine
from models_db import TASK_IN_PROGRESS
from tests.helpers import make_task

CREATOR = "ou_sender"
OWNER = "ou_zhangsan"
OTHER = "ou_lisi"


class FakeUpdater:
    """假的上卡片接口。记下每次改了谁、改成了什么，并按需失败。"""

    def __init__(self, fail_ids=()):
        self.calls = []
        self.fail_ids = set(fail_ids)

    def __call__(self, message_id, card):
        self.calls.append((message_id, card))
        return message_id not in self.fail_ids

    @property
    def ids(self):
        return [mid for mid, _ in self.calls]


def _register(task, message_id, viewer="", kind="task", version=None):
    return repo.register_task_card(
        task.uid, message_id, viewer or task.chat_id,
        "open_id" if viewer else "chat_id",
        viewer_open_id=viewer, kind=kind,
        version=task.version if version is None else version,
    )


def _titles(card):
    return [e["text"]["content"] for e in card["elements"] if e.get("text")]


# ------------------------------------------------------------------ 登记

def test_card_without_message_id_is_not_registered(database):
    task = make_task(owners=(OWNER,))
    assert _register(task, "") is False
    assert repo.list_live_task_cards(task.uid) == []


def test_registering_same_message_twice_is_idempotent(database):
    """重试发送会把同一张卡登记两遍，(message_id) 唯一约束要挡住。"""
    task = make_task(owners=(OWNER,))
    assert _register(task, "om_a", OWNER) is True
    assert _register(task, "om_a", OWNER) is False
    assert len(repo.list_live_task_cards(task.uid)) == 1


def test_cards_of_unknown_task_are_ignored(database):
    assert repo.register_task_card("nope", "om_x", OWNER) is False


# ------------------------------------------------------------------ 同步

def test_stale_cards_are_refreshed_to_latest_state(database):
    """核心场景：报阻塞的卡，解除阻塞之后必须变成「进行中」。"""
    task = make_task(owners=(OWNER,), status=TASK_IN_PROGRESS)

    # 阻塞时发过的两张卡：一张给负责人，一张群里的公共卡
    _register(task, "om_dm_owner", OWNER, kind="event", version=0)
    _register(task, "om_group", "", kind="event", version=0)

    repo.transition_task(task.uid, "block", operator_open_id=OWNER)
    repo.transition_task(task.uid, "unblock", operator_open_id=OWNER)

    updater = FakeUpdater()
    stats = card_sync.sync_task(task.uid, updater=updater)

    assert stats["updated"] == 2
    assert set(updater.ids) == {"om_dm_owner", "om_group"}
    label = state_machine.STATUS_LABELS[TASK_IN_PROGRESS]
    for _mid, card in updater.calls:
        # 卡头写的是新状态，不是那张红着的「阻塞」
        assert card["header"]["title"]["content"] == f"任务·{label}"
        assert card["header"]["template"] != "red"


def test_up_to_date_card_is_skipped(database):
    """刚发出去那张新卡已经是最新的，不该再打一次接口。"""
    task = make_task(owners=(OWNER,), status=TASK_IN_PROGRESS)
    _register(task, "om_fresh", OWNER, version=task.version)

    updater = FakeUpdater()
    stats = card_sync.sync_task(task.uid, updater=updater)

    assert stats["skipped"] == 1
    assert updater.calls == []


def test_private_card_keeps_its_own_viewer(database):
    """给负责人私聊的卡，刷新后按钮还是按他的权限裁的 —— 不能变成公共卡。"""
    task = make_task(owners=(OWNER,), status=TASK_IN_PROGRESS)
    _register(task, "om_dm", OWNER, version=0)

    updater = FakeUpdater()
    card_sync.sync_task(task.uid, updater=updater)

    _mid, card = updater.calls[0]
    buttons = [b for e in card["elements"] if e.get("tag") == "action"
               for b in e["actions"]]
    assert buttons, "负责人应该有可点的按钮"
    # 私聊卡的 value 里没有 pub 标记；群卡有
    assert all("pub" not in b["value"] for b in buttons)
    # 而且不该出现「按你的身份校验」这句群卡专属说明
    assert not any("按你的身份校验" in t for t in _titles(card))


def test_group_card_stays_public_after_refresh(database):
    """群卡刷新后必须还是公共视角：群里所有人看同一份。"""
    task = make_task(owners=(OWNER,), status=TASK_IN_PROGRESS)
    _register(task, "om_group", "", version=0)

    updater = FakeUpdater()
    card_sync.sync_task(task.uid, updater=updater)

    _mid, card = updater.calls[0]
    buttons = [b for e in card["elements"] if e.get("tag") == "action"
               for b in e["actions"]]
    assert buttons
    assert all(b["value"].get("pub") == 1 for b in buttons)


def test_no_card_means_no_request(database):
    task = make_task(owners=(OWNER,))
    updater = FakeUpdater()
    stats = card_sync.sync_task(task.uid, updater=updater)

    assert stats["checked"] == 0
    assert updater.calls == []


def test_unknown_task_does_not_crash(database):
    assert card_sync.sync_task("nope", updater=FakeUpdater())["checked"] == 0


# ------------------------------------------------------------------ 失败与退休

def test_failed_update_is_retried_next_time(database):
    """第一次刷失败不算完，下一轮还要试 —— 限流是会好的。"""
    task = make_task(owners=(OWNER,), status=TASK_IN_PROGRESS)
    _register(task, "om_flaky", OWNER, version=0)

    stats = card_sync.sync_task(task.uid, updater=FakeUpdater(fail_ids={"om_flaky"}))
    assert stats["failed"] == 1
    assert len(repo.list_live_task_cards(task.uid)) == 1

    stats = card_sync.sync_task(task.uid, updater=FakeUpdater())
    assert stats["updated"] == 1
    assert repo.list_live_task_cards(task.uid)[0].synced_version == task.version


def test_card_retires_after_three_failures(database):
    """超过 14 天的消息飞书硬性不让改，攒够次数就别再烦接口了。"""
    task = make_task(owners=(OWNER,), status=TASK_IN_PROGRESS)
    _register(task, "om_dead", OWNER, version=0)

    updater = FakeUpdater(fail_ids={"om_dead"})
    for _ in range(3):
        card_sync.sync_task(task.uid, updater=updater)

    assert repo.list_live_task_cards(task.uid) == []
    assert repo.count_task_cards() == {"retired": 1}


def test_synced_card_records_the_version(database):
    task = make_task(owners=(OWNER,), status=TASK_IN_PROGRESS)
    _register(task, "om_v1", OWNER, version=0)

    card_sync.sync_task(task.uid, updater=FakeUpdater())

    row = repo.list_live_task_cards(task.uid)[0]
    assert row.synced_version == task.version
    assert row.fail_count == 0


# ------------------------------------------------------------------ 串起来

def test_transition_registers_nothing_but_leaves_cards_syncable(database, monkeypatch):
    """状态流转本身不碰卡片；但发件箱里那条通知带着任务和版本，
    Worker 发出去之后才能登记。这条链路断了就永远同步不到。
    """
    monkeypatch.setenv("CARD_SYNC_ENABLED", "0")
    task = make_task(owners=(OWNER,), status=TASK_IN_PROGRESS)

    repo.transition_task(task.uid, "start", operator_open_id=OWNER)

    rows = [r for r in repo.list_notifications(limit=10) if r.kind == "task_event"]
    assert rows == []          # start 不发通知（自己点的，通知自己没意义）

    repo.transition_task(task.uid, "block", operator_open_id=OWNER)
    rows = [r for r in repo.list_notifications(limit=10) if r.kind == "task_event"]
    assert [r.receive_id for r in rows] == [CREATOR]
    assert rows[0].task_uid == task.uid
    assert rows[0].task_version == repo.get_task(task.uid).version


def test_card_payload_keeps_update_multi(database):
    """飞书要求 PATCH 前后卡片都声明 update_multi，少一个就改不动。
    这条断言看着鸡肋，删了它以后有人给卡片换布局就会踩。
    """
    task = make_task(owners=(OWNER,), status=TASK_IN_PROGRESS)
    for viewer in (OWNER, None):
        card = cards.build_task_card(task, viewer)
        assert card["config"]["update_multi"] is True
        # 顺手确认能被序列化成合法 JSON（飞书要求 content 是 JSON 字符串）
        assert json.loads(json.dumps(card, ensure_ascii=False))
