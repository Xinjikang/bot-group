"""Gateway execution ownership and reconciliation using official Hermes interfaces."""
from __future__ import annotations

import json
from datetime import datetime
import logging
import os
import sqlite3
from pathlib import Path
import threading
import time
import uuid

from . import db, hermes_runtime
from . import hermes_gateway_ledger as ledger, hermes_gateway_sessions as sessions
from . import hermes_prompt_state as prompt_state
from .hermes_gateway_client import GatewayClient, GatewayRefusal, GatewayTransportError, discover, ensure

logger = logging.getLogger(__name__)
_workers = set()
_lock = threading.Lock()


def enabled():
    from .session_adapter import _hermes_home
    return hermes_runtime.gateway_source(hermes_runtime.source_directory(_hermes_home()))


def connection(profile, *, home=None, command=None, read_only=False, on_event=None):
    from . import bot_identity
    from . import hermes_connections
    from .session_adapter import _hermes_home
    home = Path(home) if home else bot_identity.hermes_home(profile, _hermes_home())
    command = command or ([] if read_only else hermes_runtime.command())
    return hermes_connections.acquire(home, command, callback=on_event, business=not read_only), home, command


def _binding(conn, client, bot, project, profile, kind, thread_id, title, workdir, model_args):
    from . import wake
    key = (project, bot['id'], kind, thread_id or 0)
    row = conn.execute('SELECT * FROM session_bindings WHERE project=? AND bot_id=? AND kind=? AND thread_id=?', key).fetchone()
    if row:
        if row['runtime'] != 'hermes':
            raise GatewayRefusal('session_runtime_mismatch')
        if row['switching']:
            raise GatewayRefusal('session_switch_pending')
        snapshot = sessions.attach(client, row['session_id'])
    else:
        found = client.rpc('session.list', title=title)['sessions']
        if len(found) > 1:
            raise GatewayRefusal('ambiguous_session_title')
        if found:
            snapshot = sessions.attach(client, found[0]['session_id'])
        else:
            prompt = wake.wake_system_prompt(kind, project, workdir)
            request_id = uuid.uuid5(uuid.NAMESPACE_URL, repr(key) + ':' + prompt_state.digest(prompt)).hex
            snapshot = sessions.new(client, title=title, workdir=workdir, system_prompt=prompt,
                                    model_args=model_args, request_id=request_id)
            prompt_state.created(conn, client.endpoint['profile_id'], snapshot['session_id'], prompt)
    if snapshot['info']['source'] != 'acp':
        old_sid = snapshot['session_id']
        snapshot = sessions.migrate_legacy(client, snapshot, workdir=workdir)
        conn.execute('INSERT OR IGNORE INTO hermes_session_migrations VALUES(?,?,?,?)',
                     (profile, old_sid, snapshot['session_id'], time.time()))
    conn.execute("INSERT INTO session_bindings(project,bot_id,profile,kind,thread_id,session_id,runtime,generation,title,updated_at) "
        "VALUES(?,?,?,?,?,?,'hermes',0,?,?) ON CONFLICT(project,bot_id,kind,thread_id) DO UPDATE SET "
        "session_id=excluded.session_id,switching=0,updated_at=excluded.updated_at",
        (project, bot['id'], profile, kind, thread_id or 0, snapshot['session_id'], title, db.now_iso()))
    conn.commit()
    return snapshot


def active(session):
    conn = db.get_connection()
    try:
        return bool(ledger.active(conn, session))
    finally:
        conn.close()


def process_state(session):
    """Gateway ownership has no per-execution OS process to terminate."""
    conn = db.get_connection()
    try:
        row = conn.execute('SELECT state,cleanup_pending,started_at FROM hermes_executions '
                           'WHERE session=? ORDER BY started_at DESC LIMIT 1', (session,)).fetchone()
        if row is None:
            return False
        latest = conn.execute('SELECT MAX(started_at) FROM wake_runs WHERE session=?', (session,)).fetchone()[0]
        if latest is not None and latest > row['started_at']:
            return False
        return [] if row['state'] == 'terminal' and not row['cleanup_pending'] else None
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc):
            return False
        raise
    finally:
        conn.close()


def _publish(row):
    from . import wake, events
    run = {key: row[key] for key in ('session', 'project', 'profile', 'kind', 'thread_id', 'parent_id', 'round')}
    run.update(wake_started_at=row['started_at'], spawned_at=datetime.fromtimestamp(row['started_at']).isoformat(),
               channel='hermes-gateway', state=row['state'])
    with wake._session_lock:
        wake._running_sessions.add(row['session'])
        wake._running_wakes[row['session']] = run
        if row['cancel_reason'] == 'user':
            wake._intentional_stops.add(row['session'])
    events.bus.publish('wake.spawned', run)


def spawn(profile, kind, signal, thread_id, project, *, bot, workdir, model_args,
          bound_task=None, job_id=None, trigger_msg_id=None):
    from . import wake, browser_resources
    session = wake.session_name(profile, kind, thread_id, project)
    client = None
    row = None
    attempted = False
    conn = db.get_connection()
    try:
        if ledger.active(conn, session):
            return {'ok': False, 'busy': True, 'detail': 'gateway execution still active'}
        if ledger.cleanup_pending(conn, session):
            return {'ok': False, 'busy': True, 'detail': 'gateway cleanup pending'}
        client, home, command = connection(profile)
        description = client.rpc('runtime.describe')
        if not {'durable-admission-v1', 'acp-session-mcp-v1'} <= set(description['capabilities']):
            raise GatewayRefusal('gateway_capabilities_unavailable')
        snapshot = _binding(conn, client, bot, project, profile, kind, thread_id, session, workdir, model_args)
        if snapshot['running'] or snapshot['pending']:
            return {'ok': False, 'busy': True, 'detail': 'native session has pending execution'}
        if signal is None:
            signal = wake._finalize_injection_signal(profile, kind, thread_id, project,
                                                     trigger_msg_id, bound_task, [])
        elif bound_task is not None:
            signal += wake._TASK_SUFFIX.format(task_id=bound_task)
        current_prompt = wake.wake_system_prompt(kind, project, workdir)
        prompt_hash = prompt_state.pending_hash(conn, description['profile_id'], snapshot['session_id'], current_prompt)
        if prompt_hash is not None:
            signal = current_prompt + '\n\n' + signal
        start = time.time()
        context = dict(profile=profile, project=project, thread_id=thread_id, session=session, wake_started_at=start)
        floor = conn.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
        topic = conn.execute('SELECT turn_parent_id,round FROM threads WHERE id=? AND project=?',
                             (thread_id, project)).fetchone() if thread_id is not None else None
        conn.execute('INSERT INTO wake_runs(session,started_at,project,profile,thread_id,runtime_session_id,bot_id,runtime) '
                     "VALUES(?,?,?,?,?,?,?,'hermes')", (session, start, project, profile, thread_id, snapshot['session_id'], bot['id']))
        row = ledger.prepare(conn, session=session, project=project, profile=profile, kind=kind, thread_id=thread_id,
            task_id=bound_task, job_id=job_id, event_floor=floor, source='', python=json.dumps(command), home=home,
            workdir=workdir, session_id=snapshot['session_id'], profile_id=description['profile_id'], started_at=start,
            parent_id=topic['turn_parent_id'] if topic else None, round_no=topic['round'] if topic else None,
            prompt_hash=prompt_hash)
        _publish(row)
        server = wake._mcp_server_config(profile, context=context, chat=kind == 'chat',
                                       signing_generation=row['signing_generation'])
        resource_env = dict(os.environ)
        browser_resources.register(conn, resource_env, project=project, profile=profile, session=session,
                                   started_at=start, thread_id=thread_id, task_id=bound_task)
        server['env'].update({key: resource_env[key] for key in
                             ('BH_RUNTIME_DIR', 'BH_RUNTIME_DIR_SHARED', 'BH_TMP_DIR', 'BH_TMP_DIR_SHARED', 'BU_NAME')})
        server['env']['GROUP_GATEWAY_EXECUTION'] = '1'
        conn.commit()
        sessions.configure_tracked(conn, project, client, snapshot,
                                   workdir=workdir, model_args=model_args, server=server)
        attempted = True
        receipt = client.rpc('prompt.submit', session_id=row['session_id'], submission_id=row['submission_id'], text=signal)
        ledger.apply_receipt(conn, row['execution_id'], receipt)
        _start_worker(row['execution_id'])
        return {'ok': True, 'channel': 'hermes-gateway', 'detail': 'admission ' + receipt['admission_id']}
    except Exception as exc:
        if row is None:
            raise
        if attempted:
            ledger.uncertain(conn, row['execution_id'], type(exc).__name__)
        else:
            ledger.not_submitted(conn, row['execution_id'], str(exc))
        _start_worker(row['execution_id'])
        return {'ok': True, 'channel': 'hermes-gateway', 'detail': 'execution pending reconciliation'}
    finally:
        conn.close()
        if client:
            client.close()


def _finish(conn, row, client):
    from . import wake, browser_resources, heartbeat, events, steer_ledger
    if client is not None:
        if row['cancel_reason'] == 'user' and row['kind'] == 'work' and row['outcome'] != 'not_submitted':
            snapshot = sessions.attach(client, row['session_id'])
            target = row['rewind_target']
            if target is None:
                target = next((message['row_id'] for message in reversed(snapshot['messages'])
                               if message.get('role') == 'user' and type(message.get('row_id')) is int
                               and isinstance(message.get('timestamp'), (int, float))
                               and message['timestamp'] >= row['started_at']
                               and not message.get('archived')), None)
                if target is not None:
                    conn.execute('UPDATE hermes_executions SET rewind_target=? WHERE execution_id=?',
                                 (target, row['execution_id']))
                    conn.commit()
            if target is not None:
                sessions.mutate(client, snapshot, 'rewind', {'target_message_id': target},
                                request_id='stop-' + row['execution_id'])
                prompt_state.withdraw(conn, row['execution_id'], 'rewound')
                conn.commit()
        sessions.clear_mcp(client, row['session_id'])
    elif row['outcome'] != 'not_submitted':
        return False
    code = 0 if row['outcome'] == 'completed' else 1
    conn.execute('UPDATE wake_runs SET ended_at=?,exit_code=? WHERE session=? AND started_at=?',
                 (row['ended_at'], code, row['session'], row['started_at']))
    conn.commit()
    if row['cancel_reason'] not in ('user', 'shutdown'):
        if code:
            wake._report_execution_error(row['profile'], row['project'], row['thread_id'], row['session'],
                row['execution_id'], 'Hermes gateway execution failed: ' + (row['detail'] or row['outcome']),
                exit_code=code, job_id=row['job_id'])
        wake._handle_task_exit(row['profile'], row['project'], row['kind'], row['session'], row['started_at'],
            row['ended_at'], code, row['event_floor'], task_id=row['task_id'], job_id=row['job_id'],
            error=row['detail'] or row['outcome'])
    browser_resources.finish_run(row['session'], row['started_at'])
    steer_ledger.reconcile_targets(conn)
    ledger.finish_cleanup(conn, row['execution_id'])
    with wake._session_lock:
        wake._running_sessions.discard(row['session'])
        wake._running_wakes.pop(row['session'], None)
        wake._intentional_stops.discard(row['session'])
    events.bus.publish('wake.finished', {'session': row['session'], 'wake_started_at': row['started_at'], 'ended_at': row['ended_at']})
    run = conn.execute('SELECT pid FROM wake_runs WHERE session=? AND started_at=?',
                       (row['session'], row['started_at'])).fetchone()
    heartbeat.on_exit(row['profile'], pid=(run['pid'] or 0) if run else 0, thread_id=row['thread_id'])
    return True


def _watch(execution_id):
    from . import wake
    client = None
    conn = db.get_connection()
    try:
        while True:
            if wake._shutting_down.is_set():
                if client:
                    client.close()
                    client = None
                time.sleep(0.1)
                continue
            row = ledger.read(conn, execution_id)
            try:
                if client is None:
                    client, _, _ = connection(row['profile'], home=row['home'], command=json.loads(row['python']))
                    if wake._shutting_down.is_set():
                        continue
                    sessions.attach(client, row['session_id'])
                if row['state'] == 'terminal':
                    if _finish(conn, row, client):
                        return
                    time.sleep(1)
                    continue
                if row['execution_deadline'] and time.time() >= row['execution_deadline'] and not row['cancel_reason']:
                    ledger.cancel(conn, execution_id, 'timeout')
                row = ledger.reconcile(client, conn, execution_id)
                from . import hermes_approvals
                hermes_approvals.sync(conn, row, client)
                from . import hermes_gateway_steer
                hermes_gateway_steer.observe_session(conn, client, row['session_id'], row['started_at'])
                from . import wake
                with wake._session_lock:
                    if row['session'] in wake._running_wakes:
                        wake._running_wakes[row['session']]['state'] = row['state']
                if row['state'] == 'terminal':
                    continue
                if row['state'] == 'manual':
                    logger.error('Hermes execution %s entered manual; automatic reconciliation stopped', execution_id)
                    return
            except Exception as exc:
                from . import wake
                if wake._shutting_down.is_set():
                    # Keep the durable state and pause until shutdown completes or reopens.
                    if client:
                        client.close()
                        client = None
                    continue
                detail = exc.reason if isinstance(exc, GatewayRefusal) else type(exc).__name__
                if row['state'] == 'terminal':
                    # Persisted cleanup deadlines bound retries across engine restarts.
                    if row['cleanup_deadline'] and time.time() >= row['cleanup_deadline']:
                        ledger.mark_cleanup_failed(conn, execution_id, detail + '; cleanup deadline exceeded')
                        logger.error('Hermes cleanup deadline exceeded for execution %s; '
                                     'manual retry required: %s', execution_id, detail)
                        return
                    attempts = ledger.register_cleanup_retry(conn, execution_id, detail)
                    if attempts >= ledger.CLEANUP_MAX_ATTEMPTS:
                        ledger.mark_cleanup_failed(conn, execution_id, detail)
                        logger.error('Hermes cleanup failed for execution %s after %d attempts; '
                                     'manual retry required: %s', execution_id, attempts, detail)
                        return
                    delay = (ledger.read(conn, execution_id)['next_retry_at'] or 0) - time.time()
                    if client:
                        client.close()
                        client = None
                    time.sleep(delay if delay > 0 else 0)
                    continue
                ledger.uncertain(conn, execution_id, detail)
                if client:
                    client.close()
                    client = None
            time.sleep(3)
    finally:
        if client:
            client.close()
        conn.close()
        with _lock:
            _workers.discard(execution_id)


def _start_worker(execution_id):
    with _lock:
        if execution_id in _workers:
            return
        _workers.add(execution_id)
    threading.Thread(target=_watch, args=(execution_id,), daemon=True, name='hermes-reconcile').start()


def recover():
    conn = db.get_connection()
    try:
        for row in ledger.active(conn):
            _publish(row)
            _start_worker(row['execution_id'])
        # terminal 后未清理的行重启清理 worker（不 _publish：已终态不再发 spawned）。
        # 已耗尽（cleanup_failed）的行不自动重试，只续剩余额度（设计 6.3 / D2）。
        for row in ledger.cleanup_pending(conn):
            # Expired cleanup work remains failed until an explicit retry.
            if row['cleanup_deadline'] and time.time() >= row['cleanup_deadline']:
                ledger.mark_cleanup_failed(conn, row['execution_id'], 'cleanup deadline exceeded')
                continue
            if (row['retry_count'] or 0) < ledger.CLEANUP_MAX_ATTEMPTS:
                _start_worker(row['execution_id'])
    finally:
        conn.close()


def managed_runs() -> set[tuple[str, float]]:
    """Exact execution identities, including durable terminal cleanup work."""
    conn = db.get_connection()
    try:
        return {(row['session'], row['started_at']) for row in conn.execute(
            'SELECT session,started_at FROM hermes_executions')}
    finally:
        conn.close()


def close_connections(timeout: float = 5.0) -> None:
    """引擎重启本地收尾：集中、有界地关闭所有 viewer 连接（不取消远端执行、不终止 bot）。"""
    from .hermes_gateway_client import close_all
    close_all(timeout)


def cancel(session, *, reason='user', timeout=30):
    state = process_state(session)
    if state is False:
        return None
    if state == []:
        return True
    conn = db.get_connection()
    try:
        rows = ledger.active(conn, session)
        if not rows:
            # 无执行占用：terminal 后可能只剩清理待办（会话禁用但不算 active）。
            pending = ledger.cleanup_pending(conn, session)
            if not pending:
                return None
            for row in pending:
                if (row['retry_count'] or 0) < ledger.CLEANUP_MAX_ATTEMPTS:
                    _start_worker(row['execution_id'])
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not ledger.cleanup_pending(conn, session):
                    return True
                time.sleep(0.1)
            return False
        for row in rows:
            ledger.cancel(conn, row['execution_id'], reason)
            _start_worker(row['execution_id'])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not ledger.active(conn, session):
                return True
            time.sleep(0.1)
        return False
    finally:
        conn.close()
