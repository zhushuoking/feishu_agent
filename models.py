"""消息标准化：把飞书事件转换成系统内部统一的 MessageCommand。

设计要点（对应设计文档 5.1 与 10.2）：
1. 真人名字不能用文本里的 @ 占位符推断，必须走 mentions 的 open_id
2. 时间统一转成带时区的 datetime，基准是消息的 create_time，不是 now()
3. 机器人自己发的消息要能识别出来，否则提醒会被再次当成新任务
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

TZ_SHANGHAI = timezone(timedelta(hours=8))


@dataclass
class Mention:
    """消息里被 @ 的对象。key 是文本中的占位符，open_id 才是可靠标识。"""

    key: str
    name: str
    open_id: str


@dataclass
class MessageCommand:
    event_id: str
    message_id: str
    chat_id: str
    chat_type: str
    sender_open_id: str
    sender_type: str
    text_raw: str
    text: str
    mentions: List[Mention] = field(default_factory=list)
    root_id: Optional[str] = None
    parent_id: Optional[str] = None
    sent_at: Optional[datetime] = None

    @property
    def is_from_bot(self) -> bool:
        """机器人的消息必须过滤掉，否则自己发的提醒会被识别成新任务。"""
        return self.sender_type in ("app", "bot")

    @property
    def is_thread_reply(self) -> bool:
        """是否为回复消息。Day 9 的反馈关联主要靠回复树定位。"""
        return bool(self.root_id or self.parent_id)

    def summary(self) -> str:
        return (
            f"[{self.chat_type}] {self.sender_open_id[:12]}… "
            f"@{','.join(m.name for m in self.mentions) or '-'} : {self.text[:40]}"
        )


def clean_text(raw: str, mentions: List[Mention]) -> str:
    """把 @_user_1 这类占位符换成真实名字，便于模型理解；换不到就删除。"""
    text = raw or ""
    for m in mentions:
        text = text.replace(m.key, f"@{m.name}" if m.name else "")
    return text.strip()


def _get(obj: Any, *path, default=None):
    cur = obj
    for key in path:
        cur = getattr(cur, key, None)
        if cur is None:
            return default
    return cur


def parse_event(event: Any, header: Any = None) -> MessageCommand:
    """把 lark-oapi 的 P2ImMessageReceiveV1 事件转成 MessageCommand。"""
    msg = event.message
    sender = event.sender

    raw_text = ""
    try:
        import json

        raw_text = (json.loads(msg.content or "{}") or {}).get("text", "")
    except Exception:
        raw_text = ""

    mentions = [
        Mention(
            key=getattr(m, "key", "") or "",
            name=getattr(m, "name", "") or "",
            open_id=_get(m, "id", "open_id", default="") or "",
        )
        for m in (msg.mentions or [])
    ]

    sent_at = None
    create_time = getattr(msg, "create_time", None)
    if create_time:
        try:
            sent_at = datetime.fromtimestamp(int(create_time) / 1000, tz=TZ_SHANGHAI)
        except (TypeError, ValueError):
            sent_at = None

    event_id = _get(header, "event_id", default=None) or getattr(msg, "message_id", "")

    return MessageCommand(
        event_id=event_id,
        message_id=getattr(msg, "message_id", "") or "",
        chat_id=getattr(msg, "chat_id", "") or "",
        chat_type=getattr(msg, "chat_type", "") or "",
        sender_open_id=_get(sender, "sender_id", "open_id", default="") or "",
        sender_type=getattr(sender, "sender_type", "") or "",
        text_raw=raw_text,
        text=clean_text(raw_text, mentions),
        mentions=mentions,
        root_id=getattr(msg, "root_id", None) or None,
        parent_id=getattr(msg, "parent_id", None) or None,
        sent_at=sent_at,
    )
