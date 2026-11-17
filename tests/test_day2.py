"""Day 2 单测：标准化、去重、机器人消息过滤。

跑法：.venv\\Scripts\\python.exe -m pytest -q
"""

import os
import tempfile

import models
import store


class FakeId:
    def __init__(self, open_id):
        self.open_id = open_id


class FakeMention:
    def __init__(self, key, name, open_id):
        self.key = key
        self.name = name
        self.id = FakeId(open_id)


class FakeSender:
    def __init__(self, open_id, sender_type):
        self.sender_id = FakeId(open_id)
        self.sender_type = sender_type


class FakeMessage:
    def __init__(self, text, mentions=None, create_time="1794753412739",
                 root_id=None, parent_id=None):
        self.message_id = "om_test_001"
        self.chat_id = "oc_test_group"
        self.chat_type = "group"
        self.content = '{"text": "%s"}' % text
        self.mentions = mentions or []
        self.create_time = create_time
        self.root_id = root_id
        self.parent_id = parent_id


class FakeEvent:
    def __init__(self, message, sender):
        self.message = message
        self.sender = sender


class FakeHeader:
    event_id = "evt_test_001"


def test_clean_text_replaces_placeholder_with_name():
    mentions = [models.Mention(key="@_user_1", name="张三", open_id="ou_zhangsan")]
    assert models.clean_text("@_user_1 请本周五前交报告", mentions) == "@张三 请本周五前交报告"


def test_clean_text_removes_placeholder_when_no_name():
    mentions = [models.Mention(key="@_user_1", name="", open_id="ou_x")]
    assert models.clean_text("@_user_1 交报告", mentions) == "交报告"


def test_mentions_carry_open_id_not_name():
    """负责人识别必须拿 open_id，名字只用于显示。"""
    event = FakeEvent(
        FakeMessage("@_user_1 请周五前交报告", [FakeMention("@_user_1", "张三", "ou_zhangsan")]),
        FakeSender("ou_sender", "user"),
    )
    cmd = models.parse_event(event, FakeHeader())
    assert cmd.mentions[0].open_id == "ou_zhangsan"
    assert cmd.mentions[0].name == "张三"
    assert cmd.text == "@张三 请周五前交报告"
    assert cmd.text_raw == "@_user_1 请周五前交报告"


def test_bot_message_is_flagged():
    event = FakeEvent(
        FakeMessage("今日任务提醒", []),
        FakeSender("ou_bot", "app"),
    )
    assert models.parse_event(event, FakeHeader()).is_from_bot is True


def test_user_message_is_not_bot():
    event = FakeEvent(FakeMessage("测试", []), FakeSender("ou_user", "user"))
    assert models.parse_event(event, FakeHeader()).is_from_bot is False


def test_reply_detection():
    event = FakeEvent(
        FakeMessage("已完成", [], root_id="om_root", parent_id="om_root"),
        FakeSender("ou_user", "user"),
    )
    assert models.parse_event(event, FakeHeader()).is_thread_reply is True


def test_sent_at_is_shanghai_aware():
    event = FakeEvent(FakeMessage("测试", []), FakeSender("ou_user", "user"))
    cmd = models.parse_event(event, FakeHeader())
    assert cmd.sent_at is not None
    assert cmd.sent_at.utcoffset().total_seconds() == 8 * 3600


def test_dedupe_drops_second_occurrence():
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    store.init_db(db)
    assert store.is_new_event("evt_1", "om_1", "oc_1", db_path=db) is True
    assert store.is_new_event("evt_1", "om_1", "oc_1", db_path=db) is False
    assert store.is_new_event("evt_2", "om_2", "oc_1", db_path=db) is True
    assert store.count_events(db) == 2


def test_dedupe_allows_empty_event_id():
    """没有 event_id 时不做去重，交给上层用 message_id 兜底。"""
    db = os.path.join(tempfile.mkdtemp(), "t.db")
    store.init_db(db)
    assert store.is_new_event("", "om_1", "oc_1", db_path=db) is True
    assert store.is_new_event("", "om_1", "oc_1", db_path=db) is True
