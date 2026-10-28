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
    rewind_target INTEGER,
    detail TEXT NOT NULL DEFAULT '',
    UNIQUE(session, started_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS hermes_execution_active
ON hermes_executions(session) WHERE state != 'terminal' OR cleanup_pending=1;
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


def active(conn, session=None):
    query = "SELECT * FROM hermes_executions WHERE (state!='terminal' OR cleanup_pending=1)"
    return conn.execute(query + (' AND session=?' if session else ''), (session,) if session else ()).fetchall()


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
                         "state='terminal',outcome=?,ended_at=?,cleanup_pending=1,detail='' WHERE execution_id=?",
                         (aid, gen, epoch, receipt['outcome'], now, execution_id))
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
    conn.execute("UPDATE hermes_executions SET cleanup_pending=0,detail='' WHERE execution_id=? AND state='terminal'",
                 (execution_id,))
    conn.commit()


def not_submitted(conn, execution_id, detail):
    """Only for a failure before the first prompt.submit was attempted."""
    conn.execute("UPDATE hermes_executions SET state='terminal',outcome='not_submitted',"
                 "ended_at=?,cleanup_pending=1,detail=? WHERE execution_id=? AND admission_id IS NULL",
                 (time.time(), detail, execution_id))
    conn.commit()


def reconcile(client, conn, execution_id, *, now=None):
    """Query before cancelling; transport loss never triggers prompt resubmission."""
    row = read(conn, execution_id)
    if row['state'] == 'terminal':
        return row
    if not row['admission_id']:
        uncertain(conn, execution_id, 'Submission receipt unavailable; manual reconciliation required', now=now)
        return read(conn, execution_id)
    receipt = client.rpc('prompt.receipt', session_id=row['session_id'], admission_id=row['admission_id'])
    row = apply_receipt(conn, execution_id, receipt, now=now)
    if row['state'] == 'terminal' or not row['cancel_reason']:
        return row
    if receipt['status'] == 'queued':
        reply = client.rpc('prompt.cancel', session_id=row['session_id'], admission_id=row['admission_id'])
        row = apply_receipt(conn, execution_id, reply, now=now)
    elif receipt['status'] == 'started' and row['generation'] is not None:
        client.rpc('session.interrupt', session_id=row['session_id'], execution_generation=row['generation'])
    # An interrupt acknowledgement is a session handle, not an admission receipt.
    return row
