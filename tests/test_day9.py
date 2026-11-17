"""Day 9 单测：确认卡片 + 卡片回调。

不连飞书、不发网络请求。验证三件事：
1. 卡片 JSON 结构对不对（少一个 tag 飞书就报错，但报错信息很难懂）
2. 按钮 value 里带的东西够不够回调把单建出来
3. 重复点击 / 已过期 / 找不到候选，这些异常分支会不会建出第二张单
"""

import json

import pytest

import cards
import card_action
import repo
from models_db import CAND_REJECTED, TASK_TODO
from tests.helpers import make_cmd, make_recognition, save_one


def _buttons(card):
    """把卡片里所有按钮抠出来，方便断言。"""
    found = []
    for element in card.get("elements", []):
        if element.get("tag") == "action":
            found.extend(element.get("actions", []))
    return found


class _Obj:
    """把 dict 包成对象，模拟 lark SDK 的事件对象。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


# ---------------------------------------------------------------- 卡片结构

def test_candidate_card_has_required_structure(database):
    cand = save_one()
    card = cards.build_candidate_card(cand)

    assert card["config"]["wide_screen_mode"] is True
    assert card["config"]["update_multi"] is True   # 群里共享卡片必须能更新
    assert card["header"]["title"]["tag"] == "plain_text"
    assert isinstance(card["elements"], list) and card["elements"]


def test_candidate_card_has_two_buttons_carrying_uid(database):
    cand = save_one()
    buttons = _buttons(cards.build_candidate_card(cand))

    assert len(buttons) == 2
    acts = {b["value"]["act"] for b in buttons}
    assert acts == {cards.ACTION_CONFIRM, cards.ACTION_REJECT}
    # 每个按钮都必须带 candidate_uid，否则回调不知道操作谁
    for b in buttons:
        assert b["value"]["cid"] == cand.uid
        assert isinstance(b["value"], dict)   # 不能是字符串


def test_card_content_is_readable_json(database):
    """发给飞书的是字符串，必须能解析回来，且中文不能被转义成 \\uXXXX。"""
    cand = save_one()
    content = cards.card_to_content(cards.build_candidate_card(cand))

    assert isinstance(content, str)
    assert "确认建单" in content
    assert json.loads(content)["header"]["title"]["content"]


def test_owner_is_rendered_as_at_tag(database):
    """用 <at id=...> 让人名由飞书自己渲染，省掉查通讯录的接口和权限。"""
    cand = save_one()
    card = cards.build_candidate_card(cand)
    fields = card["elements"][0]["fields"]
    owner = next(f for f in fields if "负责人" in f["text"]["content"])

    assert '<at id="ou_zhangsan"></at>' in owner["text"]["content"]
    # 序列化成字符串后引号会被转义，所以不要在 JSON 字符串上断言原文
    assert "ou_zhangsan" in cards.card_to_content(card)


def test_assumed_deadline_is_marked(database):
    """只到日的截止时间是系统推定的，卡片上必须写出来。"""
    cand = save_one()
    assert "推定" in cards.card_to_content(cards.build_candidate_card(cand))


def test_confirmed_card_has_no_buttons(database):
    cand = save_one()
    task = repo.confirm_candidate(cand.uid, operator_open_id="ou_mgr")
    card = cards.build_confirmed_card(cand, task)

    assert _buttons(card) == []
    assert card["header"]["template"] == cards.TEMPLATE_DONE


def test_rejected_card_has_no_buttons(database):
    cand = save_one()
    card = cards.build_rejected_card(cand, "ou_mgr")
    assert _buttons(card) == []


# ---------------------------------------------------------------- 回调：确认

def test_confirm_creates_task(database):
    cand = save_one()
    outcome = card_action.process(cards.ACTION_CONFIRM, cand.uid, "ou_mgr")

    assert outcome.ok is True
    assert outcome.toast_type == "success"
    assert _buttons(outcome.card) == []
    assert repo.count_tasks() == 1
    assert repo.get_candidate(cand.uid).status == "confirmed"

    task = repo.list_tasks()[0]
    assert task.status == TASK_TODO
    assert task.owner_open_ids == ["ou_zhangsan"]


def test_confirm_twice_does_not_create_second_task(database):
    """用户连点两下 / 飞书重推 —— 这是最容易被忽略的坑。"""
    cand = save_one()
    first = card_action.process(cards.ACTION_CONFIRM, cand.uid, "ou_mgr")
    second = card_action.process(cards.ACTION_CONFIRM, cand.uid, "ou_mgr")

    assert first.ok is True
    assert second.ok is False
    assert second.toast_type == "warning"
    assert "已经确认过" in second.toast_content
    assert repo.count_tasks() == 1


def test_confirm_unknown_candidate(database):
    outcome = card_action.process(cards.ACTION_CONFIRM, "deadbeef" * 4, "ou_mgr")
    assert outcome.ok is False
    assert outcome.toast_type == "error"
    assert repo.count_tasks() == 0


def test_confirm_expired_candidate_is_refused(database):
    """过了 72 小时的候选不能再建单，否则等于凭空补出一条谁也没确认过的任务。"""
    repo.save_candidates(make_recognition(), make_cmd_for_expiry(), ttl_hours=-2)
    cand = repo.list_candidates(status=None)[0]
    assert repo.expire_candidates() == 1

    outcome = card_action.process(cards.ACTION_CONFIRM, cand.uid, "ou_mgr")
    assert outcome.ok is False
    assert outcome.toast_type == "warning"
    assert "过期" in outcome.toast_content
    assert repo.count_tasks() == 0


def make_cmd_for_expiry():
    return make_cmd(message_id="om_expiring")


def test_confirm_rejected_candidate_is_refused(database):
    cand = save_one()
    repo.reject_candidate(cand.uid, "ou_mgr")
    outcome = card_action.process(cards.ACTION_CONFIRM, cand.uid, "ou_mgr")

    assert outcome.ok is False
    assert "忽略" in outcome.toast_content
    assert repo.get_candidate(cand.uid).status == CAND_REJECTED
    assert repo.count_tasks() == 0


# ---------------------------------------------------------------- 回调：忽略

def test_reject_marks_candidate(database):
    cand = save_one()
    outcome = card_action.process(cards.ACTION_REJECT, cand.uid, "ou_mgr")

    assert outcome.ok is True
    assert outcome.toast_type == "info"
    assert repo.get_candidate(cand.uid).status == CAND_REJECTED
    assert repo.count_tasks() == 0


def test_reject_twice_is_noop(database):
    cand = save_one()
    card_action.process(cards.ACTION_REJECT, cand.uid, "ou_mgr")
    second = card_action.process(cards.ACTION_REJECT, cand.uid, "ou_mgr")
    assert second.ok is False
    assert second.toast_type == "warning"


# ---------------------------------------------------------------- 边界

def test_empty_cid_is_handled(database):
    outcome = card_action.process(cards.ACTION_CONFIRM, "", "ou_mgr")
    assert outcome.ok is False
    assert outcome.toast_type == "error"


def test_unknown_action_is_handled(database):
    cand = save_one()
    outcome = card_action.process("delete_everything", cand.uid, "ou_mgr")
    assert outcome.ok is False
    assert "未知操作" in outcome.toast_content
    assert repo.get_candidate(cand.uid).status == "pending"


# ---------------------------------------------------------------- 飞书适配层

def test_handle_extracts_fields_and_returns_response(database):
    cand = save_one()
    event = _Obj(event=_Obj(
        action=_Obj(value={"act": cards.ACTION_CONFIRM, "cid": cand.uid}),
        operator=_Obj(open_id="ou_mgr"),
        context=_Obj(open_message_id="om_card", open_chat_id="oc_001"),
    ))

    resp = card_action.handle(event)

    assert resp is not None
    assert resp.toast.type == "success"
    assert resp.toast.content == "已建单"
    assert resp.card.type == "raw"
    assert resp.card.data["header"]["template"] == cards.TEMPLATE_DONE
    assert repo.count_tasks() == 1


def test_handle_never_raises_on_garbage(database):
    """回调抛异常 → 飞书重推 → 用户看到按钮没反应。这里必须兜住。"""
    resp = card_action.handle(_Obj(event=None))
    assert resp is not None
    assert resp.toast.type == "error"


def test_handle_supports_long_keys(database):
    """value 里用 action / candidate_uid 这种长 key 也要能认。"""
    cand = save_one()
    event = _Obj(event=_Obj(
        action=_Obj(value={"action": cards.ACTION_REJECT, "candidate_uid": cand.uid}),
        operator=_Obj(open_id="ou_mgr"),
    ))
    resp = card_action.handle(event)
    assert resp.toast.type == "info"
    assert repo.get_candidate(cand.uid).status == CAND_REJECTED
