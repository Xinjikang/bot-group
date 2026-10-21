"""Read-only import preview, atomic registration and reviewed file supplements."""
import json
import os
from pathlib import Path

from fastapi import HTTPException
from . import bot_identity, db, project_permissions as permissions, project_team, project_workspaces as ws, taskpool
from . import project_import_files as supplements
from . import project_setup_progress as progress

MAX_DOCUMENTS = 64
MAX_ENTRIES = 1000
TEXT_BYTES = 8192
AGENTS_BYTES = 65536
PATH_HINT = '支持本地盘普通绝对路径及可解析的目录联接、符号链接和 subst；UNC 与扩展前缀路径请改用本地盘路径。'


def import_root(raw):
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(400, 'root 必须是已有目录的绝对路径')
    # UNC aliases may refer to a registered local directory with a different key.
    # Keep this boundary explicit until a shared file-identity policy is available.
    if os.name == 'nt' and raw.strip().replace('/', '\\').startswith('\\\\'):
        raise HTTPException(400, PATH_HINT)
    try:
        root = ws.canonical(raw)
        if os.name == 'nt' and root.startswith('\\\\'):
            raise HTTPException(400, PATH_HINT)
        if not Path(root).is_dir():
            raise HTTPException(400, '项目主目录不存在')
        return root
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(400, '目录无法解析或访问') from exc


def selected_root(body):
    if body.get('creation_mode', 'import') not in ('new', 'import'):
        raise HTTPException(400, '无效的项目创建方式')
    if body.get('creation_mode') == 'new':
        root = supplements.files._root(body.get('root'))
        conn = db.get_connection()
        try:
            if matches(conn, str(root)):
                return str(root)
            # A discarded initializer can retain documentation and test assets.
            # The shared publisher preserves them while configuration starts fresh.
            from .project_setup_history import matching
            completed = any(p['readiness_status'] == 'ready' for p in matching(conn, str(root)))
            if not completed:
                for row in conn.execute("SELECT root,plan FROM project_initializations WHERE phase='discarded'"):
                    if ws.key(row['root']) == ws.key(str(root)):
                        plan = json.loads(row['plan'])
                        if plan.get('root_stamp') and root.exists() and supplements.files._stamp(root) == plan['root_stamp']:
                            return str(root)
        finally:
            conn.close()
        if root.exists() and any(p.name != '.git' for p in root.iterdir()):
            raise HTTPException(409, '不能在已有项目上新建，请选择空目录或新路径')
        return str(root)
    return import_root(body.get('root'))


def validate_location(body, root):
    if not body.get('git_setup'):
        ws.validate_config({**body, 'root': root})
        return
    if body.get('directory_mode', 'shared') not in ('shared', 'worktree'):
        raise HTTPException(400, '请选择共用目录或独立 worktree')
    if body.get('directory_mode') == 'worktree':
        parent = supplements.files._root(body.get('worktree_root'))
        main = Path(root)
        if main == parent or main in parent.parents or parent in main.parents:
            raise HTTPException(400, 'worktree 根目录须与项目主目录分开')
        conn = db.get_connection()
        try:
            for row in conn.execute('SELECT root,worktree_root FROM projects WHERE removed=0'):
                for raw in (row['root'], row['worktree_root']):
                    if not raw:
                        continue
                    other = Path(ws.canonical(raw))
                    if parent == other or parent in other.parents or other in parent.parents:
                        raise HTTPException(409, 'worktree 根目录与已有项目目录冲突')
        finally:
            conn.close()


def matches(conn, root):
    target = ws.key(root)
    found = []
    for row in conn.execute('SELECT name,display_name,root,readiness_status,archived FROM projects WHERE removed=0 ORDER BY name'):
        try:
            if ws.key(row['root']) == target:
                found.append(dict(row))
        except (HTTPException, OSError, ValueError, RuntimeError):
            # Historical invalid/missing roots remain available through project opening.
            continue
    return found


def _documents(root):
    base = Path(root)
    result, warnings = [], []
    entries = 0

    def add(path):
        try:
            # Reparse points may escape the selected directory; only preview its files.
            resolved = path.resolve()
            if base not in resolved.parents or not resolved.is_file():
                warnings.append(f'跳过目录外或无效文件：{path.relative_to(base)}')
                return
            size = resolved.stat().st_size
            limit = AGENTS_BYTES if path.name == 'AGENTS.md' else TEXT_BYTES
            with resolved.open('rb') as stream:
                data = stream.read(limit + 1)
            result.append({'path': path.relative_to(base).as_posix(), 'size': size,
                           'content': data[:limit].decode('utf-8-sig', errors='replace'),
                           'truncated': len(data) > limit})
        except OSError:
            warnings.append(f'读取失败：{path.relative_to(base)}')

    for name in ('AGENTS.md', 'README.md'):
        if (base / name).is_file():
            add(base / name)
    if not any(d['path'] == 'AGENTS.md' for d in result):
        warnings.append('未发现可读的 AGENTS.md；启用补齐项目基础文件后将根据模板生成。')
    for name in ('doc', 'docs'):
        folder = base / name
        if not folder.is_dir():
            continue
        if folder.resolve() != folder or folder.is_symlink():
            warnings.append(f'跳过链接文档目录：{name}')
            continue
        try:
            for current, dirs, files in os.walk(folder, followlinks=False,
                    onerror=lambda exc: warnings.append(f'文档目录读取失败：{name}')):
                dirs[:] = sorted(d for d in dirs if not (Path(current) / d).is_symlink()
                                 and (Path(current) / d).resolve() == Path(current) / d)
                entries += len(dirs) + len(files)
                if entries > MAX_ENTRIES or len(result) >= MAX_DOCUMENTS:
                    warnings.append('文档扫描达到上限（1000 条目录项 / 64 份文档）；完整文件保留在项目目录。')
                    break
                for file in sorted(files):
                    if Path(file).suffix.lower() in ('.md', '.txt', '.rst', '.adoc'):
                        add(Path(current) / file)
                        if len(result) >= MAX_DOCUMENTS:
                            warnings.append('文档预览达到 64 份上限；完整文件保留在项目目录。')
                            break
        except OSError:
            warnings.append(f'文档目录读取失败：{name}')
    return result, warnings


def preview(body):
    root = selected_root(body)
    extra = {}
    if 'template' in body:
        selected = body['template']
        if not isinstance(selected, dict) or set(selected) - {'id', 'version'} or 'id' not in selected:
            raise HTTPException(400, 'template 需要 id 和可选 version')
        template, default_roles = supplements.defaults(selected)
        extra = {'template': {'id': template['id'], 'version': template['version']}, 'default_roles': default_roles,
                 'owner_role': supplements.templates.owner_role_name(template)}
        extra['git'] = supplements.git.inspect(root)
        if 'roles' in body:
            _config(body)
            extra['file_plan'] = supplements.plan(body, root)
            validate_location(body, root)
    conn = db.get_connection()
    try:
        existing = matches(conn, root)
        if not existing and body.get('creation_mode', 'import') == 'import':
            from .project_setup_history import historical_config
            extra['historical_config'] = historical_config(conn, root)
        if 'workspace_resources' in body:
            _config(body)
            from . import workspace_resources
            extra['workspace_resources'] = workspace_resources.plan(conn, {**body, 'root': root})
        bots = [dict(r) for r in conn.execute(
            "SELECT b.id AS bot_id,b.profile AS host_profile,b.display_name,b.runtime,"
            "m.profile AS member_profile FROM bots b LEFT JOIN members m ON m.bot_id=b.id "
            "WHERE b.profile<>'user' ORDER BY b.id")]
    finally:
        conn.close()
    documents, warnings = _documents(root) if Path(root).exists() else ([], [])
    return {'root': root, 'existing_projects': existing, 'documents': documents,
            'warnings': warnings, 'path_hint': PATH_HINT,
            'bots': bots, 'permissions': [{'key': k, 'label': v} for k, v in permissions.PERMISSIONS.items()],
            'file_action': 'supplement' if body.get('supplement') or body.get('initialize_git') else 'preserve', **extra}


def _permission_list(value):
    if not isinstance(value, list) or any(not isinstance(p, str) or p not in permissions.PERMISSIONS for p in value):
        raise HTTPException(400, 'permissions 必须是有效权限代码数组')
    return sorted(set(value))


def _config(body):
    allowed = {'root', 'display_name', 'description', 'directory_mode', 'worktree_root', 'roles', 'members', 'owner_bot_id',
               'template', 'supplement', 'plan_hash', 'initialize_git', 'agents_edit',
               'creation_mode', 'git_setup', 'git_baseline', 'commit_files', 'reviewed_files', 'request_id', 'workspace_resources'}
    if set(body) - allowed:
        raise HTTPException(400, '包含未知导入配置项')
    if 'git_setup' in body and type(body['git_setup']) is not bool:
        raise HTTPException(400, 'git_setup 必须为布尔值')
    if body.get('creation_mode') == 'new' and not body.get('supplement'):
        raise HTTPException(400, '新建项目需要生成基础文件')
    for field in ('display_name', 'description', 'directory_mode', 'worktree_root'):
        value = body.get(field, '' if field != 'directory_mode' else 'shared')
        if not isinstance(value, str):
            raise HTTPException(400, f'{field} 必须是文本')
    if not body.get('display_name', '').strip() or len(body['display_name']) > 200:
        raise HTTPException(400, '请填写长度不超过 200 的项目显示名')
    roles, members = body.get('roles', []), body.get('members', [])
    if not isinstance(roles, list) or not isinstance(members, list) or len(roles) > 100 or len(members) > 100:
        raise HTTPException(400, 'roles 和 members 必须是长度不超过 100 的数组')
    names, ids = set(), set()
    for role in roles:
        if not isinstance(role, dict) or set(role) != {'name', 'permissions'}:
            raise HTTPException(400, '角色需要 name 和 permissions')
        name = role['name']
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100 or any(ord(c) < 32 for c in name):
            raise HTTPException(400, '角色名必须是长度不超过 100 的单行文本')
        if name.strip() in names:
            raise HTTPException(400, '角色名重复')
        names.add(name.strip())
        _permission_list(role['permissions'])
    for member in members:
        if not isinstance(member, dict) or set(member) != {'bot_id', 'role', 'permissions'}:
            raise HTTPException(400, '成员需要 bot_id、单个 role 和 permissions')
        bot_id, role = member['bot_id'], member['role']
        if type(bot_id) is not int or bot_id <= 0 or bot_id in ids:
            raise HTTPException(400, 'bot_id 必须是唯一的正整数')
        ids.add(bot_id)
        if not isinstance(role, str) or (role.strip() and role.strip() not in names):
            raise HTTPException(400, '成员必须选择一个已定义角色，或留空')
        _permission_list(member['permissions'])
    owner = body.get('owner_bot_id')
    if owner is not None and (type(owner) is not int or owner not in ids):
        raise HTTPException(400, '负责人必须是此次选择的成员，或留空')
    if 'template' in body:
        selected = body['template']
        if not isinstance(selected, dict) or set(selected) != {'id', 'version'}:
            raise HTTPException(400, '请选择模板并重新预览')
        template, _ = supplements.defaults(selected)
        owner_name = supplements.templates.owner_role_name(template)
        if owner_name not in names:
            raise HTTPException(400, f'请保留模板负责人角色：{owner_name}')
        owners = [m['bot_id'] for m in members if m['role'].strip() == owner_name]
        if len(owners) > 1:
            raise HTTPException(400, f'{owner_name}角色只能分配给一位成员')
        owner = owners[0] if owners else None
    elif body.get('supplement') or 'plan_hash' in body or 'initialize_git' in body or 'agents_edit' in body:
        raise HTTPException(400, '补齐文件需要模板与确认预览')
    return roles, members, owner


def submit(body, actor):
    with ws.LOCK:
        _config(body)
        replay = progress.begin(body)
        if replay is not None:
            return replay
        try:
            return _submit(body, actor)
        except Exception as exc:
            progress.failed(body, exc)
            raise
        finally:
            progress.ACTIVE.pop(body.get('request_id'), None)


def _submit(body, actor):
    roles, members, owner_id = _config(body)
    root = selected_root(body)
    publication = supplements.Publication(root)
    from .project_setup_git import Preparation
    preparation = Preparation(root)
    conn = db.get_connection()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            existing = matches(conn, root)
            if existing:
                raise HTTPException(409, {'code': 'directory_registered', 'message': '该目录已登记，请打开已有项目。',
                                          'existing_projects': existing})
            file_plan = supplements.plan(body, root) if 'template' in body else None
            planned_resources = []
            if 'workspace_resources' in body:
                from . import workspace_resources
                planned_resources = workspace_resources.plan(conn, {**body, 'root': root})
                if len(planned_resources) != len(body['workspace_resources']):
                    raise HTTPException(409, '请预览并确认完整工作区资源配置后提交')
            validate_location(body, root)
            if file_plan and body.get('plan_hash') != file_plan['plan_hash']:
                raise HTTPException(409, {'code': 'import_preview_changed', 'message': '导入配置或文件已变化，请返回重新预览。'})
            for member in members:
                bot = conn.execute('SELECT profile FROM bots WHERE id=?', (member['bot_id'],)).fetchone()
                if not bot or db.is_builtin_user(bot['profile']):
                    raise HTTPException(400, '所选 Bot 已失效，请重新预览')
            result = taskpool.project_add_in_transaction(
                conn, '', root, body.get('description', ''), display_name=body['display_name'],
                directory_mode=body.get('directory_mode', 'shared'), worktree_root=body.get('worktree_root', ''), actor=actor)
            if 'error' in result:
                raise HTTPException(400, result['error'])
            project = result['project']
            progress.phase(body, '配置角色、成员和项目授权')
            role_ids = {r['name'].strip(): project_team.create_role(conn, project, r['name'], actor)['id'] for r in roles}
            if 'template' in body:
                template, _ = supplements.defaults(body['template'])
                project_team.save_owner_role(conn, project, role_ids[supplements.templates.owner_role_name(template)])
            grants = {p: [] for p in permissions.PERMISSIONS}
            for role in roles:
                for p in _permission_list(role['permissions']):
                    grants[p].append(permissions._role_grant(role_ids[role['name'].strip()]))
            handles = {}
            for member in members:
                handle = bot_identity.enroll(conn, member['bot_id'], project)
                handles[member['bot_id']] = handle
                ws.audit(conn, project, actor, 'member.enroll', {'profile': handle, 'bot_id': member['bot_id']})
                project_team.assign_role(conn, project, handle, member['role'], actor)
                for p in _permission_list(member['permissions']):
                    grants[p].append(handle)
            permissions.save_in_transaction(conn, project,
                {'grants': grants, 'revision': permissions._snapshot(conn, project)['revision']}, actor)
            owner = handles.get(owner_id, '')
            taskpool._save_project_owner(conn, project, owner)
            if file_plan:
                progress.phase(body, '准备项目目录和基础文件')
                publication.publish(file_plan)
                if file_plan.get('git_plan'):
                    progress.phase(body, '准备 Git 基线提交')
                    baseline = preparation.commit_selected(file_plan['git_plan'])
                    if body.get('directory_mode') == 'worktree':
                        progress.phase(body, '准备并校验成员工作区')
                        preparation.workspaces(conn, project, handles.values(), body['worktree_root'], baseline)
                        ws.inspect(root, root)
            if 'workspace_resources' in body:
                p = ws.project(conn, project)
                for resource in planned_resources:
                    path = root if resource['key'] == 'shared' else ws.selected_path(conn, p, handles[int(resource['key'][4:])])
                    workspace_resources.save(conn, project, path, resource['config'])
            progress.phase(body, '完成项目校验')
            conn.execute("UPDATE projects SET readiness_status='ready' WHERE name=?", (project,))
            ws.audit(conn, project, actor, 'project.import', {'root': root, 'members': handles,
                     'roles': role_ids, 'owner_profile': owner, 'file_action': 'supplement' if body.get('supplement') or body.get('initialize_git') else 'preserve',
                     'template': body.get('template'), 'initialize_git': body.get('initialize_git', False),
                     'files': [{k: f[k] for k in ('path', 'action', 'sha256')} for f in (file_plan or {}).get('files', [])]})
            result = {**result, 'readiness_status': 'ready', 'owner_profile': owner, 'members': list(handles.values()),
                'file_action': 'supplement' if body.get('supplement') or body.get('initialize_git') else 'preserve'}
            if body.get('creation_mode', 'import') == 'import' and body.get('agents_edit') and body.get('git_setup'):
                result['review_reminder'] = 'AGENTS.md 已按当前模板生成，请检查项目约定。已有项目文档与测试脚本已保留，请核对其中的角色、流程及目录约定。'
                if file_plan['document_review']:
                    result['review_reminder'] += '\n以下已有文档与当前模板内容不同，请检查并按需修改：\n' + '\n'.join(file_plan['document_review'])
            progress.complete(conn, body, result)
            from .codex_project_access import commit_enrollment
            commit_enrollment(conn, project, handles.values())
        return result
    except OSError as exc:
        preparation.rollback()
        publication.rollback(preserve_git=bool(preparation.commit))
        raise HTTPException(409, {'code': 'import_files_failed', 'message': '基础文件写入失败，请检查目录权限、磁盘空间或并发修改后重新预览。'}) from exc
    except Exception:
        preparation.rollback()
        publication.rollback(preserve_git=bool(preparation.commit))
        raise
    finally:
        conn.close()
