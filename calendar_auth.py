"""个人日历一次授权、加密保存与自动续期。回调仅绑定实际登录的飞书账号。"""

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
from Crypto.Cipher import AES
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert

import calendar_api
import db
import permissions
from models_db import CalendarAccount, CalendarOAuthState, PersonalCalendarEvent, Task, TaskCalendarEvent

SCOPES = "auth:user.id:read calendar:calendar calendar:calendar:readonly offline_access"
app = FastAPI(docs_url=None, redoc_url=None)


class AuthorizationRequired(RuntimeError):
    pass


def base_url() -> str:
    value = os.getenv("CALENDAR_AUTH_BASE_URL", "http://127.0.0.1:8766").rstrip("/")
    address = urlparse(value)
    if os.getenv("CALENDAR_AUTH_REQUIRE_PUBLIC", "0") == "1" and (
            address.scheme != "https" or not address.hostname
            or address.hostname in ("localhost", "127.0.0.1", "::1")):
        raise AuthorizationRequired("个人日历授权入口等待管理员配置可访问的 HTTPS 回调地址")
    return value


def authorization_link(open_id: str) -> str:
    return base_url() + "/calendar/authorize?" + urlencode({"user_id": open_id})


def _key() -> bytes:
    path = Path(os.getenv("CALENDAR_TOKEN_KEY_FILE", db.current_db_path() + ".calendar.key"))
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        key = path.read_bytes()
    else:
        key = secrets.token_bytes(32)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(key)
            stream.flush()
            os.fsync(stream.fileno())
    if len(key) != 32:
        raise AuthorizationRequired("日历凭证密钥不可用，请恢复密钥后重试")
    return key


def _encrypt(tokens: dict) -> str:
    cipher = AES.new(_key(), AES.MODE_GCM)
    ciphertext, tag = cipher.encrypt_and_digest(json.dumps(tokens).encode())
    return base64.b64encode(cipher.nonce + tag + ciphertext).decode()


def _decrypt(value: str) -> dict:
    try:
        raw = base64.b64decode(value)
        cipher = AES.new(_key(), AES.MODE_GCM, nonce=raw[:16])
        return json.loads(cipher.decrypt_and_verify(raw[32:], raw[16:32]))
    except (ValueError, KeyError):
        raise AuthorizationRequired("日历凭证无法解密，请恢复密钥或重新连接日历") from None


@contextmanager
def _account_lock():
    # refresh_token 单次有效，跨进程锁覆盖读取、刷新及保存的完整过程。
    connection = sqlite3.connect(db.current_db_path() + ".calendar-auth-lock.db", timeout=30)
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield
    finally:
        connection.rollback()
        connection.close()


def _exchange(**grant) -> dict:
    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            response = client.post("https://accounts.feishu.cn/oauth/v3/token", data={
                "client_id": os.getenv("FEISHU_APP_ID", ""),
                "client_secret": os.getenv("FEISHU_APP_SECRET", ""), **grant,
            })
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        raise AuthorizationRequired("飞书授权服务暂时不可用，请稍后重试") from None
    if response.is_error or payload.get("code", 0) != 0 or not payload.get("access_token"):
        raise AuthorizationRequired(f"日历授权失效或失败，请重新连接（错误码 {payload.get('code', 'unknown')}）")
    if not payload.get("refresh_token") or int(payload.get("expires_in", 0)) <= 0:
        raise AuthorizationRequired("授权未包含有效的离线访问权限，请启用 offline_access 后重新连接")
    return payload


def _save(open_id: str, calendar_id: str, tokens: dict) -> None:
    values = dict(open_id=open_id, app_id=os.getenv("FEISHU_APP_ID", ""),
                  calendar_id=calendar_id, encrypted_tokens=_encrypt(tokens),
                  expires_at=db.now() + timedelta(seconds=int(tokens["expires_in"])),
                  updated_at=db.now())
    with db.session_scope() as session:
        session.execute(insert(CalendarAccount).values(**values).on_conflict_do_update(
            index_elements=["open_id"], set_=values,
        ))


def begin_authorization(open_id: str) -> str:
    redirect_uri = base_url() + "/calendar/callback"
    with db.session_scope() as session:
        tasks = list(session.scalars(select(Task)).all())
        permitted = any(permissions.chat_enabled(task.chat_id) and open_id in {
            *task.owner_open_ids, task.creator_open_id, task.effective_reviewer_open_id,
        } for task in tasks)
        if not permitted:
            raise AuthorizationRequired("该账号尚无启用群里的任务，请先建单后再连接日历")
        state = secrets.token_urlsafe(32)
        session.execute(delete(CalendarOAuthState).where(CalendarOAuthState.expires_at < db.now()))
        session.add(CalendarOAuthState(
            state_hash=hashlib.sha256(state.encode()).hexdigest(), open_id=open_id,
            expires_at=db.now() + timedelta(minutes=10), used=0,
        ))
    return "https://accounts.feishu.cn/open-apis/authen/v1/authorize?" + urlencode({
        "client_id": os.getenv("FEISHU_APP_ID", ""), "response_type": "code",
        "redirect_uri": redirect_uri, "scope": SCOPES, "state": state,
    })


def finish_authorization(code: str, state: str) -> str:
    digest = hashlib.sha256(state.encode()).hexdigest()
    with db.session_scope() as session:
        record = session.get(CalendarOAuthState, digest)
        if record is None or record.used or record.expires_at <= db.now():
            raise AuthorizationRequired("授权链接已过期或已使用，请重新发送“连接日历”")
        open_id = record.open_id
        claimed = session.execute(update(CalendarOAuthState).where(
            CalendarOAuthState.state_hash == digest, CalendarOAuthState.used == 0,
            CalendarOAuthState.expires_at > db.now(),
        ).values(used=1))
        if claimed.rowcount != 1:
            raise AuthorizationRequired("授权链接已使用，请重新连接日历")
    tokens = _exchange(grant_type="authorization_code", code=code,
                       redirect_uri=base_url() + "/calendar/callback")
    token = tokens["access_token"]
    ok, user, _ = calendar_api._request("GET", "/authen/v1/user_info", access_token=token)
    if not ok or user.get("open_id") != open_id:
        raise AuthorizationRequired("登录账号与任务卡账号不一致，请使用本人飞书账号重新连接")
    ok, data, _ = calendar_api._request("POST", calendar_api.CALENDAR_PATH + "/primary",
                                       access_token=token, params={"user_id_type": "open_id"})
    calendars = data.get("calendars") or []
    calendar = calendars[0].get("calendar", {}) if calendars else {}
    if not ok or not calendar.get("calendar_id") or calendar.get("role") not in ("owner", "writer"):
        raise AuthorizationRequired("无法获取可写的个人主日历，请检查用户身份的日历权限")
    with _account_lock():
        _save(open_id, calendar["calendar_id"], tokens)
    with db.session_scope() as session:
        session.execute(update(TaskCalendarEvent).where(TaskCalendarEvent.task_uid.in_(
            select(PersonalCalendarEvent.task_uid).where(PersonalCalendarEvent.open_id == open_id),
        )).values(retry_at=None))
    return open_id


class UserCalendarAPI:
    def __init__(self, token: str):
        self._token = token

    def create_event(self, calendar_id, body, key):
        return calendar_api.create_event(calendar_id, body, key, access_token=self._token)

    def update_event(self, calendar_id, event_id, body):
        return calendar_api.update_event(calendar_id, event_id, body, access_token=self._token)

    def delete_event(self, calendar_id, event_id):
        return calendar_api.delete_event(calendar_id, event_id, access_token=self._token)

    def last_error(self):
        return calendar_api.last_error()


def api_for_user(open_id: str):
    with _account_lock():
        with db.session_scope() as session:
            account = session.get(CalendarAccount, open_id)
        if account is None or account.app_id != os.getenv("FEISHU_APP_ID", ""):
            raise AuthorizationRequired("请在机器人私聊中发送“连接日历”，完成一次授权")
        tokens = _decrypt(account.encrypted_tokens)
        if account.expires_at <= db.now() + timedelta(minutes=2):
            tokens = _exchange(grant_type="refresh_token", refresh_token=tokens["refresh_token"])
            _save(open_id, account.calendar_id, tokens)
        return account.calendar_id, UserCalendarAPI(tokens["access_token"])


@app.get("/calendar/authorize")
def authorize(user_id: str):
    try:
        return RedirectResponse(begin_authorization(user_id))
    except AuthorizationRequired as exc:
        return PlainTextResponse(str(exc), status_code=400)


@app.get("/calendar/callback")
def callback(code: str = "", state: str = "", error: str = ""):
    if error or not code or not state:
        return PlainTextResponse("授权未完成，请回到机器人私聊重新连接。", status_code=400)
    try:
        finish_authorization(code, state)
    except AuthorizationRequired as exc:
        return PlainTextResponse(str(exc), status_code=400)
    return PlainTextResponse("个人日历已连接。后续任务自动写入，已有待同步任务会由 Worker 补齐。")


def start_server() -> None:
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(
        app, host=os.getenv("CALENDAR_AUTH_HOST", "127.0.0.1"),
        port=int(os.getenv("CALENDAR_AUTH_PORT", "8766")),
        access_log=False, log_level="warning",
    ))
    threading.Thread(target=server.run, name="calendar-auth", daemon=True).start()
