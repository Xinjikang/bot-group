"""Durable admission identities, cancellation intent and terminal cleanup outbox."""
from __future__ import annotations

import time
import uuid


SCHEMA = """
CREATE TABLE IF NOT EXISTS hermes_executions (
    execution_id TEXT PRIMARY KEY,
    session TEXT NOT NULL,
    started_at REAL NOT NULL,
    project TEXT NOT NULL,
    profile TEXT NOT NULL,
    kind TEXT NOT NULL,
    thread_id INTEGER,
    parent_id INTEGER,
    round INTEGER,
    task_id INTEGER,
    job_id INTEGER,
    event_floor INTEGER,
    source TEXT NOT NULL,
    python TEXT NOT NULL,
    home TEXT NOT NULL,
    workdir TEXT NOT NULL,
    session_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    submission_id TEXT NOT NULL UNIQUE,
    admission_id TEXT,
    generation INTEGER,
    authority_epoch INTEGER,
    state TEXT NOT NULL DEFAULT 'submitting',
    cancel_reason TEXT,
    submit_deadline REAL NOT NULL,
    cancel_deadline REAL,
    reconcile_deadline REAL NOT NULL,
    execution_deadline REAL,
    outcome TEXT,
    ended_at REAL,
    cleanup_pending INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL,
    cleanup_deadline REAL,
    rewind_target INTEGER,
    detail TEXT NOT NULL DEFAULT '',
    UNIQUE(session, started_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS hermes_execution_active
ON hermes_executions(session) WHERE state != 'terminal';
CREATE TABLE IF NOT EXISTS hermes_session_migrations (
    profile TEXT NOT NULL, old_session_id TEXT NOT NULL, new_session_id TEXT NOT NULL,
    created_at REAL NOT NULL, PRIMARY KEY(profile, old_session_id)
);
"""


def initialize(conn):
    conn.executescript(SCHEMA)


def prepare(conn, *, session, project, profile, kind, thread_id, task_id, job_id,
            event_floor, source, python, home, workdir, session_id, profile_id,
            started_at=None, parent_id=None, round_no=None):
    """Commit the write-ahead identity before prompt.submit can reach the wire."""
    start = time.time() if started_at is None else started_at
    eid, submission = uuid.uuid4().hex, uuid.uuid4().hex
    conn.execute('INSERT INTO hermes_executions '
        '(execution_id,session,started_at,project,profile,kind,thread_id,task_id,job_id,event_floor,'
        'source,python,home,workdir,session_id,profile_id,submission_id,submit_deadline,'
        'reconcile_deadline,execution_deadline) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (eid, session, start, project, profile, kind, thread_id, task_id, job_id, event_floor,
         str(source), str(python), str(home), str(workdir), session_id, profile_id, submission,
         start + 30, start + 150, start + 3600 if task_id is not None else None))
    conn.execute('UPDATE hermes_executions SET parent_id=?,round=? WHERE execution_id=?',
                 (parent_id, round_no, eid))
    conn.commit()
    return read(conn, eid)


def read(conn, execution_id):
    return conn.execute('SELECT * FROM hermes_executions WHERE execution_id=?', (execution_id,)).fetchone()


CLEANUP_MAX_ATTEMPTS = 5
CLEANUP_BUDGET_SECONDS = 300.0
_CLEANUP_BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0)


def active(conn, session=None):
    """执行占用：只看 state != terminal；cleanup_pending 不算占用（设计 4.1）。"""
    query = "SELECT * FROM hermes_executions WHERE state != 'terminal'"
    return conn.execute(query + (' AND session=?' if session else ''), (session,) if session else ()).fetchall()


def cleanup_pending(conn, session=None):
    """清理待办：terminal 后尚未完成本地清理的行（会话禁用与清理 worker 重启用）。"""
    query = "SELECT * FROM hermes_executions WHERE state='terminal' AND cleanup_pending=1"
    return conn.execute(query + (' AND session=?' if session else ''), (session,) if session else ()).fetchall()


def _cleanup_backoff(retry_count):
    idx = min(retry_count, len(_CLEANUP_BACKOFF) - 1)
    return _CLEANUP_BACKOFF[idx]


def cancel(conn, execution_id, reason='user', *, now=None):
    """Cancellation retries keep the first intent and absolute deadlines."""
    now = time.time() if now is None else now
    conn.execute("UPDATE hermes_executions SET cancel_reason=COALESCE(cancel_reason,?), "
                 "cancel_deadline=COALESCE(cancel_deadline,?), "
                 "reconcile_deadline=CASE WHEN cancel_deadline IS NULL THEN ? ELSE reconcile_deadline END, "
                 "state=CASE WHEN state='manual' THEN state ELSE 'cancelling' END "
                 "WHERE execution_id=? AND state!='terminal'",
                 (reason, now + 30, now + 150, execution_id))
    conn.commit()
    return read(conn, execution_id)


def uncertain(conn, execution_id, detail, *, now=None):
    now = time.time() if now is None else now
    conn.execute("UPDATE hermes_executions SET state=CASE "
                 "WHEN reconcile_deadline<=? OR state='manual' THEN 'manual' ELSE 'unknown' END,detail=? "
                 "WHERE execution_id=? AND state!='terminal'", (now, detail, execution_id))
    conn.commit()


def apply_receipt(conn, execution_id, receipt, *, now=None):
    """Only the matching admission's authoritative terminal receipt releases it."""
    now = time.time() if now is None else now
    conn.execute('BEGIN IMMEDIATE')
    try:
        row = read(conn, execution_id)
        ref = receipt.get('ref') or {}
        aid, status = receipt.get('admission_id'), receipt.get('status')
        if (row is None or ref.get('session_id') != row['session_id']
                or ref.get('profile_id') != row['profile_id'] or not isinstance(aid, str) or not aid
                or row['admission_id'] is not None and aid != row['admission_id']
                or status not in ('queued', 'started', 'unknown', 'terminal')):
            raise ValueError('Receipt identity or state mismatch')
        if row['state'] == 'terminal':
            conn.commit()
            return row
        gen, epoch = receipt.get('execution_generation'), receipt.get('authority_epoch')
        if gen is not None and type(gen) is not int or type(epoch) is not int:
            raise ValueError('Receipt generation or epoch invalid')
        if row['generation'] is not None and gen is not None and gen < row['generation']:
            raise ValueError('Stale receipt generation')
        if row['authority_epoch'] is not None and epoch < row['authority_epoch']:
            raise ValueError('Stale receipt epoch')
        if status == 'terminal':
            if not isinstance(receipt.get('outcome'), str) or not receipt['outcome']:
                raise ValueError('Terminal receipt has no outcome')
            conn.execute("UPDATE hermes_executions SET admission_id=?,generation=?,authority_epoch=?,"
                         "state='terminal',outcome=?,ended_at=?,cleanup_pending=1,cleanup_deadline=?,detail='' "
                         "WHERE execution_id=?",
                         (aid, gen, epoch, receipt['outcome'], now, now + CLEANUP_BUDGET_SECONDS, execution_id))
        else:
            state = 'running' if status == 'started' else 'submitting' if status == 'queued' else 'unknown'
            if row['state'] == 'manual' or status == 'unknown' and now >= row['reconcile_deadline']:
                state = 'manual'
            elif row['cancel_reason']:
                state = 'cancelling'
            conn.execute('UPDATE hermes_executions SET admission_id=?,generation=?,authority_epoch=?,'
                         'state=? WHERE execution_id=?', (aid, gen, epoch, state, execution_id))
        conn.commit()
        return read(conn, execution_id)
    except BaseException:
        conn.rollback()
        raise


def finish_cleanup(conn, execution_id):
    conn.execute("UPDATE hermes_executions SET cleanup_pending=0,retry_count=0,"
                 "next_retry_at=NULL,cleanup_deadline=NULL,detail='' "
                 "WHERE execution_id=? AND state='terminal'", (execution_id,))
    conn.commit()


def register_cleanup_retry(conn, execution_id, detail, *, now=None):
    """登记一次清理失败：retry_count 递增、按退避序列写 next_retry_at、记录原因。

    返回新的 retry_count；retry_count 达到上限即耗尽（由调用方判定 cleanup_failed）。
    计数与 next_retry_at 持久化，重启后只续剩余额度（设计 6.2/6.3）。
    """
    now = time.time() if now is None else now
    row = read(conn, execution_id)
    if row is None or row['state'] != 'terminal':
        return 0
    attempts = (row['retry_count'] or 0) + 1
    conn.execute("UPDATE hermes_executions SET retry_count=?,next_retry_at=?,detail=? "
                 "WHERE execution_id=?", (attempts, now + _cleanup_backoff(attempts - 1), detail, execution_id))
    conn.commit()
    return attempts


def mark_cleanup_failed(conn, execution_id, cause):
    """清理耗尽：保留 cleanup_pending=1 与会话禁用，detail 打 cleanup_failed 标记（设计 6.3）。"""
    row = read(conn, execution_id)
    if row is None or row['state'] != 'terminal':
        return
    conn.execute("UPDATE hermes_executions SET detail=? WHERE execution_id=?",
                 (f'cleanup_failed after {row["retry_count"]} attempts: {cause}', execution_id))
    conn.commit()


def not_submitted(conn, execution_id, detail):
    """Only for a failure before the first prompt.submit was attempted."""
    conn.execute("UPDATE hermes_executions SET state='terminal',outcome='not_submitted',"
                 "ended_at=?,cleanup_pending=1,detail=? WHERE execution_id=? AND admission_id IS NULL",
                 (time.time(), detail, execution_id))
    conn.commit()


def reconcile(client, conn, execution_id, *, now=None):
    """Query before cancelling; transport loss never triggers prompt resubmission.

    单次 RPC 超时 = min(默认 30s, 剩余 reconcile 预算)（设计 6.1 / D3），
    卡住的 RPC 不得越过首次 cancel + 150s 的硬门槛。
    """
    now = time.time() if now is None else now
    row = read(conn, execution_id)
    if row['state'] == 'terminal':
        return row
    if not row['admission_id']:
        uncertain(conn, execution_id, 'Submission receipt unavailable; manual reconciliation required', now=now)
        return read(conn, execution_id)
    remaining = max(0.0, (row['reconcile_deadline'] or 0) - now)
    rpc_timeout = min(30.0, remaining) if remaining > 0 else 0.0
    receipt = client.rpc('prompt.receipt', session_id=row['session_id'], admission_id=row['admission_id'],
                         timeout=rpc_timeout)
    row = apply_receipt(conn, execution_id, receipt, now=now)
    if row['state'] == 'terminal' or not row['cancel_reason']:
        return row
    if receipt['status'] == 'queued':
        reply = client.rpc('prompt.cancel', session_id=row['session_id'], admission_id=row['admission_id'],
                           timeout=rpc_timeout)
        row = apply_receipt(conn, execution_id, reply, now=now)
    elif receipt['status'] == 'started' and row['generation'] is not None:
        client.rpc('session.interrupt', session_id=row['session_id'],
                   execution_generation=row['generation'], timeout=rpc_timeout)
    # An interrupt acknowledgement is a session handle, not an admission receipt.
    return row
