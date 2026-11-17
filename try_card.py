"""命令行工具：构造并预览/发送卡片（候选确认卡 + 任务状态卡）。

卡片 JSON 的坑在于「看起来对、发出去报错」，而报错信息往往只有一句
muted 的 400。所以先不加 --send，看清结构，再决定发不发。

用法：
    .venv\\Scripts\\python.exe try_card.py                    # 打印最近一条候选的卡片
    .venv\\Scripts\\python.exe try_card.py --cid 71d02610     # 指定候选（uid 前缀）
    .venv\\Scripts\\python.exe try_card.py --demo             # 库里没数据时用假数据造一张
    .venv\\Scripts\\python.exe try_card.py --task 0293        # 打印任务卡（带状态按钮）
    .venv\\Scripts\\python.exe try_card.py --task 0293 --as ou_xxx   # 以某人的身份看会出哪些按钮
    .venv\\Scripts\\python.exe try_card.py --send-user ou_xxx # 私聊发给你自己
    .venv\\Scripts\\python.exe try_card.py --send-chat oc_xxx # 发到群里

--send-* 是真实发消息，会出现在飞书里。不加参数只会打印，不联网。
"""

import argparse
import json
import sys
from datetime import datetime, timedelta

import cards
import db
import feishu_api
import repo
import state_machine
from models import TZ_SHANGHAI


class _DemoCandidate:
    """库里没数据时用来造卡片，验证渲染效果。"""

    uid = "demo0000demo0000demo0000demo0000"
    seq = 1
    message_id = "om_demo_message_id"
    chat_id = "oc_demo_chat"
    chat_type = "group"
    sender_open_id = "ou_demo_sender"
    title = "提交供应商整改报告"
    deliverable = "供应商整改报告（含整改项、责任人、完成时间）"
    acceptance_criteria = "质量部签字确认"
    deadline_text = "本周五前"
    deadline_at = datetime.now(tz=TZ_SHANGHAI) + timedelta(days=3)
    deadline_status = "resolved"
    deadline_rule = "本-week-4|default-end"
    priority = "urgent"
    confidence = 0.86
    assignee_open_ids = ["ou_demo_assignee"]
    missing_fields = ["acceptance_criteria"]
    clarification_question = "这条任务的验收标准是什么？"
    status = "pending"
    expires_at = datetime.now(tz=TZ_SHANGHAI) + timedelta(hours=72)


def _pick(cid_prefix: str = ""):
    db.init_db()
    if cid_prefix:
        for cand in repo.list_candidates(status=None, limit=200):
            if cand.uid.startswith(cid_prefix):
                return cand
        return None
    rows = repo.list_candidates(status="pending", limit=1)
    if rows:
        return rows[0]
    rows = repo.list_candidates(status=None, limit=1)
    return rows[0] if rows else None


def _pick_task(prefix: str):
    db.init_db()
    for task in repo.list_tasks(limit=200):
        if task.uid.startswith(prefix):
            return task
    return None


def _print_card(title: str, subtitle: str, card, compact: bool) -> None:
    content = json.dumps(card, ensure_ascii=False, indent=None if compact else 2)
    print(f"{title}：{subtitle}")
    print("-" * 70)
    print(content)
    print("-" * 70)


def main() -> None:
    parser = argparse.ArgumentParser(description="卡片调试工具")
    parser.add_argument("--cid", help="候选 uid 前缀")
    parser.add_argument("--task", metavar="UID前缀", help="打印任务卡（带状态按钮）")
    parser.add_argument("--as", dest="viewer", default="",
                        help="以谁的 open_id 看这张任务卡（决定出哪些按钮）")
    parser.add_argument("--demo", action="store_true", help="用假数据造一张卡片")
    parser.add_argument("--send-user", metavar="OPEN_ID", help="真实私聊发给某人")
    parser.add_argument("--send-chat", metavar="CHAT_ID", help="真实发到某个群")
    parser.add_argument("--compact", action="store_true", help="紧凑输出 JSON")
    args = parser.parse_args()

    if args.task:
        task = _pick_task(args.task)
        if task is None:
            print(f"没找到任务：{args.task}")
            sys.exit(1)
        viewer = args.viewer or None
        card = cards.build_task_card(task, viewer)
        # 群卡（没传 --as）渲染的是「状态允许的全部动作」，私聊卡才按人过滤
        acts = (cards.task_actions_for(task, viewer) if viewer
                else state_machine.next_actions(task.status))
        if not args.send_user and not args.send_chat:
            _print_card(
                "任务卡",
                f"{task.uid[:8]}  {task.title}  v{task.version}  "
                f"按钮={'、'.join(acts) or '（无）'}",
                card, args.compact,
            )
            if not viewer:
                print("这是群卡视角（渲染状态允许的全部动作）。"
                      "想看某个人能点哪些，加 --as ou_xxx。")
            return
        if args.send_user:
            mid = feishu_api.send_card_to_user(args.send_user, card)
            print(f"私聊发送{'成功' if mid else '失败'}：{mid or '-'}")
        if args.send_chat:
            mid = feishu_api.send_card_to_chat(args.send_chat, card)
            print(f"群发发送{'成功' if mid else '失败'}：{mid or '-'}")
        return

    cand = _DemoCandidate() if args.demo else _pick(args.cid or "")
    if cand is None:
        print("库里没有候选。加 --demo 用假数据看效果，或先跑 run.bat 发条消息。")
        sys.exit(1)

    card = cards.build_candidate_card(cand)

    if not args.send_user and not args.send_chat:
        _print_card("候选卡", f"{cand.uid[:8]}  {cand.title}  按钮=确认/忽略", card,
                    args.compact)
        print("要真发出去：加 --send-user <你的 open_id> 或 --send-chat <群 chat_id>")
        return

    if args.send_user:
        mid = feishu_api.send_card_to_user(args.send_user, card)
        print(f"私聊发送{'成功' if mid else '失败'}：{mid or '-'}")
    if args.send_chat:
        mid = feishu_api.send_card_to_chat(args.send_chat, card)
        print(f"群发发送{'成功' if mid else '失败'}：{mid or '-'}")


if __name__ == "__main__":
    main()
