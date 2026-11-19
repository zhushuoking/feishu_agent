"""测试夹具：临时库 + 构造测试数据的工具函数。"""

from datetime import datetime

import pytest

import db
import repo
from tests.helpers import save_one


@pytest.fixture(autouse=True)
def _no_event_flush(monkeypatch):
    """测试里绝不真发消息、绝不真改卡片。

    Day 13 之后，每次状态流转都会往发件箱塞事件通知，并且会尝试后台发送。
    测试必须把它关掉：只验「入队」这一步，发送逻辑在 Day 12 单独测过。

    Day 14 之后还多了一条：状态一变会开线程去 PATCH 历史卡片。
    同样要关 —— 单测里没有真卡片可改，真跑起来只会刷一堆失败日志。
    同步逻辑本身用注入假 updater 的方式单独验（tests/test_card_sync.py）。
    """
    monkeypatch.setenv("EVENT_FLUSH_IMMEDIATE", "0")
    monkeypatch.setenv("CARD_SYNC_ENABLED", "0")


@pytest.fixture()
def database(tmp_path):
    """每个用例一个独立库文件，用完销毁，互不污染。"""
    path = str(tmp_path / "test.db")
    db.configure(path)
    db.init_db()
    yield path
    db.get_engine().dispose()


@pytest.fixture()
def task(database):
    """一条已建单的任务：发起人 ou_sender，负责人 ou_zhangsan，截止本周五。

    Day 10 和 Day 11 都用它 —— 权限用例和卡片用例面对的是同一条任务，
    各自造一份容易出现「测试里是负责人，实现里以为不是」的对不上。
    """
    cand = save_one()
    return repo.confirm_candidate(cand.uid, operator_open_id="ou_sender")
