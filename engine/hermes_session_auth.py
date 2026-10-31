"""Durable gateway session grants, idle credential handoff and revocation outbox."""
from __future__ import annotations

import hashlib
import json
import logging
import threading

from fastapi import HTTPException

from . import auth, db

_LOCKS = [threading.RLock() for _ in range(64)]
LOG = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS hermes_session_grants (
    project TEXT NOT NULL, bot_id INTEGER NOT NULL, profile TEXT NOT NULL,
    kind TEXT NOT NULL, thread_id INTEGER NOT NULL, session TEXT NOT NULL,
    session_id TEXT NOT NULL, generation INTEGER NOT NULL,
    signing_generation INTEGER NOT NULL, pending_generation INTEGER,
    revoked INTEGER NOT NULL DEFAULT 0, detached INTEGER NOT NULL DEFAULT 0,
    home TEXT NOT NULL, command TEXT NOT NULL, workdir TEXT NOT NULL,
    model_args TEXT NOT NULL,
    PRIMARY KEY(project, bot_id, kind, thread_id, generation)
);
CREATE TRIGGER IF NOT EXISTS hermes_grant_binding_update AFTER UPDATE ON session_bindings
WHEN OLD.session_id<>NEW.session_id OR OLD.generation<>NEW.generation OR OLD.runtime<>NEW.runtime
BEGIN
    UPDATE hermes_session_grants SET revoked=1 WHERE project=OLD.project AND bot_id=OLD.bot_id
    AND kind=OLD.kind AND thread_id=OLD.thread_id AND generation=OLD.generation;
END;
CREATE TRIGGER IF NOT EXISTS hermes_grant_binding_delete AFTER DELETE ON session_bindings
BEGIN
    UPDATE hermes_session_grants SET revoked=1 WHERE project=OLD.project AND bot_id=OLD.bot_id
    AND kind=OLD.kind AND thread_id=OLD.thread_id;
END;
CREATE TRIGGER IF NOT EXISTS hermes_grant_member_delete AFTER DELETE ON project_members
BEGIN
    UPDATE hermes_session_grants SET revoked=1 WHERE project=OLD.project AND profile=OLD.profile;
END;
CREATE TRIGGER IF NOT EXISTS hermes_grant_member_update AFTER UPDATE ON project_members
WHEN NEW.active=0 OR OLD.role_id IS NOT NEW.role_id
BEGIN
    UPDATE hermes_session_grants SET revoked=1 WHERE project=OLD.project AND profile=OLD.profile;
END;
CREATE TRIGGER IF NOT EXISTS hermes_grant_project_update AFTER UPDATE ON projects
WHEN NEW.archived<>0 OR NEW.removed<>0
BEGIN
    UPDATE hermes_session_grants SET revoked=1 WHERE project=OLD.name;
END;
CREATE TRIGGER IF NOT EXISTS hermes_grant_thread_update AFTER UPDATE ON threads
BEGIN
    UPDATE hermes_session_grants SET revoked=1 WHERE thread_id=OLD.id AND kind='chat'
    AND (NEW.status='closed' OR NOT EXISTS (SELECT 1 FROM json_each(NEW.members) WHERE value=profile));
END;
CREATE TRIGGER IF NOT EXISTS hermes_grant_thread_delete AFTER DELETE ON threads
BEGIN
    UPDATE hermes_session_grants SET revoked=1 WHERE thread_id=OLD.id AND kind='chat';
END;
"""


def lock_for(session):
    return _LOCKS[int.from_bytes(hashlib.sha256(session.encode()).digest()[:2], 'big') % len(_LOCKS)]


def key(row):
    return tuple(row[k] for k in ('project', 'bot_id', 'kind', 'thread_id', 'generation'))


WHERE = 'project=? AND bot_id=? AND kind=? AND thread_id=? AND generation=?'


def context(row):
    return {k: (row[k] or None) if k == 'thread_id' else row[k] for k in auth.SESSION_FIELDS}


def validate(conn, scope):
    """Validate the signed binding and current membership before any business check."""
    from .access import require_project, require_thread
    row = conn.execute(
        'SELECT g.* FROM hermes_session_grants g JOIN session_bindings b '
        'ON b.project=g.project AND b.bot_id=g.bot_id AND b.kind=g.kind AND b.thread_id=g.thread_id '
        'AND b.session_id=g.session_id AND b.generation=g.generation '
        "WHERE g.project=? AND g.profile=? AND g.session_id=? AND g.generation=? AND g.revoked=0 "
        "AND b.runtime='hermes' AND b.switching=0",
        (scope['project'], scope['profile'], scope['session_id'], scope['generation'])).fetchall()
    row = next((r for r in row if context(r) == {k: scope[k] for k in auth.SESSION_FIELDS}), None)
    if row is None or scope['signing_generation'] not in (row['signing_generation'], row['pending_generation']):
        raise HTTPException(401, 'Session credential revoked or superseded')
    require_project(conn, scope['profile'], scope['project'])
    if scope['thread_id'] is not None:
        require_thread(conn, scope['profile'], scope['thread_id'])
        if conn.execute('SELECT status FROM threads WHERE id=?', (scope['thread_id'],)).fetchone()[0] == 'closed':
            raise HTTPException(401, 'Session topic closed')
    return row


def execution(conn, scope, *, required=True):
    validate(conn, scope)
    rows = conn.execute("SELECT * FROM hermes_executions WHERE session=? AND session_id=? AND state<>'terminal'",
                        (scope['session'], scope['session_id'])).fetchall()
    if not rows and not required:
        return None
    if len(rows) != 1 or rows[0]['cancel_reason'] or rows[0]['state'] == 'manual':
        raise HTTPException(409, 'Session has no writable active execution')
    row = rows[0]
    if any(row[k] != scope[k] for k in ('profile', 'project', 'thread_id')):
        raise HTTPException(401, 'Session execution identity mismatch')
    return row


def resolve(conn, scope):
    row = execution(conn, scope)
    return {**scope, 'wake_started_at': row['started_at']}


def _signing_generation():
    from .signing_keys import RecoveryError
    generation = auth.signing_keys()[0]
    if type(generation) is not int or generation <= 0:
        raise RecoveryError('Persistent session credentials require initialized signing keys')
    return generation


def grant(conn, *, project, profile, bot_id, kind, thread_id, session, home, command, workdir, model_args):
    """Create a fresh binding generation after revocation; retain old rows for detach."""
    signing_generation = _signing_generation()
    binding = conn.execute('SELECT * FROM session_bindings WHERE project=? AND bot_id=? AND kind=? AND thread_id=?',
                           (project, bot_id, kind, thread_id or 0)).fetchone()
    old = conn.execute('SELECT * FROM hermes_session_grants WHERE ' + WHERE, key(binding)).fetchone()
    if old and old['revoked']:
        # Detach the old subscription before a replacement grant uses the same native session.
        detach(conn, old)
        generation = conn.execute('SELECT MAX(generation)+1 FROM hermes_session_grants WHERE project=? '
                                  'AND bot_id=? AND kind=? AND thread_id=?',
                                  (project, bot_id, kind, thread_id or 0)).fetchone()[0]
        conn.execute('UPDATE session_bindings SET generation=? WHERE project=? AND bot_id=? '
                     'AND kind=? AND thread_id=?', (generation, project, bot_id, kind, thread_id or 0))
        binding = conn.execute('SELECT * FROM session_bindings WHERE project=? AND bot_id=? AND kind=? AND thread_id=?',
                               (project, bot_id, kind, thread_id or 0)).fetchone()
    conn.execute('INSERT INTO hermes_session_grants(project,bot_id,profile,kind,thread_id,session,session_id,generation,'
                 'signing_generation,home,command,workdir,model_args) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) '
                 'ON CONFLICT(project,bot_id,kind,thread_id,generation) DO UPDATE SET '
                 'home=excluded.home,command=excluded.command,workdir=excluded.workdir,model_args=excluded.model_args',
                 (project, bot_id, profile, kind, thread_id or 0, session, binding['session_id'], binding['generation'],
                  signing_generation, str(home), json.dumps(command), workdir, json.dumps(model_args)))
    conn.commit()
    return conn.execute('SELECT * FROM hermes_session_grants WHERE ' + WHERE, key(binding)).fetchone()


def server(row, generation):
    from . import wake, browser_resources
    from .message_execution import CONTEXT_ENV
    ctx = context(row)
    spec = wake._mcp_server_config(row['profile'], chat=row['kind'] == 'chat')
    spec['env'].update(GROUP_ENGINE_TOKEN=auth.session_token(row['profile'], ctx, signing_generation=generation),
                       GROUP_GATEWAY_EXECUTION='1')
    spec['env'][CONTEXT_ENV] = json.dumps({**ctx, 'credential_type': 'session'})
    spec['env'].update(browser_resources.session_environment(ctx))
    return spec


def configure(conn, row, client, snapshot):
    from . import hermes_gateway_sessions as sessions, signing_keys
    current = _signing_generation()
    if snapshot['running'] or snapshot['pending']:
        raise HTTPException(409, 'Session credential handoff requires idle session')
    if current != row['signing_generation']:
        conn.execute('UPDATE hermes_session_grants SET pending_generation=? WHERE ' + WHERE, (current, *key(row)))
        conn.commit()
    result = sessions.configure_tracked(conn, row['project'], client, snapshot, workdir=row['workdir'],
        model_args=json.loads(row['model_args']), server=server(row, current))
    validate(conn, {**context(row), 'signing_generation': current})
    conn.execute('UPDATE hermes_session_grants SET signing_generation=?,pending_generation=NULL WHERE ' + WHERE,
                 (current, *key(row)))
    conn.commit()
    signing_keys.retire(conn)
    return result


def detach(conn, row):
    if row['detached']:
        return
    from . import hermes_gateway
    client = None
    try:
        client, _, _ = hermes_gateway.connection(row['profile'], home=row['home'],
                                                command=json.loads(row['command']), read_only=True)
        snapshot = client.rpc('session.resume', session_id=row['session_id'])
        client.rpc('session.detach', session_id=row['session_id'], subscription_id=snapshot['subscription_id'])
        conn.execute('UPDATE hermes_session_grants SET detached=1 WHERE ' + WHERE, key(row))
        conn.commit()
    except hermes_gateway.GatewayRefusal as exc:
        if exc.reason != 'not_found':
            raise
        conn.execute('UPDATE hermes_session_grants SET detached=1 WHERE ' + WHERE, key(row))
        conn.commit()
    finally:
        if client:
            client.close()


def maintain():
    """Retry durable revocations and restart handoffs without blocking engine startup."""
    from . import hermes_gateway, hermes_gateway_ledger as ledger, signing_keys, wake
    if wake._shutting_down.is_set():
        return
    current = _signing_generation()
    conn = db.get_connection()
    try:
        rows = conn.execute('SELECT * FROM hermes_session_grants WHERE (revoked=1 AND detached=0) OR '
                            '(revoked=0 AND (signing_generation<>? OR pending_generation IS NOT NULL))', (current,)).fetchall()
        for row in rows:
            guard = lock_for(row['session'])
            if not guard.acquire(blocking=False):
                continue
            client = None
            try:
                row = conn.execute('SELECT * FROM hermes_session_grants WHERE ' + WHERE, key(row)).fetchone()
                if row['revoked']:
                    detach(conn, row)
                elif not ledger.active(conn, row['session']) and not ledger.cleanup_pending(conn, row['session']):
                    client, _, _ = hermes_gateway.connection(row['profile'], home=row['home'],
                                                            command=json.loads(row['command']), read_only=True)
                    snapshot = client.rpc('session.resume', session_id=row['session_id'])
                    configure(conn, row, client, snapshot)
            except Exception as exc:
                LOG.warning('Session resource maintenance deferred: %s', type(exc).__name__)
                conn.rollback()
            finally:
                if client:
                    client.close()
                guard.release()
        signing_keys.retire(conn)
    finally:
        conn.close()
