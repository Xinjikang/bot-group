"""Discard proven initialization debris; seed imports from completed removed projects."""
import json
from pathlib import Path

from fastapi import HTTPException
from . import db, project_initialization as files, project_permissions as permissions
from . import project_workspaces as ws


def matching(conn, root):
    result = []
    for row in conn.execute('SELECT * FROM projects ORDER BY removed_at DESC,created_at DESC,name DESC'):
        try:
            if ws.key(row['root']) == ws.key(root):
                result.append(dict(row))
        except (HTTPException, OSError, ValueError, RuntimeError):
            continue
    return result


def historical_config(conn, root):
    """Only completed, explicitly removed projects supply configuration defaults."""
    source = next((p for p in matching(conn, root) if p['removed'] and p['readiness_status'] == 'ready'), None)
    if not source:
        return None
    setup = {}
    for row in conn.execute("SELECT payload,result FROM project_setup_requests WHERE status='ready' ORDER BY updated_at DESC"):
        if json.loads(row['result']).get('project') == source['name']:
            setup = json.loads(row['payload'])
            break
    template = setup.get('template')
    if not template:
        for row in conn.execute("SELECT detail FROM project_config_events WHERE project=? AND action='project.import' ORDER BY id DESC", (source['name'],)):
            template = json.loads(row['detail']).get('template')
            if template:
                break
    if not template:
        row = conn.execute("SELECT plan FROM project_initializations WHERE project=? AND status='ready' ORDER BY updated_at DESC LIMIT 1", (source['name'],)).fetchone()
        if row:
            template = json.loads(row['plan']).get('template')
    snapshot = permissions._snapshot(conn, source['name'])
    grants = snapshot['grants']
    roles = [{'name': r['name'], 'permissions': [p for p, targets in grants.items()
              if permissions._role_grant(r['id']) in targets]} for r in snapshot['roles']]
    members = []
    for row in conn.execute('SELECT pm.profile,pm.role,m.bot_id FROM project_members pm '
                            'JOIN members m ON m.profile=pm.profile JOIN bots b ON b.id=m.bot_id '
                            'WHERE pm.project=? AND pm.active=1', (source['name'],)):
        if db.is_builtin_user(row['profile']):
            continue
        members.append({'bot_id': row['bot_id'], 'role': row['role'],
                        'permissions': [p for p, targets in grants.items() if row['profile'] in targets]})
    # Current setup validates these drafts through the same role/member and Git checks.
    resources = setup.get('workspace_resources', [])
    shared = conn.execute('SELECT config FROM workspace_resources WHERE project=? AND path=?', (source['name'], ws.key(root))).fetchone()
    if shared and source['directory_mode'] != 'worktree':
        resources = [{'key': 'shared', 'config': json.loads(shared['config'])}]
    return {**{k: source[k] for k in ('display_name', 'description', 'directory_mode', 'worktree_root')},
            'source_project': source['name'], 'roles': roles, 'members': members,
            'owner_role': next((r['name'] for r in snapshot['roles'] if r['is_owner']), None),
            'template_id': template.get('id') if template else None,
            'workspace_resources': resources, 'git_baseline': setup.get('git_baseline', 'existing')}


def discard(conn, root, actor):
    """Called under the setup lock. Never infer ownership from a filename alone."""
    projects = matching(conn, root)
    if any(not p['removed'] and (p['archived'] or p['readiness_status'] == 'ready') for p in projects):
        return []
    incomplete = [p for p in projects if not p['removed'] and p['readiness_status'] != 'ready']
    for project in incomplete:
        if not conn.execute("SELECT 1 FROM project_initializations WHERE project=? AND status<>'ready'", (project['name'],)).fetchone():
            raise HTTPException(409, '该项目尚未就绪且缺少初始化归属记录，请检查项目状态')
    for project in incomplete:
        ws.ensure_idle(project['name'])
        for table, condition in (('tasks', "status IN ('pending','in_progress','blocked')"),
                                 ('wake_jobs', "status IN ('queued','running')"),
                                 ('project_workspaces', '1=1')):
            if conn.execute(f'SELECT 1 FROM {table} WHERE project=? AND {condition}', (project['name'],)).fetchone():
                raise HTTPException(409, '初始化残留仍有任务或工作区占用，请先处理占用')
    plans = []
    for row in conn.execute("SELECT * FROM project_initializations WHERE status<>'ready' AND phase<>'discarded'"):
        if ws.key(row['root']) != ws.key(root):
            continue
        plan = json.loads(row['plan'])
        if plan['mode'] != 'new':
            continue
        # A completed removed project owns its files even if an older draft remains.
        if any(p['readiness_status'] == 'ready' for p in projects):
            continue
        plans.append((dict(row), plan))
    candidates, directories = {}, set()
    base = files._safe(Path(root).resolve())
    for row, plan in plans:
        if plan.get('root_stamp') and base.exists() and files._stamp(base) != plan['root_stamp']:
            raise HTTPException(409, '项目目录已被替换，保留现有文件，请检查目录')
        if row['phase'] == 'preview':
            continue
        for item in plan['files']:
            if item['action'] != 'create':
                continue
            from .project_templates import relative_path
            path = files._safe(base.joinpath(*relative_path(item['path']).parts))
            # Only the explicitly replaceable rules file is disposable. Published
            # documentation, scripts and source remain project assets, even if a
            # failed initialization originally supplied their template contents.
            if item['path'] == 'AGENTS.md':
                candidates[path] = item['sha256']
            temp = path.with_name(f'.{row["id"]}.{path.name}.tmp')
            candidates[temp] = item['sha256']
            # Partial staging is disposable only at the exact recorded operation path.
            part = files._safe(temp.with_name(temp.name + '.part'))
            if part.is_file():
                candidates[part] = files._hash(part)
            directories.update(p for p in path.parents if p != base and base in p.parents)
    removed = []
    for path, expected in candidates.items():
        if files._hash(path) == expected:
            path.unlink()
            removed.append(path.relative_to(base).as_posix())
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        files._safe(directory)
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    with conn:
        for row, _ in plans:
            files._record(conn, row['id'], phase='discarded', status='failed',
                          error=files._json({'code': 'discarded', 'message': '旧初始化已清理，请重新配置项目'}))
        for project in incomplete:
            conn.execute('UPDATE projects SET removed=1,archived=1,removed_at=? WHERE name=?', (db.now_iso(), project['name']))
            ws.audit(conn, project['name'], actor, 'project.initialization.discard', {'files': removed})
    return removed


def prepare(body, actor):
    from . import project_import as imports
    from . import project_setup_progress as progress
    if set(body) - {'root', 'creation_mode', 'template', 'template_explicit'}:
        raise HTTPException(400, '目录准备只接受目录、创建方式和模板')
    mode = body.get('creation_mode', 'import')
    if mode not in ('new', 'import'):
        raise HTTPException(400, '无效的项目创建方式')
    root = str(files._root(body.get('root'))) if mode == 'new' else imports.import_root(body.get('root'))
    with ws.LOCK:
        conn = db.get_connection()
        try:
            for row in conn.execute("SELECT id,payload FROM project_setup_requests WHERE status='running'"):
                if row['id'] in progress.ACTIVE and ws.key(json.loads(row['payload'])['root']) == ws.key(root):
                    raise HTTPException(409, '该目录正在配置，请等待当前操作结束')
            removed = discard(conn, root, actor)
        finally:
            conn.close()
        request = {k: v for k, v in body.items() if k != 'template_explicit'}
        conn = db.get_connection()
        try:
            history = historical_config(conn, root) if mode == 'import' else None
        finally:
            conn.close()
        if history and history['template_id'] in ('software', 'research', 'writing', 'product-prototype', 'algorithm', 'testing', 'blank') and not body.get('template_explicit'):
            request['template'] = {'id': history['template_id']}
        result = imports.preview(request)
        result['cleaned_files'] = removed
        return result
