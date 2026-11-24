"""Day 9：卡片按钮回调处理。

回调这层有三条硬约束，代码结构是围着它们设计的：

1. 【3 秒内必须响应】飞书等不到响应就重推，用户会看到"点了没反应"。
   所以 process() 里只做本地数据库读写：不调模型、不发消息、不查通讯录。
   要在回调里做的事，一律先落库、让 Worker 去做。

2. 【重推 = 并发】同一次点击可能进来两次。靠 repo 里的条件更新抢占，
   第二次拿到 None，回一句"已处理"，绝不建第二张单。

3. 【不能 500】回调抛异常，用户看到的是按钮点了没反应，排查时还没有
   任何用户侧线索。所以最外层统一兜底，永远返回一张能看懂的卡片。

process() 是纯逻辑（只依赖 repo 和 cards），不依赖 lark 的模型对象，
这样单测不用构造 SDK 里的那一堆类。
"""

import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional

import cards
import events
import native_task
import permissions
import repo
from models_db import CAND_CONFIRMED, CAND_EXPIRED, CAND_REJECTED

_STALE_REASON = {
    CAND_CONFIRMED: "这条候选已经确认过了，任务已经建好",
    CAND_REJECTED: "这条候选已经被忽略过了",
    CAND_EXPIRED: "这条候选已经过期（超过 72 小时没处理）",
}


@dataclass
class Outcome:
    """回调的处理结果：一句 toast + 一张替换后的卡片。"""

    toast_type: str          # success / error / warning / info
    toast_content: str
    card: Optional[Dict[str, Any]]
    ok: bool = True


def process(action: str, cid: str = "", operator_open_id: str = "",
            tid: str = "", ver: Optional[int] = None,
            pub: bool = False, message_id: str = "") -> Outcome:
    """执行按钮动作。

    cid = 候选 uid（确认/忽略），tid = 任务 uid（状态流转）。
    两种卡片共用一个进程入口，靠 value 里带了哪个 id 分流。

    pub = 这张卡是群里发的公共卡。它决定「点完之后还哪张卡」：
    公共卡必须还公共卡（一张卡多人看，不能变成某个人的私人视角），
    私聊卡才按点击者的权限重新渲染。
    """
    if tid:
        return _process_task(action, tid, operator_open_id, ver, pub)

    try:
        return _process_candidate(action, cid, operator_open_id, message_id)
    except repo.CandidateActionError as exc:
        # 只给点击者提示，授权人仍可使用原卡。
        print(f"[CARD] 候选操作拒绝 cid={cid[:8]} operator={operator_open_id[:12]} 原因={exc}")
        return Outcome("warning", str(exc), None, ok=False)


def _process_candidate(action: str, cid: str, operator_open_id: str,
                       message_id: str = "") -> Outcome:
    if not cid:
        return Outcome(
            "error", "卡片数据不完整，请联系管理员",
            cards.build_stale_card(None, "卡片里没有候选标识"),
            ok=False,
        )

    cand = repo.get_candidate(cid)
    if cand is None:
        return Outcome(
            "error", "找不到这条候选",
            cards.build_stale_card(None, "候选不在库里（可能被清理了）"),
            ok=False,
        )

    if action == cards.ACTION_CONFIRM:
        task = repo.confirm_candidate(cid, operator_open_id=operator_open_id)
        if task is None:
            # 抢占失败：已确认 / 已驳回 / 已过期
            fresh = repo.get_candidate(cid) or cand
            reason = _STALE_REASON.get(fresh.status, "这条候选已经不在待确认状态")
            return Outcome(
                "warning", reason,
                cards.build_stale_card(fresh, reason),
                ok=False,
            )
        print(f"[CARD] 确认建单 candidate={cid[:8]} task={task.uid[:8]} "
              f"operator={operator_open_id[:12]}")
        # 回调替换的确认卡也会被发起人继续查看，必须参与后续状态同步。
        if message_id:
            repo.register_task_card(
                task.uid, message_id, operator_open_id,
                viewer_open_id=operator_open_id, kind="confirmed",
                version=task.version,
            )
        # 给负责人推任务卡：建单之后真正要干活的是他，不是发起人。
        # 放后台线程发，回调本身必须秒回（飞书 3 秒不响应会重推）。
        _push_task_card_async(task)
        _reply_task_created_async(cand.message_id, task)
        try:
            native_task.on_created_async(task)
        except Exception as exc:
            print(f"[NATIVE TASK] {task.uid} 启动同步失败：{exc}")
        # 日历同步：确认建单后去应用主日历建一个「截止前 15 分钟」的占用块。
        # 内部自带开关和后台线程，没开权限时这一行等于什么都没做。
        try:
            import calendar_sync
            calendar_sync.on_created_async(task.uid)
        except Exception as exc:
            print(f"[CAL] 建单后日历同步启动失败：{type(exc).__name__}: {exc}")
        return Outcome(
            "success", "已建单",
            cards.build_task_card(task, operator_open_id),
        )

    if action == cards.ACTION_REJECT:
        rejected = repo.reject_candidate(cid, operator_open_id=operator_open_id,
                                         reason="卡片上点了忽略")
        if not rejected:
            fresh = repo.get_candidate(cid) or cand
            reason = _STALE_REASON.get(fresh.status, "这条候选已经不在待确认状态")
            return Outcome("warning", reason, cards.build_stale_card(fresh, reason), ok=False)
        print(f"[CARD] 已忽略 candidate={cid[:8]} operator={operator_open_id[:12]}")
        return Outcome(
            "info", "已忽略，不会建单",
            cards.build_rejected_card(cand, operator_open_id),
        )

    return Outcome(
        "error", f"未知操作：{action}",
        None,
        ok=False,
    )


def _process_task(action: str, tid: str, operator_open_id: str,
                  ver: Optional[int], pub: bool = False) -> Outcome:
    """任务卡上的状态按钮。

    三件事按顺序做：拿当前任务 → 带版本号流转 → 用新任务重新渲染卡片。
    重新渲染很关键：点完之后按钮必须跟着新状态变，否则用户会对着
    一张「待开始」的卡片反复点「开始处理」。

    viewer 的规则：
    - 私聊卡（pub=False）→ 按点击者的权限渲染，他看不到自己点不了的按钮
    - 群卡（pub=True）→ 渲染成公共视角，因为群里所有人都看这一份
    """
    task = repo.get_task(tid)
    if task is None:
        return Outcome(
            "error", "找不到这条任务",
            cards.build_stale_card(None, "任务不在库里"),
            ok=False,
        )

    business_roles = permissions.roles_of(task, operator_open_id) - {permissions.ROLE_ADMIN}
    if not business_roles:
        return Outcome("warning", "当前账号无权操作此任务", None, ok=False)
    viewer = None if pub else operator_open_id
    if action == "supplement" and permissions.can(task, operator_open_id, action).ok:
        return Outcome("info", "请回复本任务卡填写交付物和截止时间",
                       cards.build_task_card(task, viewer))
    result = repo.transition_task(
        tid, action, operator_open_id=operator_open_id, expected_version=ver,
    )

    if result.ok:
        fresh = result.task
        print(f"[CARD] 状态流转 task={tid[:8]} {result.message} "
              f"operator={operator_open_id[:12]}")
        # 状态变了，告诉该知道的人（提交验收 → 发起人；打回 → 负责人）。
        # 放后台线程发，回调本身必须秒回。
        events.flush_async()
        return Outcome(
            "success", result.message,
            cards.build_task_card(fresh, viewer),
        )

    # 失败分三种，处理方式完全不同：
    # 1) 版本冲突：别人先改了。卡片上的按钮是旧的，刷新成最新即可
    # 2) 状态/权限不允许：把原因写在卡片上，让人知道为什么点不动
    # 3) 任务不存在：上面已经挡掉了
    latest = repo.get_task(tid) or task
    if result.message.startswith("请回复本任务卡"):
        return Outcome("info", result.message, cards.build_task_card(latest, viewer, hint=result.message), ok=False)
    if result.conflict:
        return Outcome(
            "warning", "任务已被别人更新，卡片已刷新",
            cards.build_task_card(latest, viewer,
                                  hint="有人先一步改了状态，这张卡已是最新的。"),
            ok=False,
        )

    return Outcome(
        "error", result.message,
        cards.build_task_card(latest, viewer,
                              hint=f"操作未生效：{result.message}"),
        ok=False,
    )


def _push_task_card_async(task) -> None:
    """后台线程给负责人推任务卡。

    为什么不在回调里同步发：飞书等 3 秒就重推，发消息要走网络，
    一旦慢一次就会重复推送。回调只负责落库和响应，发消息扔后台。
    线程是 daemon：进程退出时不会挂住。
    """
    def _run() -> None:
        try:
            import feishu_api
            feishu_api.push_task_card(task)
        except Exception as exc:  # 通知失败不能影响业务
            print(f"[CARD] 推任务卡失败 task={task.uid[:8]} "
                  f"{type(exc).__name__}: {exc}")

    threading.Thread(target=_run, name=f"push-{task.uid[:8]}", daemon=True).start()


def _reply_task_created_async(message_id: str, task) -> None:
    """独立发送发起人私聊回执，回调和负责人任务卡不等待它。"""
    def _run() -> None:
        try:
            import feishu_api
            feishu_api.reply_task_created(message_id, task)
        except Exception as exc:
            print(f"[REPLY] 私聊建单回执异常 task={task.uid} "
                  f"original_message={message_id or '-'}: {type(exc).__name__}: {exc}")

    threading.Thread(target=_run, name=f"reply-{task.uid[:8]}", daemon=True).start()


# ---------------------------------------------------------------- 飞书适配层

def _to_response(outcome: Outcome):
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTriggerResponse,
    )

    # 注意：这里必须传「原始字典」，不能传已经构造好的 CallBackToast /
    # CallBackCard 实例。SDK 的 init() 会拿字典递归反序列化，
    # 遇到实例会抛 UnmarshalException（错误信息还写着 expected X but was X，
    # 特别难懂）。
    payload = {
        "toast": {
            "type": outcome.toast_type,
            "content": outcome.toast_content,
        },
    }
    if outcome.card is not None:
        payload["card"] = {"type": "raw", "data": outcome.card}
    return P2CardActionTriggerResponse(payload)


def handle(event: Any):
    """长连接里的卡片回调入口。event 是 P2CardActionTrigger。

    这里不做任何耗时操作，也绝不抛异常 —— 抛了飞书只会重推。
    """
    try:
        data = event.event
        action_obj = getattr(data, "action", None)
        value = (getattr(action_obj, "value", None) or {}) if action_obj else {}
        action = value.get("act") or value.get("action") or ""
        cid = value.get("cid") or value.get("candidate_uid") or ""
        tid = value.get("tid") or value.get("task_uid") or ""
        pub = bool(value.get("pub"))
        ver = value.get("ver")
        if isinstance(ver, str) and ver.isdigit():
            ver = int(ver)
        elif not isinstance(ver, int):
            ver = None
        operator = getattr(getattr(data, "operator", None), "open_id", "") or ""

        print(f"[CARD] 收到回调 action={action} "
              f"cid={cid[:8] or '-'} tid={tid[:8] or '-'} ver={ver} "
              f"pub={int(pub)} operator={operator[:12]}")
        context = getattr(data, "context", None)
        message_id = getattr(context, "open_message_id", "") or ""
        return _to_response(process(action, cid, operator, tid=tid, ver=ver, pub=pub,
                                    message_id=message_id))
    except Exception as exc:
        print(f"[CARD] 回调处理异常：{type(exc).__name__}: {exc}")
        try:
            return _to_response(Outcome(
                "error", "系统处理出错，请稍后重试或联系管理员",
                cards.build_stale_card(None, "回调处理时发生异常"),
                ok=False,
            ))
        except Exception:
            return None
