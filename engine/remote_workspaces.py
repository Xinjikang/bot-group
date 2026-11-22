"""Host-side Git workspace preparation over an authenticated relay link."""
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
import json
import os
import re
import subprocess
import threading
import time
from urllib.parse import urlsplit
import uuid

from fastapi import HTTPException
from . import db, relay_server

_lock = threading.Lock()
_requests = {}
_preparing = {}
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='remote-workspace')


def receive(link, request_id, result):
    if not isinstance(request_id, str) or not isinstance(result, dict):
        return
    with _lock:
        future = _requests.pop((link, request_id), None)
    if future is not None:
        future.set_result(result)


def _request(member, spec, timeout):
    link = relay_server.active_link(member)
    if link is None or link.closed:
        raise HTTPException(409, '远程节点离线，请连接后重试')
    rid, future = uuid.uuid4().hex, Future()
    with _lock:
        _requests[link, rid] = future
    try:
        link.send_text(json.dumps({'relay': {'kind': 'workspace_request', 'generation': link.generation,
                                             'request_id': rid}, 'workspace': spec}))
        deadline = time.monotonic() + timeout
        while not future.done():
            if link.closed or time.monotonic() >= deadline:
                raise HTTPException(504, '远程工作区未应答，请检查连接和插件版本后重试')
            try:
                future.result(timeout=0.5)
            except TimeoutError:
                continue
        result = future.result()
        if link is not relay_server.active_link(member):
            raise HTTPException(409, '远程节点已重新连接，请重试')
        if result.get('error'):
            raise HTTPException(409, str(result['error'])[:300])
        return result
    except ConnectionError:
        raise HTTPException(409, '远程节点连接中断，请重试') from None
    finally:
        with _lock:
            _requests.pop((link, rid), None)


def root_for(conn, member):
    row = conn.execute('SELECT root FROM relay_workspace_roots WHERE member=?', (member,)).fetchone()
    return row[0] if row else ''


def set_root(member, value):
    root = _request(member, {'action': 'validate', 'root': value}, 12).get('root')
    if not isinstance(root, str) or not root:
        raise HTTPException(502, '远程插件未返回已核验的目录')
    with closing(db.get_connection()) as conn, conn:
        conn.execute('INSERT INTO relay_workspace_roots VALUES(?,?) ON CONFLICT(member) DO UPDATE SET root=excluded.root',
                     (member, root))
        conn.execute('DELETE FROM relay_project_workspaces WHERE member=? AND root<>?', (member, root))
        projects = conn.execute('SELECT project FROM project_members WHERE profile=? AND active=1', (member,)).fetchall()
    for project, in projects:
        schedule(project, member)
    return {'root': root}


def state(conn, project, member, mode):
    root = root_for(conn, member)
    if not root:
        return {'valid': False, 'path': '', 'error': '请先在远程节点设置中保存协作根目录'}
    row = conn.execute('SELECT result FROM relay_project_workspaces WHERE project=? AND member=? AND mode=? AND root=?',
                       (project, member, mode, root)).fetchone()
    return json.loads(row[0]) if row else {'valid': False, 'path': '', 'error': '远程工作区待准备'}


def configuration(conn, project, member):
    row = conn.execute('SELECT p.root,p.directory_mode,p.worktree_root FROM projects p JOIN project_members m ON m.project=p.name '
                       'JOIN relay_nodes n ON n.member=m.profile WHERE p.name=? AND m.profile=? AND m.active=1 '
                       'AND p.archived=0 AND n.credential_revoked=0', (project, member)).fetchone()
    if not row:
        raise HTTPException(409, '请先启用项目中的远程成员并核对节点凭据')
    root = root_for(conn, member)
    if not root:
        raise HTTPException(409, '请先在远程节点设置中保存协作根目录')
    return root, row['root'], row['directory_mode'] or ('worktree' if row['worktree_root'] else 'shared')


def _git_value(directory, *args):
    env = os.environ.copy()
    for key in ('GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY'):
        env.pop(key, None)
    try:
        return subprocess.check_output(['git', '-C', directory, *args], stderr=subprocess.DEVNULL,
                                       text=True, encoding='utf-8', timeout=10, env=env).strip()
    except (subprocess.SubprocessError, OSError, UnicodeError):
        raise HTTPException(409, '请核对办公机项目仓库的 origin 和 master 分支') from None


def _prepare(project, member):
    with closing(db.get_connection()) as conn:
        plan = configuration(conn, project, member)
        root, office, mode = plan
        try:
            origin = _git_value(office, 'config', '--get', 'remote.origin.url')
            url = urlsplit(origin) if '://' in origin else None
            if not (re.fullmatch(r'[\w.-]+@[\w.-]+:[\w./-]+', origin) or
                    (url and url.scheme in ('https', 'ssh') and url.hostname and not url.password
                     and not url.query and not url.fragment and (url.scheme == 'ssh' or not url.username))):
                raise HTTPException(409, '项目 origin 须为远端可访问且不含凭据的 SSH 或 HTTPS 地址')
            baseline = _git_value(office, 'rev-parse', '--verify', 'refs/heads/master^{commit}')
            result = _request(member, dict(action='prepare', root=root, mode=mode, project=project,
                                           member=member, origin=origin, baseline=baseline), 210)
            if (result.get('valid') is not True or type(result.get('dirty')) is not bool
                    or not all(isinstance(result.get(k), str) and result[k] for k in ('main', 'path', 'branch', 'head'))):
                raise HTTPException(502, '远程插件返回了无效的工作区状态')
            result = {k: result[k] for k in ('main', 'path', 'branch', 'head', 'dirty')}
            result.update(valid=True, root=root, mode=mode, baseline=baseline, checked_at=time.time())
        except HTTPException as exc:
            result = {'valid': False, 'path': '', 'error': str(exc.detail)}
        except ValueError:
            result = {'valid': False, 'path': '', 'error': '项目 origin 地址无效'}
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if configuration(conn, project, member) != plan:
                raise HTTPException(409, '工作区配置已变更，请重新准备')
            conn.execute('INSERT OR REPLACE INTO relay_project_workspaces VALUES(?,?,?,?,?)',
                         (member, project, root, mode, json.dumps(result, ensure_ascii=False)))
        if not result['valid']:
            raise HTTPException(409, result['error'])
        return result


def schedule(project, member):
    with closing(db.get_connection()) as conn:
        row = conn.execute('SELECT r.root,p.root,p.directory_mode FROM relay_workspace_roots r,projects p '
                           'WHERE r.member=? AND p.name=?', (member, project)).fetchone()
    key = project, member, tuple(row) if row else ()
    with _lock:
        future = _preparing.get(key)
        if future is not None and not future.done():
            return future
        future = _pool.submit(_prepare, project, member)
        _preparing[key] = future
    def forget(completed):
        with _lock:
            if _preparing.get(key) is completed:
                _preparing.pop(key)
    future.add_done_callback(forget)
    return future


def for_session(project, member):
    with closing(db.get_connection()) as conn:
        if not conn.execute('SELECT 1 FROM relay_nodes WHERE member=?', (member,)).fetchone():
            return None
    if not project:
        raise HTTPException(409, '远程会话需要指定项目工作区')
    return schedule(project, member).result()
