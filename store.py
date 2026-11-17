"""事件去重存储。

飞书在客户端超时、网络抖动时会重复推送同一个事件；长连接重连后也可能补发。
设计文档 5.1 要求 event_id 唯一，重复事件直接返回已处理，不重复建单、不重复通知。

这里先用标准库 sqlite3，够用且零依赖。
Day 8 引入 SQLAlchemy 时这一层会整体重写，但对外接口（is_new_event）保持不变。
"""

import os
import sqlite3
from datetime import datetime

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "task_agent.db")


def connect(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(db_path: str = DEFAULT_DB) -> None:
    with connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS event_inbox (
                event_id    TEXT PRIMARY KEY,
                message_id  TEXT,
                chat_id     TEXT,
                received_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_event_inbox_chat ON event_inbox(chat_id)"
        )


def is_new_event(event_id: str, message_id: str = "", chat_id: str = "",
                 db_path: str = DEFAULT_DB) -> bool:
    """登记事件并判断是否首次出现。

    返回 True  = 第一次见到，继续处理
    返回 False = 重复事件，直接丢弃

    用 INSERT OR IGNORE + rowcount 判断，避免「先查后插」在并发下失效。
    """
    if not event_id:
        # 没有 event_id 就不做去重，交给上层用 message_id 兜底
        return True

    with connect(db_path) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO event_inbox(event_id, message_id, chat_id, received_at) "
            "VALUES (?, ?, ?, ?)",
            (event_id, message_id, chat_id, datetime.now().isoformat(timespec="seconds")),
        )
        return cur.rowcount == 1


def count_events(db_path: str = DEFAULT_DB) -> int:
    with connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM event_inbox").fetchone()
        return int(row[0]) if row else 0
