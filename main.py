"""主入口：消息接入 → 去重 → 状态反馈 → 任务识别 → 候选落库 → 确认卡片。

当前进度（Day 13）：
1. 长连接接入、机器人消息过滤、event_id 去重         —— Day 2
2. 模型识别候选 + Pydantic 校验 + 防幻觉负责人        —— Day 3
3. deadline_text 换算成 deadline_at                  —— Day 4
4. 候选写入 task_candidate 表，等人工确认             —— Day 8
5. 确认卡片 + 卡片回调（点按钮建单）                  —— Day 9
6. 状态机 + 权限 + 乐观锁 + 任务卡按钮                —— Day 10-11
7. 日报、发件箱、Worker                              —— Day 12
8. 状态变更即时通知 + 群里一句话改状态                —— Day 13

主链路已经闭环：
群消息 → 识别 → 确认建单 → 状态流转 → 提醒 → 状态变更反馈

还没做：多维表格出口、反馈走 LLM 兜底、延期改截止。
"""

import os
import sys

# 飞书接口必须直连，绝不能走本机代理（Clash 等）。
# 症状：SSLError(SSLEOFError(8, '[SSL: UNEXPECTED_EOF_WHILE_READING]'))，
# 连 /callback/ws/endpoint 都建不起来，然后无限重连。
# 只要把飞书域名加进 NO_PROXY，即使系统开了全局代理也不受影响。
_FEISHU_NO_PROXY = "open.feishu.cn,.feishu.cn,msg-frontier.feishu.cn,localhost,127.0.0.1"
os.environ["NO_PROXY"] = ",".join(
    filter(None, [os.environ.get("NO_PROXY", ""), _FEISHU_NO_PROXY])
)
os.environ["no_proxy"] = os.environ["NO_PROXY"]

import lark_oapi as lark
from dotenv import load_dotenv
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

import models
import store
import detector
import db
import repo
import cards
import card_action
import feedback
import events
import feishu_api

load_dotenv()

APP_ID = os.getenv("FEISHU_APP_ID", "")
APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")

STATS = {"total": 0, "from_bot": 0, "duplicate": 0, "accepted": 0}
_db_ready = False
# 识别出候选后是否自动推确认卡片。调提示词阶段可以关掉，免得刷屏。
AUTO_PUSH_CARD = os.getenv("CARD_AUTO_PUSH", "1").strip() != "0"


def on_message(data: P2ImMessageReceiveV1) -> None:
    cmd = models.parse_event(data.event, data.header)
    STATS["total"] += 1

    # 1) 过滤机器人自身消息：必须在去重之前，避免污染 event_inbox
    if cmd.is_from_bot:
        STATS["from_bot"] += 1
        print(f"[SKIP] 机器人自己的消息 sender_type={cmd.sender_type}")
        return

    # 2) 去重：同一 event_id 只处理一次
    if not store.is_new_event(cmd.event_id, cmd.message_id, cmd.chat_id):
        STATS["duplicate"] += 1
        print(f"[DUP ] 重复事件 {cmd.event_id} 已忽略")
        return

    # 3) 状态反馈：回复原消息说「做完了」「卡住了」，直接改状态。
    #    放在识别任务之前 —— 「做完了」要是先过一遍任务识别，
    #    模型很可能把它当成一条新任务建出来。
    try:
        applied = feedback.apply_feedback(cmd)
        if applied.text:
            print(f"[反馈] {applied.text}")
            feishu_api.send_text_to_chat(cmd.chat_id, applied.text)
            if applied.ok:
                events.flush_async()
            return
    except Exception as exc:  # 反馈层出错不该挡住正常的任务识别
        print(f"[反馈] 处理异常：{type(exc).__name__}: {exc}")

    # 4) 进入后续流程：Day 3 调模型识别任务
    STATS["accepted"] += 1
    print("=" * 64)
    print(f"message_id : {cmd.message_id}")
    print(f"chat_id    : {cmd.chat_id}")
    print(f"chat_type  : {cmd.chat_type}")
    print(f"sender     : {cmd.sender_open_id}  (type={cmd.sender_type})")
    print(f"text_raw   : {cmd.text_raw}")
    print(f"text       : {cmd.text}")
    print(f"mentions   : {[(m.name, m.open_id) for m in cmd.mentions]}")
    print(f"is_reply   : {cmd.is_thread_reply}  root={cmd.root_id} parent={cmd.parent_id}")
    print(f"sent_at    : {cmd.sent_at}")

    # 4) 任务识别（模型只给候选，不建单、不发消息）
    try:
        result = detector.detect(cmd)
        print(f"识别结果   : 类型={result.candidate_type} 置信度={result.confidence:.2f} "
              f"是任务={result.is_task_candidate}")
        if result.clarification_question:
            print(f"需要澄清   : {result.clarification_question}")
        for i, task in enumerate(result.tasks, 1):
            print(f"  候选{i}    : {task.title}")
            print(f"             交付物={task.deliverable} 时间原文={task.deadline_text}")
            print(f"             截止时间={task.deadline_at} 状态={task.deadline_status} "
                  f"规则={task.deadline_rule}")
            print(f"             负责人={task.assignee_mention_ids} 缺失={task.missing_fields}")
            print(f"             证据={task.evidence}")

        # 5) 落库：候选是待确认状态，还没建单、还没通知任何人
        saved = repo.save_candidates(result, cmd)
        for cand in saved:
            print(f"  [入库]     uid={cand.uid[:8]} status={cand.status} "
                  f"过期于={cand.expires_at:%m-%d %H:%M}")
        if result.tasks and not saved:
            print("  [入库]     全部已存在，本次没有新增候选（重复识别）")

        # 6) 推确认卡片。发不出去不影响主流程 —— 候选已经在库里，
        #    可以用 show_db.py 手动确认。
        if saved and AUTO_PUSH_CARD:
            for cand in saved:
                feishu_api.push_confirm_card(cand, cards.build_candidate_card(cand))
        elif saved:
            print("  [卡片]     CARD_AUTO_PUSH=0，跳过推送")
    except detector.DetectionError as exc:
        print(f"[FAIL] 识别失败，应转人工队列：{exc}")
    except Exception as exc:  # 落库失败不该让长连接挂掉
        print(f"[FAIL] 落库失败：{type(exc).__name__}: {exc}")
    finally:
        # 计数放在 finally，失败时也能看到整体处理概况，方便排错
        cand_counts = repo.count_candidates_by_status() if _db_ready else {}
        cand_desc = " ".join(f"{k}={v}" for k, v in sorted(cand_counts.items())) or "无"
        print(f"计数       : 收到 {STATS['total']} / 机器人 {STATS['from_bot']} "
              f"/ 重复 {STATS['duplicate']} / 有效 {STATS['accepted']} "
              f"/ 库内事件 {store.count_events()}")
        print(f"候选库     : {cand_desc} / 正式任务 {repo.count_tasks() if _db_ready else 0}")


def main() -> None:
    global _db_ready

    if not APP_ID or not APP_SECRET:
        raise SystemExit("缺少凭证，请先运行 set_cred.bat")

    store.init_db()   # 事件去重表（sqlite3 直连）
    db.init_db()      # 业务表（SQLAlchemy）：候选 / 任务 / 负责人 / 状态流水
    _db_ready = True

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        # 卡片按钮回调。长连接模式下和普通事件走同一条 WS，不用配公网回调地址，
        # 也就不需要自己做签名校验 —— SDK 内部已经验过了。
        .register_p2_card_action_trigger(card_action.handle)
        .build()
    )
    client = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )

    print("已就绪：消息接入 + 识别 + 时间解析 + 候选落库 + 确认卡片")
    print(f"卡片推送：{'开启' if AUTO_PUSH_CARD else '关闭'}（CARD_AUTO_PUSH=0 可关）")
    print("去测试群发消息试试；识别出来会私聊你一张确认卡片，点「确认建单」")
    print("查看数据：.venv\\Scripts\\python.exe show_db.py --all")
    client.start()


if __name__ == "__main__":
    main()
