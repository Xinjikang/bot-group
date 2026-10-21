"""Reviewed supplements and backed-up AGENTS.md edits for an existing project."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import difflib

from fastapi import HTTPException
from . import project_initialization as files, project_templates as templates
from . import project_import_git as git

OWNER_ROLE = '项目负责人'
OWNER_PERMISSIONS = ['req.create', 'req.update', 'req.resume', 'req.complete', 'req.cancel']


def defaults(selected):
    if selected['id'] == 'blank':
        contents = files._contents('{{PROJECT_NAME}}', '{{PROJECT_ROOT}}')
        contents['AGENTS.md'] = '''# {{PROJECT_NAME}} — 项目协作约定

项目根目录：{{PROJECT_ROOT}}
项目文档入口：[docs/INDEX.md](docs/INDEX.md)。

## 角色与授权

{{ROLE_TABLE}}

角色与成员的对应关系以本项目当前花名册为准。交接前核对接手人的当前角色和授权。
按需求确认范围和交付物，由项目负责人协调分工；成员完成必要自测并说明结果，再按确认的流程审查和验收。

## 工作目录

{{WORKSPACE_RULES}}

开始任务前核对引擎注入的项目 ID、实际目录和任务上下文。任务引用、状态与交接以引擎记录为准。
涉及范围、费用和外部发布时按项目授权处理。文档、验证记录和使用说明随成果更新。
'''
        contents['README.md'] = '''# {{PROJECT_NAME}}

{{PROJECT_DESCRIPTION}}

- [协作规则](AGENTS.md)
- [文档入口](docs/INDEX.md)

团队配置以项目设置中的成员、角色及负责人为准；请补充本项目的使用方式与验证命令。
'''
        template = {'id': 'blank', 'version': '1.0.0', 'roles': [], 'directory_modes': ['shared', 'worktree'],
                    'contents': contents}
    else:
        template = templates.load(selected['id'], selected.get('version'))
    owner_name = templates.owner_role_name(template)
    owner = next((r for r in template['roles'] if r['name'] == owner_name), None)
    return template, [{'name': owner_name, 'permissions': (owner['permissions'] if owner else OWNER_PERMISSIONS).copy()},
                      *[{'name': r['name'], 'permissions': r['permissions']} for r in template['roles'] if r['name'] != owner_name]]


def plan(body, root):
    selected = body.get('template')
    if not isinstance(selected, dict) or set(selected) != {'id', 'version'}:
        raise HTTPException(400, '请选择模板并重新预览')
    template, _ = defaults(selected)
    if selected['version'] != template['version']:
        raise HTTPException(409, '模板版本已变化，请重新预览')
    mode = body.get('directory_mode', 'shared')
    if mode not in template['directory_modes']:
        raise HTTPException(400, '模板不支持此协作方式')
    if type(body.get('supplement', False)) is not bool:
        raise HTTPException(400, 'supplement 必须为布尔值')
    base = files._safe(Path(root))
    initialize_git = body.get('initialize_git', False)
    if type(initialize_git) is not bool:
        raise HTTPException(400, 'initialize_git 必须为布尔值')
    git_state = git.inspect(root) if template['id'] == 'software' or body.get('git_setup') else None
    if initialize_git and (git_state is None or git_state['status'] != 'missing'):
        raise HTTPException(409, '仅尚未建立仓库的软件项目可以初始化 Git，请重新预览')
    if mode == 'worktree' and not body.get('git_setup') and git_state and (git_state['status'] != 'existing' or not git_state['has_commit']):
        raise HTTPException(400, 'worktree 模式需要项目根目录已有 Git 仓库及初始提交。请先使用共用目录完成导入，手动创建初始提交后再切换。')
    document_dir = 'doc' if (base / 'doc').is_dir() and not (base / 'docs').exists() else 'docs'
    roles = body.get('roles', [])
    responsibilities = {r['name']: r['responsibilities'] for r in template['roles']}
    responsibilities.setdefault(templates.owner_role_name(template), '管理需求、协调成员和跟进交付；按当前项目流程开展验收')
    variables = {'PROJECT_NAME': body['display_name'], 'PROJECT_DESCRIPTION': body.get('description', ''),
                 'PROJECT_ID': '以引擎注入的稳定项目 ID 为准', 'PROJECT_ROOT': root, 'DIRECTORY_MODE': mode,
                 'ROLE_TABLE': '\n'.join(f"- {r['name']}：{responsibilities.get(r['name'], '按项目约定分工')}；默认权限：{', '.join(r['permissions']) or '无'}。" for r in roles),
                 'WORKSPACE_RULES': templates.workspace_rules(template['id'], mode, template['roles']),
                 **{f'ROLE_{r["id"].upper()}': r['name'] for r in template['roles']}}
    entries, directories = [], set()
    agents_path = files._safe(base / 'AGENTS.md')
    original_hash = files._hash(agents_path)
    if original_hash is not None:
        if agents_path.stat().st_size > 131072:
            raise HTTPException(400, 'AGENTS.md 超过 128 KiB，请先在本地整理后导入')
        try:
            original = agents_path.read_bytes().decode('utf-8')
        except UnicodeError as exc:
            raise HTTPException(400, 'AGENTS.md 需要 UTF-8 编码，请先在本地转换') from exc
    else:
        original = ''
    draft = original if original_hash is not None and not body.get('git_setup') else templates.render(
        template['contents']['AGENTS.md'].replace('docs/', document_dir + '/'), variables,
        {r['id'].upper() for r in template['roles']})
    edit = body.get('agents_edit')
    if edit is not None:
        if not isinstance(edit, dict) or set(edit) != {'content', 'original_sha256'}:
            raise HTTPException(400, 'AGENTS.md 编辑需要正文和原文件指纹')
        content = edit['content']
        if not isinstance(content, str) or not content.strip() or len(content.encode('utf-8')) > 131072:
            raise HTTPException(400, 'AGENTS.md 正文不能为空且不能超过 128 KiB')
        if edit['original_sha256'] != original_hash:
            raise HTTPException(409, 'AGENTS.md 已被其他程序修改，请重新读取目录')
        digest = files._sha(content.encode('utf-8'))
        action = 'preserve' if digest == original_hash else 'replace' if original_hash else 'create'
        backup = 'AGENTS.md.backup-' + original_hash if action == 'replace' else None
        if backup:
            backup_hash = files._hash(files._safe(base / backup))
            if backup_hash is not None and backup_hash != original_hash:
                raise HTTPException(409, 'AGENTS.md 备份路径已被占用')
            entries.append({'path': backup, 'action': 'preserve' if backup_hash else 'create',
                            'sha256': original_hash, 'content': original})
        entries.append({'path': 'AGENTS.md', 'action': action, 'sha256': digest,
                        'original_sha256': original_hash, 'content': content,
                        'backup': backup, 'diff': ''.join(difflib.unified_diff(
                            original.splitlines(keepends=True), content.splitlines(keepends=True),
                            fromfile='原 AGENTS.md', tofile='确认后的 AGENTS.md'))})
    contents = dict(template['contents']) if body.get('supplement') else {}
    if edit is not None:
        contents.pop('AGENTS.md', None)
    if (template['id'] == 'software' and body.get('supplement')) or initialize_git:
        contents['.gitignore'] = git.GITIGNORE
    for target, content in contents.items():
        target = target.replace('docs/', document_dir + '/', 1) if target.startswith('docs/') else target
        path = base / '.gitignore' if target == '.gitignore' else base.joinpath(*templates.relative_path(target).parts)
        files._safe(path)
        if document_dir == 'doc':
            content = content.replace('docs/', 'doc/')
        content = templates.render(content, variables, {r['id'].upper() for r in template['roles']})
        existing = files._hash(path)
        entries.append({'path': target, 'action': 'preserve' if existing is not None else 'create',
                        'sha256': existing or files._sha(content.encode('utf-8')),
                        'content': '' if existing is not None else content})
        for parent in path.parents:
            if parent == base:
                break
            if parent.exists() and not parent.is_dir():
                raise HTTPException(409, '模板目录与现有文件冲突：' + str(parent))
            if not parent.exists():
                directories.add(parent.relative_to(base).as_posix())
    document_review = []
    for target, content in template['contents'].items():
        if target == 'AGENTS.md' or not target.endswith('.md'):
            continue
        target = target.replace('docs/', document_dir + '/', 1) if target.startswith('docs/') else target
        path = base.joinpath(*templates.relative_path(target).parts)
        if path.is_file():
            if document_dir == 'doc':
                content = content.replace('docs/', 'doc/')
            expected = templates.render(content, variables, {r['id'].upper() for r in template['roles']})
            if files._hash(path) != files._sha(expected.encode('utf-8')):
                document_review.append(target)
    result = {'files': entries, 'document_review': document_review, 'directories': sorted(directories), 'root_stamp': files._stamp(base) if base.exists() else None,
              'parent_stamp': files._stamp(base.parent),
              'agents_draft': {'content': draft, 'original_sha256': original_hash},
              'git': git_state, 'initialize_git': initialize_git}
    if body.get('git_setup') and (mode == 'worktree' or body.get('creation_mode') == 'new' and initialize_git):
        from . import project_setup_git
        result['git_plan'] = project_setup_git.plan(body, root, entries, git_state)
    configuration = {k: v for k, v in body.items() if k not in ('plan_hash', 'request_id')}
    digest = hashlib.sha256(json.dumps([configuration, result], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {**result, 'plan_hash': digest}


class Publication:
    """Only removes this attempt's unchanged files on a failed DB transaction."""
    def __init__(self, root):
        self.root = Path(root)
        self.created = []
        self.directories = []
        self.replaced = []

    def publish(self, plan):
        if plan['root_stamp'] is None:
            if files._stamp(files._safe(self.root.parent)) != plan['parent_stamp']:
                raise HTTPException(409, '父目录已变化，请重新预览')
            self.root.mkdir()
            self.directories.append(self.root)
            plan = {**plan, 'root_stamp': files._stamp(self.root)}
        if files._stamp(files._safe(self.root)) != plan['root_stamp']:
            raise HTTPException(409, '项目目录已变化，请重新预览')
        for relative in sorted(plan['directories'], key=lambda p: (p.count('/'), p)):
            directory = files._safe(self.root / relative)
            directory.mkdir()  # A concurrent creator requires a new preview.
            self.directories.append(directory)
        for item in plan['files']:
            path = files._safe(self.root / item['path'])
            if item['action'] == 'preserve':
                if files._hash(path) != item['sha256']:
                    raise HTTPException(409, '已有文件发生变化，请重新预览')
                continue
            # Publish a complete file atomically, with an exclusive destination.
            fd, temporary = tempfile.mkstemp(prefix='.import-', dir=path.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(item['content'].encode('utf-8'))
                    stream.flush()
                    os.fsync(stream.fileno())
                files._safe(path)
                if item['action'] == 'replace':
                    before = path.read_bytes()
                    if files._sha(before) != item['original_sha256']:
                        raise HTTPException(409, 'AGENTS.md 已变化，请重新预览')
                    os.replace(temporary, path)
                    self.replaced.append((path, before, item['sha256'], files._stamp(path), self.root / item['backup']))
                else:
                    os.link(temporary, path)
                    self.created.append((path, item['sha256'], files._stamp(path)))
            finally:
                Path(temporary).unlink(missing_ok=True)
        if plan.get('initialize_git'):
            git.initialize(self, branch='main' if plan.get('git_plan') else None)

    def rollback(self, preserve_git=False):
        protected = set()
        for path, before, digest, stamp, backup in reversed(self.replaced):
            restored = False
            try:
                if files._stamp(files._safe(path)) == stamp and files._hash(path) == digest:
                    fd, temporary = tempfile.mkstemp(prefix='.import-rollback-', dir=path.parent)
                    try:
                        with os.fdopen(fd, 'wb') as stream:
                            stream.write(before)
                        os.replace(temporary, path)
                        restored = True
                    finally:
                        Path(temporary).unlink(missing_ok=True)
            except (OSError, HTTPException):
                pass
            if not restored:
                protected.add(backup)
        for path, digest, stamp in reversed(self.created):
            if preserve_git and (self.root / '.git') in path.parents:
                continue
            if path in protected:
                continue
            try:
                if files._stamp(files._safe(path)) == stamp and files._hash(path) == digest:
                    path.unlink()
            except (OSError, HTTPException):
                pass  # Preserve files modified by another writer.
        for directory in reversed(self.directories):
            if preserve_git and (directory == self.root / '.git' or (self.root / '.git') in directory.parents):
                continue
            try:
                files._safe(directory).rmdir()
            except (OSError, HTTPException):
                pass
