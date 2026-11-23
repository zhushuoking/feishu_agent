"""Day 9：飞书交互卡片构造（纯函数，不碰网络、不碰数据库）。

为什么单独一层：
卡片 JSON 是最容易「看起来对、发出去报错」的东西 —— 少一个 tag、
value 写成字符串而不是对象、按钮不在 action 容器里，飞书只会给一句
muted 的错误。把它做成纯函数，就能拿单测卡住结构，不用每次都发到群里试。

关于卡片版本：这里用卡片 1.0（config + header + elements）。
2.0 更花哨但字段多、坑多，首期用 1.0 够表达「一条任务 + 两个按钮」。

两个实用技巧：
1. 候选人名用 `<at id=ou_xxx></at>`，让飞书自己渲染成真名。
   这样不用额外调通讯录接口、不用申请 contact 权限，也不会因为
   缓存里的名字过期而显示错人。
2. 已处理的卡片一律把按钮去掉。留着按钮就会有人点，
   点了才告诉他「已处理」是很差的体验。
"""

import json
import os
from typing import Any, Dict, List, Optional

import permissions
import state_machine
from models_db import (
    TASK_BLOCKED, TASK_CANCELLED, TASK_DONE, TASK_IN_PROGRESS,
    TASK_NEEDS_INFO, TASK_PENDING_ACCEPTANCE, TASK_TODO,
)

ACTION_CONFIRM = "confirm"
ACTION_REJECT = "reject"

# 卡片配色：蓝=待处理，绿=成功，灰=已废弃，橙=需要澄清
TEMPLATE_PENDING = "blue"
TEMPLATE_NEEDS_INFO = "orange"
TEMPLATE_DONE = "green"
TEMPLATE_MUTED = "grey"

# 任务状态 → 卡片头部配色。用颜色先传达状态，别让人读文字才知道。
STATUS_TEMPLATE = {
    TASK_NEEDS_INFO: "orange",
    TASK_TODO: "blue",
    TASK_IN_PROGRESS: "turquoise",
    TASK_BLOCKED: "red",
    TASK_PENDING_ACCEPTANCE: "orange",
    TASK_DONE: "green",
    TASK_CANCELLED: "grey",
}

# 按钮配色：主操作醒目，破坏性操作标红，其余中性
BUTTON_KIND = {
    "start": "primary",
    "submit": "primary",
    "accept": "primary",
    "unblock": "primary",
    "block": "danger",
    "cancel": "danger",
    "reject": "default",
}

# 日报每类最多列几条。列多了卡片长到没人看，而且卡片元素数量有上限。
MAX_SECTION_ITEMS = int(os.getenv("DIGEST_MAX_PER_SECTION", "4"))


def _md(content: str) -> Dict[str, Any]:
    return {"tag": "lark_md", "content": content}


def _field(label: str, value: str, short: bool = True) -> Dict[str, Any]:
    return {"is_short": short, "text": _md(f"**{label}**\n{value}")}


def at(open_id: Optional[str]) -> str:
    """把人渲染成 @名字。open_id 为空就写「未指定」。"""
    if not open_id:
        return "未指定"
    return f'<at id="{open_id}"></at>'


def _fmt_dt(value, rule: Optional[str] = None) -> str:
    """截止时间。只精确到日的会加「推定」标记 —— 那是系统补的 18:00，
    不是用户原话，得让人看得出来。"""
    if value is None:
        return "未确定"
    text = value.strftime("%m-%d %H:%M")
    if rule and "default-end" in rule:
        text += "（推定当日下班）"
    return text


def _deadline_line(cand) -> str:
    raw = cand.deadline_text or ""
    resolved = _fmt_dt(cand.deadline_at, cand.deadline_rule)
    if cand.deadline_status == "resolved":
        return f"{resolved}　←　原话「{raw}」"
    if cand.deadline_status == "unresolved":
        return f"算不出来　←　原话「{raw}」，需要人工确认"
    return "消息里没说时间"


def _owner_line(cand) -> str:
    ids = cand.assignee_open_ids or []
    return "、".join(at(i) for i in ids) if ids else "未指定"


def _missing_line(cand) -> str:
    labels = {
        "assignee": "负责人", "deliverable": "交付物", "deadline": "截止时间",
        "acceptance_criteria": "验收标准", "task_content": "任务内容",
    }
    fields = list(cand.missing_fields or [])
    if cand.deadline_status in {"missing", "unresolved"} and "deadline" not in fields:
        fields.append("deadline")
    items = [labels.get(f, f) for f in fields]
    return "、".join(items) if items else "无"


def _button(text: str, kind: str, action: str, cid: str = "",
            tid: str = "", ver: Optional[int] = None,
            pub: bool = False) -> Dict[str, Any]:
    """value 用短 key（act/cid/tid/ver/pub）—— 卡片 value 会跟着每次回调来回传，
    字段名越短，飞书那边序列化越省事，也不容易超长度限制。

    cid = candidate uid（候选卡片用），tid = task uid（任务卡片用）。
    两种卡片共用 act 命名空间：confirm/reject 是候选动作，
    start/submit/accept 这些是状态机动作，不会撞。

    pub = 这张卡是「公共卡」（发在群里，所有人看同一份）。
    回调要靠它决定点完之后还哪张卡：公共卡还公共卡，私聊卡还私聊卡。
    """
    value: Dict[str, Any] = {"act": action}
    if cid:
        value["cid"] = cid
    if tid:
        value["tid"] = tid
    if ver is not None:
        # 带上点按钮时看到的版本号，回调里当乐观锁用
        value["ver"] = ver
    if pub:
        value["pub"] = 1
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": kind,
        "value": value,
    }


def _note(content: str) -> Dict[str, Any]:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": content}]}


# ---------------------------------------------------------------- 待确认卡片

def build_candidate_card(cand) -> Dict[str, Any]:
    """群/私聊里推给发起人确认的卡片。"""
    template = TEMPLATE_NEEDS_INFO if cand.needs_info else TEMPLATE_PENDING

    elements: List[Dict[str, Any]] = [
        {
            "tag": "div",
            "fields": [
                _field("任务", cand.title, short=False),
                _field("截止", _deadline_line(cand), short=False),
                _field("负责人", _owner_line(cand)),
                _field("交付物", cand.deliverable or "未指定"),
                _field("仍缺", _missing_line(cand)),
                _field("优先级", "紧急" if cand.priority == "urgent" else "普通"),
            ],
        }
    ]

    if cand.clarification_question:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": _md(f"**需要澄清**\n{cand.clarification_question}"),
        })

    elements.append({"tag": "hr"})
    elements.append({
        "tag": "div",
        "text": _md("确认后在系统里建单，并通知负责人；如果这不是任务，点「忽略」。"),
    })
    elements.append({
        "tag": "action",
        "actions": [
            _button("确认建单", "primary", ACTION_CONFIRM, cand.uid),
            _button("忽略", "default", ACTION_REJECT, cand.uid),
        ],
    })
    elements.append(_note(
        f"来源消息 {cand.message_id[:16]}…　识别置信度 {cand.confidence:.2f}　"
        f"候选 {cand.uid[:8]}"
    ))

    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": "识别到一条可能的任务，请确认"},
        },
        "elements": elements,
    }


# ---------------------------------------------------------------- 任务卡（带状态按钮）

def task_actions_for(task, viewer_open_id: Optional[str]) -> List[str]:
    """这个人在这条任务上能看到哪些按钮 = 状态允许的 ∩ 权限允许的。

    两层缺一不可：
    - 只看状态：陌生人也能看到「验收通过」，点了才被拒，体验差
    - 只看权限：已完成的任务还会给负责人显示按钮，点了报错
    """
    if not viewer_open_id:
        return []
    return [
        name for name in state_machine.next_actions(task.status)
        if permissions.can(task, viewer_open_id, name).ok
    ]


def build_task_card(task, viewer_open_id: Optional[str] = None,
                    hint: Optional[str] = None) -> Dict[str, Any]:
    """任务卡：任务信息 + 当前状态下的操作按钮。

    【两种视角，别搞混】
    - 传了 viewer_open_id = 私聊卡。一对一发出去，可以按这个人的权限过滤按钮，
      他看不到自己点不了的按钮，体验最好。
    - 不传 viewer = 群卡。**一张卡多人看**，飞书没法按读者渲染不同按钮，
      所以只能渲染「当前状态允许的全部动作」，点击时再按点击者身份校验。

    之前这里踩过一个坑：群卡也按负责人一个人的权限过滤，结果待验收的任务
    在群里一个按钮都没有，发起人明明能验收，却看着卡片上写「你没有任何可
    执行的动作」—— 任务就这样卡住了。卡片渲染是「一次渲染、多人观看」，
    这个前提决定了群卡不能做权限裁剪。
    """
    status = task.status
    label = state_machine.STATUS_LABELS.get(status, status)
    template = STATUS_TEMPLATE.get(status, TEMPLATE_PENDING)

    status_text = f"{label}　v{task.version}"
    if task.is_overdue:
        status_text += "　**已逾期**"

    fields = [
        _field("任务", task.title, short=False),
        _field("状态", status_text),
        _field("截止", _fmt_dt(task.deadline_at)),
        _field("负责人", "、".join(at(i) for i in task.owner_open_ids) or "未指定"),
        _field("交付物", task.deliverable or "未指定"),
        _field("发起人", at(task.creator_open_id)),
        _field("验收人", at(task.effective_reviewer_open_id)),
    ]
    if task.acceptance_criteria:
        fields.append(_field("验收标准", task.acceptance_criteria, short=False))
    if task.completion_note:
        fields.append(_field("完成说明", task.completion_note, short=False))
    if task.review_note:
        fields.append(_field("处理意见", task.review_note, short=False))
    if task.status == TASK_NEEDS_INFO:
        fields.append(_field("待补充", "、".join(task.missing_info), short=False))

    elements: List[Dict[str, Any]] = [{"tag": "div", "fields": fields}]
    if task.status == TASK_NEEDS_INFO:
        elements.append({"tag": "div", "text": _md(
            "请回复本卡填写：\n补充信息\n交付物：需要提交的具体成果\n截止时间：明确的日期和时间"
        )})

    if hint:
        elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": _md(hint)})

    public = not viewer_open_id
    actions = (state_machine.next_actions(status) if public
               else task_actions_for(task, viewer_open_id))

    elements.append({"tag": "hr"})
    if actions:
        elements.append({
            "tag": "action",
            "actions": [
                _button(state_machine.ACTION_LABELS[name],
                        BUTTON_KIND.get(name, "default"), name,
                        tid=task.uid, ver=task.version, pub=public)
                for name in actions
            ],
        })
        if public:
            elements.append({
                "tag": "div",
                "text": _md("点按钮时按你的身份校验，没有权限会被拒绝。"),
            })
    elif state_machine.is_terminal(status):
        elements.append({
            "tag": "div",
            "text": _md("任务已结束，不需要再操作。"),
        })
    elif status == TASK_PENDING_ACCEPTANCE and viewer_open_id in task.owner_open_ids:
        elements.append({"tag": "div", "text": _md("已提交验收，可回复本任务卡补充完成说明。")})
    else:
        # 群卡不会出现「一个按钮都没有」——状态允许的动作总会渲染出来。
        elements.append({
            "tag": "div",
            "text": _md("当前账号没有可执行的动作。"),
        })

    elements.append(_note(f"任务 {task.uid[:8]}　状态 v{task.version}"))

    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": f"任务·{label}"},
        },
        "elements": elements,
    }


# ---------------------------------------------------------------- 日报卡（Day 12）

def _humanize_delta(delta) -> str:
    """把时间差说成人话。『逾期 3 天 7 小时』没人愿意读。"""
    total = int(abs(delta.total_seconds()))
    hours = total // 3600
    if hours < 1:
        return "不到 1 小时"
    if hours < 24:
        return f"{hours} 小时"
    days, rest = divmod(hours, 24)
    return f"{days} 天" if rest < 6 else f"{days} 天 {rest} 小时"


def _digest_item(index: int, task, viewer: str, now) -> List[Dict[str, Any]]:
    """日报里的一条任务：一行字 + 最多一个按钮。

    为什么只放一个按钮：日报是「提醒你去动手」，不是操作台。
    把 5 个动作都摆出来，人会一个都不点。
    """
    label = state_machine.STATUS_LABELS.get(task.status, task.status)
    bits = [f"截止 {_fmt_dt(task.deadline_at)}", label]
    if task.is_overdue:
        bits.append(f"已逾期 {_humanize_delta(now - task.deadline_at)}")

    elements: List[Dict[str, Any]] = [{
        "tag": "div",
        "text": _md(f"**{index}. {task.title}**\n" + " · ".join(bits)),
    }]

    actions = task_actions_for(task, viewer)
    if actions:
        primary = actions[0]
        elements.append({
            "tag": "action",
            "actions": [_button(state_machine.ACTION_LABELS[primary],
                                BUTTON_KIND.get(primary, "default"), primary,
                                tid=task.uid, ver=task.version)],
        })
    return elements


def build_digest_card(digest, now=None) -> Dict[str, Any]:
    """一人一条的每日提醒。三段：已逾期 / 今天到期 / 明天到期。

    digest 是 notify.Digest，now 用来算「逾期多久」——
    必须由调用方传，卡片层不自己去取当前时间（否则测试没法固定时间）。
    """
    from datetime import datetime

    from models import TZ_SHANGHAI

    now = now or datetime.now(tz=TZ_SHANGHAI)

    if digest.has_overdue:
        template = "red"
    elif digest.sections.get("today"):
        template = "orange"
    else:
        template = "blue"

    month_day = digest.date[5:].replace("-", "月") + "日"

    elements: List[Dict[str, Any]] = []
    counter = 0
    for cat, title in (("overdue", "已逾期"), ("today", "今天到期"), ("tomorrow", "明天到期")):
        rows = digest.sections.get(cat) or []
        if not rows:
            continue
        if elements:
            elements.append({"tag": "hr"})
        elements.append({"tag": "div", "text": _md(f"**{title}　{len(rows)} 条**")})

        for task in rows[:MAX_SECTION_ITEMS]:
            counter += 1
            elements.extend(_digest_item(counter, task, digest.open_id, now))
        if len(rows) > MAX_SECTION_ITEMS:
            elements.append({
                "tag": "div",
                "text": _md(f"还有 {len(rows) - MAX_SECTION_ITEMS} 条没列出来，"
                            f"用 taskctl.py list 看全部"),
            })

    elements.append({"tag": "hr"})
    elements.append(_note(
        f"共 {digest.total} 条　{digest.date}　点按钮可以直接推进状态"
    ))

    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text",
                      "content": f"今日任务 · {month_day}（{digest.total} 条）"},
        },
        "elements": elements,
    }


# ---------------------------------------------------------------- 终态卡片

def build_confirmed_card(cand, task) -> Dict[str, Any]:
    """确认之后的回执：没有候选按钮，避免二次点击。

    （任务本身的操作按钮在 build_task_card 里，那张卡会单独推给负责人）
    """
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": TEMPLATE_DONE,
            "title": {"tag": "plain_text", "content": "已建单"},
        },
        "elements": [
            {
                "tag": "div",
                "fields": [
                    _field("任务", task.title, short=False),
                    _field("状态", state_machine.STATUS_LABELS.get(task.status, task.status)),
                    _field("截止", _fmt_dt(task.deadline_at)),
                    _field("负责人", "、".join(at(i) for i in task.owner_open_ids) or "未指定"),
                ],
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": _md("负责人已收到带操作按钮的任务卡，后续状态在那里推进。"),
            },
            _note(f"任务 {task.uid[:8]}　候选 {cand.uid[:8]}"),
        ],
    }


def build_merged_task_card(task) -> Dict[str, Any]:
    """无法撤回的历史副本只保留合并提示，操作入口由原卡承载。"""
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"template": TEMPLATE_MUTED,
                   "title": {"tag": "plain_text", "content": "任务卡已合并"}},
        "elements": [{"tag": "div", "text": _md(
            f"{task.title}\n请在本会话的原任务卡继续处理。"
        )}],
    }


def build_rejected_card(cand, operator_open_id: str = "") -> Dict[str, Any]:
    who = at(operator_open_id) if operator_open_id else "有人"
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": TEMPLATE_MUTED,
            "title": {"tag": "plain_text", "content": "已忽略，不会建单"},
        },
        "elements": [
            {
                "tag": "div",
                "fields": [
                    _field("原候选", cand.title, short=False),
                    _field("处理人", who, short=False),
                ],
            },
            _note(f"候选 {cand.uid[:8]}　如果想恢复，请让发起人重新发一次要求。"),
        ],
    }


def build_stale_card(cand, reason: str = "这条候选已经处理过了") -> Dict[str, Any]:
    """候选不在了 / 已处理 / 已过期时返回的卡片。"""
    title = cand.title if cand is not None else "（候选已不存在）"
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "template": TEMPLATE_MUTED,
            "title": {"tag": "plain_text", "content": "操作未生效"},
        },
        "elements": [
            {"tag": "div", "text": _md(f"{reason}\n**任务**：{title}")},
            _note("这张卡片已失效，不用再点。"),
        ],
    }


def card_to_content(card: Dict[str, Any]) -> str:
    """发送时要塞进 content 字段的字符串。ensure_ascii=False 很关键：
    否则中文变成 \\uXXXX，虽然能用，但排错时完全看不懂。"""
    return json.dumps(card, ensure_ascii=False)
