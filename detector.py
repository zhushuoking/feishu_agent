"""任务识别层：消息 -> 结构化任务候选。

这一层只负责「调模型 + 校验 + 后置检查」，不碰数据库、不发通知。
设计文档 4.4 明确：大模型模块只返回结构化候选，不直接写库、不直接发飞书消息。
"""

from typing import List, Optional

import deadline
import llm
import prompts
from models import Mention, MessageCommand
from schemas import TaskRecognition


class DetectionError(RuntimeError):
    """识别失败。上层应把消息转入人工队列，不要静默丢弃。"""


def _validate_mentions(result: TaskRecognition, mentions: List[Mention]) -> TaskRecognition:
    """后置校验：模型填的负责人必须真的在当前消息的 mentions 里。

    这是防「模型幻觉造人」的最后一道闸。文档 8.3.1 要求 assignee_mention_ids
    只能引用当前消息 mentions 中的标识，但提示词管不住，必须在代码里卡。
    """
    valid = {m.open_id for m in mentions if m.open_id} | {m.key for m in mentions if m.key}

    for task in result.tasks:
        kept = [mid for mid in task.assignee_mention_ids if mid in valid]
        dropped = [mid for mid in task.assignee_mention_ids if mid not in valid]
        task.assignee_mention_ids = kept
        if kept:
            # 有人了就从缺失字段里去掉 assignee
            task.missing_fields = [f for f in task.missing_fields if f != "assignee"]
        elif dropped:
            # 模型给了人但全是不存在的 ID，视为缺失负责人
            if "assignee" not in task.missing_fields:
                task.missing_fields.append("assignee")
        else:
            if "assignee" not in task.missing_fields:
                task.missing_fields.append("assignee")
    return result


def detect(cmd: MessageCommand) -> TaskRecognition:
    """识别一条消息。抛 DetectionError 表示无法给出可信结果。"""
    user_prompt = prompts.build_user_prompt(
        chat_type=cmd.chat_type,
        sent_at=cmd.sent_at,
        sender_open_id=cmd.sender_open_id,
        mentions=cmd.mentions,
        text=cmd.text,
    )

    try:
        raw = llm.chat_json(prompts.SYSTEM_PROMPT, user_prompt)
    except llm.LLMError as exc:
        raise DetectionError(str(exc)) from exc

    try:
        result = TaskRecognition.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError
        raise DetectionError(f"模型输出未通过 Schema 校验：{exc}") from exc

    result = _validate_mentions(result, cmd.mentions)

    # 时间换算放在模型之后、业务之前：模型只抄原文，日期由确定性代码算。
    # 基准是消息的 sent_at，不是 now()。
    deadline.apply_to_tasks(result.tasks, cmd.sent_at)
    return result


def detect_text(text: str, *, mentions: Optional[List[Mention]] = None,
                sender_open_id: str = "ou_debug", chat_type: str = "group"):
    """纯文本识别，给命令行调试用，不依赖飞书事件。"""
    from datetime import datetime

    from models import TZ_SHANGHAI, clean_text

    mentions = mentions or []
    cmd = MessageCommand(
        event_id="evt_debug",
        message_id="om_debug",
        chat_id="oc_debug",
        chat_type=chat_type,
        sender_open_id=sender_open_id,
        sender_type="user",
        text_raw=text,
        text=clean_text(text, mentions),
        mentions=mentions,
        sent_at=datetime.now(tz=TZ_SHANGHAI),
    )
    return detect(cmd)
