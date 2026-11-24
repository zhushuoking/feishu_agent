"""Day 16：任务 ↔ 飞书日历同步。

【链路】
确认建单 → 在应用主日历建一个 [截止前 15min, 截止] 的占用块 →
           把负责人和发起人加为参与人（两人日历里就出现这条日程）
状态流转 → 更新中文状态；验收完成 → 标记已完成并清除提醒
取消任务 → DELETE 删掉日程
Worker → 补偿失败操作，按最新任务状态同步时间和状态

【三条硬规则】
1. 全程后台线程。飞书等 3 秒不响应就重推回调，网络请求不能塞进回调里。
2. 全程不抛异常、不回滚业务。建单/状态流转**已经成功**了，日历挂了
   只能记一行失败日志，不能让用户的按钮变成「点了没反应」。
3. 幂等。回调可能被重推、两台机器可能同时跑、网络可能重试。
   靠 task_calendar_event 的 task_uid 唯一约束 + 固定 idempotency_key
   双保险：先在库里占一行，再带固定 key 调创建接口，飞书侧也不会重复建。

【为什么不建在用户主日历】
项目只有 tenant_access_token，没有 user_access_token 的 OAuth 流程。
应用对个人主日历没有 writer 权限，硬写报 191002 no calendar access_role。
应用自己的主日历 role=owner，把人加为参与人后效果一样。
"""

import json
import os
import sqlite3
import threading
from html import escape
from typing import Any, Dict, Optional

import db
import permissions
import repo
from state_machine import STATUS_LABELS
from models_db import CAL_CREATED, CAL_DELETED, CAL_FAILED, CAL_SKIPPED, TASK_CANCELLED, TASK_DONE, TASK_NEEDS_INFO, TASK_TODO

DEFAULT_LEAD_MINUTES = 15
COMPLETED_PREFIX = "已完成｜"


class PersonalSyncError(RuntimeError):
    pass


def _enabled() -> bool:
    """默认关。权限没发版时主链路必须完全不受影响。

    调用时读环境变量（跟 card_sync 一样），不能 import 时读死，
    否则测试用 monkeypatch 关不掉。
    """
    return os.getenv("CALENDAR_SYNC_ENABLED", "0").strip() != "0"


def _lead_minutes() -> int:
    try:
        return max(1, int(os.getenv("CALENDAR_LEAD_MINUTES",
                                    str(DEFAULT_LEAD_MINUTES))))
    except ValueError:
        return DEFAULT_LEAD_MINUTES


def _reminder_minutes() -> Optional[int]:
    """飞书 reminders[].minutes 是「相对日程开始时间提前多少分钟」。

    日程开始已经是 deadline-15min，所以 0 才正好落在截止前 15 分钟；
    设 15 会变成提前 30 分钟。某些租户不接受 0，可以设 -1 干脆不传提醒，
    只靠日程占用块 + 加参与人时的通知。
    """
    raw = os.getenv("CALENDAR_REMINDER_MINUTES", "0").strip()
    try:
        value = int(raw)
    except ValueError:
        return 0
    return None if value < 0 else value


def _time_info(ts: int) -> Dict[str, str]:
    """飞书 v4 的 time_info：timestamp 是**秒级字符串**，不是毫秒、不是整数。"""
    return {"timestamp": str(int(ts)), "timezone": "Asia/Shanghai"}


def _description(task, completed: bool = False,
                 source_message_id: str = "") -> str:
    """日程说明。open_id 不是人名，但我们手里只有 open_id —— 宁可写 ID，
    也不要去猜姓名或者塞一个假的 @ 标签（日历 description 只解析 HTML）。
    """
    lines = [
        f"截止时间：{task.deadline_at.strftime('%Y-%m-%d %H:%M')} (Asia/Shanghai)"
        if task.deadline_at else "截止时间：未明确",
        f"负责人：{'、'.join(task.owner_open_ids) or '未指定'}",
        f"发起人：{task.creator_open_id or '-'}",
    ]
    if task.deliverable:
        lines.append(f"交付物：{task.deliverable}")
    if task.acceptance_criteria:
        lines.append(f"验收标准：{task.acceptance_criteria}")
    if task.description:
        lines.append(f"说明：{task.description}")
    if task.completion_note:
        lines.append(f"完成说明：{task.completion_note}")
    if source_message_id:
        lines.append(f"原消息 ID：{source_message_id}")
    lines.append(f"来源群：{task.chat_id or '-'}")
    lines.append(f"任务编号：{task.uid}")
    lines.append("验收结果：已完成" if completed else
                 f"当前状态：{STATUS_LABELS.get(task.status, task.status)}")
    return "<br>".join(escape(line) for line in lines)


def build_event_body(task, lead_minutes: Optional[int] = None,
                     source_message_id: str = "",
                     completed: bool = False) -> Optional[Dict[str, Any]]:
    """纯函数：任务 → 飞书「创建日程」请求体。

    没有明确截止时间就返回 None。时间解析器把「尽快 / 抽空 / 已过点」
    判成 unresolved，这种任务只建任务卡；硬建会得到一个 1970 年的日程。
    """
    if task is None or task.deadline_at is None or task.status == TASK_NEEDS_INFO:
        return None

    lead = _lead_minutes() if lead_minutes is None else lead_minutes
    end_ts = int(task.deadline_at.timestamp())
    start_ts = end_ts - lead * 60
    completed = completed or task.status == TASK_DONE
    label = STATUS_LABELS.get(task.status, task.status)
    summary = task.title if task.status == TASK_TODO else f"{label}｜{task.title}"
    if completed:
        summary = f"{COMPLETED_PREFIX}{task.title}"

    body: Dict[str, Any] = {
        "summary": summary,
        "description": _description(task, completed, source_message_id),
        "start_time": _time_info(start_ts),
        "end_time": _time_info(end_ts),
        "free_busy_status": "free" if completed else "busy",
        # 参与人只能看不能改：有人在客户端手改日程，就会和任务状态不一致
        "attendee_ability": "can_see_others",
        "visibility": "default",
        # 建单消息由任务卡发送，日历只同步日程和到期提醒。
        "need_notification": False,
    }
    reminder = _reminder_minutes()
    if completed:
        body["reminders"] = []
    elif reminder is not None:
        body["reminders"] = [{"minutes": reminder}]
    return body


def _calendar_id(api) -> str:
    """优先用 .env 里配的，留空则自动查应用主日历。"""
    configured = os.getenv("FEISHU_CALENDAR_ID", "").strip()
    if configured:
        return configured
    return api.primary_calendar_id() or ""


def _source_message_id(task) -> str:
    """原消息 ID 存在候选上，任务表只留了 candidate_uid。

    飞书没有可靠的公开消息永久链接，所以只把 message_id 写进说明，
    不硬拼一个可能点不开的 URL。
    """
    if not task.candidate_uid:
        return ""
    cand = repo.get_candidate(task.candidate_uid)
    return cand.message_id if cand else ""


def _default_api():
    import calendar_api  # 延迟导入：单测注入假 api 时不需要 httpx

    return calendar_api


def _api_error(api, message: str) -> str:
    detail = getattr(api, "last_error", lambda: "")()
    return f"{message}：{detail}" if detail else message


# ------------------------------------------------------------------ 同步与补偿


def sync_task(task_uid: str, api=None, accounts=None) -> Dict[str, int]:
    """按最新任务同步。跨线程、跨进程互斥，失败留到 Worker 重试。"""
    stats = {"created": 0, "attendees": 0, "updated": 0,
             "deleted": 0, "skipped": 0, "failed": 0}
    lock = sqlite3.connect(db.current_db_path() + ".calendar-lock.db", timeout=0)
    try:
        try:
            lock.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                stats["skipped"] = 1
                return stats
            raise
        try:
            if os.getenv("CALENDAR_SYNC_MODE", "application") == "personal":
                if accounts is None:
                    import calendar_auth
                    accounts = calendar_auth
                _sync_personal_task(task_uid, api or _default_api(), accounts, stats)
            else:
                _sync_task(task_uid, api or _default_api(), stats)
        except Exception as exc:
            row = repo.get_calendar_event(task_uid)
            if row:
                repo.defer_calendar_sync(task_uid, f"{type(exc).__name__}: {exc}")
            stats["failed"] += 1
            print(f"[CAL] task={task_uid[:8]} 同步异常：{type(exc).__name__}: {exc}")
        return stats
    finally:
        lock.rollback()
        lock.close()


def _sync_task(task_uid: str, api, stats) -> None:
    task = repo.get_task(task_uid)
    if task is None:
        stats["skipped"] = 1
        return
    row = repo.get_calendar_event(task_uid)
    if row is None:
        row = repo.reserve_calendar_event(task_uid, "", task.deadline_at)

    # 创建响应丢失时，远端可能已成功。先恢复 event_id，再按最新状态更新或删除。
    if not row.event_id and row.request_body:
        event_id = api.create_event(row.calendar_id, json.loads(row.request_body), row.idempotency_key)
        if not event_id:
            repo.mark_calendar_event_failed(task_uid, _api_error(api, "恢复日程创建结果失败"))
            stats["failed"] += 1
            return
        repo.mark_calendar_event_created(task_uid, event_id)
        row = repo.get_calendar_event(task_uid)
        stats["created"] += 1
        task = repo.get_task(task_uid)

    # 取消、完成和无截止时间的任务不会补建日程。
    if task.status in (TASK_CANCELLED, TASK_NEEDS_INFO) or task.deadline_at is None or (
            task.status == TASK_DONE and not row.event_id):
        if row.event_id and row.status == CAL_CREATED:
            if not api.delete_event(row.calendar_id, row.event_id):
                repo.defer_calendar_sync(task_uid, _api_error(api, "删除日程失败"))
                stats["failed"] += 1
                return
            stats["deleted"] += 1
        if task.status == TASK_CANCELLED:
            repo.mark_calendar_event_deleted(task_uid)
        else:
            repo.clear_calendar_event(task_uid)
        repo.finish_calendar_sync(task_uid, task.version, task.deadline_at)
        stats["skipped"] += 1
        return

    if row.status == CAL_DELETED:
        stats["skipped"] += 1
        return

    created = False
    if not row.event_id:
        calendar_id = row.calendar_id or _calendar_id(api)
        row = repo.reserve_calendar_event(task_uid, calendar_id, task.deadline_at)
        if not row.calendar_id:
            repo.mark_calendar_event_failed(task_uid, _api_error(api, "拿不到 calendar_id（检查日历配置和权限）"))
            stats["failed"] += 1
            return
        body = build_event_body(task, source_message_id=_source_message_id(task))
        request_body = repo.prepare_calendar_request(task_uid, json.dumps(body, ensure_ascii=False))
        event_id = api.create_event(row.calendar_id, json.loads(request_body), row.idempotency_key)
        if not event_id:
            repo.mark_calendar_event_failed(task_uid, _api_error(api, "创建日程接口失败"))
            stats["failed"] += 1
            return
        repo.mark_calendar_event_created(task_uid, event_id)
        row = repo.get_calendar_event(task_uid)
        stats["created"] += 1
        created = True
        # 网络调用期间可能已提交、验收或取消，重新读取后再邀请参与人。
        fresh = repo.get_task(task_uid)
        if fresh.version != task.version or fresh.deadline_at != task.deadline_at:
            _sync_task(task_uid, api, stats)
            return

    needs_update = row.synced_version < task.version or row.deadline_at != task.deadline_at
    if needs_update and not created:
        body = build_event_body(task, source_message_id=_source_message_id(task))
        patch = {key: body[key] for key in (
            "summary", "description", "start_time", "end_time", "free_busy_status", "reminders"
        ) if key in body}
        patch["reminders"] = body.get("reminders", [])
        if not api.update_event(row.calendar_id, row.event_id, patch):
            repo.defer_calendar_sync(task_uid, _api_error(api, "更新日程失败"))
            stats["failed"] += 1
            return
        stats["updated"] += 1

    if not row.attendees_synced:
        recipients = [user for user in dict.fromkeys([
            *task.owner_open_ids, task.creator_open_id or "",
            task.effective_reviewer_open_id or "",
        ]) if user]
        if not api.add_attendees(row.calendar_id, row.event_id, recipients):
            repo.defer_calendar_sync(task_uid, _api_error(api, "日程已建，参与人添加失败"))
            stats["failed"] += 1
            return
        repo.mark_calendar_event_attendees_synced(task_uid)
        stats["attendees"] += len(recipients)

    repo.finish_calendar_sync(task_uid, task.version, task.deadline_at)
    if not created and not needs_update and not stats["attendees"]:
        stats["skipped"] += 1


def _sync_personal_task(task_uid: str, legacy_api, accounts, stats) -> None:
    task = repo.get_task(task_uid)
    if task is None:
        stats["skipped"] += 1
        return
    aggregate = repo.get_calendar_event(task_uid) or repo.reserve_calendar_event(task_uid, "", task.deadline_at)
    recipients = {person for person in [*task.owner_open_ids, task.creator_open_id,
                                       task.effective_reviewer_open_id] if person}
    existing = {row.open_id: row for row in repo.list_personal_calendar_events(task_uid)}
    errors = []
    for open_id in sorted(recipients | existing.keys()):
        row = existing.get(open_id) or repo.reserve_personal_calendar_event(task_uid, open_id)
        try:
            _sync_personal_event(task, open_id in recipients, row, accounts, stats)
        except Exception as exc:
            # 凭证及 token 不进入错误文本，仅使用受控的授权提示或异常类型。
            from calendar_auth import AuthorizationRequired
            error = str(exc) if isinstance(exc, (AuthorizationRequired, PersonalSyncError)) else f"个人日历同步异常（{type(exc).__name__}）"
            repo.update_personal_calendar_event(row.id, last_error=error[:200], status=CAL_FAILED)
            errors.append(f"{open_id[:12]} {error}")
            stats["failed"] += 1
    if errors:
        repo.defer_calendar_sync(task_uid, "；".join(errors))
        return

    # 旧创建请求超时后可能已在远端成功，恢复 ID 后再清理。
    if aggregate.sync_mode != "personal" and aggregate.request_body and not aggregate.event_id:
        recovered = legacy_api.create_event(aggregate.calendar_id, json.loads(aggregate.request_body),
                                             aggregate.idempotency_key)
        if not recovered:
            repo.defer_calendar_sync(task_uid, "恢复旧日程创建结果失败")
            stats["failed"] += 1
            return
        repo.mark_calendar_event_created(task_uid, recovered)
        aggregate = repo.get_calendar_event(task_uid)
    # 已有任务从旧邀请模式迁移时，个人副本全部成功后清理原应用日程。
    if aggregate.event_id and aggregate.status != CAL_DELETED:
        if not legacy_api.delete_event(aggregate.calendar_id, aggregate.event_id):
            repo.defer_calendar_sync(task_uid, _api_error(legacy_api, "清理旧邀请日程失败"))
            stats["failed"] += 1
            return
        repo.mark_calendar_event_deleted(task_uid)
    elif not aggregate.event_id:
        repo.mark_calendar_event_created(task_uid, "")
    repo.mark_calendar_event_attendees_synced(task_uid)
    repo.mark_calendar_sync_mode(task_uid, "personal")
    repo.finish_calendar_sync(task_uid, task.version, task.deadline_at)


def _sync_personal_event(task, is_recipient: bool, row, accounts, stats) -> None:
    remove = not is_recipient or task.status in (TASK_CANCELLED, TASK_NEEDS_INFO) or task.deadline_at is None
    if not row.event_id and not row.request_body and (remove or task.status == TASK_DONE):
        repo.update_personal_calendar_event(row.id, status=CAL_SKIPPED,
                                            synced_version=task.version, deadline_at=task.deadline_at,
                                            last_error=None)
        stats["skipped"] += 1
        return
    calendar_id, api = accounts.api_for_user(row.open_id)
    if row.calendar_id and row.calendar_id != calendar_id:
        raise PersonalSyncError("个人主日历发生变化，请检查账号日历配置")
    if not row.calendar_id:
        repo.update_personal_calendar_event(row.id, calendar_id=calendar_id)
    event_id = row.event_id
    created = False
    if not event_id:
        body = json.loads(row.request_body) if row.request_body else build_event_body(
            task, source_message_id=_source_message_id(task),
        )
        repo.update_personal_calendar_event(row.id, request_body=json.dumps(body, ensure_ascii=False))
        event_id = api.create_event(calendar_id, body, row.idempotency_key)
        if not event_id:
            raise PersonalSyncError(_api_error(api, "创建个人日程失败"))
        repo.update_personal_calendar_event(row.id, event_id=event_id, status=CAL_CREATED)
        stats["created"] += 1
        created = not row.request_body
    if remove:
        if row.status != CAL_DELETED and not api.delete_event(calendar_id, event_id):
            raise PersonalSyncError(_api_error(api, "清理个人日程失败"))
        # 清除截止后重新补齐允许创建新日程，已取消任务不会被补建。
        import uuid
        repo.update_personal_calendar_event(
            row.id, event_id="", request_body=None, status=CAL_SKIPPED,
            idempotency_key="task-personal-" + uuid.uuid4().hex,
            synced_version=task.version, deadline_at=task.deadline_at, last_error=None,
        )
        stats["deleted"] += 1
        return
    if not created and (row.synced_version != task.version or row.deadline_at != task.deadline_at):
        body = build_event_body(task, source_message_id=_source_message_id(task))
        patch = {key: body[key] for key in ("summary", "description", "start_time", "end_time", "free_busy_status")}
        patch["reminders"] = body.get("reminders", [])
        patch["need_notification"] = False
        if not api.update_event(calendar_id, event_id, patch):
            raise PersonalSyncError(_api_error(api, "更新个人日程失败"))
        stats["updated"] += 1
    repo.update_personal_calendar_event(row.id, status=CAL_CREATED, synced_version=task.version,
                                        deadline_at=task.deadline_at, last_error=None)


def sync_created(task_uid: str, api=None) -> Dict[str, int]:
    return sync_task(task_uid, api)


def sync_transition(task_uid: str, action: str, api=None) -> Dict[str, int]:
    # action 仅是触发原因，渲染和删除始终依据当前数据库状态。
    return sync_task(task_uid, api)


def sync_deadline_changed(task_uid: str, api=None) -> Dict[str, int]:
    return sync_task(task_uid, api)


def reconcile(now=None, api=None, limit: int = 50, dry_run: bool = False) -> Dict[str, int]:
    """Worker 定时补建、重试和追平版本，关闭开关或 dry-run 时不写库。"""
    stats = {"checked": 0, "failed": 0}
    if not _enabled():
        return stats
    chat_ids = [chat.strip() for chat in os.getenv("FEISHU_ALLOWED_CHAT_IDS", "").split(",")
                if permissions.chat_enabled(chat.strip())]
    for task_uid in repo.pending_calendar_tasks(now or db.now(), limit, chat_ids=chat_ids,
                                               mode=os.getenv("CALENDAR_SYNC_MODE", "application")):
        task = repo.get_task(task_uid)
        if task is None or not permissions.chat_enabled(task.chat_id):
            continue
        stats["checked"] += 1
        if not dry_run:
            stats["failed"] += sync_task(task_uid, api)["failed"]
    return stats


# ------------------------------------------------------------------ 后台入口

def _spawn(name: str, fn) -> None:
    if not _enabled():
        return

    def _run() -> None:
        try:
            fn()
        except Exception as exc:  # 日历失败绝不能冒到业务层
            print(f"[CAL] {name} 异常：{type(exc).__name__}: {exc}")

    threading.Thread(target=_run, name=name, daemon=True).start()


def on_created_async(task_uid: str) -> None:
    _spawn(f"cal-create-{task_uid[:8]}", lambda: sync_created(task_uid))


def on_transition_async(task_uid: str, action: str) -> None:
    _spawn(f"cal-{action}-{task_uid[:8]}",
           lambda: sync_transition(task_uid, action))


def on_deadline_changed_async(task_uid: str) -> None:
    _spawn(f"cal-resched-{task_uid[:8]}",
           lambda: sync_deadline_changed(task_uid))


# ------------------------------------------------------------------ 诊断 CLI

def _main() -> None:
    """手动验证 / 补建。用户刚开完权限，第一步永远是 --check。

        .venv\\Scripts\\python.exe calendar_sync.py --check
        .venv\\Scripts\\python.exe calendar_sync.py --list
        .venv\\Scripts\\python.exe calendar_sync.py --task <uid前8位>
        .venv\\Scripts\\python.exe calendar_sync.py --task <uid> --apply
        .venv\\Scripts\\python.exe calendar_sync.py --task <uid> --action cancel --apply
        .venv\\Scripts\\python.exe calendar_sync.py --task <uid> --action reschedule --apply
    """
    import argparse
    import json

    from dotenv import load_dotenv

    load_dotenv()

    parser = argparse.ArgumentParser(description="任务 ↔ 飞书日历同步")
    parser.add_argument("--check", action="store_true",
                        help="验凭证 / 权限 / 主日历，不写任何数据")
    parser.add_argument("--create-calendar", action="store_true",
                        help="建一个共享日历并打印 ID（绕开查主日历需要的只读权限）")
    parser.add_argument("--list", action="store_true", help="列出日程映射")
    parser.add_argument("--task", help="任务 uid（可以只给前 8 位）")
    parser.add_argument("--apply", action="store_true",
                        help="真的调飞书接口；不给就只打印计划")
    parser.add_argument("--action", default="create", help="兼容旧参数，实际按任务当前状态同步",
                        choices=["create", "accept", "cancel", "reschedule"])
    args = parser.parse_args()

    db.init_db()

    import calendar_api

    if args.create_calendar:
        name = os.getenv("CALENDAR_NAME", "任务跟进")
        calendar_id = calendar_api.create_calendar(name)
        if not calendar_id:
            print("[FAIL] 创建共享日历失败，看上面的 [CAL] 日志")
            return
        print(f"[OK] 已创建共享日历「{name}」")
        print(f"  calendar_id : {calendar_id}")
        print("  把它填进 .env 的 FEISHU_CALENDAR_ID，之后建日程不再需要查主日历")
        return

    if args.check:
        if os.getenv("CALENDAR_SYNC_MODE", "application") == "personal":
            from models_db import CalendarAccount
            from sqlalchemy import select
            with db.session_scope() as session:
                accounts = list(session.scalars(select(CalendarAccount)).all())
            print(f"个人日历模式，已连接 {len(accounts)} 个账号")
            print("每人首次在机器人私聊发送“连接日历”，完成授权后 Worker 自动补齐日程。")
            return
        info = calendar_api.check()
        if not info.get("ok"):
            print(f"[FAIL] {info.get('error')}")
            print(
                "\n排查顺序：\n"
                "  1) calendar:calendar（更新日历及日程信息）已开通并创建版本发布\n"
                "     （高级权限可能还要企业管理员审核）\n"
                "  2) 【最常见】查主日历另需一个**只读**权限，三选一：\n"
                "     calendar:calendar:readonly / calendar:calendar.calendar:readonly /\n"
                "     calendar:calendar:read —— 权限管理里搜「获取日历、日程及忙闲信息」\n"
                "     或「读取日历信息」，勾上后同样要再发一次版本\n"
                "  3) 应用已开启机器人能力、App ID/Secret 正确\n"
                "  4) 不想再发版：跑 --create-calendar 建共享日历，\n"
                "     把返回的 ID 填进 FEISHU_CALENDAR_ID，只用写权限即可\n"
                "  5) 如果 FEISHU_CALENDAR_ID 已经填好，这一步报错不影响建日程，\n"
                "     可以直接用 --task <uid> --apply 试一次"
            )
            return
        print("[OK] 日历权限可用")
        print(f"  calendar_id : {info['calendar_id']}")
        print(f"  日历名      : {info['summary']}")
        print(f"  role / type : {info['role']} / {info['type']}")
        print(f"  机器人      : {info['bot_open_id'][:16]}…")
        print("\n把 calendar_id 填进 .env 的 FEISHU_CALENDAR_ID 可以少一次查询；"
              "留空会自动查。")
        return

    if args.list or not args.task:
        rows = repo.list_calendar_events(limit=30)
        print(f"数据库：{db.current_db_path()}")
        print(f"日程映射 {len(rows)} 条：")
        for row in rows:
            print(f"  {row.task_uid[:8]}  {row.status:<8} "
                  f"event={row.event_id[:16] or '-':<16} "
                  f"v={row.synced_version} fail={row.fail_count} "
                  f"retry={row.retry_at or '-'}  {row.last_error or ''}")
            for personal in repo.list_personal_calendar_events(row.task_uid):
                print(f"    个人 {personal.open_id[:12]}… {personal.status} "
                      f"v{personal.synced_version} {personal.last_error or ''}")
        print("\n加 --task <uid 前 8 位> 查看某条任务的日程计划")
        return

    matched = [t for t in repo.list_tasks(limit=300)
               if t.uid == args.task or t.uid.startswith(args.task)]
    if not matched:
        print(f"找不到任务：{args.task}")
        return
    if len(matched) > 1:
        print(f"前缀匹配到 {len(matched)} 条任务，请给更长的 uid")
        return

    task = matched[0]
    print(f"任务 {task.uid}  {task.status}  {task.title}")
    print(f"截止：{task.deadline_at or '未明确'}")

    row = repo.get_calendar_event(task.uid)
    print(f"日程：{row.event_id if row else '未创建'}")
    if task.status == TASK_CANCELLED or task.deadline_at is None:
        print("同步计划：清理已有日程")
    elif task.status == TASK_DONE and (row is None or not row.event_id):
        print("同步计划：已完成任务跳过补建")
    else:
        print("同步目标：")
        print(json.dumps(build_event_body(task, source_message_id=_source_message_id(task)),
                         ensure_ascii=False, indent=2))
    if not args.apply:
        print("\n[dry-run] 加 --apply 才执行同步")
        return
    stats = sync_task(task.uid)
    print(f"结果：{stats}")


if __name__ == "__main__":
    _main()
