"""Day 8：仓储层。所有数据库读写都从这里走，业务代码不直接碰 SQL。

为什么单独一层而不是在 main.py 里 session.commit()：
1. 卡片回调（Day 9）和定时 Worker（Day 12）都会复用同一套读写，
   逻辑散在入口文件里，迟早出现「这里加了口过滤条件、那里忘了」
2. 单测可以只测这一层，不需要启动飞书长连接
3. 后面换 PostgreSQL 时，改的是 db.py 一行，不是全项目
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import db
import permissions
import state_machine
from models import MessageCommand
from models_db import (
    CAL_CREATED, CAL_DELETED, CAL_FAILED, CAL_PENDING, CAL_SKIPPED,
    CAND_CONFIRMED, CAND_EXPIRED, CAND_PENDING, CAND_REJECTED,
    NOTIFY_FAILED, NOTIFY_PENDING, NOTIFY_SENT, NOTIFY_SKIPPED,
    TASK_DONE, TASK_NEEDS_INFO, TASK_PENDING_ACCEPTANCE, TASK_TERMINAL, TASK_TODO,
    NotificationOutbox, PersonalCalendarEvent, Task, TaskAssignee, TaskCalendarEvent, TaskCandidate,
    TaskCard, TaskStatusLog,
)
from schemas import TaskRecognition

# 候选默认存活时间（设计文档默认配置：72 小时）
DEFAULT_CANDIDATE_TTL_HOURS = 72


def _uid() -> str:
    return uuid.uuid4().hex


def save_candidates(result: TaskRecognition, cmd: MessageCommand,
                    ttl_hours: int = DEFAULT_CANDIDATE_TTL_HOURS) -> List[TaskCandidate]:
    """把一次识别的所有候选落库，返回本次真正新建的那些。

    幂等：同一条消息重复识别（模型重试、事件重投）不会产生重复候选，
    靠 UniqueConstraint(message_id, seq) 兜底，插入前先查一次只是为了返回干净的结果。
    """
    if not result.tasks:
        return []

    created_at = db.now()
    expires_at = created_at + timedelta(hours=ttl_hours)
    fresh: List[TaskCandidate] = []

    with db.session_scope() as s:
        existing = set(
            s.scalars(
                select(TaskCandidate.seq).where(TaskCandidate.message_id == cmd.message_id)
            ).all()
        )

        for seq, task in enumerate(result.tasks, 1):
            if seq in existing:
                continue
            cand = TaskCandidate(
                uid=_uid(),
                seq=seq,
                event_id=cmd.event_id,
                message_id=cmd.message_id,
                chat_id=cmd.chat_id,
                chat_type=cmd.chat_type,
                sender_open_id=cmd.sender_open_id,
                message_sent_at=cmd.sent_at,
                raw_text=cmd.text,
                title=task.title,
                description=task.description,
                deliverable=task.deliverable,
                acceptance_criteria=task.acceptance_criteria,
                deadline_text=task.deadline_text,
                deadline_at=task.deadline_at,
                deadline_status=task.deadline_status,
                deadline_rule=task.deadline_rule,
                priority=task.priority,
                candidate_type=result.candidate_type,
                confidence=result.confidence,
                assignee_open_ids=list(task.assignee_mention_ids),
                missing_fields=list(task.missing_fields),
                evidence=task.evidence,
                clarification_question=result.clarification_question,
                status=CAND_PENDING,
                expires_at=expires_at,
                created_at=created_at,
                updated_at=created_at,
            )
            s.add(cand)
            fresh.append(cand)

        s.flush()  # 拿到自增 id 之前先落盘，之后对象仍可用（expire_on_commit=False）

    return fresh


def get_candidate(uid: str) -> Optional[TaskCandidate]:
    with db.session_scope() as s:
        return s.scalar(select(TaskCandidate).where(TaskCandidate.uid == uid))


def list_candidates(status: Optional[str] = CAND_PENDING, chat_id: Optional[str] = None,
                    limit: int = 20) -> List[TaskCandidate]:
    """status 传 None 表示不限状态。"""
    stmt = select(TaskCandidate)
    if status:
        stmt = stmt.where(TaskCandidate.status == status)
    if chat_id:
        stmt = stmt.where(TaskCandidate.chat_id == chat_id)
    stmt = stmt.order_by(TaskCandidate.created_at.desc()).limit(limit)
    with db.session_scope() as s:
        return list(s.scalars(stmt).all())


def list_candidates_for_info_reminder(now, created_before) -> List[TaskCandidate]:
    """取出已到提醒时间、尚可处理并且还没有提醒记录的候选。"""
    already_queued = select(NotificationOutbox.uid).where(
        NotificationOutbox.dedupe_key == "NEEDS_INFO:" + TaskCandidate.uid
    ).exists()
    with db.session_scope() as s:
        return list(s.scalars(
            select(TaskCandidate)
            .where(
                TaskCandidate.status == CAND_PENDING,
                TaskCandidate.chat_id.in_(permissions.enabled_chat_ids()),
                TaskCandidate.created_at <= created_before,
                (TaskCandidate.expires_at.is_(None) | (TaskCandidate.expires_at > now)),
                TaskCandidate.sender_open_id.is_not(None),
                TaskCandidate.sender_open_id != "",
                ~already_queued,
            )
            .order_by(TaskCandidate.created_at)
        ))


def pending_candidates_by_prefix(prefix: str) -> List[TaskCandidate]:
    with db.session_scope() as s:
        return list(s.scalars(select(TaskCandidate).where(
            TaskCandidate.status == CAND_PENDING,
            TaskCandidate.uid.startswith(prefix, autoescape=True),
        ).limit(2)))


def count_candidates_by_status() -> dict:
    with db.session_scope() as s:
        rows = s.execute(
            select(TaskCandidate.status, func.count()).group_by(TaskCandidate.status)
        ).all()
    return {status: count for status, count in rows}


def _candidate_expired(current):
    return (TaskCandidate.expires_at.is_not(None) & (TaskCandidate.expires_at <= current)) | (
        TaskCandidate.expires_at.is_(None)
        & (TaskCandidate.created_at <= current - timedelta(hours=DEFAULT_CANDIDATE_TTL_HOURS))
    )


def expire_candidates(limit: int = 500) -> int:
    """把过了 expires_at 还没人处理的候选标记为 expired。

    Day 12 的 Worker 会定时调它。过期不删数据 —— 保留是为了复盘
    「模型到底提了多少没人理的候选」，这个指标直接反映提示词质量。
    """
    current = db.now()
    with db.session_scope() as s:
        eligible = select(TaskCandidate.id).where(
            TaskCandidate.status == CAND_PENDING, _candidate_expired(current)
        ).limit(limit)
        return s.execute(
            update(TaskCandidate).where(TaskCandidate.id.in_(eligible), TaskCandidate.status == CAND_PENDING)
            .values(status=CAND_EXPIRED, updated_at=current)
            .execution_options(synchronize_session=False)
        ).rowcount


class CandidateActionError(ValueError):
    """候选动作被拒绝；事务回滚，提示可直接展示给操作人。"""


def _claim_candidate(session, uid, operator_open_id, new_status):
    current = db.now()
    # 先取得写锁，同时校验有效期和状态。后续任一校验失败都会回滚这次抢占。
    cand = session.scalar(
        update(TaskCandidate)
        .where(TaskCandidate.uid == uid, TaskCandidate.status == CAND_PENDING,
               ~_candidate_expired(current))
        .values(status=new_status, updated_at=current)
        .returning(TaskCandidate)
    )
    observed = cand or session.scalar(select(TaskCandidate).where(TaskCandidate.uid == uid))
    if observed is None:
        return None
    if not permissions.may_handle_candidate(observed, operator_open_id):
        raise CandidateActionError("当前账号无权在此群处理这条候选")
    if cand is None and observed.status == CAND_PENDING:
        raise CandidateActionError("候选已过期，请重新发起")
    return cand


def reject_candidate(uid: str, operator_open_id: str = "", reason: str = "") -> bool:
    with db.session_scope() as s:
        return _claim_candidate(s, uid, operator_open_id, CAND_REJECTED) is not None


def confirm_candidate(uid: str, operator_open_id: str = "",
                      assignee_open_ids: Optional[List[str]] = None,
                      reviewer_open_id: Optional[str] = None) -> Optional[Task]:
    """人工确认候选 → 生成正式任务，并写第一条状态日志。

    可传入补正后的负责人列表。条件更新只让一个调用取得有效的待确认候选。
    权限或必填项校验失败时抛出 CandidateActionError，回滚对候选的更新。
    """
    current = db.now()
    with db.session_scope() as s:
        cand = _claim_candidate(s, uid, operator_open_id, CAND_CONFIRMED)
        if cand is None:
            return None

        owners = assignee_open_ids if assignee_open_ids is not None else list(
            cand.assignee_open_ids or []
        )
        if not (cand.title or "").strip() or "task_content" in (cand.missing_fields or []):
            raise CandidateActionError("请先补充任务内容，再确认建单")
        if not (cand.sender_open_id or "").strip():
            raise CandidateActionError("请先补充发起人，再确认建单")
        if assignee_open_ids is None and "assignee" in (cand.missing_fields or []):
            raise CandidateActionError("负责人身份尚未核实，请补充有效负责人后再确认")
        owners = list(dict.fromkeys(owners))
        if not owners or not all(isinstance(owner, str) and owner.startswith("ou_")
                                 and owner[3:].strip() and owner == owner.strip() for owner in owners):
            raise CandidateActionError("请先补充有效的负责人，再确认建单")
        reviewer = reviewer_open_id if reviewer_open_id is not None else (
            permissions.default_reviewer(cand.chat_id) or cand.sender_open_id
        )
        if not reviewer.startswith("ou_") or not reviewer[3:].strip() or reviewer != reviewer.strip():
            raise CandidateActionError("请指定有效的验收人")

        task = Task(
            uid=_uid(),
            candidate_uid=cand.uid,
            chat_id=cand.chat_id,
            title=cand.title,
            description=cand.description,
            deliverable=cand.deliverable,
            acceptance_criteria=cand.acceptance_criteria,
            deadline_text=cand.deadline_text,
            deadline_at=cand.deadline_at,
            priority=cand.priority,
            status=TASK_TODO if (cand.deliverable or "").strip() and cand.deadline_at else TASK_NEEDS_INFO,
            version=1,
            creator_open_id=cand.sender_open_id,
            reviewer_open_id=reviewer,
            created_at=current,
            updated_at=current,
        )
        s.add(task)
        s.flush()

        assignees = task.assignees
        for open_id in dict.fromkeys(owners):  # 去重且保序
            assignees.append(
                TaskAssignee(open_id=open_id, role="owner", created_at=current)
            )

        task.logs.append(TaskStatusLog(
            from_status=None, to_status=task.status,
            operator_open_id=operator_open_id or None, reason="候选确认后建单",
            created_at=current,
        ))

        # 状态在上面那条条件 UPDATE 里已经改成 confirmed 了，这里不用再改
        s.flush()
        return task


def list_tasks(status: Optional[str] = None, chat_id: Optional[str] = None,
               limit: int = 20) -> List[Task]:
    stmt = select(Task)
    if status:
        stmt = stmt.where(Task.status == status)
    if chat_id:
        stmt = stmt.where(Task.chat_id == chat_id)
    stmt = stmt.order_by(Task.created_at.desc()).limit(limit)
    with db.session_scope() as s:
        return list(s.scalars(stmt).all())


def get_task(uid: str) -> Optional[Task]:
    with db.session_scope() as s:
        return s.scalar(select(Task).where(Task.uid == uid))


def count_tasks() -> int:
    with db.session_scope() as s:
        return int(s.scalar(select(func.count()).select_from(Task)) or 0)


def count_tasks_by_status() -> dict:
    with db.session_scope() as s:
        rows = s.execute(
            select(Task.status, func.count()).group_by(Task.status)
        ).all()
    return {status: count for status, count in rows}


# ---------------------------------------------------------------- 状态流转

@dataclass
class TransitionResult:
    """一次状态流转的结果。conflict=True 表示版本冲突（别人先改了）。"""

    ok: bool
    task: Optional[Task] = None
    message: str = ""
    conflict: bool = False


def transition_task(task_uid: str, action: str, operator_open_id: str = "",
                    expected_version: Optional[int] = None,
                    note: Optional[str] = None) -> TransitionResult:
    """执行一次状态流转：校验状态机 → 校验权限 → 乐观锁更新 → 写流水。

    【为什么读和写要在一个事务里】
    两个人在卡片上同时点「开始处理」：都读到 v1，第一个 UPDATE 成功把版本推到 2，
    第二个的 WHERE version=1 就匹配不到 → rowcount=0 → 判定冲突。
    如果拆成两个事务（先读后来再写），中间那段窗口就是竞态。

    【为什么用 version 而不是状态做条件】
    用状态做条件挡不住「A 点开始、B 点报阻塞」这种：两个都合法，
    但基于的是同一个旧快照。version 保证「我以为的版本」和「实际版本」一致。
    """
    current = db.now()
    note = (note or "").strip()[:2000] or None

    with db.session_scope() as s:
        task = s.scalar(select(Task).where(Task.uid == task_uid))
        if task is None:
            return TransitionResult(False, message=f"任务不存在：{task_uid}")
        if not permissions.chat_enabled(task.chat_id):
            return TransitionResult(False, message="任务所在群未授权或已停用")

        if expected_version is not None and expected_version != task.version:
            return TransitionResult(
                False, task=task, conflict=True,
                message=f"任务已被别人更新（当前 v{task.version}），请刷新后重试",
            )

        # 1) 状态机：这个动作在当前状态下合不合法
        to_status, check = state_machine.resolve_action(task.status, action)
        if not check.ok:
            return TransitionResult(False, task=task, message=check.reason)

        # 2) 权限：这个人有没有资格做这个动作
        perm = permissions.can(task, operator_open_id, action)
        if not perm.ok:
            return TransitionResult(False, task=task, message=perm.reason)
        if action == "supplement" and task.missing_info:
            return TransitionResult(False, task=task,
                                    message="请回复本任务卡补充信息，填写交付物和截止时间")
        if action in ("reject", "cancel") and not (note or "").strip():
            example = "验收不通过：原因" if action == "reject" else "取消任务：原因"
            return TransitionResult(False, task=task, message=f"请回复本任务卡“{example}”确认操作并填写原因")

        if action == "submit" and task.status == TASK_PENDING_ACCEPTANCE:
            if not (note or "").strip():
                return TransitionResult(False, task=task, message="任务已待验收，可回复原任务卡补充完成说明")
            if task.completion_note == note.strip():
                return TransitionResult(True, task=task, message="完成说明已记录")

        from_status = task.status
        values = {
            "status": to_status,
            "version": Task.version + 1,
            "updated_at": current,
        }
        if action == "submit" and note:
            values["completion_note"] = note.strip()[:2000]
        if action in ("reject", "cancel", "accept") and note:
            values["review_note"] = note.strip()[:2000]
        if to_status == TASK_DONE:
            values["completed_at"] = current

        # 3) 乐观锁更新
        updated = s.execute(
            update(Task)
            .where(Task.uid == task_uid, Task.version == task.version)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if updated.rowcount != 1:
            return TransitionResult(
                False, task=task, conflict=True,
                message="任务状态刚刚被别人改过，请刷新后重试",
            )

        # 4) 流水：出争议时唯一能还原「谁在什么时候改了什么」的东西
        # reason 优先级：调用方写的备注 > 权限模块附带的原因（如「自验收」）> 动作名
        s.add(TaskStatusLog(
            task_id=task.id,
            from_status=from_status,
            to_status=to_status,
            operator_open_id=operator_open_id or None,
            reason=note or perm.reason or state_machine.ACTION_LABELS.get(action, action),
            created_at=current,
        ))
        s.flush()
        # 不能只 s.refresh(task)：refresh 会把关系属性打回「未加载」，
        # session 一关，调用方读 task.owner_open_ids 就 DetachedInstanceError。
        # 用 populate_existing 重新查一遍，selectin 关系会一起加载回来。
        task = s.scalars(
            select(Task)
            .where(Task.uid == task_uid)
            .execution_options(populate_existing=True)
        ).one()

    result = TransitionResult(
        True, task=task,
        message=f"{state_machine.STATUS_LABELS.get(from_status, from_status)}"
                f" → {state_machine.STATUS_LABELS.get(to_status, to_status)}",
    )

    _after_task_transition(result, action, from_status, operator_open_id)
    return result


def _after_task_transition(result, action: str, from_status: str, operator_open_id: str) -> None:
    task_uid = result.task.uid
    # 5) 事件通知：有人动了任务，得让该知道的人知道。
    # 放在 session 之外调用 —— events 会自己开一个会话写发件箱，
    # 嵌在同一个事务里会让「建单成功了但通知没入队」变成可能。
    # 延迟 import：events 依赖 repo，写在这里避免循环导入。
    try:
        import events
        events.on_transition(result, action, from_status, operator_open_id)
    except Exception as exc:
        # 通知失败不影响状态已经改成功这个事实，记日志就行
        print(f"[NOTIFY] 事件通知入队失败：{type(exc).__name__}: {exc}")

    # 6) 旧卡片同步：把这条任务历史上发出去的卡都刷成最新状态。
    # 回调的响应只改「被点的那一张」，其余卡片还是旧状态 ——
    # 不刷的话，用户翻到上面那张「阻塞」卡会以为任务还卡着。
    # 同样是后台线程：这里可能一次要改十几张卡，不能卡住回调的 3 秒预算。
    try:
        import card_sync
        card_sync.sync_async(task_uid, result.task.version)
    except Exception as exc:
        print(f"[CARD] 卡片同步启动失败：{type(exc).__name__}: {exc}")

    # 7) 日历同步：状态、标题和时间依据最新任务刷新，取消后删除。
    # 日历失败不能影响状态已经改成功这个事实，所以整段包在 try 里。
    try:
        import calendar_sync
        calendar_sync.on_transition_async(task_uid, action)
    except Exception as exc:
        print(f"[CAL] 日历同步启动失败：{type(exc).__name__}: {exc}")




def supplement_task(task_uid: str, operator_open_id: str, *,
                    deliverable: Optional[str] = None, deadline_at: Optional[datetime] = None,
                    deadline_text: Optional[str] = None, expected_version: Optional[int] = None,
                    note: Optional[str] = None) -> TransitionResult:
    """补齐待补充任务。仅接受人工提供的字段，并保留版本及变更日志。"""
    current = db.now()
    if deliverable is not None and (not isinstance(deliverable, str) or not deliverable.strip()):
        return TransitionResult(False, message="交付物不能为空")
    if deadline_at is not None and (
            not isinstance(deadline_at, datetime) or deadline_at.tzinfo is None or deadline_at <= current):
        return TransitionResult(False, message="请提供明确且尚未到期的截止时间")
    if deliverable is None and deadline_at is None:
        return TransitionResult(False, message="请填写交付物或截止时间")
    with db.session_scope() as session:
        task = session.scalar(select(Task).where(Task.uid == task_uid))
        if task is None:
            return TransitionResult(False, message="任务不存在")
        if not permissions.chat_enabled(task.chat_id) or not permissions.can(task, operator_open_id, "supplement").ok:
            return TransitionResult(False, message="当前账号无权补充这条任务的信息")
        if expected_version is not None and expected_version != task.version:
            return TransitionResult(False, task=task, conflict=True, message="任务已更新，请刷新后重试")
        if task.status != TASK_NEEDS_INFO:
            return TransitionResult(False, task=task, message="任务信息已完整，变更要求请按任务变更流程处理")
        new_deliverable = deliverable.strip()[:2000] if deliverable is not None else task.deliverable
        new_deadline = deadline_at if deadline_at is not None else task.deadline_at
        if new_deliverable == task.deliverable and new_deadline == task.deadline_at:
            return TransitionResult(True, task=task, message="这些信息已记录")
        status = TASK_TODO if task.owner_open_ids and new_deliverable and new_deadline else TASK_NEEDS_INFO
        values = dict(deliverable=new_deliverable, deadline_at=new_deadline,
                      status=status, version=Task.version + 1, updated_at=current)
        if deadline_text is not None:
            values["deadline_text"] = deadline_text[:64]
        changed = session.execute(update(Task).where(
            Task.uid == task_uid, Task.version == task.version, Task.status == TASK_NEEDS_INFO,
        ).values(**values).execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            return TransitionResult(False, conflict=True, message="任务刚刚被更新，请刷新后重试")
        session.add(TaskStatusLog(task_id=task.id, from_status=TASK_NEEDS_INFO, to_status=status,
                                  operator_open_id=operator_open_id, reason=(note or "人工补充任务信息")[:2000],
                                  created_at=current))
        session.flush()
        task = session.scalars(select(Task).where(Task.uid == task_uid).execution_options(populate_existing=True)).one()
    result = TransitionResult(True, task=task, message="信息已补齐，可以开始处理" if status == TASK_TODO else "信息已记录，仍需补充" + "、".join(task.missing_info))
    _after_task_transition(result, "supplement", TASK_NEEDS_INFO, operator_open_id)
    return result



def list_task_logs(task_uid: str) -> List[TaskStatusLog]:
    with db.session_scope() as s:
        task = s.scalar(select(Task).where(Task.uid == task_uid))
        if task is None:
            return []
        return list(s.scalars(
            select(TaskStatusLog)
            .where(TaskStatusLog.task_id == task.id)
            .order_by(TaskStatusLog.id)
        ).all())


# ---------------------------------------------------------------- 卡片登记（Day 14）

# 一条任务最多维护多少张「活卡片」。
# 为什么要有上限：一个大任务改十几次状态，历史卡片十几张，每次同步就要打
# 十几个接口。飞书单条消息 5 QPS、接口 50 QPS，量一大就会被限流。
# 只改最近的这些足够覆盖「用户眼前还看得到的卡」，更早的他自己也不翻了。
MAX_LIVE_CARDS = 20


def register_task_card(task_uid: str, message_id: str, receive_id: str,
                       receive_id_type: str = "open_id",
                       viewer_open_id: str = "",
                       kind: str = "task", version: int = 0) -> bool:
    """记下一张已发出的任务卡片。返回 True 表示新登记。

    没 message_id 就别记 —— 发送失败的卡片改不动，记了只会在同步时白刷日志。
    幂等：message_id 唯一，同一条重复登记直接忽略（重试发送时会发生）。
    """
    if not task_uid or not message_id:
        return False

    current = db.now()
    with db.session_scope() as s:
        task = s.scalar(select(Task).where(Task.uid == task_uid))
        if task is None:
            return False
        stmt = (
            sqlite_insert(TaskCard)
            .values(
                uid=_uid(),
                task_id=task.id,
                message_id=message_id,
                receive_id=receive_id,
                receive_id_type=receive_id_type,
                viewer_open_id=viewer_open_id or "",
                kind=kind,
                synced_version=int(version or 0),
                fail_count=0,
                retired=0,
                created_at=current,
                updated_at=current,
                synced_at=current,
            )
            .on_conflict_do_nothing(index_elements=["message_id"])
        )
        return s.execute(stmt).rowcount == 1


def list_live_task_cards(task_uid: str, limit: int = MAX_LIVE_CARDS) -> List[TaskCard]:
    """这条任务还没作废的卡片，最新的在前。"""
    with db.session_scope() as s:
        task = s.scalar(select(Task).where(Task.uid == task_uid))
        if task is None:
            return []
        return list(s.scalars(
            select(TaskCard)
            .where(TaskCard.task_id == task.id, TaskCard.retired == 0)
            .order_by(TaskCard.id.desc())
            .limit(limit)
        ).all())


def get_primary_task_card(task_uid: str, open_id: str) -> Optional[TaskCard]:
    """每人每个任务使用最早登记的私聊卡，包含确认后原地替换的卡。"""
    with db.session_scope() as s:
        return s.scalar(
            select(TaskCard).join(Task, TaskCard.task_id == Task.id)
            .where(Task.uid == task_uid, TaskCard.receive_id_type == "open_id",
                   TaskCard.receive_id == open_id, TaskCard.viewer_open_id == open_id,
                   TaskCard.retired == 0)
            .order_by(TaskCard.id.asc()).limit(1)
        )


def retire_task_card(message_id: str) -> None:
    with db.session_scope() as session:
        session.execute(update(TaskCard).where(TaskCard.message_id == message_id).values(retired=1))


def tasks_needing_card_sync(limit: int = 50) -> List[str]:
    duplicates = select(TaskCard.task_id).where(
        TaskCard.retired == 0, TaskCard.receive_id_type == "open_id",
    ).group_by(TaskCard.task_id, TaskCard.viewer_open_id).having(func.count() > 1)
    with db.session_scope() as session:
        return list(session.scalars(select(Task.uid).join(TaskCard).where(
            TaskCard.retired == 0,
            Task.chat_id.in_(permissions.enabled_chat_ids()),
            or_(TaskCard.synced_version < Task.version, Task.id.in_(duplicates)),
        ).distinct().limit(limit)).all())


def mark_task_card_synced(message_id: str, version: int) -> None:
    current = db.now()
    with db.session_scope() as s:
        s.execute(
            update(TaskCard)
            .where(TaskCard.message_id == message_id)
            .values(synced_version=int(version or 0), fail_count=0,
                    last_error=None, synced_at=current, updated_at=current)
            .execution_options(synchronize_session=False)
        )


def mark_task_card_failed(message_id: str, error: str = "",
                          max_fail: int = 3) -> None:
    """记一次同步失败。连续失败到上限就退休，不再试。

    为什么不一失败就退休：网络抖动、飞书限流都是可恢复的，下一轮再试就好。
    但超过 14 天的消息飞书硬性不让改（错误码 230031），那种无论试多少次
    都不会成功，靠 fail_count 自然淘汰。
    """
    with db.session_scope() as s:
        row = s.scalar(select(TaskCard).where(TaskCard.message_id == message_id))
        if row is None:
            return
        row.fail_count = (row.fail_count or 0) + 1
        row.last_error = (error or "")[:200]
        row.updated_at = db.now()
        if row.fail_count >= max_fail:
            row.retired = 1


def count_task_cards() -> dict:
    with db.session_scope() as s:
        rows = s.execute(
            select(TaskCard.retired, func.count()).group_by(TaskCard.retired)
        ).all()
    return {"retired" if flag else "live": count for flag, count in rows}


# ---------------------------------------------------------------- 通知发件箱（Day 12）

def list_active_tasks_with_deadline() -> List[Task]:
    """所有「还没结束、且有截止时间」的任务。日报就是从这里扫出来的。

    没有截止时间的任务不进日报 —— 提醒的前提是「有个时间要到了」，
    没有时间的东西放进日报只会稀释注意力。
    """
    with db.session_scope() as s:
        return list(s.scalars(
            select(Task)
            .where(Task.status.notin_((*TASK_TERMINAL, TASK_NEEDS_INFO)))
            .where(Task.chat_id.in_(permissions.enabled_chat_ids()))
            .where(Task.deadline_at.is_not(None))
            .order_by(Task.deadline_at)
        ).all())


def enqueue_notification(dedupe_key: str, receive_id: str, payload: str,
                         kind: str = "digest",
                         receive_id_type: str = "open_id",
                         msg_type: str = "interactive",
                         scheduled_at=None,
                         task_uid: Optional[str] = None,
                         task_version: int = 0) -> bool:
    """通知入队。新建或重新规划成功返回 True，已有待发送／已发送记录返回 False。

    【为什么用 on_conflict_do_nothing 而不是先查后插】
    先 SELECT 再 INSERT 之间有空窗：定时任务重叠执行（上一轮没跑完、下一轮
    已经启动）时两边都查不到，就会插两条。让数据库的唯一约束来判，
    是唯一不会错的写法。

    【为什么这段只对 SQLite 成立】
    sqlite_insert 是 SQLite 方言。将来换 PostgreSQL 要改成
    postgresql.insert(...).on_conflict_do_nothing()。换成通用写法
    （try/except IntegrityError）也可以，但异常里区分不了「哪个约束冲突」，
    不如下方言清楚。
    """
    current = scheduled_at or db.now()
    with db.session_scope() as s:
        stmt = (
            sqlite_insert(NotificationOutbox)
            .values(
                uid=_uid(),
                dedupe_key=dedupe_key,
                kind=kind,
                receive_id=receive_id,
                receive_id_type=receive_id_type,
                msg_type=msg_type,
                payload=payload,
                task_uid=task_uid,
                task_version=int(task_version or 0),
                status=NOTIFY_PENDING,
                attempts=0,
                scheduled_at=current,
                created_at=current,
                updated_at=current,
            )
        )
        if kind in ("due_24h", "overdue", "task_info"):
            # 字段补齐后，先前因条件不满足且从未投递的提醒可以重新规划。
            stmt = stmt.on_conflict_do_update(
                index_elements=["dedupe_key"],
                set_=dict(status=NOTIFY_PENDING, payload=payload, scheduled_at=current,
                          task_version=int(task_version or 0), attempts=0, last_error=None,
                          updated_at=current),
                where=and_(NotificationOutbox.status == NOTIFY_SKIPPED,
                           NotificationOutbox.last_error == "任务状态或提醒条件已改变",
                           NotificationOutbox.sent_at.is_(None), NotificationOutbox.kind == kind,
                           NotificationOutbox.receive_id == receive_id),
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=["dedupe_key"])
        return s.execute(stmt).rowcount == 1


def claim_pending_notifications(due_before=None, limit: int = 50) -> List[NotificationOutbox]:
    """取出到点该发的通知，按计划时间从早到晚。

    单进程 Worker 不需要「抢占」标记；哪天要跑多实例，这里得改成
    UPDATE ... SET status='sending' WHERE uid=? AND status='pending'
    再用 rowcount 判定归属（和 confirm_candidate 是同一个套路）。
    """
    due = due_before or db.now()
    with db.session_scope() as s:
        return list(s.scalars(
            select(NotificationOutbox)
            .where(NotificationOutbox.status == NOTIFY_PENDING)
            .where(NotificationOutbox.scheduled_at <= due)
            .order_by(NotificationOutbox.scheduled_at)
            .limit(limit)
        ).all())


def mark_notification_sent(outbox_uid: str) -> None:
    with db.session_scope() as s:
        s.execute(
            update(NotificationOutbox)
            .where(NotificationOutbox.uid == outbox_uid)
            .values(status=NOTIFY_SENT, sent_at=db.now(), updated_at=db.now())
            .execution_options(synchronize_session=False)
        )


def mark_notification_failed(outbox_uid: str, error: str,
                            max_attempts: int = 3) -> str:
    """记一次失败。返回该条的新状态：pending（还会重试）或 failed（放弃）。

    为什么不立刻标 failed：网络抖动、飞书限流、对方还没授权，都是可恢复的。
    下一次 tick 会重试，直到超过 max_attempts。
    """
    with db.session_scope() as s:
        row = s.scalar(
            select(NotificationOutbox).where(NotificationOutbox.uid == outbox_uid)
        )
        if row is None:
            return "missing"
        attempts = (row.attempts or 0) + 1
        new_status = NOTIFY_FAILED if attempts >= max_attempts else NOTIFY_PENDING
        row.attempts = attempts
        row.last_error = (error or "")[:500]
        row.status = new_status
        row.updated_at = db.now()
        return new_status


def mark_notification_skipped(outbox_uid: str, reason: str = "") -> None:
    with db.session_scope() as s:
        s.execute(
            update(NotificationOutbox)
            .where(NotificationOutbox.uid == outbox_uid)
            .values(status=NOTIFY_SKIPPED, last_error=(reason or "")[:500],
                    updated_at=db.now())
            .execution_options(synchronize_session=False)
        )


def last_task_reminder_sent_times(kind: str) -> dict:
    """每条任务最近一次「成功发出」的逾期提醒时间 → {task_uid: sent_at}。

    为什么要查已发送记录而不是「记一个 last_reminded_at 字段」：
    发件箱本来就是这件事的审计日志，再存一份状态就是两个真相源，
    迟早对不上。而且它天然只在「真发出去了」之后才算数 ——
    入队了但被免打扰压住、或发送失败重试中的，都不该把节流计时器往前推。

    status=sent 是硬条件：发失败的不能算提醒过了，否则用户永远收不到。
    """
    with db.session_scope() as s:
        rows = s.execute(
            select(NotificationOutbox.task_uid, func.max(NotificationOutbox.sent_at))
            .where(NotificationOutbox.kind == kind,
                   NotificationOutbox.status == NOTIFY_SENT,
                   NotificationOutbox.task_uid.is_not(None))
            .group_by(NotificationOutbox.task_uid)
        ).all()
    return {task_uid: sent_at for task_uid, sent_at in rows if sent_at is not None}


def last_overdue_sent_times() -> dict:
    return last_task_reminder_sent_times("overdue")


def list_tasks_needing_info() -> List[Task]:
    with db.session_scope() as session:
        return list(session.scalars(select(Task).where(
            Task.status == TASK_NEEDS_INFO, Task.chat_id.in_(permissions.enabled_chat_ids()),
        ).order_by(Task.created_at)).all())


def list_notifications(status: Optional[str] = None,
                       limit: int = 20) -> List[NotificationOutbox]:
    stmt = select(NotificationOutbox)
    if status:
        stmt = stmt.where(NotificationOutbox.status == status)
    stmt = stmt.order_by(NotificationOutbox.created_at.desc()).limit(limit)
    with db.session_scope() as s:
        return list(s.scalars(stmt).all())


def count_notifications_by_status() -> dict:
    with db.session_scope() as s:
        rows = s.execute(
            select(NotificationOutbox.status, func.count())
            .group_by(NotificationOutbox.status)
        ).all()
    return {status: count for status, count in rows}


# ---------------------------------------------------------------- 日历映射（Day 16）

def get_calendar_event(task_uid: str) -> Optional[TaskCalendarEvent]:
    with db.session_scope() as s:
        return s.scalar(
            select(TaskCalendarEvent).where(TaskCalendarEvent.task_uid == task_uid)
        )


def list_personal_calendar_events(task_uid: str) -> List[PersonalCalendarEvent]:
    with db.session_scope() as session:
        return list(session.scalars(select(PersonalCalendarEvent).where(
            PersonalCalendarEvent.task_uid == task_uid,
        )).all())


def reserve_personal_calendar_event(task_uid: str, open_id: str) -> PersonalCalendarEvent:
    with db.session_scope() as session:
        session.execute(sqlite_insert(PersonalCalendarEvent).values(
            task_uid=task_uid, open_id=open_id, calendar_id="", event_id="",
            idempotency_key="task-personal-" + _uid(), status=CAL_PENDING, synced_version=0,
        ).on_conflict_do_nothing(index_elements=["task_uid", "open_id"]))
        return session.scalar(select(PersonalCalendarEvent).where(
            PersonalCalendarEvent.task_uid == task_uid, PersonalCalendarEvent.open_id == open_id,
        ))


def update_personal_calendar_event(row_id: int, **fields) -> None:
    with db.session_scope() as session:
        session.execute(update(PersonalCalendarEvent).where(
            PersonalCalendarEvent.id == row_id,
        ).values(**fields))


def reserve_calendar_event(task_uid: str, calendar_id: str,
                           deadline_at) -> TaskCalendarEvent:
    """占坑并返回这一行。已存在就原样返回（**保留原来的 idempotency_key**）。

    这一步是幂等的核心：先落库再调飞书接口。就算接口成功、进程崩在写回
    event_id 之前，重试时带着同一个 key，飞书会返回原来的日程而不是再建一个。
    """
    current = db.now()
    with db.session_scope() as s:
        s.execute(
            sqlite_insert(TaskCalendarEvent)
            .values(
                uid=_uid(),
                task_uid=task_uid,
                calendar_id=calendar_id or "",
                event_id="",
                # "taskcal-" + 32 位 hex = 40 字符，落在官方要求的 32~128 内
                idempotency_key="taskcal-" + _uid(),
                deadline_at=deadline_at,
                status=CAL_PENDING,
                attendees_synced=0,
                fail_count=0,
                created_at=current,
                updated_at=current,
            )
            .on_conflict_do_nothing(index_elements=["task_uid"])
        )
        row = s.scalar(
            select(TaskCalendarEvent).where(TaskCalendarEvent.task_uid == task_uid)
        )
        # 一旦选定日历便保持不变，超时重试必须仍使用同一本日历和幂等键。
        if row and calendar_id and not row.calendar_id:
            row.calendar_id = calendar_id
            row.updated_at = current
    assert row is not None
    return row


def _update_calendar_event(task_uid: str, **fields) -> None:
    with db.session_scope() as s:
        s.execute(
            update(TaskCalendarEvent)
            .where(TaskCalendarEvent.task_uid == task_uid)
            .values(**fields, updated_at=db.now())
        )


def mark_calendar_event_created(task_uid: str, event_id: str) -> None:
    _update_calendar_event(
        task_uid, status=CAL_CREATED, event_id=event_id,
        last_error=None, synced_at=db.now(),
    )


def mark_calendar_event_attendees_synced(task_uid: str) -> None:
    _update_calendar_event(task_uid, attendees_synced=1, last_error=None)


def mark_calendar_event_updated(task_uid: str, deadline_at=None) -> None:
    fields = {"last_error": None, "synced_at": db.now()}
    if deadline_at is not None:
        fields["deadline_at"] = deadline_at
    _update_calendar_event(task_uid, **fields)


def mark_calendar_event_deleted(task_uid: str) -> None:
    _update_calendar_event(
        task_uid, status=CAL_DELETED, last_error=None, synced_at=db.now(),
    )


def mark_calendar_event_skipped(task_uid: str, reason: str = "") -> None:
    reserve_calendar_event(task_uid, "", None)
    _update_calendar_event(
        task_uid, status=CAL_SKIPPED, last_error=(reason or "")[:200],
    )


def mark_calendar_event_failed(task_uid: str, error: str = "") -> None:
    _update_calendar_event(
        task_uid, status=CAL_FAILED,
    )
    defer_calendar_sync(task_uid, error)


def mark_calendar_event_warning(task_uid: str, error: str = "") -> None:
    """日程已经建好，只是参与人没加上。状态保持 created，别让重试误判成没建。"""
    _update_calendar_event(task_uid, last_error=(error or "")[:200])


def finish_calendar_sync(task_uid: str, version: int, deadline_at) -> None:
    """只记录本次实际同步的快照，任务随后变化时仍会被 Worker 选中。"""
    _update_calendar_event(task_uid, synced_version=version, deadline_at=deadline_at,
                           retry_at=None, fail_count=0, last_error=None, synced_at=db.now())


def defer_calendar_sync(task_uid: str, error: str) -> None:
    """失败后按 1、2、4…分钟退避，最长一小时，重启后继续补偿。"""
    row = get_calendar_event(task_uid)
    if row is None:
        return
    failures = row.fail_count + 1
    _update_calendar_event(
        task_uid, fail_count=failures, last_error=error[:200],
        retry_at=db.now() + timedelta(minutes=min(60, 2 ** min(failures - 1, 6))),
    )


def clear_calendar_event(task_uid: str) -> None:
    """已删除无截止时间的日程，补齐截止后使用新幂等键创建。"""
    _update_calendar_event(task_uid, status=CAL_SKIPPED, event_id="",
                           attendees_synced=0, request_body=None,
                           idempotency_key="taskcal-" + _uid())


def prepare_calendar_request(task_uid: str, request_body: str) -> str:
    """保留首次创建的请求体，超时和重启后的恢复使用同一份请求。"""
    with db.session_scope() as s:
        row = s.scalar(select(TaskCalendarEvent).where(TaskCalendarEvent.task_uid == task_uid))
        if row.request_body is None:
            row.request_body = request_body
        return row.request_body


def pending_calendar_tasks(now, limit: int = 50, chat_ids=None, mode: str = "application") -> List[str]:
    """选取缺失、落后或失败到期的日程，也覆盖创建前进程退出的情况。"""
    row = TaskCalendarEvent
    import os
    cutoff = os.getenv("CALENDAR_NEW_TASK_AFTER_ID", "")
    strict = os.getenv("CALENDAR_PERSONAL_NEW_ONLY", "0") == "1"
    if strict and not cutoff.isdigit():
        return []
    with db.session_scope() as s:
        return list(s.scalars(
            select(Task.uid)
            .outerjoin(row, row.task_uid == Task.uid)
            .where(
                Task.id > int(cutoff) if strict else True,
                Task.chat_id.in_(chat_ids) if chat_ids is not None else True,
                or_(row.retry_at.is_(None), row.retry_at <= now),
                or_(
                    row.sync_mode != mode,
                    and_(row.id.is_(None), Task.status.notin_(TASK_TERMINAL),
                         Task.deadline_at.is_not(None)),
                    row.synced_version < Task.version,
                    and_(row.status == CAL_CREATED, row.attendees_synced == 0),
                    row.status.in_((CAL_PENDING, CAL_FAILED)),
                    row.deadline_at != Task.deadline_at,
                    and_(row.deadline_at.is_(None), Task.deadline_at.is_not(None)),
                    and_(row.deadline_at.is_not(None), Task.deadline_at.is_(None)),
                ),
            )
            .order_by(row.updated_at.asc(), Task.created_at.asc())
            .limit(limit)
        ).all())


def list_calendar_events(limit: int = 20) -> List[TaskCalendarEvent]:
    with db.session_scope() as s:
        return list(s.scalars(
            select(TaskCalendarEvent)
            .order_by(TaskCalendarEvent.created_at.desc())
            .limit(limit)
        ).all())


def mark_calendar_sync_mode(task_uid: str, mode: str) -> None:
    _update_calendar_event(task_uid, sync_mode=mode)
