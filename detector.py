"""任务识别层：消息 -> 结构化任务候选。

这一层只负责「调模型 + 校验 + 后置检查」，不碰数据库、不发通知。
设计文档 4.4 明确：大模型模块只返回结构化候选，不直接写库、不直接发飞书消息。
"""

import re
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
    import os
    bot_id = os.getenv("FEISHU_BOT_OPEN_ID", "")
    mapping = {}
    ambiguous = set()
    for m in mentions:
        if (not re.fullmatch(r"ou_[A-Za-z0-9_]+", m.open_id or "")
                or m.open_id == bot_id or m.key in ("all", "@all", "@_all")):
            continue
        for token in (m.open_id, m.key):
            if not token:
                continue
            if token in mapping and mapping[token] != m.open_id:
                ambiguous.add(token)
            mapping[token] = m.open_id
    for token in ambiguous:
        mapping.pop(token, None)
    for task in result.tasks:
        supplied = task.assignee_mention_ids
        kept = list(dict.fromkeys(mapping[token] for token in supplied if token in mapping))
        unresolved = any(token not in mapping for token in supplied)
        task.assignee_mention_ids = kept
        task.missing_fields = [f for f in task.missing_fields if f != "assignee"]
        if not kept or unresolved:
            task.missing_fields.append("assignee")
            result.clarification_needed = True
            result.clarification_question = "部分负责人无法从真实@成员取得有效身份，请人工核实后重新指定负责人。"
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
    _validate_priority(result, cmd.text)

    # 时间换算放在模型之后、业务之前：模型只抄原文，日期由确定性代码算。
    # 基准是消息的 sent_at，不是 now()。
    deadline.apply_to_tasks(result.tasks, cmd.sent_at)
    return result


def _validate_priority(result: TaskRecognition, source: str) -> None:
    """紧急优先级必须有当前消息中可核验的肯定要求。"""
    for task in result.tasks:
        evidence = (task.priority_evidence or "").strip()
        match = re.search(
            r"紧急任务|紧急事项|十万火急|(?:紧急|加急)[，,：:\s]*(?:请|处理|提交|完成|交付|办理)"
            r"|(?:请|务必|需要|要求)[^。！？!?；;\n]{0,12}(?:加急|紧急)"
            r"|优先(?:处理|完成|提交)|(?:立即|马上)(?:处理|提交|完成)",
            evidence,
        )
        negated = re.search(
            r"(?:不|非|无需|不用|无须|不必|取消|别|勿)[^，,。！？!?；;\n]{0,6}"
            r"(?:紧急|加急|优先|立即|马上)", evidence,
        )
        if (task.priority != "urgent" or not evidence or evidence not in source
                or evidence not in task.evidence or not match or negated):
            task.priority = "normal"
            task.priority_evidence = None


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
