"""Day 10：任务状态机（对应设计文档 7.2）。

状态机要解决的问题不是「怎么改状态」，而是「哪些状态跳转是错的」。

不许乱跳的例子：
- TODO 直接 DONE —— 没人开工，怎么会有交付物？验收环节被整段跳过
- DONE 再回 IN_PROGRESS —— 已完成的任务被悄悄复活，统计和对账全乱
- CANCELLED 复活 —— 取消是一种承诺，不能靠改状态撤销

设计 §6.3 允许待开始、进行中和阻塞任务提交验收。
待验收时补充完成说明只更新原任务与验收卡。

和设计的对应关系：
  7.2 状态定义与流转
  7.4 逾期是派生字段（is_overdue），不是状态 —— 见 models_db.Task.is_overdue
"""

from dataclasses import dataclass
from typing import Dict, FrozenSet, List

from models_db import (
    TASK_BLOCKED, TASK_CANCELLED, TASK_DONE, TASK_IN_PROGRESS,
    TASK_NEEDS_INFO, TASK_PENDING_ACCEPTANCE, TASK_STATUSES, TASK_TERMINAL, TASK_TODO,
)

# 合法的状态转移表。想加转移就改这里，别在业务代码里写 if 判断。
TRANSITIONS: Dict[str, FrozenSet[str]] = {
    TASK_NEEDS_INFO: frozenset({TASK_TODO, TASK_CANCELLED}),
    TASK_TODO: frozenset({TASK_IN_PROGRESS, TASK_BLOCKED, TASK_PENDING_ACCEPTANCE, TASK_CANCELLED}),
    TASK_IN_PROGRESS: frozenset({TASK_BLOCKED, TASK_PENDING_ACCEPTANCE, TASK_CANCELLED}),
    TASK_BLOCKED: frozenset({TASK_IN_PROGRESS, TASK_PENDING_ACCEPTANCE, TASK_CANCELLED}),
    # 验收不通过 → 打回继续做
    TASK_PENDING_ACCEPTANCE: frozenset({TASK_DONE, TASK_IN_PROGRESS, TASK_CANCELLED, TASK_PENDING_ACCEPTANCE}),
    TASK_DONE: frozenset(),
    TASK_CANCELLED: frozenset(),
}

# 状态的中文名，卡片和 CLI 都用它，别在三个地方各写一份
STATUS_LABELS = {
    TASK_NEEDS_INFO: "待补充",
    TASK_TODO: "待开始",
    TASK_IN_PROGRESS: "进行中",
    TASK_BLOCKED: "阻塞",
    TASK_PENDING_ACCEPTANCE: "待验收",
    TASK_DONE: "已完成",
    TASK_CANCELLED: "已取消",
}

# 动作名 → 目标状态这件事不能只看动作，还要看「从哪来」：
# 同样是去 IN_PROGRESS，"开始处理""解除阻塞""验收打回"是三种不同语义。
# 所以这里用 (原状态, 目标状态) 反查动作名，而不是动作直接映射状态。
_ACTION_OF: Dict[tuple, str] = {
    (TASK_NEEDS_INFO, TASK_TODO): "supplement",
    (TASK_NEEDS_INFO, TASK_CANCELLED): "cancel",
    (TASK_TODO, TASK_IN_PROGRESS): "start",
    (TASK_IN_PROGRESS, TASK_BLOCKED): "block",
    (TASK_TODO, TASK_BLOCKED): "block",
    (TASK_BLOCKED, TASK_IN_PROGRESS): "unblock",
    (TASK_IN_PROGRESS, TASK_PENDING_ACCEPTANCE): "submit",
    (TASK_TODO, TASK_PENDING_ACCEPTANCE): "submit",
    (TASK_BLOCKED, TASK_PENDING_ACCEPTANCE): "submit",
    (TASK_PENDING_ACCEPTANCE, TASK_PENDING_ACCEPTANCE): "submit",
    (TASK_PENDING_ACCEPTANCE, TASK_DONE): "accept",
    (TASK_PENDING_ACCEPTANCE, TASK_IN_PROGRESS): "reject",
    (TASK_TODO, TASK_CANCELLED): "cancel",
    (TASK_IN_PROGRESS, TASK_CANCELLED): "cancel",
    (TASK_BLOCKED, TASK_CANCELLED): "cancel",
    (TASK_PENDING_ACCEPTANCE, TASK_CANCELLED): "cancel",
}

ACTION_LABELS = {
    "supplement": "补充信息",
    "start": "开始处理",
    "block": "报阻塞",
    "unblock": "解除阻塞",
    "submit": "提交验收",
    "accept": "验收通过",
    "reject": "验收打回",
    "cancel": "取消任务",
}


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


def is_terminal(status: str) -> bool:
    return status in TASK_TERMINAL


def is_known(status: str) -> bool:
    return status in TASK_STATUSES


def _ordered(statuses) -> List[str]:
    """按状态的自然顺序排（TODO → 进行中 → …），不按字符串。"""
    return [s for s in STATUS_LABELS if s in statuses]


def check_transition(from_status: str, to_status: str) -> CheckResult:
    """只看状态本身合不合法，不看是谁在操作。"""
    if not is_known(to_status):
        return CheckResult(False, f"未知状态：{to_status}")
    if is_terminal(from_status):
        return CheckResult(
            False,
            f"任务已是终态「{STATUS_LABELS.get(from_status, from_status)}」，不能再改",
        )
    allowed = TRANSITIONS.get(from_status, frozenset())
    if to_status not in allowed:
        if not allowed:
            return CheckResult(False, f"「{from_status}」没有可用的后续状态")
        return CheckResult(
            False,
            f"不能从「{STATUS_LABELS.get(from_status, from_status)}」"
            f"直接变成「{STATUS_LABELS.get(to_status, to_status)}」，"
            f"只能变成：{'、'.join(STATUS_LABELS[s] for s in _ordered(allowed))}",
        )
    return CheckResult(True)


def resolve_action(from_status: str, action: str):
    """动作 → (目标状态, 检查结果)。动作和当前状态不匹配时给明确原因。"""
    if action not in ACTION_LABELS:
        return None, CheckResult(False, f"未知动作：{action}，可用：{'、'.join(ACTION_LABELS)}")

    # 终态要先挡。不先挡的话，下面按「动作能不能从当前状态出发」去找，
    # 终态在 _ACTION_OF 里一条都没有，最后只会吐出「不能执行」，
    # 看着像状态不对，其实是任务已经结束了 —— 这两种情况的处理方式不一样：
    # 前者刷新重试，后者根本不该再点。
    if is_terminal(from_status):
        return None, CheckResult(
            False,
            f"任务已是终态「{STATUS_LABELS.get(from_status, from_status)}」，不能再改",
        )

    for (src, dst), name in _ACTION_OF.items():
        if src == from_status and name == action:
            return dst, check_transition(from_status, dst)

    # 动作认识，但当前状态下做不了。顺手告诉用户这个动作什么状态下能用，
    # 比只说一句「不能执行」有用得多。
    valid_from = [src for (src, _dst), name in _ACTION_OF.items() if name == action]
    hint = "、".join(STATUS_LABELS[src] for src in _ordered(valid_from))
    return None, CheckResult(
        False,
        f"任务当前是「{STATUS_LABELS.get(from_status, from_status)}」，"
        f"不能执行「{ACTION_LABELS.get(action, action)}」"
        + (f"；该动作只能在「{hint}」时执行" if hint else ""),
    )


def next_actions(current_status: str) -> List[str]:
    """当前状态下状态层面可行的动作，按固定顺序返回，方便卡片按顺序出按钮。"""
    order = ["supplement", "start", "block", "unblock", "submit", "accept", "reject", "cancel"]
    hits = [name for (src, dst), name in _ACTION_OF.items() if src == current_status and src != dst]
    return [a for a in order if a in hits]


def describe(current_status: str) -> str:
    """当前状态能去哪些状态，按状态本身的自然顺序排，不按字符串排。
    （按字符串排会输出「已取消、进行中」，看着像倒着说的）"""
    allowed = TRANSITIONS.get(current_status, frozenset())
    if not allowed:
        return f"{STATUS_LABELS.get(current_status, current_status)}（终态）"
    order = list(STATUS_LABELS)
    return "、".join(
        STATUS_LABELS[s] for s in order if s in allowed
    )
