"""修订代理：POST /thread/task-update 的硬校验 + 转发任务池（方案B：单库 taskpool）。

硬校验（ENGINEERING.md 5.7，三道边界各一行判断）：
- body.task_id != thread.task_id → 400（越权改别的话题任务被物理拒绝）
- thread 无关联任务（task_id 为空）→ 404
- 话题已收口 → 403

通过后转发 taskpool.task_update（单库 group.db，带 project 过滤），
并在消息表写一条 engine 类型修订事件。
"""

from __future__ import annotations

import json
import sqlite3

from . import db, taskpool


class ProxyError(ValueError):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def task_update(
    conn: sqlite3.Connection,
    profile: str,
    thread_id: int,
    task_id: int,
    fields: dict,
    credential: str | None = None,
) -> dict:
    """修订代理入口。校验通过后转发 taskpool.task_update 并写修订事件。

    credential 为执行凭证（req#118 #202）：由 MCP 层生成随请求传入，转交 taskpool 做
    拆解中旧凭据校验；空凭据（无执行上下文）同样透传，由 taskpool 空凭据拒防绕过。
    """
    thread = conn.execute(
        "SELECT * FROM threads WHERE id = ?", (thread_id,)
    ).fetchone()
    if thread is None:
        raise ProxyError(f"thread {thread_id} not found", 404)

    # 边界 1：话题无关联任务 → 404
    if thread["task_id"] is None:
        raise ProxyError("话题未关联任务，无修订对象（404）", 404)

    # 边界 2：越权改别的话题任务 → 400
    if int(task_id) != int(thread["task_id"]):
        raise ProxyError("task_id 与话题关联任务不一致（400）", 400)

    # 边界 3：话题已收口 → 403
    if thread["status"] == "closed":
        raise ProxyError("话题已收口，不能再修订（403）", 403)

    # 项目从话题取（方案B：threads 有 project 列）
    project = thread["project"] if "project" in thread.keys() else "xjk"

    title = fields.get("title", "")
    if 'next_role' in fields:
        raise ProxyError('派单字段已改名为 next_bot，请指定具体成员', 400)
    priority = fields.get("priority")
    next_bot = fields.get("next_bot", "")

    result = taskpool.task_update(project, profile, task_id, title, priority, next_bot,
                                 credential=credential,
                                 **{key: fields[key] for key in ('description', 'design_basis',
                                    'acceptance_criteria', 'prerequisites', 'publish') if key in fields})
    if "error" in result:
        raise ProxyError(f"任务池修订失败：{result['error']}", 403 if result.get('code') == 'permission_denied' else 400)

    # 写 engine 修订事件（消息表，sender_type=engine）
    ts = db.now_iso()
    detail = {
        "action": "task_update",
        "thread_id": thread_id,
        "task_id": task_id,
        "by": profile,
        "fields": {k: v for k, v in fields.items() if v not in (None, "")},
    }
    conn.execute(
        "INSERT INTO messages (project, ts, sender, sender_type, content, mentions, thread_id) "
        "VALUES (?, ?, 'engine', 'engine', ?, '[]', ?)",
        (project, ts, json.dumps(detail, ensure_ascii=False), thread_id),
    )
    conn.commit()

    return {"updated": True, "task": result.get("task"), "event": detail}
