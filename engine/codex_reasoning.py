"""Persist native Codex JSONL events and expose the common reasoning contract."""
import json
import time

from . import db


def record(session, started_at, event):
    if not isinstance(event, dict):
        return
    if event.get('type') not in ('item.started', 'item.completed', 'turn.completed', 'turn.failed', 'error'):
        return
    conn = db.get_connection()
    try:
        conn.execute('INSERT INTO codex_run_events(session,wake_started_at,timestamp,payload) VALUES(?,?,?,?)',
                     (session, started_at, time.time(), json.dumps(event, ensure_ascii=False)))
        conn.commit()
    finally:
        conn.close()


def _message_id(result, depth=0):
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
        for key in ('structuredContent', 'content', 'text'):
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
        rows = conn.execute('SELECT id,timestamp,payload FROM codex_run_events WHERE session=? '
                            'AND wake_started_at=? AND id>? ORDER BY id',
                            (session, started_at, after_id)).fetchall()
    finally:
        conn.close()
    entries = []
    latest = after_id
    for row in rows:
        latest = row['id']
        event = json.loads(row['payload'])
        item = event.get('item') or {}
        kind = item.get('type')
        entry = dict(id=row['id'], role='assistant', reasoning='', tool_calls=[], tool_name='',
                     timestamp=row['timestamp'], reply_message_id=None,
                     finished=event.get('type') == 'turn.completed')
        if kind in ('reasoning', 'agent_message') and event['type'] == 'item.completed':
            entry['reasoning'] = item.get('text') or ''
        elif kind in ('mcp_tool_call', 'command_execution', 'web_search', 'file_change'):
            name = item.get('tool') or kind
            entry['tool_name'] = name
            details = item.get('arguments', item.get('command', item.get('query', item.get('changes', ''))))
            entry['tool_calls'] = [{'name': name, 'args_preview': json.dumps(details, ensure_ascii=False)
                                   if not isinstance(details, str) else details}]
            if event['type'] == 'item.completed' and name.endswith('group_send'):
                entry['reply_message_id'] = _message_id(item.get('result'))
        elif event.get('type') in ('error', 'turn.failed'):
            entry['reasoning'] = json.dumps(event.get('error', event.get('message', event)), ensure_ascii=False)
        if entry['reasoning'] or entry['tool_calls'] or entry['finished'] or entry['reply_message_id']:
            entries.append(entry)
    return {'entries': entries, 'latest_id': latest}
