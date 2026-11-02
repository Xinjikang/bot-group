"""Validate bot sends and persist their execution provenance at the write boundary."""
import math
import time

from fastapi import HTTPException

CONTEXT_ENV = "XDG_GROUP_ENGINE_EXECUTION"


def validate_shape(sender, body):
    required = ('project', 'thread_id', 'text', 'need_reply', 'execution')
    missing = [key for key in required if key not in body]
    if missing:
        raise HTTPException(400, 'Missing bot message fields: ' + ', '.join(missing))
    if not isinstance(body['project'], str) or not body['project'].strip():
        raise HTTPException(400, 'project must be a non-empty string')
    tid = body['thread_id']
    if tid is not None and (type(tid) is not int or tid <= 0):
        raise HTTPException(400, 'thread_id must be a positive integer or null')
    reply = body['need_reply']
    if reply is not None and (type(reply) not in (bool, int) or reply not in (0, 1)):
        raise HTTPException(400, 'need_reply must be true, false, 0, 1 or null')
    if body.get('sender', sender) != sender:
        raise HTTPException(403, 'sender does not match the bot identity')
    execution = body['execution']
    if not isinstance(execution, dict):
        raise HTTPException(400, 'execution must contain session and wake_started_at')
    session, start = execution.get('session'), execution.get('wake_started_at')
    if not isinstance(session, str) or not session or type(start) not in (int, float) or not math.isfinite(start):
        raise HTTPException(400, 'Invalid execution session or wake_started_at')
    return session, start


def validate(conn, sender, body):
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    supplied = body.get('execution') or {}
    if supplied.get('credential_type') == 'session':
        from . import hermes_session_auth
        resolved = hermes_session_auth.resolve(conn, supplied)
        if supplied.get('wake_started_at', resolved['wake_started_at']) != resolved['wake_started_at']:
            raise HTTPException(409, 'Session execution changed before write')
        body = {**body, 'execution': resolved}
    session, start = validate_shape(sender, body)
    tid = body['thread_id']
    run = conn.execute('SELECT * FROM wake_runs WHERE session=? AND started_at=?', (session, start)).fetchone()
    if run is None or run['ended_at'] is not None:
        raise HTTPException(409, 'Execution is missing or has ended; use the current execution context')
    if (run['profile'], run['project'], run['thread_id']) != (sender, body['project'], tid):
        raise HTTPException(403, 'Execution sender, project or thread does not match the message')
    if not conn.execute(
        'SELECT 1 FROM project_members pm JOIN projects p ON p.name=pm.project '
        'WHERE pm.project=? AND pm.profile=? AND pm.active=1 AND p.archived=0',
        (body['project'], sender)).fetchone():
        raise HTTPException(403, 'Bot is not an active member of this project')
    if conn.execute('SELECT 1 FROM wake_runs WHERE session=? AND started_at>? LIMIT 1', (session, start)).fetchone():
        raise HTTPException(409, 'Execution has been superseded')
    if tid is not None:
        thread = conn.execute('SELECT project FROM threads WHERE id=?', (tid,)).fetchone()
        if thread is None or thread['project'] != body['project']:
            raise HTTPException(403, 'Thread does not belong to the execution project')
    return {'session': session, 'wake_started_at': start}


def record(conn, message_id, execution):
    if execution is not None and message_id is not None:
        conn.execute('INSERT INTO message_executions(message_id,session,wake_started_at,sent_at) VALUES(?,?,?,?)',
                     (message_id, execution['session'], execution['wake_started_at'], time.time()))


def replies(conn, session, start):
    return [dict(row) for row in conn.execute(
        'SELECT message_id,sent_at FROM message_executions WHERE session=? AND wake_started_at=? ORDER BY sent_at,message_id',
        (session, start))]
