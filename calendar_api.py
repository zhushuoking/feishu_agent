"""Day 16：飞书日历 REST 薄封装。

【为什么不用 lark-oapi SDK】
日历 v4 的 time_info / reminder 都是嵌套 model，用 builder 要构造一堆对象；
而且这一层要能被单测整个替换掉（calendar_sync 支持注入 api）。
直接用 httpx 发 REST，路径和官方文档一一对应，排障时看得懂。

【原则】跟 feishu_api.py 一样：所有函数**不抛异常**，失败返回 None/False
并打日志。日历同步是附加价值，日历挂了绝不能让「点确认」变成没反应。

【开放平台前置条件】
- 权限：calendar:calendar（更新日历及日程信息），开完必须创建版本发布
- 应用身份调日历接口要求开启机器人能力，否则 190007 app bot_id not found
- 参与人必须和日程组织者（应用机器人）在同一企业内

【代理】飞书域名直连。长连接那次 Clash 掐断 open.feishu.cn 的 TLS 踩过坑，
这里 httpx 直接用 trust_env=False 彻底绕开环境代理，比 NO_PROXY 更确定。
"""

import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://open.feishu.cn/open-apis"
CALENDAR_PATH = "/calendar/v4/calendars"

_token_cache = {"value": "", "expire_at": 0.0}
_token_lock = threading.Lock()
_request_state = threading.local()


def last_error() -> str:
    return getattr(_request_state, "error", "")


def _failure(error: str) -> Tuple[bool, Dict[str, Any], str]:
    _request_state.error = error
    return False, {}, error


def _tenant_token() -> str:
    """拿 tenant_access_token 并缓存到过期前 2 分钟。

    锁是必要的：确认建单和状态流转会各起一个后台线程，两边同时发现
    token 过期会一起去刷，飞书对重复取 token 虽不敏感，但没必要。
    """
    with _token_lock:
        if _token_cache["value"] and time.time() < _token_cache["expire_at"]:
            return _token_cache["value"]

        app_id = os.getenv("FEISHU_APP_ID", "").strip()
        app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
        if not app_id or not app_secret:
            print("[CAL] 缺少 FEISHU_APP_ID / FEISHU_APP_SECRET")
            return ""

        try:
            with httpx.Client(timeout=10, trust_env=False) as client:
                resp = client.post(
                    BASE_URL + "/auth/v3/tenant_access_token/internal",
                    json={"app_id": app_id, "app_secret": app_secret},
                )
            payload = resp.json()
        except Exception as exc:
            print(f"[CAL] 取 tenant_access_token 异常：{type(exc).__name__}: {exc}")
            return ""

        if payload.get("code") != 0:
            print(f"[CAL] 取 token 失败 code={payload.get('code')} "
                  f"msg={payload.get('msg')}")
            return ""

        # 注意：token 接口和日历接口**不是同一种响应结构**。
        # 日历 v4 是 {"code":0,"data":{...}}，而 auth/v3 直接把
        # tenant_access_token / expire 放在顶层，没有 data 包装。
        # 第一次写的时候统一按 data 取，结果 --check 直接拿不到 token。
        data = payload.get("data") or payload
        token = data.get("tenant_access_token", "")
        expire = int(data.get("expire", 7200) or 7200)
        if not token:
            print("[CAL] 取 token 返回体里没有 tenant_access_token")
            return ""

        _token_cache["value"] = token
        _token_cache["expire_at"] = time.time() + max(60, expire - 120)
        return token


def _request(method: str, path: str, *,
             params: Optional[Dict[str, Any]] = None,
             body: Optional[Dict[str, Any]] = None,
             accepted_codes: tuple = (),
             access_token: Optional[str] = None,
             ) -> Tuple[bool, Dict[str, Any], str]:
    """统一的请求出口。返回 (是否成功, data, 错误文本)。

    单测不 mock httpx，而是给 calendar_sync 注入一个假 api —— 所以这一层
    不需要额外的可注入点，保持简单。
    """
    _request_state.error = ""
    token = _tenant_token() if access_token is None else access_token
    if not token:
        return _failure("拿不到 tenant_access_token")

    try:
        with httpx.Client(timeout=10, trust_env=False) as client:
            resp = client.request(
                method,
                BASE_URL + path,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                params=params,
                json=body,
            )
        payload = resp.json() if resp.content else {}
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        print(f"[CAL] {method} {path} 异常：{err}")
        return _failure(err)

    code = payload.get("code", -1)
    if code in accepted_codes:
        return True, payload.get("data") or {}, ""
    if resp.status_code >= 400 or code != 0:
        err = f"HTTP {resp.status_code} code={code} msg={payload.get('msg')}"
        print(f"[CAL] {method} {path} 失败：{err}")
        return _failure(err)

    return True, payload.get("data") or {}, ""


def check() -> Dict[str, Any]:
    """冒烟：一次调用同时验凭证、验权限、拿 calendar_id。不写任何东西。

    权限没发版时会在这里直接暴露（code 99991672 或 190002），
    不用等到真的建单才发现日历没同步。
    """
    configured = os.getenv("FEISHU_CALENDAR_ID", "").strip()
    if configured:
        ok, data, err = _request("GET", f"{CALENDAR_PATH}/{configured}")
        if not ok:
            return {"ok": False, "error": err}
        cal = data.get("calendar") or data
        if cal.get("role") not in ("owner", "writer"):
            return {"ok": False, "error": "目标日历需要 writer 或 owner 权限"}
        return {"ok": True, "calendar_id": cal.get("calendar_id", configured),
                "summary": cal.get("summary", ""), "role": cal.get("role", ""),
                "type": cal.get("type", ""), "bot_open_id": ""}
    ok, data, err = _request(
        "POST", CALENDAR_PATH + "/primary",
        params={"user_id_type": "open_id"},
    )
    if not ok:
        return {"ok": False, "error": err}

    items = data.get("calendars") or []
    if not items:
        return {"ok": False, "error": "主日历返回为空"}

    cal = items[0].get("calendar") or {}
    return {
        "ok": True,
        "calendar_id": cal.get("calendar_id", ""),
        "summary": cal.get("summary", ""),
        "role": cal.get("role", ""),
        "type": cal.get("type", ""),
        "bot_open_id": items[0].get("user_id", ""),
    }


def primary_calendar_id() -> Optional[str]:
    """取应用主日历的 calendar_id。注意是 POST，不是 GET。

    【踩过的坑】这个接口鉴权只认**只读** scope：
        calendar:calendar:readonly / calendar:calendar.calendar:readonly /
        calendar:calendar:read
    只开 calendar:calendar（写）会报 99991672，尽管那个权限的中文描述
    写着「获得全部日历接口的授权」—— 文档描述不等于逐接口鉴权。
    """
    info = check()
    if not info.get("ok"):
        return None
    return info.get("calendar_id") or None


def create_calendar(summary: str = "任务跟进") -> Optional[str]:
    """建一个共享日历，返回 calendar_id。只需要写权限 calendar:calendar。

    用途：不想为了「查主日历」再发一次版时，用这个绕过去。
    应用对自己创建的日历是 owner，之后建日程、加参与人都没问题。
    副作用是飞书里多一个日历实体，所以只由 --create-calendar 显式触发，
    正常链路绝不自动创建。
    """
    ok, data, _ = _request(
        "POST", CALENDAR_PATH,
        body={
            "summary": summary,
            "description": "由飞书任务跟进 Agent 创建，用于存放任务截止日程",
            "permissions": "private",
        },
    )
    if not ok:
        return None
    return ((data.get("calendar") or {}).get("calendar_id") or "").strip() or None


def create_event(calendar_id: str, body: Dict[str, Any],
                 idempotency_key: str, *, access_token: Optional[str] = None) -> Optional[str]:
    """创建日程，返回 event_id。

    idempotency_key 在「应用 + 日历」维度唯一，32~128 字符。
    重复确认 / 网络重试带同一个 key 时飞书不会建出第二个日程，
    这是幂等的第二道保险（第一道是库里的 task_uid 唯一约束）。
    """
    ok, data, _ = _request(
        "POST", f"{CALENDAR_PATH}/{calendar_id}/events",
        params={"idempotency_key": idempotency_key, "user_id_type": "open_id"},
        body=body, access_token=access_token,
    )
    if not ok:
        return None
    event_id = ((data.get("event") or {}).get("event_id") or "").strip()
    if not event_id:
        print("[CAL] 创建日程返回成功但没拿到 event_id")
        _request_state.error = "创建日程返回成功但没有 event_id"
        return None
    return event_id


def add_attendees(calendar_id: str, event_id: str,
                  open_ids: List[str]) -> bool:
    """给日程加参与人。创建日程接口不支持直接传参与人，必须单独调这一下。

    不传 need_notification：该接口默认给新参与人发日程通知，传错字段
    反而可能触发 190002。参与人必须和应用机器人在同一企业内。
    """
    attendees = [
        {"type": "user", "user_id": open_id}
        for open_id in dict.fromkeys(open_ids)
        if open_id and open_id.strip()
    ]
    if not attendees:
        return True

    ok, _, _ = _request(
        "POST", f"{CALENDAR_PATH}/{calendar_id}/events/{event_id}/attendees",
        params={"user_id_type": "open_id"},
        body={"attendees": attendees},
    )
    return ok


def update_event(calendar_id: str, event_id: str,
                 body: Dict[str, Any], *, access_token: Optional[str] = None) -> bool:
    """更新日程。改起止时间时 start_time / end_time 必须同时传才生效。"""
    ok, _, _ = _request(
        "PATCH", f"{CALENDAR_PATH}/{calendar_id}/events/{event_id}",
        body=body, access_token=access_token,
    )
    return ok


def delete_event(calendar_id: str, event_id: str, *, access_token: Optional[str] = None) -> bool:
    """删除日程。当前身份（应用）是组织者，有权限。"""
    ok, _, _ = _request(
        "DELETE", f"{CALENDAR_PATH}/{calendar_id}/events/{event_id}",
        accepted_codes=(193001, 193003), access_token=access_token,
    )
    return ok
