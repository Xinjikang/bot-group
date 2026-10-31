"""Persist native DSH JSONL events and expose the common reasoning contract.

事件契约（0.2.1-rc.5 headless --json 实装 projection）：
- session：{type, sessionId, cwd}——真实会话 ID 在开头；
- thinking：{type, text}——推理块；
- text：{type, text}——正文块；
- tool_call：{type, callId, tool, input}；
- tool_result：{type, callId, status: completed|error, result}；
- status：{type, phase: turn_start|step_start|step_end|turn_end, ...}；
- final：{type, text}——仅含 type/text（不依赖 final.reason）。
"""
import json
import time

from . import db

_RECORD_TYPES = ('session', 'thinking', 'text', 'tool_call', 'tool_result', 'final', 'status', 'error')


def record(session, started_at, event):
    if not isinstance(event, dict):
        return
    if event.get('type') not in _RECORD_TYPES:
        return
    conn = db.get_connection()
    try:
        conn.execute('INSERT INTO dsh_run_events(session,wake_started_at,timestamp,payload) '
                     'VALUES(?,?,?,?)',
                     (session, started_at, time.time(), json.dumps(event, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()


def _message_id(result, depth=0):
    """从 group_send 工具结果里解析 message_id（与 codex_reasoning 同契约）。"""
    if depth > 8:
        return None
    if isinstance(result, str):
        try:
            return _message_id(json.loads(result), depth + 1)
        except ValueError:
            return None
    if isinstance(result, dict):
        mid = result.get('message_id')
        if type(mid) is int and mid > 0:
            return mid
        for key in ('structuredContent', 'content', 'text', 'result'):
            mid = _message_id(result.get(key), depth + 1)
            if mid is not None:
                return mid
    if isinstance(result, list):
        for part in result:
            mid = _message_id(part, depth + 1)
            if mid is not None:
                return mid
    return None


def read(session, started_at, after_id=0, ended_at=None):
    conn = db.get_connection()
    try:
        # Exact execution identity includes late reader writes after process exit.
        rows = conn.execute('SELECT id,timestamp,payload FROM dsh_run_events WHERE session=? '
                            'AND wake_started_at=? AND id>? ORDER BY id',
                            (session, started_at, after_id)).fetchall()
    finally:
        conn.close()
    entries = []
    latest = after_id
    for row in rows:
        latest = row['id']
        event = json.loads(row['payload'])
        kind = event.get('type')
        entry = dict(id=row['id'], role='assistant', reasoning='', tool_calls=[], tool_name='',
                     timestamp=row['timestamp'], reply_message_id=None,
                     finished=kind == 'final')
        if kind in ('thinking', 'text', 'final'):
            entry['reasoning'] = event.get('text') or ''
        elif kind == 'tool_call':
            name = event.get('tool') or ''
            entry['tool_name'] = name
            entry['tool_calls'] = [{'name': name,
                                    'args_preview': json.dumps(event.get('input'), ensure_ascii=False)}]
        elif kind == 'tool_result':
            name = event.get('tool') or ''
            entry['tool_name'] = name
            if event.get('status') == 'completed' and name.endswith('group_send'):
                entry['reply_message_id'] = _message_id(event.get('result'))
        elif kind == 'error':
            entry['reasoning'] = json.dumps(event, ensure_ascii=False)
        if entry['reasoning'] or entry['tool_calls'] or entry['finished'] or entry['reply_message_id']:
            entries.append(entry)
    return {'entries': entries, 'latest_id': latest}
