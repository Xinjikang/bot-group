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
    from . import hermes_delegations
    if not hermes_delegations.owned(conn, row):
        raise HTTPException(409, 'Execution no longer owns its task or topic turn')
    if not required and hermes_delegations.waiting(conn, row):
        return None
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
    _signing_generation()
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
    # Pin generation selection and its new reference against rotation/retirement.
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    signing_generation = _signing_generation()
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


def configure(conn, row, client, snapshot, *, force_mcp=False, refresh_model=True):
    from . import hermes_gateway_sessions as sessions, signing_keys, relay, wake
    current = _signing_generation()
    if snapshot['running'] or snapshot['pending']:
        raise HTTPException(409, 'Session credential handoff requires idle session')
    if current != row['signing_generation']:
        conn.execute('UPDATE hermes_session_grants SET pending_generation=? WHERE ' + WHERE, (current, *key(row)))
        conn.commit()
    spec = server(row, current)
    # req#168 随包 MCP 配置：远程节点按节点配置渲染 MCP 启动命令与 env（经 relay
    # 帧下发、插件原样转发到本机 gateway）；configure 前同步问询当前连接取得
    # 「本次配置的远端实际摘要」复核（同连接原地换包、配置早于异步上报也能
    # 取到本次值，manager node1981 ③）——无问询能力（本机连接）走库内摘要；
    # 远端渲染缺 command 拒绝（绝不回落本机配置）；GROUP_ENGINE_URL 以远端渲染
    # 值为准。身份令牌与上下文继续由引擎注入（manager node1977 ③）。
    fetch = getattr(client, 'request_mcp_snapshot', None)
    spec = relay.merge_mcp_spec(conn, row["profile"], spec, engine_url=wake._engine_url(),
                                fetch_report=fetch)
    workdir = row['workdir']
    if not refresh_model and relay.read_node(conn, row['profile']) is not None:
        # A signing-key refresh resumes the existing session, including grants from older engines.
        workdir = snapshot['info'].get('cwd')
    result = sessions.configure_tracked(conn, row['project'], client, snapshot, workdir=workdir,
        model_args=json.loads(row['model_args']), server=spec, force_mcp=force_mcp,
        member=row['profile'], refresh_model=refresh_model)
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


def log_deferred(row, phase, exc):
    """Identify a failed session operation while redacting credential-bearing details."""
    from .execution_errors import redact
    detail = getattr(exc, 'detail', getattr(exc, 'reason', str(exc)))
    detail = redact(detail).replace('\r', '\\r').replace('\n', '\\n')
    LOG.warning('Session resource maintenance deferred: phase=%s profile=%r project=%r '
                'session=%r session_id=%r home=%r signing_generation=%s '
                'error=%s status=%s reason=%s', phase, row['profile'], row['project'],
                row['session'], row['session_id'], row['home'], row['signing_generation'],
                type(exc).__name__, getattr(exc, 'status_code', None), detail)


def maintain():
    """Retry durable revocations and restart handoffs without blocking engine startup."""
    from . import hermes_gateway, hermes_gateway_ledger as ledger, relay, relay_server, signing_keys, wake
    if wake._shutting_down.is_set():
        return
    current = _signing_generation()
    conn = db.get_connection()
    try:
        rows = conn.execute("""
            SELECT g.*, n.member AS relay_member FROM hermes_session_grants g
            LEFT JOIN relay_nodes n ON n.member=g.profile
            WHERE (n.member IS NULL OR n.state='online')
              AND ((g.revoked=1 AND g.detached=0)
                OR (g.revoked=0 AND (g.signing_generation<>? OR g.pending_generation IS NOT NULL)))
        """, (current,)).fetchall()
        for row in rows:
            member = row['relay_member']
            if member is not None and (relay_server.active_link(member) is None or not relay.is_online(conn, member)):
                continue
            guard = lock_for(row['session'])
            if not guard.acquire(blocking=False):
                continue
            client = None
            phase = 'load_grant'
            try:
                row = conn.execute('SELECT * FROM hermes_session_grants WHERE ' + WHERE, key(row)).fetchone()
                if row is None:
                    continue
                if row['revoked']:
                    phase = 'detach'
                    detach(conn, row)
                elif not ledger.active(conn, row['session']) and not ledger.cleanup_pending(conn, row['session']):
                    phase = 'connect'
                    client, _, _ = hermes_gateway.connection(row['profile'], home=row['home'],
                                                            command=json.loads(row['command']), read_only=True)
                    phase = 'resume'
                    snapshot = client.rpc('session.resume', session_id=row['session_id'])
                    phase = 'configure'
                    configure(conn, row, client, snapshot, refresh_model=False)
            except hermes_gateway.RelayOffline:
                # The link can close between the eligibility check and lease acquisition.
                conn.rollback()
            except Exception as exc:
                log_deferred(row, phase, exc)
                conn.rollback()
            finally:
                if client:
                    client.close()
                guard.release()
        signing_keys.retire(conn)
    finally:
        conn.close()
