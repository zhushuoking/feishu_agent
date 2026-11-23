"""命令行工具：看库里到底存了什么。

用法：
    .venv\\Scripts\\python.exe show_db.py               # 候选 + 任务 + 通知概览
    .venv\\Scripts\\python.exe show_db.py --candidates  # 只看候选（按时间倒序）
    .venv\\Scripts\\python.exe show_db.py --tasks       # 只看任务
    .venv\\Scripts\\python.exe show_db.py --outbox      # 看通知发件箱（Day 12）
    .venv\\Scripts\\python.exe show_db.py --expire      # 手动跑一次候选过期（模拟 Worker）
    .venv\\Scripts\\python.exe show_db.py --confirm <uid>   # 手动确认一条候选，看建单效果

为什么要这个工具：Day 9 的卡片还没做，候选落库之后如果只能靠读 SQL 验证，
那等于没验证。先能看见，再谈自动化。
"""

import argparse
import os
import sys

import httpx
from dotenv import load_dotenv

import db
import repo
from models_db import CAND_PENDING, NOTIFY_FAILED


def _fmt_time(value) -> str:
    return value.strftime("%m-%d %H:%M") if value else "-"


def show_overview() -> None:
    print(f"数据库：{db.current_db_path()}")
    print("-" * 78)
    cand = repo.count_candidates_by_status()
    if cand:
        print("候选：" + "  ".join(f"{k}={v}" for k, v in sorted(cand.items())))
    else:
        print("候选：空")
    print(f"正式任务：{repo.count_tasks()}")
    notify_stats = repo.count_notifications_by_status()
    if notify_stats:
        print("通知：" + "  ".join(f"{k}={v}" for k, v in sorted(notify_stats.items())))
    else:
        print("通知：空（还没跑过 worker.py）")


def show_outbox(status=None, limit: int = 20) -> None:
    rows = repo.list_notifications(status=status, limit=limit)
    print(f"发件箱（status={status or '全部'}）共 {len(rows)} 条，最多显示 {limit} 条")
    print("=" * 78)
    for row in rows:
        print(f"[{row.uid[:8]}] {row.dedupe_key}")
        print(f"   状态={row.status}  尝试={row.attempts}  类型={row.kind}  "
              f"收件人={row.receive_id[:16]}…")
        print(f"   计划={_fmt_time(row.scheduled_at)}  发出={_fmt_time(row.sent_at)}  "
              f"内容={len(row.payload)} 字节")
        if row.last_error:
            print(f"   最后错误={row.last_error[:70]}")
        print()


def show_candidates(status=None, limit: int = 20) -> None:
    rows = repo.list_candidates(status=status, limit=limit)
    label = status or "全部"
    print(f"候选（status={label}）共 {len(rows)} 条，最多显示 {limit} 条")
    print("=" * 78)
    for cand in rows:
        print(f"[{cand.uid[:8]}] {cand.title}")
        print(f"   状态={cand.status}  置信度={cand.confidence:.2f}  类型={cand.candidate_type}")
        print(f"   截止={cand.deadline_at or '未解析'}  "
              f"原文={cand.deadline_text or '-'}  [{cand.deadline_status}]")
        print(f"   负责人={cand.assignee_open_ids or '无'}  缺失={cand.missing_fields or '无'}")
        print(f"   群={cand.chat_id[:12]}…  消息时间={_fmt_time(cand.message_sent_at)}  "
              f"过期={_fmt_time(cand.expires_at)}")
        if cand.clarification_question:
            print(f"   待澄清={cand.clarification_question[:60]}")
        print()


def show_tasks(limit: int = 20) -> None:
    rows = repo.list_tasks(limit=limit)
    print(f"正式任务共 {repo.count_tasks()} 条，最多显示 {limit} 条")
    print("=" * 78)
    for task in rows:
        flag = " [逾期]" if task.is_overdue else ""
        print(f"[{task.uid[:8]}] {task.title}{flag}")
        print(f"   状态={task.status}  v{task.version}  截止={_fmt_time(task.deadline_at)}")
        print(f"   负责人={task.owner_open_ids or '无'}  来自候选={task.candidate_uid or '-'}")
        print()


def _verified_operator() -> str:
    """由飞书验证当前用户身份，避免命令行直接自报 open_id。"""
    token = os.environ.get("FEISHU_USER_ACCESS_TOKEN", "").strip()
    if not token:
        raise ValueError("请使用飞书确认卡，或为当前终端设置 FEISHU_USER_ACCESS_TOKEN")
    try:
        response = httpx.get(
            "https://open.feishu.cn/open-apis/authen/v1/user_info",
            headers={"Authorization": f"Bearer {token}"}, timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") == 0:
            operator = (data.get("data") or {}).get("open_id")
            if isinstance(operator, str) and operator.startswith("ou_"):
                return operator
    except (httpx.HTTPError, ValueError):
        pass
    raise ValueError("飞书未能验证当前用户身份，请检查网络和用户访问令牌")


def main() -> None:
    parser = argparse.ArgumentParser(description="查看任务库")
    parser.add_argument("--candidates", action="store_true", help="只看候选")
    parser.add_argument("--tasks", action="store_true", help="只看任务")
    parser.add_argument("--outbox", action="store_true", help="看通知发件箱")
    parser.add_argument("--failed", action="store_true", help="只看发送失败的通知")
    parser.add_argument("--all", action="store_true", help="候选含非 pending 状态")
    parser.add_argument("--expire", action="store_true", help="跑一次候选过期")
    parser.add_argument("--confirm", metavar="UID", help="确认指定候选（前缀即可）")
    parser.add_argument("--reviewer", metavar="OPEN_ID", help="确认建单时指定验收人")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    load_dotenv()
    db.init_db()

    if args.expire:
        n = repo.expire_candidates()
        print(f"已把 {n} 条候选标记为 expired")
        return

    if args.confirm:
        uid = args.confirm
        try:
            operator = _verified_operator()
            candidates = repo.pending_candidates_by_prefix(uid)
            if len(candidates) != 1:
                raise ValueError("请提供唯一的待确认候选编号")
            task = repo.confirm_candidate(
                candidates[0].uid, operator_open_id=operator, reviewer_open_id=args.reviewer,
            )
            if task is None:
                raise ValueError("候选已被处理")
        except ValueError as exc:
            sys.exit(str(exc))
        print(f"已建单：{task.uid[:8]}  {task.title}")
        print(f"  状态={task.status}  负责人={task.owner_open_ids}  "
              f"截止={_fmt_time(task.deadline_at)}  {'[逾期]' if task.is_overdue else ''}")
        return

    if args.outbox or args.failed:
        show_outbox(status=NOTIFY_FAILED if args.failed else None, limit=args.limit)
        return
    if args.candidates or args.all:
        show_candidates(status=None if args.all else CAND_PENDING, limit=args.limit)
        return
    if args.tasks:
        show_tasks(limit=args.limit)
        return

    show_overview()


if __name__ == "__main__":
    main()
