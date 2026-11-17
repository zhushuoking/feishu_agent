"""Day 10：权限校验（对应设计文档 7.3）。

三个角色，按「谁跟这个任务有关系」判定，不做组织架构：
  owner   —— 负责人，负责干活和报进展
  creator —— 发起人，负责验收和取消
  admin   —— 管理员，兜底处理所有人搞不定的情况

为什么不用部门/岗位那套：
首期没有组织架构数据，硬接通讯录要额外权限和一堆同步逻辑。
用 open_id 比对能覆盖 95% 的场景，剩下 5% 交给 admin 名单。

一个刻意的兜底：任务没有负责人时，发起人可以代行负责人动作。
否则「识别时没 @ 到人」的任务会永远停在待开始，谁也没法推进。
这条规则宁可宽松，也不能让任务死在那儿。
"""

import os
from dataclasses import dataclass
from typing import Set

from state_machine import ACTION_LABELS

ROLE_OWNER = "owner"
ROLE_CREATOR = "creator"
ROLE_ADMIN = "admin"

ROLE_LABELS = {
    ROLE_OWNER: "负责人",
    ROLE_CREATOR: "发起人",
    ROLE_ADMIN: "管理员",
}

# 动作 → 允许的角色。收紧权限就改这里。
ACTION_ROLES = {
    "start": {ROLE_OWNER, ROLE_ADMIN},
    "block": {ROLE_OWNER, ROLE_ADMIN},
    "unblock": {ROLE_OWNER, ROLE_ADMIN},
    "submit": {ROLE_OWNER, ROLE_ADMIN},
    "accept": {ROLE_CREATOR, ROLE_ADMIN},
    "reject": {ROLE_CREATOR, ROLE_ADMIN},
    "cancel": {ROLE_CREATOR, ROLE_ADMIN},
}


def admin_open_ids() -> Set[str]:
    """管理员名单从环境变量读，逗号分隔。改动不用重启代码逻辑，改 .env 即可。"""
    raw = os.getenv("AGENT_ADMIN_OPEN_IDS", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def roles_of(task, open_id: str) -> Set[str]:
    """这个人在这个任务上是什么角色。可能同时是负责人和发起人。"""
    if not open_id:
        return set()
    roles = set()
    if open_id in admin_open_ids():
        roles.add(ROLE_ADMIN)
    if task.creator_open_id and open_id == task.creator_open_id:
        roles.add(ROLE_CREATOR)
    if open_id in set(task.owner_open_ids):
        roles.add(ROLE_OWNER)
    return roles


def describe_roles(task, open_id: str) -> str:
    roles = roles_of(task, open_id)
    if not roles:
        return "与该任务无关"
    order = [ROLE_CREATOR, ROLE_OWNER, ROLE_ADMIN]
    return "、".join(ROLE_LABELS[r] for r in order if r in roles)


@dataclass
class Permission:
    ok: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.ok


def can(task, open_id: str, action: str) -> Permission:
    """判断某人能否对某任务执行某动作。只判权限，不判状态是否允许。"""
    if not open_id:
        return Permission(False, "拿不到操作人身份，拒绝执行")

    allowed_roles = ACTION_ROLES.get(action)
    if allowed_roles is None:
        return Permission(False, f"未知动作：{action}")

    roles = roles_of(task, open_id)

    # 先按正常矩阵判。有权限就直接过，别让兜底规则盖住正常判定。
    if roles & allowed_roles:
        # 一个小团队的现实问题：发起人常常就是负责人（自己 @ 自己布置活）。
        # 这种情况硬挡「不能自己验收自己」会让任务永远完不成，
        # 所以放行，但把「自验收」写进拒绝不得的地方 —— 返回值里带上原因，
        # 流水里就会留痕，事后复盘能一眼看出这条是自验收的。
        if {ROLE_OWNER, ROLE_CREATOR} <= roles and action in ("accept", "reject"):
            return Permission(True, "发起人同时也是负责人：自验收")
        return Permission(True)

    # 兜底：任务压根没有负责人时，发起人可以代行负责人的动作。
    # 注意这里不能写成「if not roles」—— 发起人自己一定有 creator 角色，
    # roles 永远非空，那样写兜底等于永远不生效（踩过）。
    # 只对「本该由负责人做的动作」开口子：验收/取消发起人本来就能做，
    # 走不到这里，不需要兜底。
    if (ROLE_OWNER in allowed_roles
            and not task.owner_open_ids
            and open_id == task.creator_open_id):
        return Permission(True, "任务没有负责人，发起人代行")

    if not roles:
        return Permission(
            False,
            f"你不是这个任务的负责人或发起人，不能「{ACTION_LABELS.get(action, action)}」",
        )

    needed = "、".join(ROLE_LABELS[r] for r in sorted(allowed_roles))
    mine = "、".join(ROLE_LABELS[r] for r in sorted(roles))
    return Permission(
        False,
        f"「{ACTION_LABELS.get(action, action)}」需要{needed}权限，你是{mine}",
    )


def allowed_actions(task, open_id: str, candidates) -> list:
    """在给定候选动作里，筛出这个人真能做的。卡片按钮就靠它决定显示哪几个。"""
    return [a for a in candidates if can(task, open_id, a).ok]
