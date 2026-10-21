"""Explicit, journalled project file initialization; legacy registration stays pure.

Content is staged in the immutable SQLite plan. Configuration commits before file
publication; every resumed file is reconciled against its expected SHA-256.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from fastapi import HTTPException

from . import db, project_permissions as permissions, project_workspaces as ws, taskpool


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _error(code, message):
    raise HTTPException(409, {'code': code, 'message': message})


def _safe(path):
    """Reject reparse points on every existing component, including junctions."""
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 1024:
            _error('unsafe_path', f'路径包含符号链接或重解析点，请选择实际目录：{part}')
    return path


def _root(raw):
    if not isinstance(raw, str) or not raw.strip() or any(ord(c) < 32 for c in raw):
        raise HTTPException(400, 'root 必须是绝对目录路径')
    raw = raw.strip()
    if os.name == 'nt' and raw.replace('/', '\\').startswith('\\\\'):
        raise HTTPException(400, '初始化请选择本机普通绝对路径')
    path = Path(raw)
    if not path.is_absolute() or '..' in path.parts:
        _error('unsafe_path', '请选择绝对目录路径，路径分段须位于选定目录内')
    _safe(path)
    path = path.resolve()
    if path.exists() and not path.is_dir():
        raise HTTPException(400, '所选路径必须是目录')
    if not path.parent.is_dir():
        raise HTTPException(400, '请选择已有父目录下的项目位置')
    return path


def _stamp(path):
    info = path.stat()
    return [info.st_dev, info.st_ino]


def _hash(path):
    _safe(path)
    if not path.exists():
        return None
    if not path.is_file():
        _error('file_conflict', f'目标应为文件：{path}')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _empty(root, mode):
    if root.exists() and any(p.name != '.git' or mode != 'worktree' for p in root.iterdir()):
        _error('import_required', '所选目录已有内容，请通过“首次导入已有项目”接入。')


def _contents(name, root):
    # New project conventions deliberately query the project's own roster.
    return {
        'AGENTS.md': f'''# {name} — 项目协作约定

项目根目录：`{root}`。项目文档入口：[docs/INDEX.md](docs/INDEX.md)。

## 开始工作

核对引擎注入的项目 ID、工作目录和任务上下文。读取任务说明、设计依据、验收标准与前置交付记录。

## 分工与协作

角色与成员的对应关系以本项目当前花名册为准。每次交接前核对接手人的当前角色，按项目流程选定具体成员。每名成员关联一个项目角色，同角色可有多名成员。角色分工与项目授权分别维护。

空白项目的成员和 Bot 授权初始为空。用户配置成员、角色、负责人和权限，并在本文件中明确拆解、执行、审查、验收与关闭的职责后，团队按约定开展协作。

任务应写清目标、设计依据、验收条件和前置依赖；前置交付完成后开展后续工作。多人方案通过话题讨论并记录结论，点对点通知用于递交工作和传达结果。交接时记录成果、验证依据、问题和具体接手人。任务与需求由本项目流程指定的负责人确认完成后关闭。

## 交付

按任务验收标准验证，说明修改、结果与边界，维护 [README.md](README.md) 和 [文档索引](docs/INDEX.md)。技术障碍先协调项目负责人；负责人未配置、不可用或协调后仍无法解决时，交用户决定。涉及范围、优先级、费用和生产外部动作时，说明影响与建议，等待用户决定。
''',
        'README.md': f'''# {name}

空白项目已初始化。请补充目标、使用方式和验证命令。

- [项目协作约定](AGENTS.md)
- [文档索引](docs/INDEX.md)

文件就绪后，在项目设置中配置成员、角色、负责人及 Bot 授权，再开始团队执行。
''',
        'docs/INDEX.md': f'''# {name} — 文档索引

- [项目说明](../README.md)
- [项目协作约定](../AGENTS.md)

后续需求、设计、验收及运行文档在此登记。
''',
    }


def _load(conn, initialization_id):
    row = conn.execute('SELECT * FROM project_initializations WHERE id=?', (initialization_id,)).fetchone()
    if row is None:
        raise HTTPException(404, '初始化记录不存在')
    return dict(row)


def _view(row, conn):
    plan = json.loads(row['plan'])
    completed = json.loads(row['completed'])
    files = [{**f, 'completed': f['path'] in completed} for f in plan['files']]
    member_count = conn.execute('SELECT COUNT(*) FROM project_members WHERE project=? AND active=1',
                                (row['project'],)).fetchone()[0] if row['project'] else 0
    grant_count = conn.execute('SELECT COUNT(*) FROM project_permissions WHERE project=?',
                               (row['project'],)).fetchone()[0] if row['project'] else 0
    return {'initialization_id': row['id'], 'revision': row['revision'], 'project': row['project'],
            'planned_project': plan['project_id'],
            'root': row['root'], 'mode': plan['mode'], 'phase': row['phase'], 'status': row['status'],
            'directory_mode': plan['config']['directory_mode'],
            'files': files, 'uncompleted': [f['path'] for f in files if not f['completed']],
            'error': json.loads(row['error']), 'files_ready': row['status'] == 'ready',
            'template': plan.get('template'), 'team': plan.get('team'), 'applied_team': plan.get('applied_team'),
            'team_configured': member_count > 0 and grant_count > 0,
            'rules_hint': '现有 AGENTS.md 已保留，协作流程按该文件执行。' if any(f['path'] == 'AGENTS.md' and f['action'] == 'preserve' for f in files) else '',
            'team_hint': '团队将按下方确认方案配置。' if plan.get('team') and row['phase'] == 'preview' else
                         '请配置成员、角色、负责人和 Bot 授权后开始团队执行。' if not member_count or not grant_count else '团队配置已存在；执行仍按具体成员权限校验。'}


def progress(initialization_id):
    conn = db.get_connection()
    try:
        return _view(_load(conn, initialization_id), conn)
    finally:
        conn.close()


def preview(body, actor):
    allowed = {'mode', 'project', 'root', 'display_name', 'description', 'directory_mode', 'worktree_root', 'replace_agents',
               'template', 'roles', 'members', 'owner_bot_id'}
    if set(body) - allowed:
        raise HTTPException(400, '包含未知初始化配置项')
    mode = body.get('mode', 'new')
    if mode not in ('new', 'supplement') or type(body.get('replace_agents', False)) is not bool:
        raise HTTPException(400, 'mode 为 new/supplement；replace_agents 为布尔值')
    if (mode != 'new' or not body.get('template')) and set(body) & {'template', 'roles', 'members', 'owner_bot_id'}:
        raise HTTPException(400, '场景和团队配置需要显式新建模板项目')
    conn = db.get_connection()
    try:
        if mode == 'supplement':
            project = body.get('project')
            if not isinstance(project, str) or not project:
                raise HTTPException(400, '补文件需要已登记 project ID')
            existing = ws.project(conn, project)
            config = {k: existing[k] for k in ('root', 'display_name', 'description', 'directory_mode', 'worktree_root')}
            config['directory_mode'] = config['directory_mode'] or 'shared'
            config['display_name'] = config['display_name'] or project
        else:
            if body.get('replace_agents') or body.get('project'):
                raise HTTPException(400, '新建项目使用空白配置')
            config = {k: body.get(k, 'shared' if k == 'directory_mode' else '') for k in
                      ('root', 'display_name', 'description', 'directory_mode', 'worktree_root')}
            project = ''
        if any(not isinstance(v, str) for v in config.values()) or not config['display_name'].strip() or len(config['display_name']) > 200:
            raise HTTPException(400, '请填写不超过 200 字的项目名，各配置字段必须为文本')
        if config['directory_mode'] not in ('shared', 'worktree'):
            raise HTTPException(400, 'directory_mode 为 shared/worktree')
        root = _root(config['root'])
        config['root'] = str(root)
        if mode == 'new':
            _empty(root, config['directory_mode'])
        if root.exists():
            ws.validate_config(config)
        elif config['directory_mode'] == 'worktree':
            raise HTTPException(409, 'worktree 模式需要已有 Git 仓库目录，请先准备仓库')
        _safe(root / 'docs')
        if (root / 'docs').exists() and not (root / 'docs').is_dir():
            _error('file_conflict', 'docs 必须为目录')
        init_id = uuid.uuid4().hex
        project_id = project or 'p_' + uuid.uuid4().hex
        template, team = None, None
        contents = _contents(config['display_name'], root)
        if body.get('template'):
            from . import project_templates
            contents, template, team = project_templates.prepare(conn, body, config, project_id)
        files = []
        directory_stamps = {}
        for relative, content in contents.items():
            from .project_templates import relative_path
            relative_path(relative)
            for directory in (root / relative).parents:
                if directory == root:
                    break
                _safe(directory)
                if directory.exists():
                    if not directory.is_dir():
                        _error('file_conflict', f'目标父路径应为目录：{directory}')
                    directory_stamps[directory.relative_to(root).as_posix()] = _stamp(directory)
            before = _hash(root / relative)
            action = 'create' if before is None else 'preserve'
            if relative == 'AGENTS.md' and before and body.get('replace_agents'):
                action = 'replace'
            files.append({'path': relative, 'action': action, 'before_hash': before,
                          'sha256': before if action == 'preserve' else _sha(content.encode('utf-8')),
                          'content': content if action != 'preserve' else '',
                          'backup': f'.AGENTS.md.init-{init_id}.bak' if action == 'replace' else ''})
        plan = {'mode': mode, 'config': config, 'files': files,
                'project_id': project_id, 'template': template, 'team': team,
                'directory_stamps': directory_stamps,
                'parent_stamp': _stamp(root.parent), 'root_stamp': _stamp(root) if root.exists() else None,
                'docs_stamp': _stamp(root / 'docs') if (root / 'docs').exists() else None}
        serialized = _json(plan)
        revision = _sha(serialized.encode('utf-8'))
        with conn:
            conn.execute('INSERT INTO project_initializations(id,project,root,plan,revision,actor,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                         (init_id, project, str(root), serialized, revision, actor, db.now_iso(), db.now_iso()))
        return _view(_load(conn, init_id), conn)
    except OSError as exc:
        raise HTTPException(409, {'code': 'filesystem_error', 'message': f'目录读取失败：{exc}'}) from exc
    finally:
        conn.close()


def _location(plan, resume=False):
    root = _root(plan['config']['root'])
    if _stamp(root.parent) != plan['parent_stamp']:
        _error('path_changed', '项目父目录在预览后发生替换，请重新预览')
    if plan['root_stamp'] is not None and (not root.exists() or _stamp(root) != plan['root_stamp']):
        _error('path_changed', '项目目录在预览后发生替换，请重新预览')
    if plan['docs_stamp'] is not None and (not (root / 'docs').exists() or _stamp(root / 'docs') != plan['docs_stamp']):
        _error('path_changed', 'docs 目录在预览后发生替换，请重新预览')
    _safe(root / 'docs')
    for relative, stamp in plan.get('directory_stamps', {}).items():
        directory = _safe(root / relative)
        if not directory.is_dir() or _stamp(directory) != stamp:
            _error('path_changed', f'初始化目录发生替换：{relative}')
    if not resume and plan['mode'] == 'new':
        _empty(root, plan['config']['directory_mode'])
    return root


def _record(conn, init_id, **fields):
    fields['updated_at'] = db.now_iso()
    conn.execute('UPDATE project_initializations SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?',
                 (*fields.values(), init_id))


def _configure(conn, row, plan, actor):
    """One BEGIN IMMEDIATE owns project, team, grants, snapshot and audit."""
    from .project_import import matches
    root = _location(plan)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        if plan['mode'] == 'new':
            if root.exists():
                ws.validate_config(plan['config'])
            existing = matches(conn, str(root))
            if existing:
                _error('directory_registered', '该目录已登记，请打开已有项目')
            config = plan['config']
            # Directory existence is handled only after configuration commits.
            result = taskpool.project_add_in_transaction(
                conn, plan['project_id'], str(root), config['description'], display_name=config['display_name'],
                directory_mode=config['directory_mode'], worktree_root=config['worktree_root'], actor=actor)
            if 'error' in result:
                raise HTTPException(400, result['error'])
            project = result['project']
            if plan.get('team'):
                from . import project_templates
                plan['applied_team'] = project_templates.apply_team(conn, project, plan['team'], actor)
            else:
                permissions.save_in_transaction(conn, project,
                    {'grants': {p: [] for p in permissions.PERMISSIONS},
                     'revision': permissions._snapshot(conn, project)['revision']}, actor)
                taskpool._save_project_owner(conn, project, '')
        else:
            project = row['project']
            current = ws.project(conn, project)
            if ws.key(current['root']) != ws.key(str(root)):
                _error('path_changed', '项目登记目录已变更，请重新预览')
            if not conn.execute("SELECT 1 FROM projects WHERE name=? AND readiness_status='ready'", (project,)).fetchone():
                _error('initialization_busy', '该项目正在初始化，请恢复原初始化记录')
            ws.ensure_idle(project)
        conn.execute("UPDATE projects SET readiness_status='initializing' WHERE name=?", (project,))
        _record(conn, row['id'], project=project, plan=_json(plan), phase='configured', status='initializing', error='{}')
        ws.audit(conn, project, actor, 'project.initialization.configure', {'initialization_id': row['id'], 'mode': plan['mode']})
        if plan['mode'] == 'new' and plan.get('team'):
            from .codex_project_access import commit_enrollment
            commit_enrollment(conn, project, plan['applied_team']['member_profiles'].values())


def _publish(path, content, temp_path, replace=False, before_hash=None):
    """Same-directory staging + atomic publication, create uses no-clobber link.

    暂存写入原子化：先写 .part 中间态，完整写 + fsync 后 rename 成 .tmp，.tmp 只在
    完整写完后出现。写 .part 中途磁盘满/进程死只残留半份 .part，重试时识别为引擎
    未完成暂存并安全重建；.tmp 一旦存在即内容完整，若其 hash 与 expected 不符则说明
    被外部修改，保留冲突提示、不静默删除。
    """
    _safe(path)
    _safe(temp_path)
    expected = _sha(content)
    if temp_path.exists():
        if _hash(temp_path) != expected:
            _error('file_conflict', f'暂存文件发生变化，请保留并检查：{temp_path}')
    else:
        part = temp_path.with_name(temp_path.name + '.part')
        _safe(part)
        if part.exists():
            # 上次写 .part 中途中断残留的半份，属引擎未完成暂存，安全重建。
            part.unlink()
        with part.open('xb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(part, temp_path)
    _safe(path)
    if replace:
        if _hash(path) != before_hash:
            _error('file_conflict', f'暂存期间文件发生变化：{path}')
        os.replace(temp_path, path)
    else:
        os.link(temp_path, path)
    # Cleanup only a deterministic staging path with this plan's full hash.
    if temp_path.exists() and _hash(temp_path) == expected:
        temp_path.unlink()


def _after_publish(initialization_id, relative):
    """Fault-injection seam: file is durable and its journal isn't updated yet."""


def _check_tree(root, plan, init_id):
    """Only planned names and our deterministic staging files in a new tree."""
    if plan['mode'] != 'new':
        return
    names = {'.': set()}
    for f in plan['files']:
        path = Path(f['path'])
        parent = path.parent.as_posix()
        names.setdefault(parent, set()).update((path.name, f'.{init_id}.{path.name}.tmp', f'.{init_id}.{path.name}.tmp.part'))
        for directory in path.parents:
            if directory == Path('.'):
                break
            names.setdefault(directory.parent.as_posix(), set()).add(directory.name)
    if plan['config']['directory_mode'] == 'worktree':
        names['.'].add('.git')
    for relative, allowed in names.items():
        directory = _safe(root / relative)
        if directory.exists():
            if not directory.is_dir() or any(p.name not in allowed for p in directory.iterdir()):
                _error('file_conflict', f'初始化期间出现计划外内容，请保留并检查：{relative}')


def _files(conn, row, plan):
    current_project = ws.project(conn, row['project'])
    for field in ('root', 'directory_mode', 'worktree_root'):
        if current_project[field] != plan['config'][field]:
            _error('configuration_changed', '项目运行目录配置已变化，请核对初始化计划')
    root = _location(plan, resume=True)
    if not root.exists():
        root.mkdir()
    _check_tree(root, plan, row['id'])
    # Record directory identities before creating/publishing their children.
    plan['root_stamp'] = _stamp(root)
    with conn:
        _record(conn, row['id'], plan=_json(plan), phase='publishing', status='initializing', error='{}')
        conn.execute("UPDATE projects SET readiness_status='initializing' WHERE name=?", (row['project'],))
    completed = json.loads(row['completed'])
    for f in plan['files']:
        root = _location(plan, resume=True)
        path = root / f['path']
        if path.parent != root:
            for directory in reversed(path.parent.parents):
                if directory == root or not directory.is_relative_to(root):
                    continue
                _safe(directory)
                if not directory.exists():
                    directory.mkdir()
                plan.setdefault('directory_stamps', {})[directory.relative_to(root).as_posix()] = _stamp(directory)
            _safe(path.parent)
            if not path.parent.exists():
                path.parent.mkdir()
            plan.setdefault('directory_stamps', {})[path.parent.relative_to(root).as_posix()] = _stamp(path.parent)
            if (root / 'docs').exists():
                plan['docs_stamp'] = _stamp(root / 'docs')
            with conn:
                _record(conn, row['id'], plan=_json(plan))
            _check_tree(root, plan, row['id'])
        current = _hash(path)
        if current == f['sha256']:
            # Recovers a crash after publication and before completion logging.
            pass
        elif f['action'] == 'preserve' or current != f['before_hash']:
            _error('file_conflict', f'文件与已确认计划冲突，请保留并检查：{f["path"]}')
        else:
            if f['action'] == 'replace':
                backup = root / f['backup']
                before = path.read_bytes()
                if _sha(before) != f['before_hash']:
                    _error('file_conflict', 'AGENTS.md 在备份前发生变化')
                if backup.exists():
                    if _hash(backup) != f['before_hash']:
                        _error('file_conflict', 'AGENTS.md 备份文件发生变化')
                else:
                    _publish(backup, before, root / f'.{row["id"]}.backup.tmp')
                if _hash(path) != f['before_hash']:
                    _error('file_conflict', 'AGENTS.md 在替换前发生变化')
            temp = path.parent / f'.{row["id"]}.{path.name}.tmp'
            _publish(path, f['content'].encode('utf-8'), temp, f['action'] == 'replace', f['before_hash'])
            _after_publish(row['id'], f['path'])
        # Reconcile leftover temp after a process died immediately after linking.
        temp = path.parent / f'.{row["id"]}.{path.name}.tmp'
        if temp.exists() and _hash(temp) == f['sha256']:
            temp.unlink()
        if f['path'] not in completed:
            completed.append(f['path'])
        with conn:
            _record(conn, row['id'], completed=_json(completed))
    with conn:
        _record(conn, row['id'], phase='verifying')
    _location(plan, resume=True)
    _check_tree(root, plan, row['id'])
    for f in plan['files']:
        if _hash(root / f['path']) != f['sha256']:
            _error('file_conflict', f'最终校验发现文件变化：{f["path"]}')
    ws.validate_config(plan['config'], current=current_project)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("UPDATE projects SET readiness_status='ready' WHERE name=?", (row['project'],))
        _record(conn, row['id'], phase='ready', status='ready', error='{}')
        ws.audit(conn, row['project'], row['actor'], 'project.initialization.ready', {'initialization_id': row['id']})


def submit(initialization_id, body, actor):
    if set(body) != {'revision'} or not isinstance(body['revision'], str):
        raise HTTPException(400, '提交需要预览中的 revision 字符串')
    # The engine has one initializer process. This lock also serializes workspace
    # dispatch/configuration so files cannot become executable before verification.
    with ws.LOCK:
        conn = db.get_connection()
        try:
            row = _load(conn, initialization_id)
            if row['phase'] == 'discarded':
                _error('discarded', '旧初始化已清理，请通过当前向导重新配置')
            if body['revision'] != row['revision']:
                _error('preview_changed', '确认版本已失效，请重新预览')
            if row['status'] == 'ready':
                return _view(row, conn)
            plan = json.loads(row['plan'])
            try:
                if row['phase'] == 'preview':
                    for f in plan['files']:
                        if _hash(Path(row['root']) / f['path']) != f['before_hash']:
                            _error('file_conflict', f'预览后文件发生变化：{f["path"]}')
                    _configure(conn, row, plan, actor)
                    row = _load(conn, initialization_id)
                _files(conn, row, plan)
            except Exception as exc:
                conn.rollback()
                detail = getattr(exc, 'detail', None)
                error = detail if isinstance(detail, dict) else {
                    'code': 'filesystem_error' if isinstance(exc, OSError) else 'initialization_error',
                    'message': str(detail or exc)}
                # Configuration rolls back as one unit. Only the intent journal
                # survives, and a committed partial project remains non-ready.
                current = _load(conn, initialization_id)
                with conn:
                    if current['project'] and current['phase'] != 'preview':
                        conn.execute("UPDATE projects SET readiness_status='failed' WHERE name=?", (current['project'],))
                    _record(conn, initialization_id, status='failed', error=_json(error))
            return _view(_load(conn, initialization_id), conn)
        finally:
            conn.close()
