"""Gateway execution ownership and reconciliation using official Hermes interfaces."""
from __future__ import annotations

import json
from datetime import datetime
import logging
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


def connection(profile, *, home=None, command=None, read_only=False, on_event=None, history=False):
    from . import bot_identity
    from . import hermes_connections
    from .session_adapter import _hermes_home
    home = Path(home) if home else bot_identity.hermes_home(profile, _hermes_home())
    if history:
        # Transcript reads own their socket and cannot interrupt admission RPCs.
        client = hermes_connections.open_connection(home, [], on_event, allow_start=False,
            revision=hermes_connections.installed_revision(), max_message_bytes=64 * 1024 * 1024)
        return client, home, []
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


def process_state(session, profile=None):
    """Gateway ownership has no per-execution OS process to terminate.

    profile 传入时（调度路径），引擎记录判空闲的两条路径（无记录继续进程查询 /
    terminal 清空判空闲）均先核对 Hermes 原生 running/pending——异步子任务回调驱动的
    自发续跑没有 hermes_executions 记录，仅按引擎记录会漏判 busy，投递 attach 才发现
    原生占用导致任务反复退回、后方会话饥饿（req#142）。
    """
    conn = db.get_connection()
    try:
        row = conn.execute('SELECT state,cleanup_pending,started_at FROM hermes_executions '
                           'WHERE session=? ORDER BY started_at DESC LIMIT 1', (session,)).fetchone()
        if row is None:
            idle = False
        else:
            latest = conn.execute('SELECT MAX(started_at) FROM wake_runs WHERE session=?', (session,)).fetchone()[0]
            if latest is not None and latest > row['started_at']:
                idle = False
            elif row['state'] == 'terminal' and not row['cleanup_pending']:
                idle = []
            else:
                return None
        if profile is not None:
            native = _native_session_state(conn, profile, session)
            if native is not False:
                return native
        return idle
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc):
            return False
        raise
    finally:
        conn.close()


# 原生状态预检查的查询超时与缓存边界（req#142）：短 TTL 按原生会话 + 绑定代次隔离，
# /new 换 session_id、绑定切换升 generation 均自然失效；空闲缓存期间的竞态由
# spawn 阶段 attach 复核兜底。查询失败按指数退避缓存「暂缓」，避免网关不可用时
# 每次队列 tick 都吃满查询超时拖垮其他 runtime 的调度。
_NATIVE_QUERY_TIMEOUT = 5.0        # 秒：单次原生状态查询超时（超时/断连只暂缓目标会话）
_NATIVE_CACHE_TTL = 3.0            # 秒：idle/busy 结果缓存有效期
_NATIVE_FAIL_BACKOFF_BASE = 3.0    # 秒：查询失败退避基数（3,6,12,24…）
_NATIVE_FAIL_BACKOFF_CAP = 30.0    # 秒：查询失败退避上限（网关恢复后至多延迟 30s 恢复调度）
_native_cache = {}                 # {(session_id, generation): (state, expires_at, fails)}
_native_cache_lock = threading.Lock()


def _binding_for_engine_session(conn, profile, session):
    """按引擎会话名反查 Hermes 绑定行（profile 过滤，行数有限）。

    引擎会话名由 (profile, kind, thread_id, project) 派生，绑定行携带同一组字段，
    重建会话名逐一比对即可，无需解析字符串。
    """
    from . import wake
    rows = conn.execute("SELECT * FROM session_bindings WHERE profile=? AND runtime='hermes'",
                        (profile,)).fetchall()
    for row in rows:
        if wake.session_name(row['profile'], row['kind'], row['thread_id'] or None, row['project']) == session:
            return row
    return None


def _native_session_state(conn, profile, session):
    """Hermes 原生 running/pending 预检查（req#142）。

    返回 False=原生空闲（沿用既有判空闲路径）；None=原生忙或查询失败/未知（只暂缓
    本会话，调用方按 busy 跳过本次调度；其他会话不受影响）。非 Hermes 托管会话
    无绑定行，返回 False 沿用既有进程查询。

    只读观察租约在 finally 释放（Pool.sweep 依赖 entry.leases 空集做空闲回收），
    共享物理连接保持池契约；观察走底层客户端 RPC（Lease.rpc 不带超时参数），
    超时/断连只暂缓本会话。
    """
    binding = _binding_for_engine_session(conn, profile, session)
    if binding is None:
        return False
    if binding['switching']:
        return None  # /new 切换进行中：暂缓投递
    key = (binding['session_id'], binding['generation'])
    with _native_cache_lock:
        hit = _native_cache.get(key)
    if hit is not None and hit[1] > time.monotonic():
        return False if hit[0] == 'idle' else None
    lease = None
    try:
        lease, _, _ = connection(profile, read_only=True)
        snapshot = lease.client.rpc('session.resume', session_id=binding['session_id'],
                                    timeout=_NATIVE_QUERY_TIMEOUT)
        running = snapshot.get('running')
        pending = snapshot.get('pending')
        if running is None and pending is None:
            return _native_cache_failure(key, 'native session state unknown')
        state = 'busy' if (running or pending) else 'idle'
    except (GatewayTransportError, GatewayRefusal) as exc:
        # 超时/断连/会话不存在：只暂缓本会话，不按空闲放行
        return _native_cache_failure(key, str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception('native session state query failed for %s', session)
        return _native_cache_failure(key, str(exc))
    finally:
        if lease is not None:
            lease.close()
    with _native_cache_lock:
        _native_cache[key] = (state, time.monotonic() + _NATIVE_CACHE_TTL, 0)
    return False if state == 'idle' else None


def _native_cache_failure(key, detail):
    """查询失败：退避从失败完成时起算（查询超时可能长于首档退避），成功即清零。"""
    now = time.monotonic()
    with _native_cache_lock:
        fails = (_native_cache[key][2] if key in _native_cache else 0) + 1
        ttl = min(_NATIVE_FAIL_BACKOFF_BASE * (2 ** (fails - 1)), _NATIVE_FAIL_BACKOFF_CAP)
        _native_cache[key] = ('busy', now + ttl, fails)
    return None


def _native_busy_feedback(conn, session_id):
    """attach 复核发现原生 running/pending：回写观察缓存，下一轮预检查直接判忙。

    预检查缓存仍持空闲时 spawn attach 才会发现原生忙（空闲窗口内新执行启动的竞态）；
    回写使退回后同一 TTL 窗口内的调度立即跳过本会话，不再反复占队首（req#142）。
    """
    rows = conn.execute('SELECT session_id,generation FROM session_bindings WHERE session_id=?',
                        (session_id,)).fetchall()
    if not rows:
        return
    now = time.monotonic()
    with _native_cache_lock:
        for row in rows:
            _native_cache[(row['session_id'], row['generation'])] = ('busy', now + _NATIVE_CACHE_TTL, 0)


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
    from . import wake, browser_resources, hermes_session_auth
    session = wake.session_name(profile, kind, thread_id, project)
    client = None
    row = None
    attempted = False
    conn = db.get_connection()
    session_lock = hermes_session_auth.lock_for(session)
    session_lock.acquire()
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
            # attach 复核发现原生忙：回写观察缓存，退回后同一 TTL 窗口内不再反复占队首
            _native_busy_feedback(conn, snapshot['session_id'])
            return {'ok': False, 'busy': True, 'detail': 'native session has pending execution'}
        grant = hermes_session_auth.grant(conn, project=project, profile=profile, bot_id=bot['id'],
            kind=kind, thread_id=thread_id, session=session, home=home, command=command,
            workdir=workdir, model_args=model_args)
        try:
            snapshot = hermes_session_auth.configure(conn, grant, client, snapshot)
        except Exception:
            pending = conn.execute('SELECT pending_generation FROM hermes_session_grants WHERE ' +
                                   hermes_session_auth.WHERE, hermes_session_auth.key(grant)).fetchone()
            if pending and pending['pending_generation'] is not None:
                return {'ok': False, 'busy': True, 'detail': 'gateway session credential handoff pending'}
            raise
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
        browser_resources.register(conn, {}, project=project, profile=profile, session=session,
                                   started_at=start, thread_id=thread_id, task_id=bound_task,
                                   session_context=hermes_session_auth.context(grant))
        conn.commit()
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
        session_lock.release()


def _finish(conn, row, client):
    from . import wake, browser_resources, heartbeat, events, steer_ledger
    cleanup_detail = ''
    if client is not None:
        if row['cancel_reason'] == 'user' and row['kind'] == 'work' and row['outcome'] != 'not_submitted':
            snapshot = sessions.attach(client, row['session_id'])
            if snapshot.get('running') or snapshot.get('pending'):
                raise GatewayRefusal('session_busy')
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
                try:
                    sessions.mutate(client, snapshot, 'rewind', {'target_message_id': target},
                                    request_id='stop-' + row['execution_id'])
                except GatewayRefusal as exc:
                    if not exc.compacted_target:
                        raise
                    # Compaction retained the turn in its summary. Keep both the
                    # transcript and its accepted rule hash when rewind is refused.
                    cleanup_detail = 'rewind_target_compacted; transcript preserved'
                else:
                    prompt_state.withdraw(conn, row['execution_id'], 'rewound')
                    conn.commit()
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
    ledger.finish_cleanup(conn, row['execution_id'], detail=cleanup_detail)
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
            if row['state'] == 'terminal':
                if not row['cleanup_pending'] or (row['retry_count'] or 0) >= ledger.CLEANUP_MAX_ATTEMPTS:
                    return
                now = time.time()
                if row['cleanup_deadline'] and now >= row['cleanup_deadline']:
                    ledger.mark_cleanup_failed(conn, execution_id, 'cleanup deadline exceeded')
                    return
                delay = (row['next_retry_at'] or 0) - now
                if delay > 0:
                    time.sleep(min(delay, max(0, row['cleanup_deadline'] - now))
                               if row['cleanup_deadline'] else delay)
                    if wake._shutting_down.is_set() or (row['cleanup_deadline']
                            and time.time() >= row['cleanup_deadline']):
                        continue
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
                    if detail == 'session_busy' and row['cleanup_deadline']:
                        # Native cancellation may acknowledge the admission before
                        # its runner becomes idle. The absolute deadline bounds polling.
                        conn.execute('UPDATE hermes_executions SET next_retry_at=?,detail=? WHERE execution_id=?',
                                     (min(time.time() + 3, row['cleanup_deadline']), 'waiting_for_idle', execution_id))
                        conn.commit()
                        continue
                    attempts = ledger.register_cleanup_retry(conn, execution_id, detail)
                    if attempts >= ledger.CLEANUP_MAX_ATTEMPTS:
                        ledger.mark_cleanup_failed(conn, execution_id, detail)
                        logger.error('Hermes cleanup failed for execution %s after %d attempts; '
                                     'manual retry required: %s', execution_id, attempts, detail)
                        return
                    if client:
                        client.close()
                        client = None
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


def _start_worker(execution_id, *, retry_cleanup=False):
    with _lock:
        if execution_id in _workers:
            return
        if retry_cleanup:
            conn = db.get_connection()
            try:
                ledger.retry_cleanup(conn, execution_id)
            finally:
                conn.close()
        _workers.add(execution_id)
    threading.Thread(target=_watch, args=(execution_id,), daemon=True, name='hermes-reconcile').start()


def recover():
    from . import hermes_connections
    hermes_connections._POOL.start()
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
                exhausted = ((row['retry_count'] or 0) >= ledger.CLEANUP_MAX_ATTEMPTS
                             or row['cleanup_deadline'] and time.time() >= row['cleanup_deadline'])
                if exhausted and reason == 'user':
                    _start_worker(row['execution_id'], retry_cleanup=True)
                elif not exhausted:
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
