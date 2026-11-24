"""确认建单后，在飞书任务中心创建对应任务。"""

import os
import threading
import time

from calendar_api import _request


def build_payload(task):
    details = [task.description] if task.description else []
    if task.deliverable:
        details.append(f"交付物：{task.deliverable}")
    if task.acceptance_criteria:
        details.append(f"验收标准：{task.acceptance_criteria}")
    priority = "紧急" if task.priority == "urgent" else "普通"
    details.extend([f"优先级：{priority}", f"任务编号：{task.uid}"])
    body = {
        "summary": task.title,
        "description": "\n\n".join(details),
        "client_token": task.uid,
        "members": [
            {"type": "user", "role": "assignee", "id": owner}
            for owner in dict.fromkeys(task.owner_open_ids)
        ],
    }
    if task.creator_open_id and task.creator_open_id not in task.owner_open_ids:
        body["members"].append({
            "type": "user", "role": "follower", "id": task.creator_open_id,
        })
    if task.deadline_at is not None:
        body["due"] = {
            "timestamp": str(int(task.deadline_at.timestamp() * 1000)),
            "is_all_day": False,
        }
    return body


def on_created_async(task):
    if os.getenv("FEISHU_NATIVE_TASK_ENABLED", "0").strip() != "1":
        return
    body = build_payload(task)
    if not task.owner_open_ids:
        return

    def create():
        for delay in (0, 2, 5):
            if delay:
                time.sleep(delay)
            ok, data, error = _request(
                "POST", "/task/v2/tasks",
                params={"user_id_type": "open_id"}, body=body,
            )
            if ok:
                guid = (data.get("task") or {}).get("guid")
                print(f"[NATIVE TASK] {task.uid} 已同步 guid={guid}")
                return
        print(f"[NATIVE TASK] {task.uid} 同步失败：{error}")

    threading.Thread(target=create, name=f"native-task-{task.uid[:8]}",
                     daemon=True).start()
