"""Day 11 单测：任务卡 + 卡片按钮驱动状态流转。

三条主线：
1. 按钮必须「按状态 × 按权限」长出来，不是写死的
2. 点按钮必须真改状态，并且还回来的卡片要是新状态新版本
3. 卡片上带的版本号过期了，不能拿旧快照覆盖别人的修改
"""

import cards
import card_action
import repo
from models_db import (
    TASK_DONE, TASK_IN_PROGRESS, TASK_PENDING_ACCEPTANCE, TASK_TODO,
)

CREATOR = "ou_sender"      # make_cmd 里的 sender_open_id
OWNER = "ou_zhangsan"      # 候选里的负责人
STRANGER = "ou_stranger"


def _buttons(card) -> list:
    """从卡片 JSON 里把所有按钮挖出来。卡片结构变了这里会第一时间炸。"""
    found = []
    for element in card["elements"]:
        if element.get("tag") == "action":
            found.extend(element["actions"])
    return found


def _actions(card) -> list:
    return [b["value"]["act"] for b in _buttons(card)]


def _text(card) -> str:
    """把卡片里所有文案拼起来，方便断言。"""
    chunks = []
    for element in card["elements"]:
        if "text" in element:
            chunks.append(element["text"].get("content", ""))
        for field in element.get("fields", []) or []:
            chunks.append(field["text"].get("content", ""))
        if element.get("tag") == "note":
            chunks.extend(e.get("content", "") for e in element.get("elements", []))
    return "\n".join(chunks)


# ---------------------------------------------------------------- 卡片结构

def test_todo_card_buttons_differ_by_role(task):
    """同一张待开始的任务卡，负责人和发起人看到的按钮不一样 ——
    这就是「按人渲染」的意义。"""
    owner_card = cards.build_task_card(task, OWNER)
    assert _actions(owner_card) == ["start"]
    assert owner_card["header"]["title"]["content"] == "任务·待开始"

    # 取消是发起人的权利，负责人看不到这个按钮（看到了点了也会被拒）
    creator_card = cards.build_task_card(task, CREATOR)
    assert _actions(creator_card) == ["cancel"]


def test_stranger_gets_no_buttons(task):
    """私聊卡：陌生人看到一张能读的卡，但没有按钮 —— 不是看不到卡片。"""
    card = cards.build_task_card(task, STRANGER)
    assert _buttons(card) == []
    assert "没有可执行的动作" in _text(card)


def test_group_card_shows_all_status_actions(task):
    """群卡：一张卡多人看，不能按某个人的权限裁剪。

    这条是真实事故换来的：待验收的群卡按负责人一个人的权限过滤，
    结果一个按钮都没有，发起人明明能验收却看到「你没有可执行的动作」，
    任务就这么卡住了。
    """
    card = cards.build_task_card(task, None)
    assert _actions(card) == ["start", "cancel"]
    assert all(b["value"].get("pub") == 1 for b in _buttons(card))
    assert "按你的身份校验" in _text(card)


def test_pending_acceptance_group_card_has_accept_button(task):
    """事故复现：待验收的群卡必须给发起人留「验收通过」。
    同一个状态下，负责人的私聊卡不该有验收按钮。"""
    repo.transition_task(task.uid, "start", operator_open_id=OWNER)
    repo.transition_task(task.uid, "submit", operator_open_id=OWNER)
    pending = repo.get_task(task.uid)

    group_card = cards.build_task_card(pending, None)
    assert "accept" in _actions(group_card)

    owner_card = cards.build_task_card(pending, OWNER)
    assert "accept" not in _actions(owner_card)


def test_group_card_click_keeps_public_view(task):
    """群里点完之后回来还是公共卡，不能变成点击者的私人视角。"""
    card = cards.build_task_card(task, None)
    start = next(b for b in _buttons(card) if b["value"]["act"] == "start")
    value = start["value"]

    outcome = card_action.process(value["act"], operator_open_id=OWNER,
                                  tid=value["tid"], ver=value["ver"],
                                  pub=bool(value.get("pub")))
    assert outcome.ok
    assert all(b["value"].get("pub") == 1 for b in _buttons(outcome.card))


def test_group_card_rejects_stranger_but_stays_public(task):
    """群里陌生人点了会被拒，但卡片不会因此被改成他的私人视角。"""
    card = cards.build_task_card(task, None)
    start = next(b for b in _buttons(card) if b["value"]["act"] == "start")
    value = start["value"]

    outcome = card_action.process(value["act"], operator_open_id=STRANGER,
                                  tid=value["tid"], ver=value["ver"], pub=True)
    assert not outcome.ok
    assert all(b["value"].get("pub") == 1 for b in _buttons(outcome.card))
    assert repo.get_task(task.uid).status == TASK_TODO


def test_buttons_carry_tid_and_version(task):
    card = cards.build_task_card(task, OWNER)
    for button in _buttons(card):
        assert button["value"]["tid"] == task.uid
        assert button["value"]["ver"] == task.version


def test_done_card_has_no_buttons_and_says_finished(task):
    repo.transition_task(task.uid, "start", operator_open_id=OWNER)
    repo.transition_task(task.uid, "submit", operator_open_id=OWNER)
    repo.transition_task(task.uid, "accept", operator_open_id=CREATOR)

    done = repo.get_task(task.uid)
    card = cards.build_task_card(done, OWNER)
    assert _buttons(card) == []
    assert "已结束" in _text(card)
    assert done.status == TASK_DONE


def test_pending_acceptance_shows_creator_actions_only(task):
    repo.transition_task(task.uid, "start", operator_open_id=OWNER)
    repo.transition_task(task.uid, "submit", operator_open_id=OWNER)
    pending = repo.get_task(task.uid)

    # 发起人能验收，但不能再帮负责人干活
    creator_card = cards.build_task_card(pending, CREATOR)
    assert set(_actions(creator_card)) == {"accept", "reject", "cancel"}

    # 负责人只能打回/取消，不能自己验收
    owner_card = cards.build_task_card(pending, OWNER)
    assert "accept" not in _actions(owner_card)


def test_overdue_marker(task):
    repo.transition_task(task.uid, "start", operator_open_id=OWNER)
    card = cards.build_task_card(repo.get_task(task.uid), OWNER)
    assert "进行中" in _text(card)
    assert "截止" in _text(card)


# ---------------------------------------------------------------- 点按钮

def test_click_start_transitions_and_returns_new_card(task):
    card = cards.build_task_card(task, OWNER)
    start = next(b for b in _buttons(card) if b["value"]["act"] == "start")
    value = start["value"]

    outcome = card_action.process(
        value["act"], operator_open_id=OWNER,
        tid=value["tid"], ver=value["ver"],
    )
    assert outcome.ok
    assert outcome.toast_type == "success"
    assert "进行中" in outcome.toast_content

    fresh = repo.get_task(task.uid)
    assert fresh.status == TASK_IN_PROGRESS
    assert fresh.version == 2

    # 还回来的卡片必须是新状态、新版本、新按钮 —— 否则用户会反复点旧按钮
    assert _actions(outcome.card) == ["block", "submit"]
    assert all(b["value"]["ver"] == 2 for b in _buttons(outcome.card))
    assert "进行中" in _text(outcome.card)


def test_full_flow_by_clicking(task):
    """一路点到底：开始 → 提交 → 验收。全程只通过卡片 value 驱动。"""
    def click(action, viewer):
        card = cards.build_task_card(repo.get_task(task.uid), viewer)
        button = next(b for b in _buttons(card) if b["value"]["act"] == action)
        v = button["value"]
        return card_action.process(v["act"], operator_open_id=viewer,
                                   tid=v["tid"], ver=v["ver"])

    assert click("start", OWNER).ok
    assert click("submit", OWNER).ok
    assert repo.get_task(task.uid).status == TASK_PENDING_ACCEPTANCE

    done = click("accept", CREATOR)
    assert done.ok
    assert repo.get_task(task.uid).status == TASK_DONE
    assert done.toast_type == "success"


def test_stale_version_is_rejected(task):
    """两个人都拿着 v1 的卡片：第一个点成功，第二个必须被拦下。"""
    first = card_action.process("start", operator_open_id=OWNER,
                                tid=task.uid, ver=1)
    assert first.ok

    second = card_action.process("block", operator_open_id=OWNER,
                                 tid=task.uid, ver=1)
    assert not second.ok
    assert second.toast_type == "warning"
    assert "刷新" in second.toast_content or "更新" in second.toast_content

    # 状态只被改了一次，第二个请求没有生效
    fresh = repo.get_task(task.uid)
    assert fresh.status == TASK_IN_PROGRESS
    assert fresh.version == 2


def test_permission_denied_keeps_card_usable(task):
    """陌生人点不了，但卡片还是能看、状态没变。"""
    outcome = card_action.process("start", operator_open_id=STRANGER,
                                  tid=task.uid, ver=1)
    assert not outcome.ok
    assert outcome.toast_type == "error"
    assert "负责人" in outcome.toast_content or "发起人" in outcome.toast_content
    assert repo.get_task(task.uid).status == TASK_TODO
    assert outcome.card["header"]["title"]["content"] == "任务·待开始"


def test_unknown_task_returns_stale_card():
    outcome = card_action.process("start", operator_open_id=OWNER,
                                  tid="nope" * 8, ver=1)
    assert not outcome.ok
    assert "找不到" in outcome.toast_content
    assert outcome.card["header"]["title"]["content"] == "操作未生效"


def test_unknown_action_on_task(task):
    outcome = card_action.process("explode", operator_open_id=OWNER,
                                  tid=task.uid, ver=1)
    assert not outcome.ok
    assert "未知动作" in outcome.toast_content
    assert repo.get_task(task.uid).status == TASK_TODO


def test_click_without_version_still_works(task):
    """老卡片/手工调用没带 ver 时不做乐观锁校验，但状态机照旧把关。"""
    outcome = card_action.process("start", operator_open_id=OWNER, tid=task.uid)
    assert outcome.ok
    assert repo.get_task(task.uid).version == 2


def test_candidate_and_task_buttons_do_not_collide(task):
    """两种卡片共用一个 act 命名空间，确认/忽略不能被当成任务动作。"""
    cand_card = cards.build_candidate_card(
        repo.list_candidates(status=None, limit=1)[0]
    )
    cand_actions = set(_actions(cand_card))
    assert cand_actions == {"confirm", "reject"}
    assert not cand_actions & {"start", "submit", "accept"}


def test_task_card_json_is_serialisable(task):
    content = cards.card_to_content(cards.build_task_card(task, OWNER))
    assert '"tid"' in content
    assert "\\u" not in content  # 中文没被转义成 unicode 码点
