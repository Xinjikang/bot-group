"""思考过程读取：从 bot 的 state.db 读 reasoning + tool_calls，供前端思考气泡展示。

展示内容：
- 完整推理内容（reasoning 全文，不截断）
- 工具调用只显示开头（function.name + arguments 前 N 字）
- 同一次唤醒的所有内容追加到同一个气泡（按会话名 + after_id 增量拉取）
- 气泡最大高度 + 滚动条（前端控制）

数据源：Hermes 的 state.db。reasoning 逐条落库，增量读取可展示执行中的思考。

会话作用域：按 sessions.title = session_name() 精确锁定，
避免同 profile 的桌面群聊或外部私聊内容混入当前项目的思考气泡。
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

# 工具调用参数仅预览开头，长度由此常量控制。
_TOOL_ARGS_PREVIEW = 120


def _sent_message_id(content: str | None) -> int | None:
    """Decode MCP JSON, including Hermes' result and untrusted-result wrappers."""
    def visit(value, depth=0):
        if depth > 6:
            return None
        if isinstance(value, dict):
            mid = value.get("message_id")
            if type(mid) is int and mid > 0:
                return mid
            for key in ("result", "content", "text"):
                found = visit(value.get(key), depth + 1)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = visit(item, depth + 1)
                if found is not None:
                    return found
        elif isinstance(value, str):
            decoder = json.JSONDecoder()
            for index, char in enumerate(value):
                if char != "{":
                    continue
                try:
                    parsed, _ = decoder.raw_decode(value[index:])
                except ValueError:
                    continue
                found = visit(parsed, depth + 1)
                if found is not None:
                    return found
        return None
    return visit(content)


def _hermes_home() -> Path:
    """hermes 根目录（state.db 所在）。引擎环境 HERMES_HOME 已设。"""
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent.parent


def _profile_state_db(profile: str) -> Path:
    """该 profile 的 state.db 路径。default → <home>/state.db，其他 → <home>/profiles/<名>/state.db。"""
    home = _hermes_home()
    from . import bot_identity
    return bot_identity.hermes_home(profile, home) / 'state.db'


def _parse_tool_calls(raw: str | None) -> list[dict]:
    """解析 tool_calls JSON，提取 {name, args_preview}。工具调用只显示开头。"""
    if not raw:
        return []
    try:
        calls = json.loads(raw)
    except (TypeError, ValueError):
        return []
    out = []
    for c in calls:
        if not isinstance(c, dict):
            continue
        fn = c.get("function") or {}
        name = fn.get("name") or c.get("name") or ""
        args = fn.get("arguments") or ""
        if isinstance(args, str):
            preview = args[:_TOOL_ARGS_PREVIEW]
        else:
            preview = json.dumps(args, ensure_ascii=False)[:_TOOL_ARGS_PREVIEW]
        out.append({"name": name, "args_preview": preview})
    return out


def read_reasoning(profile: str, session: str, after_id: int = 0, *, wake_started_at: float, wake_ended_at: float | None = None, hermes_session_id: str | None = None) -> dict:
    """读该 profile 该会话里 id > after_id 的思考条目（reasoning + tool_calls）。

    返回 {entries: [{id, role, reasoning, tool_calls, timestamp}], latest_id}。
    entries 按 id 升序（前端按顺序追加到气泡）。
    """
    db_path = _profile_state_db(profile)
    if not db_path.exists():
        return {"entries": [], "latest_id": after_id}

    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        # 会话名 → session_id（sessions.title 精确匹配，会话作用域硬边界）
        sess = conn.execute('SELECT id FROM sessions WHERE id=?', (hermes_session_id,)).fetchone() if hermes_session_id else conn.execute(
            "SELECT id FROM sessions WHERE title=? ORDER BY started_at DESC LIMIT 1",
            (session,),
        ).fetchone()
        if sess is None:
            return {"entries": [], "latest_id": after_id}

        # A recorded execution end is authoritative. Compaction inserts a user
        # summary before copied turns (whose timestamps remain unchanged), so
        # counting user rows by ID can truncate a whole execution.
        next_turn = conn.execute(
            "SELECT timestamp FROM messages WHERE session_id=? AND active=1 "
            "AND role='user' AND timestamp>=? "
            "AND COALESCE(content,'') NOT LIKE '[CONTEXT COMPACTION%' "
            "ORDER BY timestamp,id LIMIT 1 OFFSET 1",
            (sess["id"], wake_started_at),
        ).fetchone() if wake_ended_at is None else None
        if next_turn is not None:
            wake_ended_at = min(wake_ended_at, next_turn["timestamp"]) if wake_ended_at is not None else next_turn["timestamp"]

        rows = conn.execute(
            """
            SELECT id, role, content, reasoning, reasoning_content, tool_calls, tool_name, timestamp
            FROM messages
            WHERE session_id=? AND active=1 AND id > ?
              AND timestamp >= ? AND (? IS NULL OR timestamp < ?)
              AND (role='assistant' OR tool_name LIKE '%group_send')
            ORDER BY id ASC
            """,
            (sess["id"], after_id, wake_started_at, wake_ended_at, wake_ended_at),
        ).fetchall()

        entries = []
        latest_id = after_id
        for r in rows:
            latest_id = max(latest_id, r["id"])
            # Hermes also writes compaction summaries as assistant rows. They
            # are context bookkeeping, not a completed turn or visible reasoning.
            if (r["content"] or "").lstrip().startswith('[CONTEXT COMPACTION'):
                continue
            reply_id = None
            if r["role"] == "tool" and (r["tool_name"] or "").endswith("group_send"):
                reply_id = _sent_message_id(r["content"])
            # reasoning 优先，reasoning_content 兜底（两者内容重复，reasoning 更完整）
            reasoning = (r["reasoning"] or r["reasoning_content"] or "").strip()
            tool_calls = _parse_tool_calls(r["tool_calls"])
            # Send results and final assistant rows also carry lifecycle markers.
            finished = r["role"] == "assistant" and not tool_calls and bool(r["content"])
            if not reasoning and not tool_calls and reply_id is None and not finished:
                continue
            entries.append({
                "id": r["id"],
                "role": r["role"],
                "reasoning": reasoning,
                "tool_calls": tool_calls,
                "tool_name": r["tool_name"] or "",
                "timestamp": r["timestamp"],
                "reply_message_id": reply_id,
                "finished": finished,
            })
        return {"entries": entries, "latest_id": latest_id}
    finally:
        conn.close()
