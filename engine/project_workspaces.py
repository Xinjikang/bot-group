"""Engine-owned project directories. Git operations use explicit paths and never force."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
import subprocess
import threading

from fastapi import HTTPException
from . import db

LOCK = threading.RLock()


def git(root, *args):
    try:
        result = subprocess.run(
            ['git', '--no-optional-locks', '-c', 'core.quotePath=false', '-C', str(root), *args], capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=30,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(409, f'Git 操作失败：{exc}') from exc
    if result.returncode:
        raise HTTPException(409, result.stderr.strip() or 'Git 操作失败')
    return result.stdout.strip()


def canonical(raw):
    if isinstance(raw, Path):
        raw = str(raw)
    if not isinstance(raw, str) or not raw.strip() or not Path(raw).is_absolute():
        raise HTTPException(400, '目录必须是绝对路径')
    if any(c in raw for c in ('\r', '\n', '\0', '\t')):
        raise HTTPException(400, '目录不能包含控制字符')
    return str(Path(raw).resolve())


def key(raw):
    return os.path.normcase(canonical(raw))


def project(conn, name):
    row = conn.execute('SELECT * FROM projects WHERE name=?', (name,)).fetchone()
    if row is None:
        raise HTTPException(404, '项目不存在')
    p = dict(row)
    p['directory_mode'] = p['directory_mode'] or ('worktree' if p.get('worktree_root') else 'shared')
    return p


def audit(conn, name, actor, action, detail):
    conn.execute('INSERT INTO project_config_events(project,actor,action,detail,created_at) '
                 'VALUES(?,?,?,?,?)', (name, actor, action, json.dumps(detail, ensure_ascii=False), db.now_iso()))


def registrations(root):
    rows = []
    for chunk in git(root, 'worktree', 'list', '--porcelain').split('\n\n'):
        fields = dict(line.split(' ', 1) if ' ' in line else (line, True)
                      for line in chunk.splitlines())
        if 'worktree' in fields:
            rows.append(fields)
    return rows


def inspect(root, path, snapshot=None):
    root, path = canonical(root), canonical(path)
    if not Path(path).is_dir():
        raise HTTPException(409, f'目录不存在：{path}')
    if key(git(path, 'rev-parse', '--show-toplevel')) != key(path):
        raise HTTPException(409, '请选择 Git 工作区根目录')
    common = snapshot[0] if snapshot else git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    if key(git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')) != key(common):
        raise HTTPException(409, '工作目录属于其他仓库')
    registered = next((r for r in (snapshot[1] if snapshot else registrations(root)) if key(r['worktree']) == key(path)), None)
    if registered is None or registered.get('prunable'):
        raise HTTPException(409, '工作目录未有效登记在本项目仓库')
    return {'path': path, 'head': git(path, 'rev-parse', 'HEAD'),
            'branch': registered.get('branch', '').removeprefix('refs/heads/'),
            'detached': bool(registered.get('detached')),
            'dirty': bool(git(path, 'status', '--porcelain')), 'valid': True}


def member_mode(conn, p, profile):
    if p['directory_mode'] == 'shared':
        return 'shared'
    row = conn.execute('SELECT mode FROM project_workspace_modes WHERE project=? AND profile=?',
                       (p['name'], profile)).fetchone()
    return row['mode'] if row else ('shared' if profile == 'default' else 'worktree')


def selected_path(conn, p, profile):
    if member_mode(conn, p, profile) == 'shared':
        return p['root']
    return independent_path(conn, p, profile)


def independent_path(conn, p, profile):
    row = conn.execute('SELECT path FROM project_workspaces WHERE project=? AND profile=?',
                       (p['name'], profile)).fetchone()
    if row:
        return row['path']
    # Compatibility for existing worktrees: validate before every launch, never create implicitly.
    if p.get('worktree_root'):
        if Path(profile).name != profile or profile in ('.', '..') or ':' in profile or '\\' in profile:
            raise HTTPException(400, '无效的 profile')
        return str(Path(p['worktree_root']) / profile)
    raise HTTPException(409, '成员尚未分配 worktree')


def launch_directory(conn, name, profile):
    p = project(conn, name)
    path = canonical(selected_path(conn, p, profile))
    if not Path(path).is_dir():
        raise HTTPException(409, f'工作目录不存在：{path}')
    if p['directory_mode'] == 'worktree':
        inspect(p['root'], path)
        if member_mode(conn, p, profile) == 'worktree':
            for r in conn.execute('SELECT project,profile,path FROM project_workspaces'):
                if (r['project'], r['profile']) != (name, profile) and key(r['path']) == key(path):
                    raise HTTPException(409, '工作目录已分配给其他项目或成员')
    return path


def ensure_idle(name, profile=None):
    from . import wake
    if any(r.get('project') == name and (profile is None or r.get('profile') == profile)
           for r in wake.active_runs()):
        raise HTTPException(409, '请先停止受影响的 bot，再修改工作目录')
    conn = db.get_connection()
    try:
        sql = "SELECT 1 FROM wake_jobs WHERE project=? AND status='running'"
        params = [name]
        if profile:
            sql += ' AND profile=?'
            params.append(profile)
        if conn.execute(sql, params).fetchone():
            raise HTTPException(409, '该项目仍有运行中的唤醒任务')
        # Reconcile surviving children after an engine restart (exit_code=-1 is recovery's sentinel).
        runs = conn.execute('SELECT r.session FROM wake_runs r WHERE r.project=? '
                            'AND (r.ended_at IS NULL OR r.exit_code=-1) AND r.started_at='
                            '(SELECT MAX(x.started_at) FROM wake_runs x WHERE x.session=r.session)', (name,)).fetchall()
        profiles = [profile] if profile else [r[0] for r in conn.execute(
            "SELECT profile FROM members WHERE profile<>?", (db.BUILTIN_USER_PROFILE,))]
        for run in runs:
            for member in profiles:
                if not run['session'].startswith(f'{member}-{name}-'):
                    continue
                pids = wake.find_bot_processes(member, session=run['session'])
                if pids is None or pids:
                    raise HTTPException(409, '尚未确认相关 bot 进程全部退出，请先叫停并核实')
    finally:
        conn.close()


def view(name, profile=None):
    conn = db.get_connection()
    try:
        p = project(conn, name)
        snapshot = None
        snapshot_error = None
        if p['directory_mode'] == 'worktree':
            try:
                snapshot = (git(p['root'], 'rev-parse', '--path-format=absolute', '--git-common-dir'), registrations(p['root']))
            except HTTPException as exc:
                snapshot_error = str(exc.detail)
        assigned = {r['profile']: r['path'] for r in conn.execute('SELECT profile,path FROM project_workspaces WHERE project=?', (name,))}
        members = []
        from .bot_identity import member_views
        memberships = {r['profile']: dict(r) for r in conn.execute(
            'SELECT profile,active,role,role_id FROM project_members WHERE project=?', (name,))}
        for m in member_views(conn):
            if m['profile'] == db.BUILTIN_USER_PROFILE or m['profile'] not in memberships:
                continue
            m.update(memberships[m['profile']])
            if profile is not None and m['profile'] != profile:
                continue
            from . import hermes_gateway, hermes_runtime, session_adapter, wake
            try:
                m['work_session_exists'] = bool(session_adapter.existing_session_id(
                    conn, name, m['profile'], 'work', None,
                    wake.session_name(m['profile'], 'work', None, name)))
            except (sqlite3.Error, OSError, hermes_gateway.GatewayTransportError,
                    hermes_gateway.GatewayRefusal, hermes_runtime.HermesConfigurationError,
                    subprocess.TimeoutExpired):
                m['work_session_exists'] = None
            m['workspace_assigned'] = m['profile'] in assigned
            m['directory_mode'] = member_mode(conn, p, m['profile'])
            m['assigned_workspace_path'] = assigned.get(m['profile'], '')
            try:
                m['independent_path'] = independent_path(conn, p, m['profile'])
            except HTTPException:
                m['independent_path'] = ''
            path = ''
            try:
                path = selected_path(conn, p, m['profile'])
                m['workspace'] = {'path': canonical(path), 'valid': Path(path).is_dir()}
            except HTTPException as exc:
                m['workspace'] = {'path': path, 'valid': False, 'error': str(exc.detail)}
            members.append(m)
        if p['directory_mode'] == 'worktree':
            from concurrent.futures import ThreadPoolExecutor
            def check(m):
                path = m['workspace']['path']
                try:
                    if snapshot_error:
                        raise HTTPException(409, snapshot_error)
                    m['workspace'] = inspect(p['root'], path, snapshot)
                except HTTPException as exc:
                    m['workspace'] = {'path': path, 'valid': False, 'error': str(exc.detail)}
                return m
            with ThreadPoolExecutor(max_workers=4) as pool:
                members = list(pool.map(check, members))
        history = [dict(r) for r in conn.execute('SELECT * FROM project_config_events WHERE project=? '
                                                'ORDER BY id DESC LIMIT 30', (name,))]
        repository = {}
        if p['directory_mode'] == 'worktree':
            try:
                if snapshot_error:
                    raise HTTPException(409, snapshot_error)
                repository = next((m['workspace'] for m in members if m['workspace']['path'] and key(m['workspace']['path']) == key(p['root'])), None)
                if repository is None and profile is None:
                    repository = inspect(p['root'], p['root'], snapshot)
                repository = repository or {}
            except HTTPException as exc:
                repository = {'error': str(exc.detail)}
        return {'project': p, 'members': members, 'history': history, 'repository': repository}
    finally:
        conn.close()


def validate_config(body, current=None):
    p = {**(current or {}), **body}
    if current and not any(k in body for k in ('root', 'directory_mode', 'worktree_root')):
        return
    root = canonical(p.get('root'))
    if not Path(root).is_dir():
        raise HTTPException(400, '项目主目录不存在')
    mode = p.get('directory_mode', 'shared')
    if mode not in ('shared', 'worktree'):
        raise HTTPException(400, '请选择共用主目录或独立 worktree')
    if mode == 'worktree':
        inspect(root, root)
        parent = Path(canonical(p.get('worktree_root')))
        main = Path(root)
        if parent == main or main in parent.parents or parent in main.parents:
            raise HTTPException(400, 'worktree 根目录须与项目主目录分开')
        conn = db.get_connection()
        try:
            for r in conn.execute("SELECT name,worktree_root FROM projects WHERE directory_mode='worktree' AND removed=0"):
                if r['name'] != (current or {}).get('name') and r['worktree_root'] and key(r['worktree_root']) == key(parent):
                    raise HTTPException(409, 'worktree 根目录已用于其他项目')
        finally:
            conn.close()
    if current and any(p.get(k) != current.get(k) for k in ('root', 'directory_mode', 'worktree_root')):
        ensure_idle(current['name'])
        conn = db.get_connection()
        try:
            for r in conn.execute('SELECT path FROM project_workspaces WHERE project=?', (current['name'],)):
                inspect(root, r['path'])
        finally:
            conn.close()


def manage(name, profile, body, actor):
    """Explicit create/adopt/remove. A failed DB write leaves Git state available for adoption."""
    action = body.get('action')
    if action not in ('create', 'adopt', 'remove', 'set_mode'):
        raise HTTPException(400, 'action 必须为 create/adopt/remove/set_mode')
    with LOCK:
        ensure_idle(name, profile)
        conn = db.get_connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            p = project(conn, name)
            if db.is_builtin_user(profile) or not conn.execute(
                    'SELECT 1 FROM members WHERE profile=?', (profile,)).fetchone():
                raise HTTPException(400, '请选择使用独立 worktree 的 bot')
            if p['directory_mode'] != 'worktree':
                raise HTTPException(409, '请先保存独立 worktree 模式')
            if action == 'set_mode':
                mode = body.get('mode')
                if mode not in ('shared', 'worktree'):
                    raise HTTPException(400, '请选择主目录或独立 worktree')
                before = member_mode(conn, p, profile)
                conn.execute('INSERT INTO project_workspace_modes(project,profile,mode) VALUES(?,?,?) '
                             'ON CONFLICT(project,profile) DO UPDATE SET mode=excluded.mode', (name, profile, mode))
                path = launch_directory(conn, name, profile)
                audit(conn, name, actor, 'workspace.set_mode', {'profile': profile, 'before': before, 'mode': mode, 'path': path})
                from .codex_project_access import commit_directory_access
                commit_directory_access(conn, name, [profile])
                return {'ok': True, 'path': path, 'mode': mode}
            previous = conn.execute('SELECT path FROM project_workspaces WHERE project=? AND profile=?',
                                    (name, profile)).fetchone()
            path = canonical(body.get('path') or independent_path(conn, p, profile))
            if key(path) == key(p['root']):
                raise HTTPException(400, '项目主目录必须保留')
            for r in conn.execute('SELECT name,root FROM projects'):
                other = Path(canonical(r['root']))
                target = Path(path)
                if target == other or target in other.parents or other in target.parents:
                    raise HTTPException(400, 'worktree 目录须位于项目主目录之外')
            for r in conn.execute('SELECT project,profile,path FROM project_workspaces'):
                if (r['project'], r['profile']) != (name, profile) and key(r['path']) == key(path):
                    raise HTTPException(409, '该目录已分配给其他成员')
            for r in registrations(p['root']):
                existing, target = Path(canonical(r['worktree'])), Path(path)
                if existing != target and (existing in target.parents or target in existing.parents):
                    raise HTTPException(409, 'worktree 路径不能互相嵌套')
            # Protect implicit legacy mappings too.
            for r in conn.execute("SELECT profile FROM members WHERE profile<>?", (db.BUILTIN_USER_PROFILE,)):
                if r['profile'] == profile:
                    continue
                try:
                    if key(selected_path(conn, p, r['profile'])) == key(path):
                        raise HTTPException(409, '该目录已分配给其他成员')
                except HTTPException as exc:
                    if exc.detail != '成员尚未分配 worktree':
                        raise
            if previous and key(previous['path']) != key(path):
                raise HTTPException(409, '请先清理已接管的 worktree，再更改目录')
            detail = {'profile': profile, 'path': path}
            if action == 'create':
                if Path(path).exists():
                    raise HTTPException(409, '目录已存在，请使用接管')
                start = body.get('start_ref') or 'HEAD'
                if not isinstance(start, str) or start.startswith('-'):
                    raise HTTPException(400, '无效的初始版本')
                commit = git(p['root'], 'rev-parse', '--verify', '--end-of-options', start + '^{commit}')
                git(p['root'], 'worktree', 'add', '--detach', path, commit)
                detail['start_commit'] = commit
            state = inspect(p['root'], path)
            detail['state'] = state
            if action == 'remove':
                if not previous:
                    raise HTTPException(409, '请先接管该目录再清理')
                if state['dirty']:
                    raise HTTPException(409, 'worktree 有未提交改动，请先处理')
                if not git(path, 'branch', '-a', '--contains', state['head']):
                    raise HTTPException(409, '当前提交尚无分支引用，请先保存到分支再清理')
                # No --force: Git also protects untracked files and locked worktrees.
                git(p['root'], 'worktree', 'remove', path)
                conn.execute('DELETE FROM project_workspaces WHERE project=? AND profile=?', (name, profile))
                conn.execute('DELETE FROM workspace_resources WHERE project=? AND path=?', (name, key(path)))
            else:
                conn.execute('INSERT INTO project_workspaces(project,profile,path) VALUES(?,?,?) '
                             'ON CONFLICT(project,profile) DO UPDATE SET path=excluded.path', (name, profile, path))
                conn.execute("INSERT INTO project_workspace_modes(project,profile,mode) VALUES(?,?,'worktree') "
                             "ON CONFLICT(project,profile) DO UPDATE SET mode=excluded.mode", (name, profile))
            audit(conn, name, actor, 'workspace.' + action, detail)
            if action == 'remove':
                conn.commit()
            else:
                from .codex_project_access import commit_directory_access
                commit_directory_access(conn, name, [profile])
            return {'ok': True, 'action': action, **detail}
        except Exception as exc:
            conn.rollback()
            # Git and SQLite are separate resources; preserve the partial directory for explicit adoption.
            if action == 'create' and 'commit' in locals():
                audit(conn, name, actor, 'workspace.create_failed', {
                    'profile': profile, 'path': path, 'start_commit': commit,
                    'error': str(getattr(exc, 'detail', exc)), 'recovery': '检查目录后接管或重试'})
                conn.commit()
            raise
        finally:
            conn.close()
