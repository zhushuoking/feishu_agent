"""命令行工具：只测时间解析，不调模型、不花钱、不需要网络。

用法：
    .venv\\Scripts\\python.exe try_deadline.py                  # 跑内置样例表
    .venv\\Scripts\\python.exe try_deadline.py "本周五前"        # 按当前时间当基准
    .venv\\Scripts\\python.exe try_deadline.py "下周二" --base 2026-11-13T10:00

为什么要这个工具：群里出现一句时间说法，先在这里确认系统怎么理解，
再去排查模型 —— 否则永远分不清是模型抄错了原文，还是换算规则有洞。
"""

import argparse
import sys
from datetime import datetime

import deadline
from models import TZ_SHANGHAI

# 内置样例：(基准时间描述, 原话, 期望结果)
# 期望写成人话，跑起来一眼能看出哪条口径出问题
SAMPLES = [
    ("周五 10:00", "今天下班前", "2026-11-13 18:00"),
    ("周五 10:00", "明天中午12点前", "2026-11-14 12:00"),
    ("周五 10:00", "本周五前", "2026-11-13 18:00 (默认)"),
    ("周五 10:00", "下周二", "2026-11-17 18:00 (默认)"),
    ("周五 10:00", "月底前", "2026-11-30 23:59"),
    ("周五 10:00", "三天后", "2026-11-16 18:00 (默认)"),
    ("周五 10:00", "下个月底", "2026-12-31 23:59"),
    ("周五 10:00", "周五下午3点前", "2026-11-13 15:00"),
    ("周五 10:00", "十二月底", "2026-12-31 23:59"),
    ("周五 10:00", "两周后", "2026-11-27 18:00 (默认)"),
    ("周五 10:00", "尽快", "unresolved（模糊）"),
    ("周五 10:00", "上周五前", "unresolved（过去）"),
    ("周五 20:44", "今天下班前", "unresolved（已过点）"),
    ("周五 19:00", "下班前", "2026-11-16 18:00 (默认, 顺延周一)"),
    ("", "（空的）", "missing（原话没给）"),
]


def _base(raw: str) -> datetime:
    if not raw:
        return datetime.now(tz=TZ_SHANGHAI)
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=TZ_SHANGHAI)
    except ValueError:
        raise SystemExit(f"base 格式不对：{raw}，示例 2026-11-13T10:00")


def run_samples() -> None:
    fixed = datetime(2026, 11, 13, 10, 0, tzinfo=TZ_SHANGHAI)
    print(f"基准：2026-11-13 10:00（周五）  时区 {TZ_SHANGHAI}")
    print("-" * 78)
    for base_desc, text, expect in SAMPLES:
        base = fixed
        if "20:44" in base_desc:
            base = datetime(2026, 11, 13, 20, 44, tzinfo=TZ_SHANGHAI)
        elif "19:00" in base_desc:
            base = datetime(2026, 11, 13, 19, 0, tzinfo=TZ_SHANGHAI)
        result = deadline.parse_deadline("" if text == "（空的）" else text, base)
        got = result.describe()
        print(f"[{base_desc:>9}] {text:<14} -> {got}")
        print(f"{'':>12}{'期望':<12}    {expect}")


def main() -> None:
    parser = argparse.ArgumentParser(description="截止时间解析调试工具")
    parser.add_argument("text", nargs="?", help="要解析的时间原话")
    parser.add_argument("--base", help="基准时间（消息发送时间），ISO 格式")
    args = parser.parse_args()

    if not args.text:
        run_samples()
        return

    result = deadline.parse_deadline(args.text, _base(args.base))
    print(f"原话  : {args.text}")
    print(f"基准  : {_base(args.base):%Y-%m-%d %H:%M} ({_base(args.base):%A})")
    print(f"状态  : {result.status}")
    print(f"截止  : {result.deadline_at}")
    print(f"规则  : {result.rule or '-'}")
    print(f"粒度  : {result.precision}   含系统假设: {result.assumed}")
    if result.reason:
        print(f"原因  : {result.reason}")


if __name__ == "__main__":
    sys.exit(main())
