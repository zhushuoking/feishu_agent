"""Day 8：仓储层。所有数据库读写都从这里走，业务代码不直接碰 SQL。

为什么单独一层而不是在 main.py 里 session.commit()：
1. 卡片回调（Day 9）和定时 Worker（Day 12）都会复用同一套读写，
   逻辑散在入口文件里，迟早出现「这里加了口过滤条件、那里忘了」
2. 单测可以只测这一层，不需要启动飞书长连接
3. 后面换 PostgreSQL 时，改的是 db.py 一行，不是全项目
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import List, Optional

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import db
import permissions
import state_machine
from models import MessageCommand
from models_db import (
    CAND_CONFIRMED, CAND_EXPIRED, CAND_PENDING, CAND_REJECTED,
    NOTIFY_FAILED, NOTIFY_PENDING, NOTIFY_SENT, NOTIFY_SKIPPED,
    TASK_DONE, TASK_TERMINAL, TASK_TODO,
    NotificationOutbox, Task, TaskAssignee, TaskCandidate, TaskCard, TaskStatusLog,
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


def count_candidates_by_status() -> dict:
    with db.session_scope() as s:
        rows = s.execute(
            select(TaskCandidate.status, func.count()).group_by(TaskCandidate.status)
        ).all()
    return {status: count for status, count in rows}


def expire_candidates(limit: int = 500) -> int:
    """把过了 expires_at 还没人处理的候选标记为 expired。

    Day 12 的 Worker 会定时调它。过期不删数据 —— 保留是为了复盘
    「模型到底提了多少没人理的候选」，这个指标直接反映提示词质量。
    """
    current = db.now()
    with db.session_scope() as s:
        rows = s.scalars(
            select(TaskCandidate)
            .where(TaskCandidate.status == CAND_PENDING)
            .where(TaskCandidate.expires_at.is_not(None))
            .where(TaskCandidate.expires_at < current)
            .limit(limit)
        ).all()
        for row in rows:
            row.status = CAND_EXPIRED
            row.updated_at = current
        return len(rows)


def reject_candidate(uid: str, operator_open_id: str = "", reason: str = "") -> bool:
    with db.session_scope() as s:
        # 条件更新抢占：只有还处于 pending 的候选能被驳回，
        # 两个人在卡片上同时点，只有一个的 rowcount 会是 1。
        claimed = s.execute(
            update(TaskCandidate)
            .where(TaskCandidate.uid == uid, TaskCandidate.status == CAND_PENDING)
            .values(status=CAND_REJECTED, updated_at=db.now())
            .execution_options(synchronize_session=False)
        )
        return claimed.rowcount == 1


def confirm_candidate(uid: str, operator_open_id: str = "",
                      assignee_open_ids: Optional[List[str]] = None) -> Optional[Task]:
    """人工确认候选 → 生成正式任务，并写第一条状态日志。

    assignee_open_ids 允许覆盖候选阶段推断的负责人（卡片上可以改人）。

    【为什么要条件更新】
    飞书卡片回调 3 秒不响应会重推，用户也可能连点两下。如果先 SELECT 判断
    再 INSERT，两个并发请求都能读到 pending，就会建出两张单。
    所以先用一条 UPDATE ... WHERE status='pending' 抢占，靠 rowcount 判定归属，
    抢不到的那个直接返回 None（回调里回一句"已处理"）。
    """
    current = db.now()
    with db.session_scope() as s:
        claimed = s.execute(
            update(TaskCandidate)
            .where(TaskCandidate.uid == uid, TaskCandidate.status == CAND_PENDING)
            .values(status=CAND_CONFIRMED, updated_at=current)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            return None  # 不存在、已确认、已驳回、已过期

        cand = s.scalar(select(TaskCandidate).where(TaskCandidate.uid == uid))
        if cand is None:  # 理论上不可能，防御一下
            return None

        owners = assignee_open_ids if assignee_open_ids is not None else list(
            cand.assignee_open_ids or []
        )

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
            status=TASK_TODO,
            version=1,
            creator_open_id=cand.sender_open_id,
            created_at=current,
            updated_at=current,
        )
        s.add(task)
        s.flush()

        # 先触达一次关系集合，让它变成已加载状态。
        # 不这么做的话，负责人为空（没人 @）的任务会走进「一次都没访问 assignees」的分支，
        # session 关闭后读 owner_open_ids 就 DetachedInstanceError。
        assignees = task.assignees
        for open_id in dict.fromkeys(owners):  # 去重且保序
            assignees.append(
                TaskAssignee(open_id=open_id, role="owner", created_at=current)
            )

        task.logs.append(TaskStatusLog(
            from_status=None, to_status=TASK_TODO,
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

    with db.session_scope() as s:
        task = s.scalar(select(Task).where(Task.uid == task_uid))
        if task is None:
            return TransitionResult(False, message=f"任务不存在：{task_uid}")

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

        from_status = task.status
        values = {
            "status": to_status,
            "version": Task.version + 1,
            "updated_at": current,
        }
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
            .where(Task.status.notin_(TASK_TERMINAL))
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
    """往发件箱塞一条通知。返回 True 表示真的新建了，False 表示这条已经在队列里。

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
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
        )
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


def last_overdue_sent_times() -> dict:
    """每条任务最近一次「成功发出」的逾期提醒时间 → {task_uid: sent_at}。

    为什么要查已发送记录而不是「记一个 last_reminded_at 字段」：
    发件箱本来就是这件事的审计日志，再存一份状态就是两个真相源，
    迟早对不上。而且它天然只在「真发出去了」之后才算数 ——
    入队了但被免打扰压住、或发送失败重试中的，都不该把节流计时器往前推。

    status=sent 是硬条件：发失败的不能算提醒过了，否则用户永远收不到。
    """
    with db.session_scope() as s:
        rows = list(s.scalars(
            select(NotificationOutbox)
            .where(NotificationOutbox.kind == "overdue",
                   NotificationOutbox.status == NOTIFY_SENT,
                   NotificationOutbox.task_uid.is_not(None))
            .order_by(NotificationOutbox.id)
        ).all())
    out = {}
    for row in rows:  # 按 id 升序，后面的覆盖前面的 = 取最新那条
        if row.task_uid and row.sent_at:
            out[row.task_uid] = row.sent_at
    return out


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
