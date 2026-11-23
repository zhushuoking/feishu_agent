"""Day 8：业务表定义（对应设计文档第 9 章数据模型）。

为什么候选和任务要分开两张表：
候选是「模型觉得像任务」，任务才是「人确认过、要跟进的东西」。
两者生命周期完全不同 —— 候选会被忽略、会 72 小时过期、会被驳回，
这些都不该在正式任务表里留下垃圾数据。设计文档 9.2 就是这么分的。

主键策略：对外（卡片回调、日志）一律用 uid（uuid4 hex），不用自增 id。
卡片按钮里带出去的东西用户能改，自增 ID 可以被枚举着改，
用 uid 至少让人猜不到下一条是谁。

状态字段一律存字符串而不是数据库 ENUM：
SQLite 改 ENUM 等于重建表，首期表结构还会变，用字符串 + 应用层校验更省事。
"""

from typing import List, Optional

from sqlalchemy import ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import Base, TZAwareDateTime, now

# ---- 候选状态 ----
CAND_PENDING = "pending"       # 等人工确认
CAND_CONFIRMED = "confirmed"   # 已转成正式任务
CAND_REJECTED = "rejected"     # 人工驳回：不是任务
CAND_EXPIRED = "expired"       # 超时未处理

# ---- 任务状态机（设计文档 7.2）----
TASK_TODO = "TODO"
TASK_NEEDS_INFO = "NEEDS_INFO"
TASK_IN_PROGRESS = "IN_PROGRESS"
TASK_BLOCKED = "BLOCKED"
TASK_PENDING_ACCEPTANCE = "PENDING_ACCEPTANCE"
TASK_DONE = "DONE"
TASK_CANCELLED = "CANCELLED"

TASK_STATUSES = (
    TASK_NEEDS_INFO, TASK_TODO, TASK_IN_PROGRESS, TASK_BLOCKED,
    TASK_PENDING_ACCEPTANCE, TASK_DONE, TASK_CANCELLED,
)
# 终态：不再提醒、不再允许改状态
TASK_TERMINAL = (TASK_DONE, TASK_CANCELLED)


class TaskCandidate(Base):
    """模型识别出的一条候选任务，等人工确认。"""

    __tablename__ = "task_candidate"
    __table_args__ = (
        # 同一条消息的第 seq 条候选唯一 → 重复投递/重跑识别都不会插重
        UniqueConstraint("message_id", "seq", name="uq_candidate_message_seq"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uid: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    seq: Mapped[int] = mapped_column(Integer, default=1)

    # 来源消息
    event_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    message_id: Mapped[str] = mapped_column(String(64), index=True)
    chat_id: Mapped[str] = mapped_column(String(64), index=True)
    chat_type: Mapped[Optional[str]] = mapped_column(String(16))
    sender_open_id: Mapped[Optional[str]] = mapped_column(String(64))
    message_sent_at: Mapped[Optional[object]] = mapped_column(TZAwareDateTime)
    raw_text: Mapped[Optional[str]] = mapped_column(Text)

    # 识别结果
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[Optional[str]] = mapped_column(Text)
    deliverable: Mapped[Optional[str]] = mapped_column(Text)
    acceptance_criteria: Mapped[Optional[str]] = mapped_column(Text)
    deadline_text: Mapped[Optional[str]] = mapped_column(String(64))
    deadline_at: Mapped[Optional[object]] = mapped_column(TZAwareDateTime)
    deadline_status: Mapped[str] = mapped_column(String(16), default="missing")
    deadline_rule: Mapped[Optional[str]] = mapped_column(String(64))
    priority: Mapped[str] = mapped_column(String(8), default="normal")
    candidate_type: Mapped[str] = mapped_column(String(16), default="task")
    confidence: Mapped[float] = mapped_column(default=0.0)

    # 候选阶段负责人还是「可能的人」，没确认，所以用 JSON 存
    assignee_open_ids: Mapped[list] = mapped_column(JSON, default=list)
    missing_fields: Mapped[list] = mapped_column(JSON, default=list)
    evidence: Mapped[Optional[str]] = mapped_column(Text)
    clarification_question: Mapped[Optional[str]] = mapped_column(Text)

    # 生命周期
    status: Mapped[str] = mapped_column(String(16), default=CAND_PENDING, index=True)
    expires_at: Mapped[Optional[object]] = mapped_column(TZAwareDateTime, index=True)
    created_at: Mapped[object] = mapped_column(TZAwareDateTime, default=now)
    updated_at: Mapped[object] = mapped_column(TZAwareDateTime, default=now, onupdate=now)

    @property
    def needs_info(self) -> bool:
        return bool(self.missing_fields) or self.deadline_status in {"missing", "unresolved"}

    def __repr__(self) -> str:
        return f"<Candidate {self.uid[:8]} {self.status} {self.title[:20]!r}>"


class Task(Base):
    """人工确认后的正式任务。"""

    __tablename__ = "task"
    __table_args__ = (
        # 注意：这里必须显式起名。SQLAlchemy 默认索引名是 ix_<表>_<列>，
        # task.candidate_uid 会生成 ix_task_candidate_uid，
        # 和 task_candidate.uid 撞成同一个名字，create_all 直接报
        # "index already exists"。踩过一次，别再踩。
        Index("ix_task_source_candidate", "candidate_uid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uid: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    candidate_uid: Mapped[Optional[str]] = mapped_column(String(32))

    chat_id: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[Optional[str]] = mapped_column(Text)
    deliverable: Mapped[Optional[str]] = mapped_column(Text)
    acceptance_criteria: Mapped[Optional[str]] = mapped_column(Text)
    deadline_text: Mapped[Optional[str]] = mapped_column(String(64))
    deadline_at: Mapped[Optional[object]] = mapped_column(TZAwareDateTime, index=True)
    priority: Mapped[str] = mapped_column(String(8), default="normal")

    status: Mapped[str] = mapped_column(String(24), default=TASK_TODO, index=True)
    # 乐观锁：两个人在卡片上同时点状态，后到的会撞版本号而不是互相覆盖
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    creator_open_id: Mapped[Optional[str]] = mapped_column(String(64))
    reviewer_open_id: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[object] = mapped_column(TZAwareDateTime, default=now)
    updated_at: Mapped[object] = mapped_column(TZAwareDateTime, default=now, onupdate=now)
    completed_at: Mapped[Optional[object]] = mapped_column(TZAwareDateTime)
    completion_note: Mapped[Optional[str]] = mapped_column(Text)
    review_note: Mapped[Optional[str]] = mapped_column(Text)

    assignees: Mapped[List["TaskAssignee"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", lazy="selectin"
    )
    logs: Mapped[List["TaskStatusLog"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def effective_reviewer_open_id(self) -> Optional[str]:
        return self.reviewer_open_id or self.creator_open_id

    @property
    def is_overdue(self) -> bool:
        """是否逾期。派生字段，不落库 —— 落库就要有 Worker 定时改，
        一天不改就全错。设计文档 7.4 明确用派生。"""
        if self.status in (*TASK_TERMINAL, TASK_NEEDS_INFO) or self.deadline_at is None:
            return False
        return self.deadline_at < now()

    @property
    def owner_open_ids(self) -> List[str]:
        return [a.open_id for a in self.assignees if a.role == "owner"]

    @property
    def missing_info(self) -> List[str]:
        fields = []
        if not self.owner_open_ids:
            fields.append("负责人")
        if not (self.deliverable or "").strip():
            fields.append("交付物")
        if self.deadline_at is None:
            fields.append("截止时间")
        return fields

    def __repr__(self) -> str:
        return f"<Task {self.uid[:8]} {self.status} {self.title[:20]!r}>"


class TaskAssignee(Base):
    """任务负责人 / 协作人。"""

    __tablename__ = "task_assignee"
    __table_args__ = (
        UniqueConstraint("task_id", "open_id", name="uq_assignee_task_open"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("task.id", ondelete="CASCADE"), index=True
    )
    open_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16), default="owner")  # owner / collaborator
    created_at: Mapped[object] = mapped_column(TZAwareDateTime, default=now)

    task: Mapped["Task"] = relationship(back_populates="assignees")


class TaskStatusLog(Base):
    """状态变更流水。谁在什么时候把任务从什么状态改成了什么状态。

    这张表是审计的底座：出争议时唯一能还原「当时到底谁点的」的东西。
    """

    __tablename__ = "task_status_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("task.id", ondelete="CASCADE"), index=True
    )
    from_status: Mapped[Optional[str]] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24))
    operator_open_id: Mapped[Optional[str]] = mapped_column(String(64))
    reason: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[object] = mapped_column(TZAwareDateTime, default=now, index=True)

    task: Mapped["Task"] = relationship(back_populates="logs")


class TaskCard(Base):
    """已经发出去的任务卡片登记（Day 14 补丁：旧卡片同步）。

    【为什么非要有这张表】
    飞书卡片是「发出去就定住」的静态消息。状态变了，只有「被点的那一张」
    会跟着回调的响应变，其余历史卡片还写着旧状态。
    后果就是用户的原话：「我解除阻塞后，任务发送方还显示任务阻塞」——
    他翻到三天前那张红着的「阻塞」卡，以为任务还卡着。

    这比不发通知更糟：发通知是为了让人知道进展，结果旧消息在撒谎。

    所以每发一张任务卡就把 message_id 记下来，状态一变回头把它们全改掉。
    卡片上 config 里的 "update_multi": true 必须保持 —— 飞书要求更新前后
    都显式声明，否则改不动（那个字段 cards.py 里一直有）。

    限制（写在代码里，免得到时踩）：
    - 只能改 14 天内发的消息，更早的一律失败，直接标记 retired 别再试
    - 单条消息 5 QPS，一次状态变更同步十来张卡是安全的
    - 消息必须没被撤回、没被删除
    """

    __tablename__ = "task_card"
    __table_args__ = (
        UniqueConstraint("message_id", name="uq_task_card_message"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uid: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("task.id", ondelete="CASCADE"), index=True
    )

    message_id: Mapped[str] = mapped_column(String(64), index=True)
    receive_id: Mapped[str] = mapped_column(String(64), index=True)
    receive_id_type: Mapped[str] = mapped_column(String(16), default="open_id")
    # 空字符串 = 公共卡（发在群里，一张卡多人看）。有值 = 那个人的私聊卡。
    # 同步时要按它重新渲染，否则会把「按张三权限裁剪的按钮」发给李四。
    viewer_open_id: Mapped[str] = mapped_column(String(64), default="")

    kind: Mapped[str] = mapped_column(String(16), default="task")  # task / event
    # 这张卡最后一次对齐到的任务版本。同步时只改「落后于当前版本」的卡，
    # 刚发出去的那张已经是新的，不用再打一次接口。
    synced_version: Mapped[int] = mapped_column(Integer, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    retired: Mapped[int] = mapped_column(Integer, default=0)  # 1 = 别再改了
    last_error: Mapped[Optional[str]] = mapped_column(String(200))

    created_at: Mapped[object] = mapped_column(TZAwareDateTime, default=now)
    updated_at: Mapped[object] = mapped_column(TZAwareDateTime, default=now, onupdate=now)
    synced_at: Mapped[Optional[object]] = mapped_column(TZAwareDateTime)

    def __repr__(self) -> str:
        return f"<TaskCard {self.message_id[:12]} v{self.synced_version} ret={self.retired}>"


# ---- 通知状态 ----
NOTIFY_PENDING = "pending"   # 等发送
NOTIFY_SENT = "sent"         # 已发出
NOTIFY_FAILED = "failed"     # 重试用尽，等人看
NOTIFY_SKIPPED = "skipped"   # 主动放弃（过时了、免打扰、空内容）


class NotificationOutbox(Base):
    """通知发件箱（Day 12）。

    为什么不直接调接口发：
    1. 发失败就丢了 —— 提醒的价值全在「准时送到」，丢一条等于没有
    2. 定时任务重跑会重复发 —— 靠 dedupe_key 唯一约束天然幂等
    3. 发送和业务耦合 —— 日报生成是纯计算，发送会被限流，混在一起一抖动就
       丢一整天

    所以：先算清楚「该发什么」落库，再让 Worker 去发。发成功标 sent，
    失败累加 attempts，超过上限标 failed 等人看。
    """

    __tablename__ = "notify_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uid: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    # 幂等键。同一键只允许一条，重复入队直接忽略。
    # 例：DIGEST:2026-11-17:ou_xxx（某人某天的日报）
    dedupe_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(24), default="digest")

    receive_id: Mapped[str] = mapped_column(String(64), index=True)
    receive_id_type: Mapped[str] = mapped_column(String(16), default="open_id")
    msg_type: Mapped[str] = mapped_column(String(16), default="interactive")

    # 这条通知说的是哪条任务、哪个版本（Day 14 加）。
    # 发送成功之后要靠它把这张新卡登记进 task_card —— 否则 Worker 发完
    # 就把 message_id 丢了，那张卡从此再也不会被同步。
    # 版本号单独存：发送那一刻 task.version 可能已经被下一个人改过了，
    # 用「发送时的版本」登记才对得上这张卡的真实内容。
    task_uid: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    task_version: Mapped[int] = mapped_column(Integer, default=0)

    # 卡片 JSON 直接存字符串：发件箱是「已渲染好的东西」，
    # 存任务 ID 再渲染一遍会导致「点开时内容和入队时不一致」
    payload: Mapped[str] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(16), default=NOTIFY_PENDING, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text)

    scheduled_at: Mapped[object] = mapped_column(TZAwareDateTime, default=now, index=True)
    sent_at: Mapped[Optional[object]] = mapped_column(TZAwareDateTime)
    created_at: Mapped[object] = mapped_column(TZAwareDateTime, default=now)
    updated_at: Mapped[object] = mapped_column(TZAwareDateTime, default=now, onupdate=now)

    def __repr__(self) -> str:
        return f"<Outbox {self.dedupe_key} {self.status} try={self.attempts}>"


# ---- 日历同步状态（Day 16）----
CAL_PENDING = "pending"    # 占好坑了，创建接口还没成功
CAL_CREATED = "created"    # 日程已建，event_id 有效
CAL_DELETED = "deleted"    # 任务取消，日程已删
CAL_FAILED = "failed"      # 接口失败，留痕等重试
CAL_SKIPPED = "skipped"    # 主动不建（没截止时间）


class TaskCalendarEvent(Base):
    """任务 ↔ 飞书日程的映射（Day 16）。

    【为什么必须落这张表】
    飞书没有「按业务 ID 查日程」的可靠方式。不存 event_id，验收完成时就
    不知道该 PATCH 哪一个、取消时不知道该删哪一个。用户原话就是：
    「任务和日程保存对应关系，重复操作时更新已有日程」。

    【幂等是两层】
    1. task_uid 唯一 → 同一条任务最多一行，重复点确认不会建第二个日程
    2. idempotency_key 固定存下来 → 即使创建成功但进程崩在写库之前，
       重试带着同一个 key 调飞书，飞书侧也返回原来的 event_id

    【为什么存 task_uid 而不是 task.id 外键】
    和 notify_outbox 保持一致。后台线程拿到的是 uid，用 uid 查不用先
    join task 表；detached 对象跨线程也不会踩关系懒加载。

    deadline_at 存的是「建日程那一刻的截止」：以后延期改截止，
    拿它和 task.deadline_at 一比就知道该不该 PATCH。
    """

    __tablename__ = "task_calendar_event"
    __table_args__ = (
        UniqueConstraint("task_uid", name="uq_calendar_task"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uid: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    task_uid: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    calendar_id: Mapped[str] = mapped_column(String(128))
    event_id: Mapped[str] = mapped_column(String(128), default="")
    # 32~128 字符，飞书在「应用 + 日历」维度用它去重
    idempotency_key: Mapped[str] = mapped_column(String(128))
    deadline_at: Mapped[Optional[object]] = mapped_column(TZAwareDateTime)

    status: Mapped[str] = mapped_column(String(16), default=CAL_PENDING, index=True)
    attendees_synced: Mapped[int] = mapped_column(Integer, default=0)
    synced_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    retry_at: Mapped[Optional[object]] = mapped_column(TZAwareDateTime)
    request_body: Mapped[Optional[str]] = mapped_column(Text)
    sync_mode: Mapped[str] = mapped_column(String(16), default="application", server_default="application")
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(String(200))

    created_at: Mapped[object] = mapped_column(TZAwareDateTime, default=now)
    updated_at: Mapped[object] = mapped_column(TZAwareDateTime, default=now, onupdate=now)
    synced_at: Mapped[Optional[object]] = mapped_column(TZAwareDateTime)

    def __repr__(self) -> str:
        return f"<CalendarEvent {self.task_uid[:8]} {self.status} {self.event_id[:12]}>"


class CalendarAccount(Base):
    __tablename__ = "calendar_account"
    open_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    app_id: Mapped[str] = mapped_column(String(64))
    calendar_id: Mapped[str] = mapped_column(String(128))
    encrypted_tokens: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[object] = mapped_column(TZAwareDateTime)
    updated_at: Mapped[object] = mapped_column(TZAwareDateTime, default=now)


class CalendarOAuthState(Base):
    __tablename__ = "calendar_oauth_state"
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    open_id: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[object] = mapped_column(TZAwareDateTime)
    used: Mapped[int] = mapped_column(Integer, default=0)


class PersonalCalendarEvent(Base):
    __tablename__ = "personal_calendar_event"
    __table_args__ = (UniqueConstraint("task_uid", "open_id", name="uq_personal_task_user"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_uid: Mapped[str] = mapped_column(String(32), index=True)
    open_id: Mapped[str] = mapped_column(String(64))
    calendar_id: Mapped[str] = mapped_column(String(128))
    event_id: Mapped[str] = mapped_column(String(128), default="")
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_body: Mapped[Optional[str]] = mapped_column(Text)
    synced_version: Mapped[int] = mapped_column(Integer, default=0)
    deadline_at: Mapped[Optional[object]] = mapped_column(TZAwareDateTime)
    status: Mapped[str] = mapped_column(String(16), default=CAL_PENDING)
    last_error: Mapped[Optional[str]] = mapped_column(String(200))
