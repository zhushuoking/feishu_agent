"""Day 14：旧卡片同步 —— 让历史卡片不再撒谎。

【问题】
飞书的卡片消息发出去就定住了。用户点了按钮，飞书会用回调返回的 card
**替换被点的那一张**，其它已经发出去的卡片完全不动。

于是出现用户的原话：
    「我解除阻塞后，任务发送方还显示任务阻塞」

时间线是这样的（A = 发起人，B = 负责人）：

    1. A 建单          → 给 B 推任务卡 M1（待开始）
    2. B 点「开始处理」  → M1 变成进行中
    3. B 点「报阻塞」    → M1 变阻塞；事件通知给 A 发新卡 M2（阻塞）
    4. B 点「解除阻塞」  → M1 变进行中；事件通知给 A 发新卡 M3（进行中）
    5. A 往上翻聊天记录 → M2 还是红的「阻塞」

通知没发错，是**旧消息在撒谎**。这比不发通知更坏：发通知的目的是同步认知，
结果一个说阻塞一个说进行中，人只能都别信。

【做法】
发卡时登记 message_id，状态一变回头改掉它们。
流程：发送成功 → repo.register_task_card 记账 → 状态流转 → card_sync 批量 PATCH

【为什么不「只更新一张卡」】
只更新不新建也不行：PATCH 不会让消息重新置顶，也不会有红点提醒，
A 永远不会知道 B 提交了验收。所以新消息照发（负责提醒），
旧卡片照刷（负责不误导），两者是不同的职责。

【为什么不把这些卡「撤回」】
撤回会在聊天记录里留下「xx 撤回了一条消息」，比一张写错的卡更让人起疑。
卡片自带状态，改成正确的就够了。
"""

import os
from typing import Callable, Dict, Optional

import cards
import db
import repo

# 同步开关。测试里必须关掉：它会开线程去调飞书接口。
# 注意要在调用时读环境变量，不能在 import 时读死 —— 测试是用
# monkeypatch.setenv 关它的，模块早就 import 过了。
def _enabled() -> bool:
    return os.getenv("CARD_SYNC_ENABLED", "1").strip() != "0"


def _stale_hint():
    """刷新后的卡上要不要加一句说明。

    默认不加：「历史消息」这种字样对用户没有信息量，卡上状态和按钮本身
    已经是新的了。留着旧 hint（比如「把任务标记为阻塞」）才是真正误导人的东西。
    """
    return os.getenv("CARD_SYNC_HINT", "").strip() or None


def sync_task(task_uid: str, updater: Optional[Callable] = None,
              limit: int = repo.MAX_LIVE_CARDS) -> Dict[str, int]:
    """把一条任务的所有活卡片刷成当前状态。返回统计。

    updater 是参数不是写死的（和 worker 的 sender 一个套路）：
    单测里塞一个假函数就能验「该刷哪几张、刷成什么样」，
    不用真往飞书发请求，也不用 mock 掉整个 SDK。

    只刷「落后于当前版本」的卡：
    - 刚发出去那张新卡，synced_version 已经等于 task.version → 跳过
    - 被点的那张卡由回调响应更新，飞书侧也已经是新的 → 版本也追平了
      （transition 前它记的是旧版本，所以这里仍会再刷一次，内容一样，
       多一次请求但绝不会刷成错的 —— 宁可多发一次，不可漏发）
    """
    stats = {"checked": 0, "updated": 0, "failed": 0, "skipped": 0}

    task = repo.get_task(task_uid)
    if task is None:
        return stats

    rows = repo.list_live_task_cards(task_uid, limit=limit)
    stats["checked"] = len(rows)
    if not rows:
        return stats

    update = updater or _default_updater

    for row in rows:
        if (row.synced_version or 0) >= task.version:
            stats["skipped"] += 1
            continue

        # 按这张卡原本的视角重新渲染：
        # 私聊卡按那个人的权限裁按钮，群卡保持公共视角。
        # 拿错视角的后果很具体 —— 群里会出现一张「按某个人权限裁剪」的卡，
        # 别人看着自己没有的按钮，以为系统坏了。
        viewer = row.viewer_open_id or None
        card = cards.build_task_card(task, viewer, hint=_stale_hint())

        try:
            ok = bool(update(row.message_id, card))
        except Exception as exc:  # updater 理论上不抛，兜一层
            ok, error = False, f"{type(exc).__name__}: {exc}"
            print(f"[CARD] 同步异常 message_id={row.message_id[:12]}… {error}")
        else:
            error = "" if ok else "更新返回失败"

        if ok:
            repo.mark_task_card_synced(row.message_id, task.version)
            stats["updated"] += 1
        else:
            repo.mark_task_card_failed(row.message_id, error)
            stats["failed"] += 1

    return stats


def _default_updater(message_id: str, card) -> bool:
    """真正打接口的那一步。延迟 import：单测不需要飞书 SDK。"""
    import feishu_api

    return feishu_api.update_card(message_id, card)


def sync_async(task_uid: str, version: Optional[int] = None) -> None:
    """后台线程同步。version 只用来打日志，实际以库里最新的为准。

    为什么不在回调里同步做：一次要改十几张卡，每张都是网络请求，
    飞书等 3 秒不响应就重推，重推会让同一张卡被连点两次。
    """
    if not _enabled():
        return

    def _run() -> None:
        try:
            stats = sync_task(task_uid)
            if stats["updated"] or stats["failed"]:
                print(f"[CARD] 旧卡片同步 task={task_uid[:8]} "
                      f"改了 {stats['updated']} 张，失败 {stats['failed']} 张，"
                      f"跳过 {stats['skipped']} 张")
        except Exception as exc:  # 同步失败绝不影响业务
            print(f"[CARD] 旧卡片同步异常 task={task_uid[:8]} "
                  f"{type(exc).__name__}: {exc}")

    import threading

    threading.Thread(target=_run, name=f"card-sync-{task_uid[:8]}", daemon=True).start()


# ---------------------------------------------------------------- 诊断 / 手动补刷

def _main() -> None:
    """手动补刷历史卡片。改完代码想验证、或者之前同步失败了一批，用它。

        .venv\\Scripts\\python.exe card_sync.py --list
        .venv\\Scripts\\python.exe card_sync.py --task <任务 uid 前 8 位>
        .venv\\Scripts\\python.exe card_sync.py --task <uid> --dry-run
    """
    import argparse

    from dotenv import load_dotenv

    load_dotenv()

    parser = argparse.ArgumentParser(description="旧卡片同步")
    parser.add_argument("--task", help="任务 uid（可以只给前 8 位）")
    parser.add_argument("--list", action="store_true", help="列出最近的任务和卡片数")
    parser.add_argument("--dry-run", action="store_true", help="只打印要改几张，不真发")
    args = parser.parse_args()

    db.init_db()
    print(f"数据库：{db.current_db_path()}")

    if args.list or not args.task:
        for t in repo.list_tasks(limit=15):
            print(f"  {t.uid[:8]}  v{t.version:<3} {t.status:<20} {t.title[:24]}")
        print("\n加 --task <uid 前 8 位> 可以看这条任务的卡片并补刷")
        return

    # 支持只给前 8 位：手抄 32 位 uid 太容易错
    matched = [t for t in repo.list_tasks(limit=200)
               if t.uid == args.task or t.uid.startswith(args.task)]
    if not matched:
        print(f"找不到任务：{args.task}")
        return
    if len(matched) > 1:
        print(f"前缀匹配到 {len(matched)} 条任务，请给更长的 uid")
        return

    task = matched[0]
    rows = repo.list_live_task_cards(task.uid)
    print(f"任务 {task.uid}  v{task.version}  {task.status}  {task.title}")
    print(f"活卡片 {len(rows)} 张：")
    for row in rows:
        flag = "落后" if (row.synced_version or 0) < task.version else "已最新"
        print(f"  {row.message_id[:16]}…  {row.kind:<6} "
              f"v{row.synced_version} → {flag}  fail={row.fail_count}")

    if args.dry_run:
        stale = [r for r in rows if (r.synced_version or 0) < task.version]
        print(f"\n[dry-run] 会刷新 {len(stale)} 张")
        return

    stats = sync_task(task.uid)
    print(f"\n结果：检查 {stats['checked']} / 刷新 {stats['updated']} / "
          f"失败 {stats['failed']} / 跳过 {stats['skipped']}")


if __name__ == "__main__":
    _main()
