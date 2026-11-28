"""Day 9：飞书消息发送（薄封装）。

原则：这一层所有函数都**不抛异常**，失败返回 None 并打日志。
理由：卡片发不出去是「通知失败」，不是「业务流程失败」。
如果因为会话被解散、机器人被踢、权限被改就让整个回调 500，
用户看到的是"点确认没反应"，比"没收到卡片"更糟。

代理：和 main.py 一样，飞书域名必须直连，否则 Clash 会掐断 TLS。
"""

import json
import os
import uuid
from typing import Any, Dict, Optional

_FEISHU_NO_PROXY = "open.feishu.cn,.feishu.cn,msg-frontier.feishu.cn,localhost,127.0.0.1"
os.environ["NO_PROXY"] = ",".join(
    filter(None, [os.environ.get("NO_PROXY", ""), _FEISHU_NO_PROXY])
)
os.environ["no_proxy"] = os.environ["NO_PROXY"]

import lark_oapi as lark
from dotenv import load_dotenv
from lark_oapi.api.im.v1 import (
    CreateMessageRequest, CreateMessageRequestBody,
    PatchMessageRequest, PatchMessageRequestBody,
    DeleteMessageRequest,
)

load_dotenv()

_client: Optional[lark.Client] = None


def get_client() -> lark.Client:
    global _client
    if _client is None:
        app_id = os.getenv("FEISHU_APP_ID", "")
        app_secret = os.getenv("FEISHU_APP_SECRET", "")
        if not app_id or not app_secret:
            raise RuntimeError("缺少飞书凭证，请先运行 set_cred.bat")
        _client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .log_level(lark.LogLevel.WARNING)  # 发送失败自己会打印，不要刷屏
            .build()
        )
    return _client


def _send(receive_id: str, receive_id_type: str, msg_type: str,
          content: str, message_key: str = "") -> Optional[str]:
    req = (
        CreateMessageRequest.builder()
        .receive_id_type(receive_id_type)
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type(msg_type)
            .content(content)
            .uuid(str(uuid.uuid5(uuid.NAMESPACE_URL, message_key)) if message_key else None)
            .build()
        )
        .build()
    )
    try:
        resp = get_client().im.v1.message.create(req)
    except Exception as exc:
        print(f"[CARD] 发送异常 receive_id_type={receive_id_type}: {type(exc).__name__}: {exc}")
        return None

    if not resp.success():
        print(f"[CARD] 发送失败 code={resp.code} msg={resp.msg} "
              f"log_id={getattr(resp, 'get_log_id', lambda: '-')()}")
        return None
    return resp.data.message_id if resp.data else None


def reply_task_created(message_id: str, task) -> Optional[str]:
    """建单回执统一发送到发起人与机器人的私聊。message_id 保留兼容调用。"""
    import cards

    if not task.creator_open_id:
        print(f"[CARD] 建单回执缺少发起人 task={task.uid}")
        return None
    return _deliver_task_card(task, task.creator_open_id,
                              cards.build_task_card(task, task.creator_open_id),
                              kind="task_created")


def send_card_to_chat(chat_id: str, card: Dict[str, Any]) -> Optional[str]:
    return _send(chat_id, "chat_id", "interactive",
                 json.dumps(card, ensure_ascii=False))


def update_card(message_id: str, card: Dict[str, Any]) -> bool:
    """原地更新一张已经发出去的卡片（PATCH /im/v1/messages/:message_id）。

    这是「旧卡片不再撒谎」的唯一手段：回调的响应只能改被点的那一张，
    其它历史卡片得靠这个接口回头去改。

    几条硬限制（都是官方文档写死的，踩了就知道）：
    - 只能改 14 天内发的消息，超了报 230031，重试没有意义
    - 卡片 config 里前后都要有 "update_multi": true，否则改不动
      （cards.py 里每张卡都带着，别删）
    - 只能改机器人自己发的、没被撤回没被删除的消息
    - 单条消息 5 QPS

    权限：官方给的是「获取与发送单聊群组消息 / 以应用身份发消息 / 更新消息」
    三者任一，我们已经有了前两个，**不需要额外申请**。
    """
    if not message_id:
        return False

    req = (
        PatchMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            PatchMessageRequestBody.builder()
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        .build()
    )
    try:
        resp = get_client().im.v1.message.patch(req)
    except Exception as exc:
        print(f"[CARD] 更新异常 message_id={message_id[:12]}… "
              f"{type(exc).__name__}: {exc}")
        return False

    if not resp.success():
        print(f"[CARD] 更新失败 message_id={message_id[:12]}… "
              f"code={resp.code} msg={resp.msg}")
        return False
    return True


def delete_message(message_id: str) -> bool:
    """清理本机器人已登记的重复任务卡。"""
    try:
        response = get_client().im.v1.message.delete(
            DeleteMessageRequest.builder().message_id(message_id).build(),
        )
        if not response.success():
            print(f"[CARD] 清理重复卡失败 code={response.code} msg={response.msg}")
        return response.success()
    except Exception as exc:
        print(f"[CARD] 清理重复卡片失败 message={message_id[:12]} {type(exc).__name__}")
        return False


def _remember_card(task, message_id: str, receive_id: str,
                   receive_id_type: str, viewer_open_id: str = "") -> None:
    """把发出去的卡片记进 task_card，状态变了才找得到它。

    为什么在这儿做而不是让调用方做：message_id 只有发送成功的这一刻才有，
    让上层去接返回值，等于要求每个调用点都记得记账 —— 迟早漏一个。
    记账失败不影响发送已经成功这个事实，所以只打日志。
    """
    try:
        import repo

        repo.register_task_card(
            task.uid, message_id, receive_id, receive_id_type,
            viewer_open_id=viewer_open_id,
            kind="task", version=int(getattr(task, "version", 0) or 0),
        )
    except Exception as exc:
        print(f"[CARD] 卡片登记失败 task={getattr(task, 'uid', '?')[:8]} "
              f"{type(exc).__name__}: {exc}")


def send_card_to_user(open_id: str, card: Dict[str, Any],
                      message_key: str = "") -> Optional[str]:
    return _send(open_id, "open_id", "interactive",
                 json.dumps(card, ensure_ascii=False), message_key=message_key)


def send_text_to_chat(chat_id: str, text: str) -> Optional[str]:
    """纯文本。调试和降级用：卡片发不出去时至少能发句人话。"""
    return _send(chat_id, "chat_id", "text",
                 json.dumps({"text": text}, ensure_ascii=False))


def send_text_to_user(open_id: str, text: str, message_key: str = "") -> Optional[str]:
    return _send(open_id, "open_id", "text",
                 json.dumps({"text": text}, ensure_ascii=False), message_key=message_key)


def push_confirm_card(cand, card: Dict[str, Any],
                      prefer: Optional[str] = None) -> Optional[str]:
    """确认卡统一私聊发起人。prefer 参数保留供旧调用使用。"""
    import repo

    sender = cand.sender_open_id
    if not sender:
        print(f"[CARD] 确认卡缺少发起人 candidate={cand.uid}")
        return None
    key = f"CANDIDATE:{cand.uid}"
    message_id = send_card_to_user(sender, card, message_key=key)
    if not message_id:
        repo.enqueue_notification(key, sender, json.dumps(card, ensure_ascii=False),
                                  kind="candidate_confirm")
        print(f"[CARD] 确认卡私聊失败，已安排重试 candidate={cand.uid}")
    return message_id


def _deliver_task_card(task, open_id: str, card: Dict[str, Any],
                       kind: str) -> Optional[str]:
    """立即私聊，失败时使用同一消息标识交给现有发件箱重试。"""
    import repo

    key = f"{kind}:{task.uid}:{open_id}"
    message_id = upsert_task_card(task, open_id, card)
    if not message_id:
        repo.enqueue_notification(
            key, open_id, json.dumps(card, ensure_ascii=False), kind=kind,
            task_uid=task.uid, task_version=task.version,
        )
        print(f"[CARD] 任务卡私聊失败，已安排重试 task={task.uid[:8]}")
    return message_id


def upsert_task_card(task, open_id: str, card: Dict[str, Any]) -> Optional[str]:
    """已有卡片只更新。首次发送在所有通知种类间使用相同的去重标识。"""
    import repo

    current = repo.get_primary_task_card(task.uid, open_id)
    if current:
        if current.synced_version > task.version:
            return current.message_id
        if not update_card(current.message_id, card):
            return None
        repo.mark_task_card_synced(current.message_id, task.version)
        return current.message_id
    message_id = send_card_to_user(open_id, card, message_key=f"TASK:{task.uid}:{open_id}")
    if message_id:
        _remember_card(task, message_id, open_id, "open_id", viewer_open_id=open_id)
    return message_id


def push_task_card(task, card: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """带操作按钮的任务卡统一私聊各负责人。"""
    import cards

    sent = 0
    for open_id in dict.fromkeys(task.owner_open_ids):
        rendered = card or cards.build_task_card(task, open_id)
        if _deliver_task_card(task, open_id, rendered, kind="task_assignment"):
            sent += 1
    expected = len(set(task.owner_open_ids))
    if sent != expected:
        print(f"[CARD] 负责人通知未全部送达：成功{sent}，目标{expected}；请核查应用可用范围与私聊权限")
    return "ok" if expected and sent == expected else None
