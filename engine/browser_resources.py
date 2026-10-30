"""Release only browser-harness instances in engine-issued execution namespaces."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import socket
import sqlite3
import time

from . import db

logger = logging.getLogger(__name__)
TIMEOUT = 10.0


def directory(resource_id):
    if not re.fullmatch(r'[a-f0-9]{32}', resource_id):
        raise ValueError('invalid browser resource id')
    return Path(db.db_path()).resolve().parent / 'cache' / 'browser-runtime' / resource_id


def session_environment(context):
    identity = [context[k] for k in ('profile', 'project', 'session_id', 'generation')]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]
    root = directory(key)
    return dict(BH_RUNTIME_DIR=str(root), BH_RUNTIME_DIR_SHARED='1',
                BH_TMP_DIR=str(root / 'artifacts'), BH_TMP_DIR_SHARED='1', BU_NAME=f'bg-{key}')


def register(conn, env, *, project, profile, session, started_at, thread_id, task_id, session_context=None):
    key = (session_environment(session_context)['BU_NAME'][3:] if session_context else
           hashlib.sha256(f'{session}\0{started_at!r}'.encode()).hexdigest()[:32])
    root = directory(key)
    root.mkdir(parents=True, exist_ok=True)
    conn.execute('INSERT INTO browser_resources(id,project,profile,session,started_at,thread_id,task_id) '
                 'VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET started_at=excluded.started_at, '
                 'task_id=excluded.task_id,release_requested=0',
                 (key, project, profile, session, started_at, thread_id, task_id))
    env.update(BH_RUNTIME_DIR=str(root), BH_RUNTIME_DIR_SHARED='1',
               BH_TMP_DIR=str(root / 'artifacts'), BH_TMP_DIR_SHARED='1', BU_NAME=f'bg-{key}')


def bind_task(conn, project, profile, task_id):
    # A work invocation can claim its first/next task after spawn. Chat scopes
    # and older invocations must retain their own attribution.
    conn.execute('UPDATE browser_resources SET task_id=?,release_requested=0 WHERE project=? '
                 'AND profile=? AND thread_id IS NULL AND EXISTS '
                 '(SELECT 1 FROM wake_runs w WHERE w.session=browser_resources.session '
                 'AND w.started_at=browser_resources.started_at AND w.ended_at IS NULL)',
                 (task_id, project, profile))


def mark_task(conn, project, task_id, profile=None):
    sql = 'UPDATE browser_resources SET release_requested=1 WHERE project=? AND task_id=?'
    args = [project, task_id]
    if profile is not None:
        sql += ' AND profile=?'
        args.append(profile)
    conn.execute(sql, args)


def _request(endpoint, record, meta):
    if os.name == 'nt':
        client = socket.create_connection(('127.0.0.1', record['port']), timeout=TIMEOUT)
    else:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(TIMEOUT)
        client.connect(str(endpoint))
    with client:
        client.settimeout(TIMEOUT)
        req = {'meta': meta}
        if os.name == 'nt':
            req['token'] = record['token']
        client.sendall((json.dumps(req) + '\n').encode())
        data = b''
        while not data.endswith(b'\n'):
            part = client.recv(4096)
            if not part:
                break
            data += part
            if len(data) > 65536:
                raise ValueError('oversized harness response')
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError('invalid harness response')
        return result


def _pid_alive(pid):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE, no termination right
        if not handle:
            if ctypes.get_last_error() == 87:  # no such process
                return False
            raise OSError('cannot verify browser process exit')
        try:
            return kernel.WaitForSingleObject(handle, 0) != 0
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _close_scope(resource_id):
    root = directory(resource_id)
    if not root.exists():
        return
    # Keep screenshots/logs. Runtime endpoints are the only communication targets.
    for pid_file in root.glob('bu-*.pid'):
        raw = pid_file.read_text(encoding='utf-8').strip()
        pid = json.loads(raw)['pid'] if raw.startswith('{') else int(raw.split()[0])
        if type(pid) is not int or pid <= 0:
            raise ValueError('invalid harness pid')
        if not _pid_alive(pid):
            continue
        endpoint = pid_file.with_suffix('.port' if os.name == 'nt' else '.sock')
        record = json.loads(endpoint.read_text(encoding='utf-8')) if os.name == 'nt' else {}
        identity = _request(endpoint, record, 'ping')
        if identity.get('pong') is not True or identity.get('pid') != pid:
            raise ValueError('harness identity mismatch')
        result = _request(endpoint, record, 'shutdown')
        if result.get('ok') is not True:
            raise RuntimeError('harness shutdown failed: ' + str(result.get('error', 'invalid response')))
        deadline = time.monotonic() + TIMEOUT
        while _pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _pid_alive(pid) or endpoint.exists():
            raise TimeoutError('harness exit/endpoint cleanup did not complete')
        # The harness leaves its pid file behind. Remove only the exact record
        # just verified dead, so a later PID reuse cannot block another sweep.
        if pid_file.exists() and pid_file.read_text(encoding='utf-8').strip() == raw:
            pid_file.unlink()


def release(*, project=None, task_id=None, profile=None, session=None, started_at=None,
            pending_only=False, exclude_managed=False):
    """Return failures without undoing an already committed task transition.

    Marked rows stay until their execution ends: the owner may still be wrapping
    up and create another harness; finish_run performs the final sweep.
    """
    conn = db.get_connection()
    try:
        clauses, args = [], []
        for field, value in (('project', project), ('task_id', task_id), ('profile', profile),
                             ('session', session), ('started_at', started_at)):
            if value is not None:
                clauses.append(f'{field}=?')
                args.append(value)
        if pending_only:
            clauses.append('release_requested=1')
        where = ' AND '.join(clauses) or '1=1'
        rows = list(conn.execute(f'SELECT * FROM browser_resources WHERE {where}', args))
        if exclude_managed:
            # 引擎重启本地收尾：保留 gateway 托管未终态执行的浏览器资源（bot 继续运行，设计 4.4）。
            try:
                managed = {tuple(r) for r in conn.execute(
                    "SELECT session, started_at FROM hermes_executions WHERE state != 'terminal'")}
            except sqlite3.OperationalError as exc:
                if 'no such table' not in str(exc):
                    raise
                managed = set()
            rows = [row for row in rows if (row['session'], row['started_at']) not in managed]
        # 只标记本次要释放的行（exclude_managed 时保留 gateway 未终态执行资源不被标记）。
        for row in rows:
            conn.execute('UPDATE browser_resources SET release_requested=1 WHERE id=?', (row['id'],))
        conn.commit()
        errors = []
        for row in rows:
            error = ''
            try:
                _close_scope(row['id'])
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                error = str(exc)
                errors.append({'resource': row['id'], 'error': error})
                logger.warning('Browser cleanup %s: %s', row['id'], error)
            conn.execute('UPDATE browser_resources SET last_error=? WHERE id=?', (error, row['id']))
            if not error:
                conn.execute('DELETE FROM browser_resources WHERE id=? AND NOT EXISTS '
                             '(SELECT 1 FROM wake_runs WHERE session=? AND started_at=? AND ended_at IS NULL)',
                             (row['id'], row['session'], row['started_at']))
            conn.commit()
        return {'ok': not errors, 'scopes': len(rows), 'errors': errors}
    finally:
        conn.close()


def finish_run(session, started_at):
    return release(session=session, started_at=started_at, pending_only=True)
