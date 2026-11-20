"""Day 12：定时 Worker（对应设计文档 12.x）。

一个进程，每 60 秒 tick 一次，干三件事：

1. 把过期的候选标掉（Day 8 写的 expire_candidates 终于有人调了）
2. 算当天的日报，塞进发件箱
3. 把到点的通知发出去，失败的重试

为什么不用 APScheduler：
首期只有一个「每天 9 点」的活儿，`while True + sleep` 就够了。
多一个依赖就多一个版本冲突，等真要 cron 表达式、错峰、并发的时候再换。
这个判断标准值得记住：**定时任务的复杂度来自调度规则，不是来自循环本身。**

为什么和 main.py 分开进程：
main.py 扛着飞书长连接，是「实时」链路；Worker 是「定时」链路。
放一个进程里，长连接一重连或一卡住，定时任务跟着停；反过来 Worker 里
一个慢查询也会卡住消息接收。分开跑，互不拖累。

用法：
    .venv\\Scripts\\python.exe worker.py --once                  # 跑一轮就退出（调试）
    .venv\\Scripts\\python.exe worker.py                         # 常驻，60 秒一轮
    .venv\\Scripts\\python.exe worker.py --once --dry-run         # 只看要发什么，不真发
    .venv\\Scripts\\python.exe worker.py --once --now 2026-11-17T09:01  # 假装现在是这个时间
"""

import argparse
import json
import os
import sys
import time as time_module
from datetime import datetime
from typing import Callable, Optional

from dotenv import load_dotenv

load_dotenv()

import cards
import db
import notify
import repo

DEFAULT_INTERVAL = int(os.getenv("WORKER_INTERVAL_SECONDS", "60"))


def _default_sender(row) -> bool:
    """真正发出去的那一步。延迟 import：单测里不需要飞书 SDK。"""
    import feishu_api

    card = json.loads(row.payload)
    if row.receive_id_type == "chat_id":
        message_id = feishu_api.send_card_to_chat(row.receive_id, card)
        viewer = ""                      # 群卡是公共视角
    else:
        message_id = feishu_api.send_card_to_user(row.receive_id, card)
        viewer = row.receive_id          # 私聊卡按这个人渲染

    if not message_id:
        return False

    # 发出去就得记下来（Day 14）。不记的话 message_id 发完就丢了，
    # 这张卡永远停在发送时的状态 —— 用户翻上去看到的还是「阻塞」。
    _remember_card(row, message_id, viewer)
    return True


def _remember_card(row, message_id: str, viewer_open_id: str = "") -> None:
    """把发件箱里这条通知对应的卡片登记进 task_card。

    task_uid 为空的是日报，日报是「一人一份当天汇总」，没有后续状态变更，
    不需要同步，直接跳过。
    """
    task_uid = getattr(row, "task_uid", "") or ""
    if not task_uid:
        return
    try:
        repo.register_task_card(
            task_uid, message_id, row.receive_id, row.receive_id_type,
            viewer_open_id=viewer_open_id,
            kind=row.kind, version=int(getattr(row, "task_version", 0) or 0),
        )
    except Exception as exc:
        print(f"  [WARN] 卡片登记失败 {row.dedupe_key}：{type(exc).__name__}: {exc}")


def drain_outbox(now: Optional[datetime] = None,
                 sender: Optional[Callable] = None,
                 dry_run: bool = False,
                 limit: int = 50) -> dict:
    """把发件箱里到点的通知发出去。返回统计。

    sender 是参数不是写死的：单测里塞一个假函数就能验重试逻辑，
    不用真的发消息给自己。
    """
    now = now or db.now()
    stats = {"sent": 0, "failed": 0, "skipped": 0, "quiet": 0}

    if notify.in_quiet_hours(now):
        # 免打扰不是「丢掉」，是「先不发」：scheduled_at 不动，
        # 等 08:00 之后下一轮 tick 自然会捡起来。超过补发时限才放弃。
        stats["quiet"] = 1
        return stats

    rows = repo.claim_pending_notifications(now, limit)
    send = sender or _default_sender

    for row in rows:
        if notify.is_too_late(row.scheduled_at, now):
            repo.mark_notification_skipped(row.uid, "超过补发时限，不再打扰")
            stats["skipped"] += 1
            print(f"  [SKIP] {row.dedupe_key} 计划 {row.scheduled_at:%m-%d %H:%M} "
                  f"已经太晚")
            continue

        if dry_run:
            print(f"  [DRY ] {row.dedupe_key} → {row.receive_id[:12]}… "
                  f"({len(row.payload)} 字节)")
            continue

        try:
            ok = bool(send(row))
            error = "" if ok else "发送返回失败（凭证/可用范围/会话可能有问题）"
        except Exception as exc:  # sender 理论上不抛，兜一层
            ok, error = False, f"{type(exc).__name__}: {exc}"

        if ok:
            repo.mark_notification_sent(row.uid)
            stats["sent"] += 1
            print(f"  [SENT] {row.dedupe_key} → {row.receive_id[:12]}…")
        else:
            status = repo.mark_notification_failed(row.uid, error)
            stats["failed"] += 1
            print(f"  [FAIL] {row.dedupe_key} 第 {row.attempts + 1} 次：{error} "
                  f"→ {status}")

    return stats


def _enqueue_reminders(reminders, now, dry_run: bool, label: str) -> int:
    """把 plan_* 算出来的提醒塞进发件箱。返回真正入队的条数。

    卡片在**入队这一刻**渲染好，不是发送时渲染：中间状态可能已经变了，
    发送时再渲染会把「明天截止」改成「已完成」，人看得莫名其妙。
    这与 Day 12 日报的做法一致。
    """
    queued = 0
    for r in reminders:
        card = cards.build_task_card(r.task, r.receive_id, hint=r.hint)
        payload = cards.card_to_content(card)

        if dry_run:
            print(f"  [DRY ] {label} {r.receive_id[:12]}… "
                  f"task={r.task.uid[:8]} key={r.dedupe_key}")
            # dry-run 也要把「本来会入队几条」算进统计，
            # 否则屏幕上打了三行 [DRY]，末尾却显示「到期提醒 0」，看着像没生效
            queued += 1
            continue

        if repo.enqueue_notification(
            r.dedupe_key, r.receive_id, payload,
            kind=r.kind, scheduled_at=now,
            # 带上任务和版本：发成功后 Worker 会把这张卡登记进 task_card，
            # 状态一变也能跟着刷新（Day 14 的机制）
            task_uid=r.task.uid, task_version=r.task.version,
        ):
            queued += 1
            print(f"  [QUEUE] {label} {r.receive_id[:12]}… "
                  f"task={r.task.uid[:8]} {r.dedupe_key}")
    return queued


def tick(now: Optional[datetime] = None, dry_run: bool = False,
         force_plan: bool = False,
         sender: Optional[Callable] = None) -> dict:
    """一轮完整的活儿。返回统计，方便 main 里打印和测试断言。

    sender 一路透传到 drain_outbox：测试注入假发送函数，既能验重试逻辑，
    又不会真往飞书发消息。

    三类提醒的生成节奏不一样，别混：
    - 日报：每天 9 点之后才规划（should_plan）
    - 到期前 24h / 逾期：每轮都算，靠 dedupe_key 兜住重复，不受 9 点闸门限制
      （「周五 10:00 截止」的任务不该等到周六才提醒）
    """
    now = now or db.now()
    stats = {"expired": 0, "digests": 0, "enqueued": 0, "due": 0, "overdue": 0,
             "sent": 0, "failed": 0, "skipped": 0, "quiet": 0}

    stats["expired"] = repo.expire_candidates()

    if notify.should_plan(now) or force_plan:
        for digest in notify.plan_digests(now):
            stats["digests"] += 1
            card = cards.build_digest_card(digest, now)
            payload = cards.card_to_content(card)

            if dry_run:
                print(f"  [DRY ] 日报 {digest.open_id[:12]}… {digest.total} 条 "
                      f"({len(payload)} 字节) key={digest.dedupe_key}")
                continue

            if repo.enqueue_notification(
                digest.dedupe_key, digest.open_id, payload,
                kind="digest", scheduled_at=now,
            ):
                stats["enqueued"] += 1
                print(f"  [QUEUE] 日报 {digest.open_id[:12]}… {digest.total} 条 "
                      f"{digest.dedupe_key}")

    if notify.due_reminders_enabled():
        stats["due"] = _enqueue_reminders(
            notify.plan_due_reminders(now), now, dry_run, "到期提醒"
        )

    if notify.overdue_reminders_enabled():
        stats["overdue"] = _enqueue_reminders(
            notify.plan_overdue_reminders(
                now, last_sent=repo.last_overdue_sent_times()
            ),
            now, dry_run, "逾期提醒",
        )

    stats.update(drain_outbox(now, sender=sender, dry_run=dry_run))
    return stats


def _parse_now(text: str) -> datetime:
    from models import TZ_SHANGHAI

    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_SHANGHAI)
    return dt


def main() -> None:
    parser = argparse.ArgumentParser(description="定时提醒 Worker")
    parser.add_argument("--once", action="store_true", help="只跑一轮就退出")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                        help=f"循环间隔秒数（默认 {DEFAULT_INTERVAL}）")
    parser.add_argument("--now", help="假装现在是这个时间，如 2026-11-17T09:01")
    parser.add_argument("--dry-run", action="store_true", help="不真发，只打印")
    parser.add_argument("--force-plan", action="store_true",
                        help="无视 09:00 之前不生成日报的限制")
    args = parser.parse_args()

    db.init_db()

    fixed_now = _parse_now(args.now) if args.now else None
    if args.now and not args.once:
        print("--now 只能配 --once 用（循环里时间是流动的）")
        sys.exit(2)

    print(f"Worker 启动：间隔 {args.interval}s，数据库 {db.current_db_path()}")
    print(f"日报时段：{notify.SEND_HOUR}:00 之后　免打扰："
          f"{notify.QUIET_START_HOUR}:00-{notify.QUIET_END_HOUR}:00　"
          f"补发上限：{notify.MAX_DELAY_HOURS} 小时")
    print(f"到期提醒：{'开' if notify.due_reminders_enabled() else '关'}（前 24 小时）　"
          f"逾期提醒：{'开' if notify.overdue_reminders_enabled() else '关'}"
          f"（每 {notify.OVERDUE_INTERVAL_WORKDAYS} 个工作日）")

    if args.once:
        stats = tick(fixed_now, dry_run=args.dry_run, force_plan=args.force_plan)
        print(f"本轮：过期候选 {stats['expired']}　日报 {stats['digests']} 条 "
              f"（入队 {stats['enqueued']}）　到期提醒 {stats['due']}　"
              f"逾期提醒 {stats['overdue']}　发送 {stats['sent']}　"
              f"失败 {stats['failed']}　放弃 {stats['skipped']}"
              + ("　（免打扰时段，跳过发送）" if stats["quiet"] else ""))
        return

    while True:
        try:
            stats = tick(dry_run=args.dry_run, force_plan=args.force_plan)
            if any(stats.values()):
                print(f"[{db.now():%H:%M:%S}] 过期 {stats['expired']}　"
                      f"日报 {stats['digests']}　入队 {stats['enqueued']}　"
                      f"到期 {stats['due']}　逾期 {stats['overdue']}　"
                      f"发送 {stats['sent']}　失败 {stats['failed']}")
        except Exception as exc:  # 一轮出错不能让 Worker 死掉
            print(f"[ERROR] tick 失败：{type(exc).__name__}: {exc}")
        time_module.sleep(args.interval)


if __name__ == "__main__":
    main()
