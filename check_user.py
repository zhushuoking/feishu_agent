"""诊断：某个 open_id 到底能不能收到机器人的私聊。

为什么需要这个工具：
「他没收到消息」有五种完全不同的原因，错误码都一样地只写一句 muted 的提示。
与其猜，不如把飞书的原始返回打出来。

    .venv\\Scripts\\python.exe check_user.py ou_xxx        # 只查，不发消息
    .venv\\Scripts\\python.exe check_user.py ou_xxx --send  # 再真发一条测试私聊

查两件事：
1. 这个 open_id 在不在本企业通讯录里（能查到 → 同租户内部成员）
2. 机器人能不能给他发消息（不能 → 看错误码）
"""

import argparse
import json
import os

from dotenv import load_dotenv

load_dotenv()

import feishu_api

CODE_HINTS = {
    230013: "目标用户不在应用可用范围，或被设为禁用成员。"
            "去开发者后台 应用发布→版本管理与发布→创建版本→可用范围 加人并发布；"
            "外部用户还要开「对外共享」并由他本人确认授权",
    230002: "机器人不在该会话里（群机器人被移除了？）",
    99991672: "应用缺少调用该接口的权限 scope，去权限管理里加并发布版本",
    99991663: "tenant_access_token 无效，检查 .env 里的 App ID / Secret",
    99991400: "请求过于频繁，等一会儿再试",
    230020: "对方已离职或账号被停用",
}


def check_user(open_id: str) -> None:
    from lark_oapi.api.contact.v3 import GetUserRequest

    req = (
        GetUserRequest.builder()
        .user_id_type("open_id")
        .user_id(open_id)
        .build()
    )
    try:
        resp = feishu_api.get_client().contact.v3.user.get(req)
    except Exception as exc:
        print(f"查用户异常：{type(exc).__name__}: {exc}")
        return

    if not resp.success():
        print(f"查用户失败 code={resp.code} msg={resp.msg}")
        print(f"  诊断：{CODE_HINTS.get(resp.code, '未知错误码，去文档搜 code=' + str(resp.code))}")
        print("  → 查不到不代表人不存在：外部租户的用户本来就不在你的通讯录里")
        return

    user = resp.data.user if resp.data else None
    print("查用户成功：")
    print(f"  name      = {getattr(user, 'name', '-')}")
    print(f"  en_name   = {getattr(user, 'en_name', '-')}")
    print(f"  department = {getattr(user, 'department_ids', '-')}")
    print(f"  status    = {getattr(getattr(user, 'status', None), 'is_activated', '-')}")


def try_send(open_id: str) -> None:
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

    body = (
        CreateMessageRequestBody.builder()
        .receive_id(open_id)
        .msg_type("text")
        .content(json.dumps({"text": "这是一条连通性测试消息，收到说明机器人能私聊你。"},
                            ensure_ascii=False))
        .build()
    )
    req = (
        CreateMessageRequest.builder()
        .receive_id_type("open_id")
        .request_body(body)
        .build()
    )
    try:
        resp = feishu_api.get_client().im.v1.message.create(req)
    except Exception as exc:
        print(f"发送异常：{type(exc).__name__}: {exc}")
        return

    if resp.success():
        print(f"发送成功 message_id={resp.data.message_id if resp.data else '-'}")
        return

    print(f"发送失败 code={resp.code} msg={resp.msg}")
    hint = CODE_HINTS.get(resp.code)
    if hint:
        print(f"  诊断：{hint}")
    else:
        print("  诊断：文档里搜 code=" + str(resp.code))


def main() -> None:
    parser = argparse.ArgumentParser(description="诊断某人能否收到机器人私聊")
    parser.add_argument("open_id")
    parser.add_argument("--send", action="store_true", help="真发一条测试私聊")
    args = parser.parse_args()

    print(f"检查 open_id = {args.open_id}")
    print("-" * 70)
    check_user(args.open_id)

    if args.send:
        print("-" * 70)
        try_send(args.open_id)
    else:
        print("-" * 70)
        print("加 --send 才会真发一条测试消息")


if __name__ == "__main__":
    main()
