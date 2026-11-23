"""按任务参与关系和群项目经理授权校验业务操作。"""

import os
from dataclasses import dataclass
from typing import Set

from state_machine import ACTION_LABELS

ROLE_OWNER = "owner"
ROLE_CREATOR = "creator"
ROLE_ADMIN = "admin"
ROLE_MANAGER = "manager"
ROLE_REVIEWER = "reviewer"

ROLE_LABELS = {
    ROLE_OWNER: "负责人",
    ROLE_CREATOR: "发起人",
    ROLE_ADMIN: "管理员",
    ROLE_MANAGER: "项目经理",
    ROLE_REVIEWER: "验收人",
}


def enabled_chat_ids() -> Set[str]:
    def configured(name: str) -> set[str]:
        return {value.strip() for value in os.getenv(name, "").split(",") if value.strip()}

    return configured("FEISHU_ALLOWED_CHAT_IDS") - configured("FEISHU_DISABLED_CHAT_IDS")


def chat_enabled(chat_id: str) -> bool:
    return chat_id in enabled_chat_ids()


def may_handle_candidate(candidate, operator_open_id: str) -> bool:
    if not operator_open_id or not chat_enabled(candidate.chat_id):
        return False
    if candidate.sender_open_id == operator_open_id:
        return True
    # 每项只授予一个群内的候选审批权。系统管理员角色不参与此判断。
    return operator_open_id in chat_managers(candidate.chat_id)


def chat_managers(chat_id: str) -> Set[str]:
    return {
        user_id for binding in os.getenv("FEISHU_CHAT_MANAGER_BINDINGS", "").split(",")
        for group, separator, user_id in [binding.strip().partition(":")]
        if separator and group == chat_id and user_id
    }


def default_reviewer(chat_id: str) -> str:
    for binding in os.getenv("FEISHU_CHAT_REVIEWER_BINDINGS", "").split(","):
        group, separator, user_id = binding.strip().partition(":")
        if separator and group == chat_id:
            return user_id
    return ""

# 动作 → 允许的角色。收紧权限就改这里。
ACTION_ROLES = {
    "supplement": {ROLE_OWNER, ROLE_CREATOR, ROLE_REVIEWER, ROLE_MANAGER},
    "start": {ROLE_OWNER},
    "block": {ROLE_OWNER},
    "unblock": {ROLE_OWNER, ROLE_CREATOR, ROLE_MANAGER},
    "submit": {ROLE_OWNER, ROLE_MANAGER},
    "accept": {ROLE_CREATOR, ROLE_REVIEWER, ROLE_MANAGER},
    "reject": {ROLE_CREATOR, ROLE_REVIEWER, ROLE_MANAGER},
    "cancel": {ROLE_CREATOR, ROLE_MANAGER},
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
    if open_id in chat_managers(task.chat_id):
        roles.add(ROLE_MANAGER)
    if open_id == task.effective_reviewer_open_id:
        roles.add(ROLE_REVIEWER)
    return roles


def describe_roles(task, open_id: str) -> str:
    roles = roles_of(task, open_id)
    if not roles:
        return "与该任务无关"
    order = [ROLE_CREATOR, ROLE_OWNER, ROLE_REVIEWER, ROLE_MANAGER, ROLE_ADMIN]
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
    if action in ("accept", "reject") and ROLE_OWNER in roles:
        return Permission(False, "负责人不能自行验收，请由其他发起人、验收人或项目经理处理")

    if roles & allowed_roles:
        return Permission(True)

    if not roles:
        return Permission(
            False,
            f"当前账号无权「{ACTION_LABELS.get(action, action)}」",
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
