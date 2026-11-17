"""命令行调试工具：不碰飞书，直接喂一句话给模型看识别结果。

为什么需要它：调提示词要跑几十轮，每轮都去群里发消息太慢。
这里一次一轮，还能对着期望结果自动判对错。

用法：
    .venv\\Scripts\\python.exe try_detect.py "请本周五前提交供应商整改报告"
    .venv\\Scripts\\python.exe try_detect.py "报告先不用做了" --mention 李四=ou_xxx
    .venv\\Scripts\\python.exe try_detect.py --batch    # 只打印结果
    .venv\\Scripts\\python.exe try_detect.py --check    # 跑样例并对照期望判对错
"""

import argparse
import sys

import detector
from models import Mention

# (文本, 提及列表, 期望类型集合, 期望任务条数, 每条任务最多几个负责人)
SAMPLES = [
    (
        "请本周五前提交供应商整改报告。",
        [],
        {"task"},
        1,
        0,  # 没 @ 任何人，负责人必须为空
    ),
    (
        "这个方案你看一下。",
        [],
        {"unclear"},
        1,  # 模糊任务仍要留一条待澄清候选，否则系统会"忘掉"这件事
        0,  # 没有 @ 人，负责人必须为空
    ),
    (
        "昨天的会议你参加了吗？",
        [],
        {"inquiry", "unrelated"},
        0,
        None,
    ),
    (
        "明天上午十点开项目会，请准时参加。",
        [],
        {"notice"},
        0,
        None,
    ),
    (
        "报告先不用做了。",
        [],
        {"unrelated", "unclear"},
        0,
        None,
    ),
    (
        "收到，好的。",
        [],
        {"unrelated"},
        0,
        None,
    ),
    (
        "测试测试111",
        [],
        {"unrelated"},
        0,
        None,
    ),
    (
        "分别提交采购清单和试验计划，下周二前完成。",
        ["张三=ou_zhangsan", "李四=ou_lisi"],
        {"task"},
        2,
        1,  # 关键：每条任务只能有一个负责人
    ),
]


def _build_mentions(raw_items):
    mentions = []
    for idx, item in enumerate(raw_items, 1):
        name, open_id = item.split("=", 1)
        mentions.append(
            Mention(key=f"@_user_{idx}", name=name.strip(), open_id=open_id.strip())
        )
    return mentions


def run_one(text: str, mentions):
    print("=" * 70)
    print("输入:", text)
    if mentions:
        print("提及:", [(m.name, m.open_id) for m in mentions])
    try:
        result = detector.detect_text(text, mentions=mentions)
    except detector.DetectionError as exc:
        print("[失败]", exc)
        return None

    print(f"类型={result.candidate_type}  置信度={result.confidence:.2f}  "
          f"是任务={result.is_task_candidate}  需澄清={result.clarification_needed}")
    if result.clarification_question:
        print("要问的:", result.clarification_question)
    for i, task in enumerate(result.tasks, 1):
        print(f"  [{i}] {task.title}")
        print(f"      交付物={task.deliverable}")
        print(f"      时间原文={task.deadline_text}  →  截止={task.deadline_at} "
              f"[{task.deadline_status} / {task.deadline_rule}]")
        print(f"      负责人={task.assignee_mention_ids}  缺失={task.missing_fields}")
        print(f"      证据={task.evidence}")
    return result


def _judge(result, allowed_types, expected_count, max_assignees) -> list:
    """返回不符合期望的原因列表，空列表表示通过。"""
    if result is None:
        return ["调用失败"]
    problems = []
    if result.candidate_type not in allowed_types:
        problems.append(
            f"类型 {result.candidate_type} 不在期望 {sorted(allowed_types)} 内"
        )
    if len(result.tasks) != expected_count:
        problems.append(f"任务条数 {len(result.tasks)}，期望 {expected_count}")
    if max_assignees is not None:
        for i, task in enumerate(result.tasks, 1):
            if len(task.assignee_mention_ids) > max_assignees:
                problems.append(
                    f"第 {i} 条负责人有 {len(task.assignee_mention_ids)} 个，"
                    f"上限 {max_assignees}（多人多任务应一对一绑定）"
                )
    return problems


def run_batch(check: bool):
    passed = 0
    failed = []
    for text, raw_mentions, allowed, count, max_assignees in SAMPLES:
        result = run_one(text, _build_mentions(raw_mentions))
        if check:
            problems = _judge(result, allowed, count, max_assignees)
            if problems:
                failed.append((text, problems))
                print("  >>> 不符合期望：")
                for p in problems:
                    print("      -", p)
            else:
                passed += 1
                print("  >>> 通过")

    print()
    print("=" * 70)
    if check:
        print(f"回归结果：{passed}/{len(SAMPLES)} 通过")
        if failed:
            print("不通过的样例：")
            for text, problems in failed:
                print(f"  - {text}")
                for p in problems:
                    print(f"      {p}")
    else:
        print(f"共跑 {len(SAMPLES)} 条样例")


def main():
    parser = argparse.ArgumentParser(description="任务识别调试工具")
    parser.add_argument("text", nargs="?", help="要识别的消息文本")
    parser.add_argument("--mention", action="append", default=[], help="名字=open_id，可重复")
    parser.add_argument("--batch", action="store_true", help="跑内置样例集")
    parser.add_argument("--check", action="store_true", help="跑样例并对期望判对错")
    args = parser.parse_args()

    if args.batch or args.check:
        run_batch(check=args.check)
        return

    if not args.text:
        parser.print_help()
        sys.exit(1)

    run_one(args.text, _build_mentions(args.mention))


if __name__ == "__main__":
    main()
