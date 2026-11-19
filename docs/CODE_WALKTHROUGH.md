# 代码导读（详细版）

这份文档回答一个问题：**这 6000 行代码，每一块到底在干什么，为什么这么写。**

`README.md` 讲的是产品和使用，`docs/ARCHITECTURE.md` 讲的是文件清单速查。
这一份是逐层精讲——适合你刚接手、或者隔了两周回来重新捡起来的时候读。

目录：

1. [一分钟版本](#一分钟版本)
2. [四层结构](#四层结构)
3. [一条消息的完整旅程](#一条消息的完整旅程)
4. [逐层精讲](#逐层精讲)
5. [数据库表](#数据库表)
6. [十个关键设计决策](#十个关键设计决策)
7. [测试怎么读](#测试怎么读)
8. [改动手册：想改某处该动哪个文件](#改动手册想改某处该动哪个文件)
9. [推荐阅读顺序](#推荐阅读顺序)

---

## 一分钟版本

一个飞书机器人，做三件事：

```
① 从群消息里认出「这是一条派活」        —— 模型 + 规则校验
② 问人一句「要不要建单」                —— 卡片，点按钮才建
③ 建完之后盯到有人验收                  —— 状态机 + 定时提醒 + 变更通知
```

代码分四层，**一条消息从上往下穿过四层**：

| 层 | 一句话 | 关键文件 |
|---|---|---|
| 接入 | 飞书事件 → 内部对象，去重 | `main.py` `models.py` `store.py` |
| 识别 | 一段人话 → 结构化候选 | `detector.py` `llm.py` `prompts.py` `schemas.py` `deadline.py` |
| 落库 | 唯一真相源，所有写操作走这里 | `db.py` `models_db.py` `repo.py` |
| 出口 | 卡片、状态、提醒、通知 | `cards.py` `card_action.py` `state_machine.py` `permissions.py` `notify.py` `worker.py` `events.py` |

**贯穿四层的一条铁律**：模型只负责「理解」，所有「决定」由确定性代码做。
模型输出不写库、不发消息、不改状态，必须先过 `schemas.py`。

---

## 四层结构

```
┌─ 一、接入 ────────────────────────────────────────────┐
│ main.py · models.py · store.py · feedback.py          │
│ 顺序：过滤机器人 → 去重 → 状态反馈 → 识别              │
└───────────────────────────────────────────────────────┘
                          ↓
┌─ 二、识别 ────────────────────────────────────────────┐
│ prompts.py · llm.py · schemas.py · detector.py        │
│ deadline.py                                           │
│ 模型只抄原文，负责人和时间由代码卡                     │
└───────────────────────────────────────────────────────┘
                          ↓
┌─ 三、落库（唯一真相源）────────────────────────────────┐
│ db.py · models_db.py · repo.py                        │
│ 候选与正式任务分表；状态只能经 transition_task 改      │
└───────────────────────────────────────────────────────┘
                          ↓
┌─ 四、出口 ────────────────────────────────────────────┐
│ cards.py · card_action.py · state_machine.py          │
│ permissions.py · notify.py · worker.py · events.py    │
│ feishu_api.py                                         │
└───────────────────────────────────────────────────────┘
```

### 文件清单

| 文件 | 行数 | 职责 | 被绕过会怎样 |
|---|---|---|---|
| `main.py` | 178 | 长连接注册、事件分流、日志 | 程序不启动 |
| `models.py` | 119 | 飞书事件 → `MessageCommand` | 每处代码都要自己解析飞书结构 |
| `store.py` | 65 | `event_id` 去重 | 重推会重复建单 |
| `feedback.py` | 183 | 「做完了」→ 状态动作 | 只能点卡片改状态 |
| `prompts.py` | 102 | 提示词 + 版本号 | 模型不知道输出什么格式 |
| `llm.py` | 184 | httpx 直调 `/chat/completions` | 没模型可用 |
| `schemas.py` | 97 | Pydantic 校验 | 模型胡话一路写进库 |
| `detector.py` | 93 | 编排：提示词→模型→校验→防幻觉→时间 | 没有识别能力 |
| `deadline.py` | 478 | 「本周五前」→ 带时区时间 | 截止时间永远是 null |
| `db.py` | 178 | engine / session / 时区类型 | 回调和主线程抢连接 |
| `models_db.py` | 304 | 7 张表的 ORM | 数据没地方存 |
| `repo.py` | 634 | **所有数据库操作的唯一入口** | 状态改得乱七八糟 |
| `cards.py` | 478 | 卡片 JSON 纯函数 | 发不出卡 |
| `card_action.py` | 247 | 卡片按钮回调 | 点了没反应 |
| `state_machine.py` | 166 | 状态转移表 | 能从「已完成」跳回「进行中」 |
| `permissions.py` | 129 | 角色权限矩阵 | 谁都能验收别人的活 |
| `feishu_api.py` | 220 | 发消息 / 改卡片封装（不抛异常） | 一次网络抖动崩掉主流程 |
| `notify.py` | 186 | 日报生成（纯函数） | 没有每日提醒 |
| `worker.py` | 229 | 定时投递 + 重试 | 通知发一次就丢 |
| `events.py` | 145 | 状态变更即时通知 | 别人动了你的任务你不知道 |
| `card_sync.py` | 207 | 旧卡片同步（登记 message_id → 状态变了回头 PATCH） | 聊天记录里全是互相矛盾的旧状态 |

**三个最大的文件**：`repo.py`(634)、`cards.py`(478)、`deadline.py`(478)，合计约占全部业务代码的四分之一。

---

## 一条消息的完整旅程

以群里一句 `@张三 周五前把报告发我` 为例：

### 阶段一：进来

```
1. 长连接收到事件
   main.py:on_message(data)                      ← lark-oapi 回调

2. 转成内部对象 + 过滤机器人
   models.parse_event(event, header) → MessageCommand
   ├─ clean_text() 把 "@_user_1" 占位符换成真名
   ├─ mentions[].id.open_id 提取真实身份
   ├─ create_time(毫秒) → Asia/Shanghai 的 aware datetime
   └─ cmd.is_from_bot 为 True 就直接 return

3. event_id 去重
   store.is_new_event(event_id)                  ← INSERT OR IGNORE + rowcount

4. 是状态反馈吗？
   feedback.apply_feedback(cmd)
   ├─ 命中「做完了」「卡住了」等 → 改状态 + 群里回一句 → 结束
   └─ 不是 → 继续往下
```

**第 4 步必须在识别之前**。顺序反了的话，「做完了」会先过一遍模型，
模型很可能把它当成一条新任务建出来——群里就多一条叫「做完了」的垃圾任务。

### 阶段二：识别

```
5. detector.detect(cmd)
   ├─ prompts.build_user_prompt()        拼「群类型 + 时间 + 发言人 + mentions + 原文」
   ├─ llm.chat_json(...)                 httpx 直调 /chat/completions，剥 ```json
   ├─ TaskRecognition.model_validate()    Pydantic 卡字段
   ├─ _validate_mentions()               ★ 防模型编造负责人
   └─ deadline.apply_to_tasks(tasks, cmd.sent_at)   ★ 基准是消息时间，不是 now()
```

### 阶段三：落库

```
6. repo.save_candidates(result, cmd)
   ├─ 每条 task 存成一行 task_candidate，status=pending
   ├─ 幂等靠 UniqueConstraint(message_id, seq)
   └─ expires_at = now + 72h（72 小时没人点就自动过期）

7. feishu_api.push_confirm_card(cand, card)
   └─ 先私聊发起人；私聊失败（如 230013）回落群聊
```

### 阶段四：建单与闭环

```
8. 用户点「确认建单」
   card_action.handle(event) → process("confirm", cid, operator) → _process_candidate()

9. repo.confirm_candidate(cid, operator)
   ├─ 条件 UPDATE 抢占：
   │    UPDATE task_candidate SET status='confirmed'
   │    WHERE uid=? AND status='pending'
   ├─ rowcount != 1 → 返回「已处理」（连点/重推都挡在这里）
   ├─ 建 task + task_assignee + 第一条 task_status_log
   └─ daemon 线程私聊推任务卡给负责人（不能在回调里等网络，3 秒超时）

10. 之后每次状态变更
    card_action / feedback / taskctl
      → repo.transition_task()
        ├─ ① 状态机校验
        ├─ ② 权限校验
        ├─ ③ 乐观锁 UPDATE（version）
        ├─ ④ 写流水
        └─ ⑤ events.on_transition() 入队通知（在事务外）
      → events.flush_async() 起线程立刻发
```

---

## 逐层精讲

### 一、接入层

#### `main.py` — 分流器

不是业务代码，是**路由**。它只做判断，不做实现：

```python
if cmd.is_from_bot:
    return                     # 1. 机器人自己的消息，先去重之前就挡掉

if not store.is_new_event(cmd.event_id, ...):
    return                     # 2. 重复事件

applied = feedback.apply_feedback(cmd)
if applied.text:
    return                     # 3. 状态反馈，处理完就走

result = detector.detect(cmd)  # 4. 才算真正的任务识别
```

顺序不能换。特别注意 **过滤机器人要放在去重之前**——否则机器人自己发的消息
会先被写进 `event_inbox`，污染去重表。

#### `models.py` — 飞书事件 → 内部对象

一个坑决定了一个设计：飞书把消息里的 `@人名` 替换成占位符 `@_user_1`，
真名和 open_id 只在 `mentions[]` 里。

```python
def clean_text(raw: str, mentions: List[Mention]) -> str:
    for m in mentions:
        raw = raw.replace(m.key, m.name)   # @_user_1 → @张三
    return raw
```

所以**负责人识别永远走 `mentions[].id.open_id`，绝不解析文本里的名字**。
文本里的名字可能是重名、可能是历史昵称。

`MessageCommand` 是这一层的产物，之后所有函数都只认它，不再碰飞书原始结构。

#### `store.py` — 去重（65 行，但很关键）

```python
cur = conn.execute(
    "INSERT OR IGNORE INTO event_inbox(event_id, ...) VALUES (?, ...)",
    (event_id, ...),
)
return cur.rowcount == 1        # 1 = 首次，0 = 重复
```

为什么不用「先 SELECT 再 INSERT」：并发下两个请求都查到不存在，都插入成功。
`INSERT OR IGNORE` + `rowcount` 是一条语句完成的原子判断。

`event_id` 为空时直接返回 `True`（不去重）——脏数据不该让整条消息被丢掉。

#### `feedback.py` — 从一句话读出意图

三个纯函数，职责分明：

| 函数 | 干什么 | 纯不纯 |
|---|---|---|
| `classify(text)` | 「做完了」→ `submit` | 纯 |
| `resolve_tasks(cmd)` | 靠回复关系找到任务 | 只读库 |
| `apply_feedback(cmd)` | 预演 + 落库 + 组织回话 | 写库 |

`classify` 里两个细节：

```python
if _QUESTION.search(body):        # 「做完了吗」是问句，不是结论
    return None

if _NEG_TAIL.search(body[max(0, start - 3):start]):
    continue                      # 「还没做完」——往前看 3 个字查否定词
```

只往前看不往后看，所以「不做了」这种自带否定的说法不会被自己挡掉。

`apply_feedback` 里有一段**预演**：

```python
possible = [t for t in active
            if _would_succeed(t, reading.action, cmd.sender_open_id)]
if len(possible) != 1:
    return Applied(False, "请在任务卡上点按钮，别让我猜。")
```

一条消息下挂了三条任务，回复一句「做完了」——不猜，让人去点卡片。
**猜错一次改错一条任务，代价远大于让人多点一下。**

---

### 二、识别层

#### `prompts.py` — 提示词

`PROMPT_VERSION = "task-recognition-v2"`。版本号不是装饰，调提示词时必须能
说清「这次回归跑的是哪版」，否则改坏了不知道退回哪。

v1 有个真错误：多人多任务时负责人错绑。

```
"@张三 @李四，分别提交采购清单和试验计划"
→ v1 输出：两条任务，每条都填了两个人的 open_id   ✗
→ v2 规则：分别/各自/对应 必须一对一；判断不了就全留空 + 提澄清问题  ✓
```

#### `schemas.py` — 不可信输入的闸门

模型输出进了这里才有资格往下走。三处卡点：

```python
confidence: float = Field(..., ge=0.0, le=1.0)          # 0~1 之外直接报错

@field_validator("tasks")
def _cap_tasks(cls, value):                             # 一条消息最多 10 个候选
    if len(value) > MAX_TASKS_PER_MESSAGE:
        raise ValueError(...)

@model_validator(mode="after")
def _consistency(self):
    if not self.is_task_candidate:
        self.tasks = []                                 # 说不是任务就别带任务列表
    if self.is_task_candidate and not self.tasks:
        self.is_task_candidate = False                  # 说是任务却没有任务体
        self.candidate_type = "unclear"
```

最后那个「自相矛盾自动降级」很实用：模型经常 `is_task_candidate=true` 但 `tasks=[]`，
与其报错，不如降级成 `unclear` 让它进入待澄清队列。

另外注意 `deadline_at` / `deadline_status` / `deadline_rule` 三个字段——
**模型不输出这三个**，是 `deadline.py` 回填的。

#### `detector.py` — 只有 93 行，但是这一层的大脑

```python
def detect(cmd) -> TaskRecognition:
    user_prompt = prompts.build_user_prompt(...)
    raw = llm.chat_json(prompts.SYSTEM_PROMPT, user_prompt)      # 可能抛 LLMError
    result = TaskRecognition.model_validate(raw)                 # 可能抛 ValidationError
    result = _validate_mentions(result, cmd.mentions)            # 防幻觉
    deadline.apply_to_tasks(result.tasks, cmd.sent_at)           # 时间换算
    return result
```

四步，顺序固定。失败一律抛 `DetectionError`，让上层转人工队列，**不静默丢弃**。

防幻觉那段值得抄：

```python
valid = {m.open_id for m in mentions if m.open_id} | {m.key for m in mentions if m.key}
kept = [mid for mid in task.assignee_mention_ids if mid in valid]
```

为什么要 `| {m.key}`：模型有时抄的是 `@_user_1` 占位符而不是 open_id，两种都得认。

#### `deadline.py` — 478 行规则

这是全项目最「厚」的一个文件，因为中文时间说法太杂。

```python
Deadline(raw, status, deadline_at, rule, precision, assumed, reason)
```

`status` 三态，语义必须分清：

| status | 含义 | 处理 |
|---|---|---|
| `resolved` | 算出来了 | 填 `deadline_at`，从 `missing_fields` 摘掉 `deadline` |
| `unresolved` | 有表达但算不出（「尽快」「下周找个时间」） | 保留原话，标记缺失，等澄清 |
| `missing` | 原话压根没提时间 | 同上 |

四条硬规则：

1. **基准是消息的 `sent_at`，不是 `now()`** —— 补跑历史消息时用当前时间会全错
2. **只到日的表达默认取 18:00，并标记 `assumed=True`** —— 那是系统补的，得让人看出来
3. **过去时间一律 `unresolved`** —— 绝不返回一个假时间
4. **「下班前」今天已过点 → 顺延到下一个工作日**，不是简单 +1 天

---

### 三、落库层

#### `db.py` — 三个决定，都跟坑有关

```python
class TZAwareDateTime(TypeDecorator):
    impl = String
    cache_ok = True
```

**为什么不用 `DateTime(timezone=True)`**：SQLite 不存时区。写进去是
`2026-11-20 18:00+08:00`，读出来变 `2026-11-20 18:00`（naive）。
Day 12 的提醒全靠时间比较，混进一个 naive 就是 8 小时误差。

**为什么每条连接都要 PRAGMA**：

```python
@event.listens_for(engine, "connect")
def _on_connect(dbapi_conn, _record):
    cur.execute("PRAGMA foreign_keys=ON")     # 默认是关的，外键只是装饰
    cur.execute("PRAGMA journal_mode=WAL")    # 读写不互相阻塞
```

**为什么 `check_same_thread=False`**：长连接的回调线程和主线程不是同一个，
SQLite 默认禁止跨线程复用连接。

#### `models_db.py` — 五张表

见下面[数据库表](#数据库表)一节。

一个细节：`Task.assignees` 和 `Task.logs` 用的是 `lazy="selectin"`。

```python
assignees: Mapped[List["TaskAssignee"]] = relationship(..., lazy="selectin")
```

为什么不用默认的 `lazy="select"`：session 一关，访问 `task.owner_open_ids`
就 `DetachedInstanceError`。`selectin` 在查主表时一起捞回来，出了 session 随便用。

#### `repo.py` — 三层结构

```
读：list_* / get_* / count_*          只读，简单查询
写：save_candidates / confirm_candidate / reject_candidate
改状态：transition_task                唯一入口，带三道闸
通知：enqueue_notification / claim / mark_*
```

**两类幂等写法**，都是「一条语句搞定判断」：

```python
# 候选落库：唯一约束 + 先查一次只为返回干净结果
existing = set(s.scalars(select(TaskCandidate.seq)
                         .where(TaskCandidate.message_id == cmd.message_id)).all())

# 确认建单：条件 UPDATE 抢占
updated = s.execute(
    update(TaskCandidate)
    .where(TaskCandidate.uid == uid, TaskCandidate.status == CAND_PENDING)
    .values(status=CAND_CONFIRMED)
)
if updated.rowcount != 1:      # 抢不到 = 别人先点了
    return None
```

```python
# 发件箱：唯一键 + ON CONFLICT DO NOTHING
sqlite_insert(NotificationOutbox).values(...)
    .on_conflict_do_nothing(index_elements=["dedupe_key"])
```

#### `transition_task()` — 全项目最该读懂的函数

```python
def transition_task(task_uid, action, operator_open_id="",
                    expected_version=None, note=None) -> TransitionResult:
    with db.session_scope() as s:
        task = s.scalar(select(Task).where(Task.uid == task_uid))

        # 乐观锁前置检查：调用方传了版本就必须对上
        if expected_version is not None and expected_version != task.version:
            return TransitionResult(False, conflict=True, message="请刷新后重试")

        # ① 状态机：这个动作在当前状态下合不合法
        to_status, check = state_machine.resolve_action(task.status, action)
        if not check.ok:
            return TransitionResult(False, task=task, message=check.reason)

        # ② 权限：这个人有没有资格
        perm = permissions.can(task, operator_open_id, action)
        if not perm.ok:
            return TransitionResult(False, task=task, message=perm.reason)

        # ③ 乐观锁 CAS
        updated = s.execute(
            update(Task)
            .where(Task.uid == task_uid, Task.version == task.version)
            .values(status=to_status, version=Task.version + 1, ...)
        )
        if updated.rowcount != 1:
            return TransitionResult(False, conflict=True, message="刚刚被别人改过")

        # ④ 流水
        s.add(TaskStatusLog(...))

    # ⑤ 通知：放在事务之外
    events.on_transition(result, action, from_status, operator_open_id)
    return result
```

**为什么通知在 `with` 块外面**：写在事务里的话，「业务提交成功但通知入队失败」
和「通知入队了但业务回滚」都会发生。副作用失败不该回滚业务，也不该被静默吞掉——
所以拆成两段，通知失败只打日志。

**为什么 `version` 而不是用状态做条件**：用状态挡不住「A 点开始、B 点报阻塞」——
两个都合法，状态条件都匹配。版本号能。

**为什么 `s.refresh()` 不行**：

```python
# 不能 s.refresh(task)：refresh 会把关系属性打回「未加载」，
# session 一关就 DetachedInstanceError
task = s.scalars(
    select(Task).where(Task.uid == task_uid)
    .execution_options(populate_existing=True)
).one()
```

---

### 四、出口层

#### `state_machine.py` — 一张表定死规则

```python
TRANSITIONS = {
    TASK_TODO:               frozenset({TASK_IN_PROGRESS, TASK_CANCELLED}),
    TASK_IN_PROGRESS:        frozenset({TASK_BLOCKED, TASK_PENDING_ACCEPTANCE, TASK_CANCELLED}),
    TASK_BLOCKED:            frozenset({TASK_IN_PROGRESS, TASK_CANCELLED}),
    TASK_PENDING_ACCEPTANCE: frozenset({TASK_DONE, TASK_IN_PROGRESS, TASK_CANCELLED}),
    TASK_DONE:               frozenset(),
    TASK_CANCELLED:          frozenset(),
}
```

两个刻意收紧的地方：

- **`TODO` 不能直接到 `DONE`** —— 没人开工怎么会有交付物？验收环节被整段跳过
- **`BLOCKED` 不能直接到 `DONE`** —— 阻塞要先解除（回到进行中）才能继续，否则「卡了一周然后直接完成」这种过程无迹可查

「动作」不能直接映射状态，因为同一个目标状态来路不同：

```python
_ACTION_OF = {
    (TASK_TODO, TASK_IN_PROGRESS):            "start",
    (TASK_BLOCKED, TASK_IN_PROGRESS):         "unblock",     # 同样去 IN_PROGRESS
    (TASK_PENDING_ACCEPTANCE, TASK_IN_PROGRESS): "reject",   # 语义完全不同
    ...
}
```

所以是**用 `(原状态, 目标状态)` 反查动作名**，不是动作查状态。

#### `permissions.py` — 三个角色

```python
ACTION_ROLES = {
    "start":  {OWNER, ADMIN},
    "submit": {OWNER, ADMIN},
    "accept": {CREATOR, ADMIN},     # 验收是发起人的权力
    "cancel": {CREATOR, ADMIN},
}
```

两条兜底规则，都是「宁可宽松也别让任务死掉」：

```python
# 任务没有负责人时，发起人可以代行
if (ROLE_OWNER in allowed_roles and not task.owner_open_ids
        and open_id == task.creator_open_id):
    return Permission(True, "任务没有负责人，发起人代行")

# 发起人也是负责人：允许自验收，但流水里留痕
if {ROLE_OWNER, ROLE_CREATOR} <= roles and action in ("accept", "reject"):
    return Permission(True, "发起人同时也是负责人：自验收")
```

硬挡自验收会让「自己给自己派活」的任务永远走不完；偷偷放行又等于没有验收。
**放行 + 留痕**是唯一可行的中间态。

#### `cards.py` — 卡片是纯函数

不碰网络、不碰数据库，输入模型输出 JSON。所以能单测、能 `try_card.py` 直接打印。

**两种视角，别搞混**：

```python
public = not viewer_open_id
actions = (state_machine.next_actions(status) if public          # 群卡：状态允许的全部
           else task_actions_for(task, viewer_open_id))          # 私聊卡：按这个人过滤
```

| | 渲染依据 | 为什么 |
|---|---|---|
| 私聊卡（传 `viewer_open_id`） | 这个人的权限 | 一对一，可以裁剪 |
| 群卡（不传 viewer） | 状态允许的全部动作 | **一张卡多人看**，飞书没法给不同读者渲染不同按钮 |

群卡的按钮 `value` 带 `pub:1`，点击后仍返回公共视角——否则发起人点一次，
卡片就变成负责人视角了。

踩过的坑：群卡按负责人一个人的权限裁剪，结果待验收的任务在群里**一个按钮都没有**，
发起人明明能验收却看到「你没有任何可执行的动作」，任务就这样卡死。

#### `card_action.py` — 回调处理

飞书要求 **3 秒内响应**，否则重推。所以这一层只做本地库读写，不发消息、不调模型。

```python
def _process_task(action, tid, operator, ver, pub):
    viewer = None if pub else operator        # 公共卡点完还公共卡
    result = repo.transition_task(tid, action, operator_open_id=operator,
                                  expected_version=ver)
    events.flush_async()                      # 起线程发通知，不等
    fresh = repo.get_task(tid)
    return Outcome("success", result.message, cards.build_task_card(fresh, viewer))
```

响应的构造有个天坑：

```python
# 必须传原始 dict。传 CallBackToast(...) 实例会抛
# UnmarshalException: expected <class CallBackToast> but was <class CallBackToast>
return P2CardActionTriggerResponse({
    "toast": {"type": ..., "content": ...},
    "card": {"type": "raw", "data": outcome.card},
})
```

错误信息自相矛盾，极难排查。

#### `notify.py` — 日报生成（纯函数）

```python
def plan_digests(now=None) -> List[Digest]:
    """扫一遍库，算出每个负责人今天该收到什么。
    纯函数：只读库、不写库、不发消息。"""
```

`now` 是参数不是全局——否则测试要等到明天 9 点。

分三堆，`categorize()` 里有一条关键优先级：

```python
if deadline < now:
    return CAT_OVERDUE        # 逾期优先
...
if deadline < tomorrow_start:
    return CAT_TODAY
```

**逾期压过「今天到期」**：一条今天 10:00 截止、现在 11:00 的任务，
说「今天到期」会让人以为还有时间。

#### `worker.py` — 定时投递

```python
def tick(now=None, dry_run=False, force_plan=False, sender=None):
    stats["expired"] = repo.expire_candidates()        # 1. 候选过期
    if notify.should_plan(now) or force_plan:          # 2. 09:00 闸门
        for digest in notify.plan_digests(now):
            repo.enqueue_notification(...)             #    入队
    stats.update(drain_outbox(now, sender, dry_run))   # 3. 发
```

`drain_outbox` 三段式：

```python
rows = repo.claim_pending_notifications(now, limit)    # 先抢占（改状态）
for row in rows:
    if notify.is_too_late(row.scheduled_at, now):      # 太晚就放弃
        repo.mark_notification_skipped(row.uid)
        continue
    ok = bool(send(row))                               # 再发
    ...
```

**先抢占再发送**：如果先查出来再从 pending 里发，两个 Worker 同时跑会各发一遍。

`sender` 是参数不是写死的——单测塞个假函数就能验重试，不用真发消息。

时区三条规则：

| 规则 | 值 | 为什么 |
|---|---|---|
| 日报时段 | 09:00 之后才生成 | 否则半夜启动 Worker 会立刻推当天日报 |
| 免打扰 | 22:00–08:00 不发 | **延后**不是丢弃，`scheduled_at` 不动 |
| 补发时限 | 超 6 小时放弃 | 早上 9 点的「今天要交」下午 4 点才发等于骂人 |

#### `events.py` — 状态变更即时通知

和日报的分工：

| | 触发 | 收件人 | 去重键 |
|---|---|---|---|
| 日报 | 每天 09:00 扫 | 有任务到期的人 | `DIGEST:{日期}:{open_id}` |
| 事件 | 有人点了按钮 | 该知道这件事的人 | `EVENT:{任务}:{动作}:{版本}:{open_id}` |

**收件人按动作定，不按状态定**——状态是结果，动作才是原因：

| 动作 | 告诉谁 |
|---|---|
| 提交验收 / 报阻塞 | 发起人 |
| 验收通过 / 打回 / 取消 | 负责人 |
| 开始处理 | **不发**（他自己点的） |

加一条过滤：**收件人等于操作者就不发**。自己给自己派活、自己提交验收，
再推一条给自己纯属噪音。

---

## 数据库表

一共 7 张表，其中 `event_inbox` 由 `store.py` 用原生 sqlite3 管理，其余 6 张走 SQLAlchemy。

### `event_inbox`（去重）

| 字段 | 类型 | 说明 |
|---|---|---|
| `event_id` | TEXT PK | 飞书事件 ID |
| `message_id` | TEXT | |
| `chat_id` | TEXT | 建了索引 |
| `received_at` | TEXT | |

### `task_candidate`（模型候选）

| 字段 | 说明 |
|---|---|
| `uid` | 对外唯一标识（uuid4 hex），**不用自增 id 对外** |
| `seq` | 这条消息里的第几条任务，从 1 开始 |
| `message_id` + `seq` | **唯一约束**，落库幂等的靠山 |
| `event_id` `chat_id` `chat_type` `sender_open_id` `message_sent_at` | 来源追溯 |
| `raw_text` | 原话，排查时唯一能还原现场的东西 |
| `title` `description` `deliverable` `acceptance_criteria` | 任务内容 |
| `deadline_text` `deadline_at` `deadline_status` `deadline_rule` | 原文 + 解析结果 |
| `candidate_type` `confidence` | 模型判断 |
| `assignee_open_ids` | JSON 数组 |
| `missing_fields` | JSON 数组，缺哪些必备字段 |
| `evidence` | 支撑判断的原文片段 |
| `status` | pending / confirmed / rejected / expired |
| `expires_at` | 默认 72h 后过期 |

### `task`（正式任务）

| 字段 | 说明 |
|---|---|
| `uid` | 对外标识 |
| `candidate_uid` | 反查来源候选 |
| `status` | TODO / IN_PROGRESS / BLOCKED / PENDING_ACCEPTANCE / DONE / CANCELLED |
| `version` | **乐观锁**，每次变更 +1 |
| `creator_open_id` | 发起人（派活那条消息的发送者） |
| `completed_at` | 进 DONE 时写 |
| `assignees` | 关系 → `task_assignee`，`lazy="selectin"` |
| `is_overdue` | **派生属性，不落库** |

`is_overdue` 不落库的理由：落库就要有定时任务去改它，一天不改就全错。

### `task_assignee`（负责人）

| 字段 | 说明 |
|---|---|
| `task_id` | 外键，`ondelete=CASCADE` |
| `open_id` | |
| `role` | owner / collaborator |

### `task_status_log`（流水）

| 字段 | 说明 |
|---|---|
| `task_id` `from_status` `to_status` | |
| `operator_open_id` | 谁改的 |
| `reason` | 备注 > 权限模块附带原因（如「自验收」）> 动作名 |
| `created_at` | 建了索引，按时间查 |

出争议时唯一能还原「谁在什么时候改了什么」的东西。

### `notify_outbox`（发件箱）

| 字段 | 说明 |
|---|---|
| `dedupe_key` | **唯一约束**，天然幂等 |
| `kind` | digest / task_event |
| `receive_id` + `receive_id_type` | 收件人和类型（open_id / chat_id） |
| `payload` | 卡片 JSON 字符串 |
| `status` | pending / sending / sent / failed / skipped |
| `attempts` | 重试次数，到 3 次判 failed |
| `last_error` | 最后一次失败原因 |
| `scheduled_at` | 计划发送时间（免打扰时保持不动） |
| `task_uid` + `task_version` | 这条通知说的是哪条任务哪个版本。Worker 发成功后靠它把新卡登记进 `task_card` |

### `task_card`（已发出的卡片，Day 14）

| 字段 | 说明 |
|---|---|
| `message_id` | **唯一约束**，一张卡只登记一次 |
| `task_id` | 外键，`ondelete=CASCADE` |
| `viewer_open_id` | **空 = 群卡（公共视角），有值 = 那个人的私聊卡**。同步时按它重渲染，串了就出「按张三权限裁剪的卡发到群里」 |
| `synced_version` | 这张卡最后一次对齐到的任务版本，只刷落后的 |
| `fail_count` + `retired` | 连续三次刷不动就退休（超过 14 天的消息飞书硬性不让改） |

这张表存在的唯一目的：**让旧卡片不再显示过时状态**。为什么非要这张表，
见下面第 11 条决策。

---

## 十一个关键设计决策

| # | 决策 | 不这么做会怎样 |
|---|---|---|
| 1 | 模型只输出候选，`schemas.py` 卡死字段 | 模型胡话一路写进库 |
| 2 | 候选表与正式任务表分开 | 正式数据被模型噪声污染，排查无从下手 |
| 3 | 所有建单必须人工点卡片 | 一条假任务混进别人待办，几次之后没人信这个系统 |
| 4 | 去重 / 抢占都用「一条语句」完成 | 先查后写在并发下必然失效 |
| 5 | 状态变更只能走 `transition_task()` | 绕过状态机、权限和乐观锁，数据烂掉 |
| 6 | 时间基准用消息 `sent_at` | 补跑历史消息全错 |
| 7 | 通知拆成「算 / 存 / 发」 | 发失败就丢了；重跑会重复发 |
| 8 | 群卡按状态渲染，按身份校验 | 一张卡多人看，按单人裁剪会让任务卡死 |
| 9 | `is_overdue` 派生不落库 | 要定时刷新，不刷就全错 |
| 10 | 副作用（通知）在业务事务之外 | 业务回滚 / 通知丢失，二选一都是 bug |
| 11 | **发出去的卡要记账，状态变了回头改** | 旧卡片继续撒谎：用户翻到「阻塞」卡以为任务还卡着 |

> 第 11 条值得单独说几句，因为它是**唯一一个「功能都跑通了才暴露」的问题**。
>
> 卡片回调的响应只能改「被点的那一张」，这是飞书的机制，不是 bug。
> 于是同一条任务在聊天记录里会留下一串状态彼此矛盾的卡片。
> 单测全绿、主链路闭环，但用户一翻历史记录就不敢信这个系统 ——
> 这类问题只有真用起来才会撞到，也最容易在验收时被当成「偶发」放过去。
>
> 修法的关键是**发卡时就要把 `message_id` 存下来**。当天没存，事后就补不回来
> （`GET /im/v1/messages` 能捞历史但成本高得多）。所以：**任何「以后可能要改」
> 的东西，在发出的那一刻就得记下来。**

---

## 测试怎么读

`tests/test_dayN.py` 的编号对应开发顺序，**想理解某个模块，看它的测试比看实现快**。

| 文件 | 条数 | 钉住的行为 |
|---|---|---|
| `test_day2.py` | 9 | 占位符还原、open_id 优先、机器人过滤、去重、时区 |
| `test_day3.py` | 10 | Schema 边界、自相矛盾降级、**拦模型伪造负责人** |
| `test_day4.py` | 32 | 相对时间、**基准必须是消息时间**、模糊/过去/已过点必须 unresolved |
| `test_day8.py` | 17 | 落库幂等、**时区往返不丢**、外键真生效、逾期派生 |
| `test_day9.py` | 19 | 卡片结构、**连点两下不会建两张单**、回调永不抛异常 |
| `test_day10.py` | 22 | 状态机合法/非法、终态冻结、**负责人不能验收自己的活**、version 冲突、自验收留痕 |
| `test_day11.py` | 20 | 按钮按角色不同、**群卡公共视角**、旧版本卡片被拒 |
| `test_day12.py` | 30 | 分堆、`dedupe_key` 幂等、09:00 闸门、**免打扰跨零点**、补发时限、重试到放弃 |
| `test_day13.py` | 14 | 提交验收通知发起人、开始处理静默、**同一次变更只入队一次**、自己人不通知自己 |
| `test_day13b.py` | 15 | **反话和问句不能读错**、只认回复、一句话多条任务不猜 |
| `test_card_sync.py` | 14 | 卡片登记幂等、**旧卡刷成最新**、已最新的不白刷、私聊卡不串成群卡视角、三次失败才退休 |
| `test_card_sync.py` | 14 | 卡片登记幂等、**旧卡刷成最新**、已最新的不白刷、私聊卡不串成群卡视角、三次失败才退休 |

共用夹具：

- `tests/conftest.py`：`database`（每个用例独立临时库）、`task`（造一条正式任务）、`_no_event_flush`（禁掉真发消息、禁掉卡片同步）
- `tests/helpers.py`：`make_cmd` / `make_recognition` / `make_task`，直接造数据不走模型

跑：

```bat
.venv\Scripts\python.exe -m pytest -q
```

当前 **202 passed**。末尾如果出现 `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]`，
那是环境的删除保护钩子在清理 pytest 临时目录，看 `N passed` 就行。

---

## 改动手册：想改某处该动哪个文件

这是最实用的一节。别每次都从头找。

### 想加一个状态流转

1. `models_db.py` 加状态常量（如果是新状态）
2. `state_machine.py` 改 `TRANSITIONS` 和 `_ACTION_OF`
3. `state_machine.py` 改 `STATUS_LABELS` / `ACTION_LABELS`
4. `permissions.py` 的 `ACTION_ROLES` 加新动作允许谁
5. `cards.py` 的 `STATUS_TEMPLATE` / `BUTTON_KIND` 补颜色
6. `tests/test_day10.py` 补测试

### 想加一种通知

只写「算」那一段，存和发完全复用：

1. `events.py` 的 `SUMMARIES` 加动作 → 一句话
2. `events.py` 的 `recipients()` 决定告诉谁
3. 完事。发件箱、重试、幂等都不用动

### 想改卡片长相

只动 `cards.py`。它是纯函数，改完用：

```bat
.venv\Scripts\python.exe try_card.py --task 0293 --as ou_xxx
```

不联网，直接看 JSON。

### 想改提示词

1. 改 `prompts.py`，**顺手把 `PROMPT_VERSION` 加一**
2. 跑 `try_detect.py --check`（8 条内置样例，不碰飞书不花钱）

### 时间解析认不出某种说法

只动 `deadline.py`，先跑：

```bat
.venv\Scripts\python.exe try_deadline.py "下下周一之前" --base 2026-11-18T10:00
```

确认现状是啥，再改规则，然后 `pytest tests/test_day4.py`。

### 想接多维表格出口

1. 新建 `bitable.py`，封装写入
2. 在 `repo.confirm_candidate()` 成功后挂钩（参考 `transition_task` 挂 `events` 的写法）
3. 状态变更处也要同步（`transition_task` 后面）
4. 需要开放平台加 `bitable:app` 权限

---

## 推荐阅读顺序

别从 `main.py` 一路读到底，会淹没。按数据流的逆序读，从一个具体对象出发：

| 顺序 | 读什么 | 为什么先读它 |
|---|---|---|
| 1 | `models.py`（119 行） | 先搞清内部数据结构长什么样，后面到处是 `MessageCommand` |
| 2 | `models_db.py`（240 行） | 数据结构定了，再看持久化形态 |
| 3 | `state_machine.py`（166 行） | 一张表看懂业务规则，比读 ten 个 if 快 |
| 4 | `repo.transition_task()` | 全部写操作的样板：三道闸 + 流水 + 副作用 |
| 5 | `cards.build_task_card()` | 看状态和权限怎么变成界面 |
| 6 | `detector.detect()`（93 行） | 只有四步，很短，是识别层的全貌 |
| 7 | `deadline.py` | 最长，但可以只读 docstring 和 `parse_deadline` 的入口 |
| 8 | `main.py` | 最后看总装，因为前面都认识了 |

### 一个小练习

读完 `state_machine.py` 的 `TRANSITIONS`，回答：

> **为什么「阻塞」不能直接到「已完成」，必须先回「进行中」？**

提示：想想流水表 `task_status_log` 记的是什么。如果允许 BLOCKED → DONE，
这条流水会是 `阻塞 → 已完成`，中间那段「谁解的阻塞、什么时候解的」就没了。

想明白了这个，你就懂了状态机设计里最核心的取舍：**状态不只是「现在在哪」，
还是「怎么走到这里的记录」**。
