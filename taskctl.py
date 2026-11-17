"""命令行工具：手动推进任务状态，验状态机和权限。

卡片上的状态按钮还没做，先用这个把逻辑跑通、也能演示权限拦截。

用法：
    .venv\\Scripts\\python.exe taskctl.py list
    .venv\\Scripts\\python.exe taskctl.py show 0293
    .venv\\Scripts\\python.exe taskctl.py start 0293 --as ou_xxx
    .venv\\Scripts\\python.exe taskctl.py accept 0293 --as ou_xxx --note "验收通过"

不传 --as 时，依次取：环境变量 AGENT_DEFAULT_OPERATOR → 任务第一个负责人。
输出里会写明「以谁的身份执行」，别看着成功了却不知道是谁的成功。
"""

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()  # AGENT_ADMIN_OPEN_IDS / AGENT_DEFAULT_OPERATOR / DB_PATH 都写在 .env 里

import db
import permissions
import repo
import state_machine


def _fmt(value) -> str:
    return value.strftime("%m-%d %H:%M") if value else "-"


def _find(prefix: str):
    """按 uid 前缀找任务，返回 (task, 错误信息)。"""
    rows = repo.list_tasks(limit=200)
    hits = [t for t in rows if t.uid.startswith(prefix)]
    if not hits:
        return None, f"没找到任务：{prefix}"
    if len(hits) > 1:
        return None, f"前缀 {prefix} 匹配到 {len(hits)} 个任务，写长一点"
    return hits[0], ""


def _operator(task, given: str) -> str:
    if given:
        return given
    env = os.getenv("AGENT_DEFAULT_OPERATOR", "").strip()
    if env:
        return env
    owners = task.owner_open_ids
    return owners[0] if owners else ""


def cmd_list() -> None:
    tasks = repo.list_tasks(limit=30)
    if not tasks:
        print("还没有正式任务。先在卡片上确认一条候选。")
        return
    print(f"任务 {repo.count_tasks()} 条（按创建时间倒序，最多 30）")
    print("=" * 78)
    for t in tasks:
        flag = " [逾期]" if t.is_overdue else ""
        label = state_machine.STATUS_LABELS.get(t.status, t.status)
        print(f"[{t.uid[:8]}] {t.title}{flag}")
        print(f"   {label:<5} v{t.version}  截止={_fmt(t.deadline_at)}  "
              f"负责人={[o[:10] for o in t.owner_open_ids] or '无'}")
        acts = state_machine.next_actions(t.status)
        if acts:
            print(f"   可执行：{'、'.join(state_machine.ACTION_LABELS[a] for a in acts)}")
        print()


def cmd_show(prefix: str, given_operator: str) -> None:
    task, err = _find(prefix)
    if task is None:
        print(err)
        sys.exit(1)

    operator = _operator(task, given_operator)
    print(f"[{task.uid[:8]}] {task.title}")
    print(f"  状态   : {state_machine.STATUS_LABELS.get(task.status)} ({task.status}) v{task.version}")
    print(f"  截止   : {_fmt(task.deadline_at)}"
          f"{'  [逾期]' if task.is_overdue else ''}")
    print(f"  交付物 : {task.deliverable or '-'}")
    print(f"  验收   : {task.acceptance_criteria or '-'}")
    print(f"  发起人 : {task.creator_open_id or '-'}")
    print(f"  负责人 : {task.owner_open_ids or '无'}")
    print(f"  下一步 : {state_machine.describe(task.status)}")

    if operator:
        print(f"  操作人 : {operator}（{permissions.describe_roles(task, operator)}）")
        acts = permissions.allowed_actions(
            task, operator, state_machine.next_actions(task.status)
        )
        if acts:
            print("  你可以 : " + "、".join(state_machine.ACTION_LABELS[a] for a in acts))
        else:
            print("  你可以 : （没有可执行的动作）")

    print("-" * 78)
    print("状态流水：")
    for log in repo.list_task_logs(task.uid):
        print(f"  {_fmt(log.created_at)}  {log.from_status or '—'} → {log.to_status}"
              f"   by {(log.operator_open_id or '-')[:16]}   {log.reason or ''}")


def cmd_action(action: str, prefix: str, given_operator: str, note: str,
               notify: bool = True) -> None:
    task, err = _find(prefix)
    if task is None:
        print(err)
        sys.exit(1)

    operator = _operator(task, given_operator)
    if not operator:
        print("没指定操作人，用 --as ou_xxx 指定")
        sys.exit(1)

    print(f"以 {operator}（{permissions.describe_roles(task, operator)}）的身份"
          f"对 [{task.uid[:8]}] 执行「{state_machine.ACTION_LABELS.get(action, action)}」")

    result = repo.transition_task(task.uid, action, operator_open_id=operator,
                                  note=note or None)
    if result.ok:
        print(f"成功：{result.message}  现在 v{result.task.version}")
        if notify:
            # 状态变了就把发件箱里的东西发掉（提交验收 → 通知发起人 等）
            import worker
            stats = worker.drain_outbox()
            print(f"通知：发出 {stats['sent']}　失败 {stats['failed']}"
                  + ("　（免打扰时段，延后）" if stats.get("quiet") else ""))
        return

    print(f"失败：{result.message}")
    if result.conflict:
        print("（这是并发冲突，不是权限或状态问题，刷新后重试即可）")
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="任务状态操作")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list", help="列出任务")

    p_show = sub.add_parser("show", help="看任务详情与流水")
    p_show.add_argument("prefix")
    p_show.add_argument("--as", dest="operator", default="")

    for name in ("start", "block", "unblock", "submit", "accept", "reject", "cancel"):
        p = sub.add_parser(name, help=state_machine.ACTION_LABELS[name])
        p.add_argument("prefix")
        p.add_argument("--as", dest="operator", default="")
        p.add_argument("--note", default="")
        p.add_argument("--no-notify", action="store_true",
                       help="只改状态，不发通知（调试用）")

    args = parser.parse_args()
    db.init_db()

    if args.cmd == "list":
        cmd_list()
    elif args.cmd == "show":
        cmd_show(args.prefix, args.operator)
    elif args.cmd in state_machine.ACTION_LABELS:
        cmd_action(args.cmd, args.prefix, args.operator, args.note,
                   notify=not getattr(args, "no_notify", False))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
