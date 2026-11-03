"""Durable delegation waits within an engine-owned execution.

Native tool receipts establish waits. Read-only native admission discovery recovers
callbacks missed during disconnects; public RPC receipts remain the settlement authority.
"""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS hermes_delegation_cycles (
    execution_id TEXT PRIMARY KEY, binding_generation INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS hermes_delegations (
    execution_id TEXT NOT NULL, delegation_id TEXT NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(execution_id, delegation_id)
);
CREATE TABLE IF NOT EXISTS hermes_native_turns (
    execution_id TEXT NOT NULL, admission_id TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '', terminal INTEGER NOT NULL DEFAULT 0,
    outcome TEXT, generation INTEGER, authority_epoch INTEGER,
    PRIMARY KEY(execution_id, admission_id)
);
CREATE TABLE IF NOT EXISTS hermes_delegation_inputs (
    execution_id TEXT NOT NULL, request_id TEXT NOT NULL,
    message_id INTEGER NOT NULL, text TEXT NOT NULL,
    PRIMARY KEY(execution_id, request_id), UNIQUE(execution_id, message_id)
);
"""


def cycle(conn, row):
    return conn.execute('SELECT * FROM hermes_delegation_cycles WHERE execution_id=?',
                        (row['execution_id'],)).fetchone()


def pending(conn, row):
    return bool(conn.execute('SELECT 1 FROM hermes_delegations WHERE execution_id=? AND consumed=0 LIMIT 1',
                             (row['execution_id'],)).fetchone() or conn.execute(
        'SELECT 1 FROM hermes_delegation_inputs i WHERE i.execution_id=? AND NOT EXISTS '
        '(SELECT 1 FROM hermes_native_turns t WHERE t.execution_id=i.execution_id AND t.request_id=i.request_id AND t.terminal=1)',
        (row['execution_id'],)).fetchone())


def waiting(conn, row):
    """The native turn ended while its logical execution still owns a delegation."""
    return bool(row['state'] != 'terminal' and conn.execute(
        'SELECT 1 FROM hermes_native_turns WHERE execution_id=? AND admission_id=? AND terminal=1',
        (row['execution_id'], row['admission_id'])).fetchone() and pending(conn, row))


def owned(conn, row):
    record = cycle(conn, row)
    if record is None:
        return True
    binding = conn.execute("SELECT generation FROM session_bindings WHERE project=? AND profile=? "
        "AND kind=? AND thread_id=? AND session_id=? AND runtime='hermes' AND switching=0",
        (row['project'], row['profile'], row['kind'], row['thread_id'] or 0, row['session_id'])).fetchone()
    if binding is None or binding['generation'] != record['binding_generation']:
        return False
    if conn.execute('SELECT 1 FROM hermes_session_grants WHERE session=? AND session_id=? '
                    'AND generation=? AND revoked=1',
                    (row['session'], row['session_id'], record['binding_generation'])).fetchone():
        return False
    if not conn.execute('SELECT 1 FROM project_members pm JOIN projects p ON p.name=pm.project '
        'WHERE pm.project=? AND pm.profile=? AND pm.active=1 AND p.archived=0 AND p.removed=0',
        (row['project'], row['profile'])).fetchone():
        return False
    if row['kind'] == 'chat':
        topic = conn.execute('SELECT * FROM threads WHERE id=? AND project=?',
                             (row['thread_id'], row['project'])).fetchone()
        return bool(topic and topic['status'] == 'open' and topic['turn_profile'] == row['profile']
                    and topic['round'] == row['round'] and topic['turn_parent_id'] == row['parent_id'])
    if row['task_id'] is not None:
        task = conn.execute('SELECT t.* FROM tasks t JOIN requirements r ON r.id=t.requirement_id '
            "WHERE t.id=? AND t.project=? AND r.archived=0 AND r.status IN ('open','blocked')",
            (row['task_id'], row['project'])).fetchone()
        return bool(task and ((task['status'] == 'in_progress' and task['claimed_by'] == row['profile'])
                             or (task['status'] == 'pending' and task['next_bot'] == row['profile'])))
    return True


def _delegation_id(request_id):
    try:
        parts = json.loads(request_id)
    except (ValueError, TypeError):
        return None
    return parts[1] if (isinstance(parts, list) and len(parts) >= 2 and parts[0] == 'async_delegation'
                        and isinstance(parts[1], str)) else None


def _dispatched(value):
    if not isinstance(value, dict):
        return
    if value.get('status') == 'dispatched' and value.get('mode') == 'background':
        if isinstance(value.get('units'), list) and value['units']:
            for unit in value['units']:
                if isinstance(unit, dict) and isinstance(unit.get('delegation_id'), str) and unit['delegation_id']:
                    yield unit['delegation_id']
            return
        if isinstance(value.get('delegation_id'), str) and value['delegation_id']:
            yield value['delegation_id']
    # Independently delivered groups return multiple structured dispatch handles.
    for key in ('delegations', 'groups', 'results', 'units'):
        for child in value.get(key, []) if isinstance(value.get(key), list) else []:
            yield from _dispatched(child)


def capture(conn, row, receipt):
    """Called inside receipt settlement's transaction, before any exit side effects."""
    ids = set()
    result = receipt.get('result') or {}
    for message in result.get('messages', []) if isinstance(result, dict) else []:
        if message.get('role') != 'tool' or message.get('name', message.get('tool_name')) != 'delegate_task':
            continue
        try:
            value = json.loads(message.get('content', ''))
        except (TypeError, ValueError):
            continue
        ids.update(_dispatched(value))
    if ids:
        binding = conn.execute('SELECT generation FROM session_bindings WHERE project=? AND profile=? '
            'AND kind=? AND thread_id=? AND session_id=?',
            (row['project'], row['profile'], row['kind'], row['thread_id'] or 0, row['session_id'])).fetchone()
        if binding is None:
            raise ValueError('Delegation has no current engine session binding')
        conn.execute('INSERT OR IGNORE INTO hermes_delegation_cycles VALUES(?,?)',
                     (row['execution_id'], binding['generation']))
        conn.executemany('INSERT OR IGNORE INTO hermes_delegations(execution_id,delegation_id) VALUES(?,?)',
                         [(row['execution_id'], did) for did in ids])
        if row['task_id'] is None and row['kind'] == 'work':
            tasks = conn.execute("SELECT id FROM tasks WHERE project=? AND status='in_progress' AND claimed_by=?",
                                 (row['project'], row['profile'])).fetchall()
            if len(tasks) == 1:
                conn.execute('UPDATE hermes_executions SET task_id=? WHERE execution_id=?',
                             (tasks[0]['id'], row['execution_id']))
    if cycle(conn, row) is None:
        return False
    conn.execute('INSERT INTO hermes_native_turns(execution_id,admission_id,terminal,outcome,generation,authority_epoch) '
        'VALUES(?,?,?,?,?,?) ON CONFLICT(execution_id,admission_id) DO UPDATE SET '
        'terminal=excluded.terminal,outcome=excluded.outcome,generation=excluded.generation,authority_epoch=excluded.authority_epoch',
        (row['execution_id'], receipt['admission_id'], int(receipt['status'] == 'terminal'),
         receipt.get('outcome'), receipt.get('execution_generation'), receipt.get('authority_epoch')))
    turn = conn.execute('SELECT request_id FROM hermes_native_turns WHERE execution_id=? AND admission_id=?',
                         (row['execution_id'], receipt['admission_id'])).fetchone()
    did = _delegation_id(turn['request_id'])
    if did and receipt['status'] == 'terminal':
        conn.execute('UPDATE hermes_delegations SET consumed=1 WHERE execution_id=? AND delegation_id=?',
                     (row['execution_id'], did))
    return (receipt['status'] == 'terminal' and receipt.get('outcome') == 'completed'
            and not row['cancel_reason'] and pending(conn, row))


def native_turns(row, delegation_ids, request_ids):
    """Bounded, read-only discovery from the recorded profile home; never writes native state."""
    path = (Path(row['home']) / 'state.db').resolve()
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.25)) as native:
        native.row_factory = sqlite3.Row
        native.execute('PRAGMA query_only=ON')
        deadline = time.monotonic() + .5
        native.set_progress_handler(lambda: time.monotonic() >= deadline, 1000)
        values = native.execute('SELECT admission_id,request_id,status,generation,owner_epoch FROM session_admissions '
            'WHERE target_session_id=? ORDER BY seq', (row['session_id'],)).fetchall()
        return [dict(r) for r in values if _delegation_id(r['request_id']) in delegation_ids
                or r['request_id'] in request_ids]


def adopt(conn, row, discovered):
    """Keep one logical task execution while reconciling each native admission exactly once."""
    for item in discovered:
        prior = conn.execute('SELECT terminal FROM hermes_native_turns WHERE execution_id=? AND admission_id=?',
                             (row['execution_id'], item['admission_id'])).fetchone()
        if prior and prior['terminal']:
            continue
        conn.execute('INSERT OR IGNORE INTO hermes_native_turns(execution_id,admission_id,request_id) VALUES(?,?,?)',
                     (row['execution_id'], item['admission_id'], item['request_id']))
        conn.execute("UPDATE hermes_executions SET admission_id=?,generation=NULL,authority_epoch=NULL,"
            "state=?,detail='',execution_deadline=? WHERE execution_id=? AND state<>'terminal'",
            (item['admission_id'], 'cancelling' if row['cancel_reason'] else 'submitting',
             time.time() + 3600, row['execution_id']))
        conn.commit()
        return True
    return False


def discover_turns(conn, row):
    ids = {r[0] for r in conn.execute('SELECT delegation_id FROM hermes_delegations WHERE execution_id=?', (row['execution_id'],))}
    requests = {r[0] for r in conn.execute('SELECT request_id FROM hermes_delegation_inputs WHERE execution_id=?', (row['execution_id'],))}
    return native_turns(row, ids, requests)


def poll(conn, row, client):
    """Runs only on this execution's background reconciler, never on an HTTP read path."""
    from . import hermes_gateway_ledger as ledger
    if not owned(conn, row) and not row['cancel_reason']:
        row = ledger.cancel(conn, row['execution_id'], 'superseded')
    if row['cancel_reason']:
        if waiting(conn, row):
            # A fast callback can hand off its task before the next observation.
            # Capture that admission before closing the previous native turn.
            try:
                if adopt(conn, row, discover_turns(conn, row)):
                    row = ledger.read(conn, row['execution_id'])
            except sqlite3.Error:
                pass  # Cancellation remains available when native discovery is unavailable.
        client.rpc('process.stop', session_id=row['session_id'], timeout=5)
        snapshot = client.rpc('session.resume', session_id=row['session_id'], omit_messages=True, timeout=5)
        for item in snapshot.get('pending', []):
            if item['status'] == 'queued':
                client.rpc('prompt.cancel', session_id=row['session_id'], admission_id=item['admission_id'], timeout=5)
            elif item['status'] == 'started':
                client.rpc('session.interrupt', session_id=row['session_id'],
                           execution_generation=item['execution_generation'], timeout=5)
        # Local cancellation revokes writes immediately. Existing execution deadlines
        # and cleanup reconciliation cover the remote shutdown acknowledgement.
        if snapshot.get('running') or snapshot.get('pending'):
            return False
        conn.execute('UPDATE hermes_delegations SET consumed=1 WHERE execution_id=?', (row['execution_id'],))
        conn.commit()
        receipt = client.rpc('prompt.receipt', session_id=row['session_id'], admission_id=row['admission_id'], timeout=5)
        ledger.apply_receipt(conn, row['execution_id'], receipt)
        return True
    found = discover_turns(conn, row)
    if adopt(conn, row, found):
        return True
    # Idempotent request IDs reconcile a lost direct-message submission response.
    admitted = {r['request_id'] for r in found}
    for item in conn.execute('SELECT * FROM hermes_delegation_inputs WHERE execution_id=?', (row['execution_id'],)).fetchall():
        if item['request_id'] not in admitted:
            client.rpc('prompt.submit', session_id=row['session_id'], submission_id=item['request_id'], text=item['text'], timeout=5)
    if row['state'] == 'unknown':
        conn.execute("UPDATE hermes_executions SET state='waiting_delegation',detail='' WHERE execution_id=?",
                     (row['execution_id'],))
        conn.commit()
    return False


def direct_message(conn, profile, project, message_id=None, *, execution_id=None):
    from .delivery import effective_need_reply
    rows = conn.execute("SELECT m.* FROM inbox i JOIN messages m ON m.id=i.message_id WHERE i.profile=? "
        "AND i.status='pending' AND m.project=? AND m.thread_id IS NULL AND m.retract_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM hermes_delegation_inputs d WHERE d.execution_id=? AND d.message_id=m.id) ORDER BY m.id",
        (profile, project, execution_id)).fetchall()
    return next((r['id'] for r in rows if (message_id is None or r['id'] == message_id)
                 and effective_need_reply(r, profile) is not None), None)


def direct_wait(conn, profile, project, message_id=None):
    from . import wake
    row = conn.execute("SELECT * FROM hermes_executions WHERE session=? AND state='waiting_delegation' "
                       'AND cancel_reason IS NULL', (wake.session_name(profile, 'work', None, project),)).fetchone()
    if row and owned(conn, row):
        mid = direct_message(conn, profile, project, message_id, execution_id=row['execution_id'])
        if mid is not None:
            return row, mid
    return None


def enqueue_direct(conn, row, message_id, text):
    from . import hermes_gateway_ledger as ledger
    conn.execute('BEGIN IMMEDIATE')
    try:
        current = ledger.read(conn, row['execution_id'])
        if current['state'] != 'waiting_delegation' or current['cancel_reason'] or not owned(conn, current):
            conn.rollback()
            return False
        if direct_message(conn, current['profile'], current['project'], message_id) is None:
            conn.rollback()
            return False
        conn.execute('INSERT OR IGNORE INTO hermes_delegation_inputs VALUES(?,?,?,?)',
                     (row['execution_id'], uuid.uuid4().hex, message_id, text))
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise
