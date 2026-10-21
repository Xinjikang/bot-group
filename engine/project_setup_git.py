"""Reviewed Git baselines for the shared six-step project wizard.

Preview never writes to the project. Commits use an isolated index and explicit
file selection; compare-and-swap protects the branch and the user's index.
"""
import difflib
import fnmatch
import os
from pathlib import Path, PurePosixPath
import tempfile

from fastapi import HTTPException
from . import project_import_git as git, project_initialization as files
from . import project_workspaces as ws

EXCLUDED = {'.git', 'node_modules', '.venv', 'venv', '__pycache__', '.pytest_cache',
            'dist', 'build', '.next', 'coverage', 'cache'}


def checked(root, *args, **kwargs):
    result = git.run(root, '-c', 'core.quotePath=false', '-c', 'core.hooksPath=/dev/null', *args, **kwargs)
    if result.returncode:
        raise HTTPException(409, result.stderr.strip() or 'Git 操作失败，请重新检查')
    return result.stdout.strip()


def excluded(path):
    name = PurePosixPath(path).name.lower()
    return (PurePosixPath(path).parts[0].lower() == 'reports'
            or any(p.lower() in EXCLUDED for p in PurePosixPath(path).parts)
            or (name.startswith('.env') and name not in ('.env.example', '.env.sample'))
            or any(fnmatch.fnmatch(name, pattern) for pattern in
                   ('*.pem', '*.key', '*.p12', '*.pfx', 'id_rsa*', 'id_ed25519*', '*credentials*', '*secret*', '*.pyc', '*.log')))


def relative(value):
    if (not isinstance(value, str) or not value or '\\' in value or ':' in value
            or any(ord(c) < 32 for c in value)):
        raise HTTPException(400, '提交文件路径无效')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('.', '..', '.git') for p in path.parts) or path.as_posix() != value:
        raise HTTPException(400, '提交文件路径须位于项目目录内')
    return value


def snapshot(root, entries, state):
    base = Path(root)
    head = checked(root, 'rev-parse', 'HEAD') if state.get('has_commit') else None
    names = set()
    tracked = set()
    changed = set()
    if state['status'] == 'existing':
        tracked = set(checked(root, 'ls-files', '-z').split('\0')) - {''}
        changed = (set(checked(root, 'diff', '--name-only', '-z', head, '--').split('\0')) - {''}) if head else tracked
        names = changed | (set(checked(root, 'ls-files', '--others', '--exclude-standard', '-z').split('\0')) - {''})
    elif base.exists():
        for current, dirs, filenames in os.walk(base, followlinks=False):
            dirs[:] = [d for d in dirs if d.lower() not in EXCLUDED and not (Path(current) / d).is_symlink()]
            names.update((Path(current) / n).relative_to(base).as_posix() for n in filenames)
            if len(names) > 5000:
                raise HTTPException(400, '待审阅文件超过 5000 个，请先整理目录或补充 .gitignore')
    ignored = set()
    if state['status'] == 'missing' and base.exists() and names:
        with tempfile.TemporaryDirectory(prefix='project-ignore-') as temp:
            checked(temp, 'init', '--template=')
            result = git.run(base, '--git-dir=' + str(Path(temp) / '.git'), '--work-tree=' + str(base),
                             'check-ignore', '--no-index', '--stdin', '-z', input='\0'.join(sorted(names)) + '\0')
            if result.returncode not in (0, 1):
                raise HTTPException(409, '无法检查 .gitignore，请检查仓库规则')
            ignored = set(result.stdout.split('\0')) - {''}
            names -= ignored
    planned = {f['path']: f for f in entries if f['action'] != 'preserve'}
    names.update(planned)
    if len(names) > 5000:
        raise HTTPException(400, '待审阅文件超过 5000 个，请先补充 .gitignore')
    result = []
    for name in sorted(names):
        relative(name)
        path = files._safe(base / name)
        if path.is_dir():
            raise HTTPException(400, '提交范围包含子模块或嵌套仓库，请先调整范围：' + name)
        original = git.run(root, 'show', f'{head}:{name}').stdout if head and name in (tracked | changed) else ''
        digest = files._hash(path)
        item = planned.get(name)
        if item:
            content, after = item['content'], item['sha256']
        elif digest is not None:
            content = path.read_bytes()[:32768].decode('utf-8', errors='replace')
            after = digest
        else:
            content, after = '', None
        result.append({'path': name, 'before_sha256': digest, 'sha256': after,
                       'selected': not excluded(name) and not name.startswith('AGENTS.md.backup-'),
                       'excluded': excluded(name), 'deleted': after is None,
                       'diff': ''.join(difflib.unified_diff(original[:32768].splitlines(True), content[:32768].splitlines(True),
                                                         fromfile='原/' + name, tofile='新/' + name))[:40000]})
    return head, result


def plan(body, root, entries, state):
    if state['status'] not in ('missing', 'existing'):
        raise HTTPException(409, {'parent': '请选择仓库根目录后重新检查',
                                  'invalid': 'Git 仓库检查失败，请修复后重新检查',
                                  'unavailable': 'Git 命令不可用，请安装或修复后重新检查'}[state['status']])
    head, candidates = snapshot(root, entries, state)
    baseline = body.get('git_baseline', 'existing' if head else 'commit')
    if baseline not in ('existing', 'commit') or baseline == 'existing' and not head:
        raise HTTPException(400, '请选择有效的 Git 基线；首次使用需要创建提交')
    selected = body.get('commit_files')
    if selected is None:
        selected = [f['path'] for f in candidates if f['selected']]
    if not isinstance(selected, list) or any(not isinstance(p, str) for p in selected) or len(set(selected)) != len(selected):
        raise HTTPException(400, '提交范围必须是唯一文件路径列表')
    available = {f['path'] for f in candidates}
    if any(relative(p) not in available for p in selected):
        raise HTTPException(409, '提交文件范围已变化，请重新预览')
    if any(PurePosixPath(p).parts[0].lower() == 'reports' for p in selected):
        raise HTTPException(400, 'reports/ 保存测试、技术调研报告和现场证据，请通过任务记录交付并随项目备份')
    reviewed = body.get('reviewed_files', sorted(available))
    if not isinstance(reviewed, list) or any(not isinstance(p, str) for p in reviewed):
        raise HTTPException(400, '已审阅文件范围必须为路径列表')
    # A later AGENTS edit can introduce a generated change after step two.
    # Include it in the final review while preserving explicit unchecked items.
    selected = sorted(set(selected) | {f['path'] for f in entries
                      if f['action'] != 'preserve' and f['path'] not in reviewed and f['path'] in available
                      and not excluded(f['path']) and not f['path'].startswith('AGENTS.md.backup-')})
    for item in candidates:
        item['selected'] = item['path'] in selected
    branch = (git.run(root, 'symbolic-ref', '-q', 'HEAD').stdout.strip() or 'HEAD') if state['status'] == 'existing' else 'refs/heads/main'
    return {'baseline': baseline, 'head': head, 'branch': branch, 'files': candidates,
            'selected': sorted(selected) if baseline == 'commit' else [],
            'initialize': state['status'] == 'missing',
            'identity': 'Project Setup <project-setup@localhost>'}


class Preparation:
    def __init__(self, root):
        self.root = Path(root)
        self.commit = None
        self.worktrees = []
        self.parents = []
        self.index = None

    def commit_selected(self, plan):
        if (git.run(self.root, 'symbolic-ref', '-q', 'HEAD').stdout.strip() or 'HEAD') != plan['branch']:
            raise HTTPException(409, '当前分支已变化，请重新预览')
        if plan['baseline'] == 'existing':
            if checked(self.root, 'rev-parse', 'HEAD') != plan['head']:
                raise HTTPException(409, '基线提交已变化，请重新预览')
            return plan['head']
        ref = plan['branch']
        actual = git.run(self.root, 'rev-parse', '--verify', 'HEAD').stdout.strip() or None
        if actual != plan['head']:
            raise HTTPException(409, '基线提交已变化，请重新预览')
        for item in plan['files']:
            if item['path'] in plan['selected'] and files._hash(files._safe(self.root / item['path'])) != item['sha256']:
                raise HTTPException(409, '待提交文件已变化，请重新预览：' + item['path'])
        index = Path(checked(self.root, 'rev-parse', '--path-format=absolute', '--git-path', 'index'))
        before = index.read_bytes() if index.exists() else None
        with tempfile.TemporaryDirectory(prefix='project-commit-') as temp:
            isolated = Path(temp) / 'index'
            env = {'GIT_INDEX_FILE': str(isolated)}
            checked(self.root, 'read-tree', plan['head'] or '--empty', env=env)
            for name in plan['selected']:
                relative(name)
                files._safe(self.root / name)
            paths = '\0'.join(plan['selected']) + '\0'
            if plan['selected']:
                checked(self.root, '--literal-pathspecs', 'add', '-A', '-f', '--pathspec-from-file=-', '--pathspec-file-nul',
                        input=paths, env=env)
            for item in plan['files']:
                if item['path'] in plan['selected'] and files._hash(files._safe(self.root / item['path'])) != item['sha256']:
                    raise HTTPException(409, '暂存期间文件已变化，请重新预览：' + item['path'])
            tree = checked(self.root, 'write-tree', env=env)
            commit = checked(self.root, '-c', 'user.name=Project Setup', '-c', 'user.email=project-setup@localhost',
                             'commit-tree', tree, *(['-p', plan['head']] if plan['head'] else []),
                             '-m', 'Prepare project workspace')
            # Preserve unrelated staged changes while refreshing selected entries.
            isolated.unlink(missing_ok=True)
            if before is not None:
                isolated.write_bytes(before)
            else:
                checked(self.root, 'read-tree', '--empty', env=env)
            if plan['selected']:
                checked(self.root, '--literal-pathspecs', 'reset', commit, '--pathspec-from-file=-', '--pathspec-file-nul',
                        input=paths, env=env)
            after = isolated.read_bytes()
            lock = index.with_name(index.name + '.lock')
            with lock.open('xb') as stream:
                try:
                    if (index.read_bytes() if index.exists() else None) != before:
                        raise HTTPException(409, 'Git 暂存区已变化，请重新预览')
                    checked(self.root, 'update-ref', ref, commit, plan['head'] or '0' * len(commit))
                    self.commit = (ref, commit, plan['head'])
                    self.index = (index, before, after)
                    stream.write(after)
                    stream.flush()
                    os.fsync(stream.fileno())
                except Exception:
                    stream.close()
                    lock.unlink(missing_ok=True)
                    raise
            try:
                os.replace(lock, index)
            finally:
                lock.unlink(missing_ok=True)
        return commit

    def workspaces(self, conn, project, handles, parent, commit):
        parent = files._safe(Path(ws.canonical(parent)))
        if not parent.exists():
            parent.mkdir()
            self.parents.append(parent)
        for handle in handles:
            if Path(handle).name != handle or handle in ('.', '..') or ':' in handle or '\\' in handle:
                raise HTTPException(400, '成员工作区标识无效')
            path = files._safe(parent / handle)
            if path.exists():
                raise HTTPException(409, '成员工作区路径已存在：' + str(path))
            checked(self.root, 'worktree', 'add', '--detach', str(path), commit)
            self.worktrees.append(path)
            ws.inspect(self.root, path)
            conn.execute('INSERT INTO project_workspaces(project,profile,path) VALUES(?,?,?)', (project, handle, str(path)))
            conn.execute("INSERT INTO project_workspace_modes(project,profile,mode) VALUES(?,?,'worktree')", (project, handle))

    def rollback(self):
        for path in reversed(self.worktrees):
            try:
                checked(self.root, 'worktree', 'remove', str(path))
            except (HTTPException, OSError):
                return  # Preserve a concurrently modified worktree and its baseline.
        for path in reversed(self.parents):
            try:
                path.rmdir()
            except OSError:
                pass
        if self.commit:
            ref, commit, before = self.commit
            try:
                index, original, after = self.index
                current = index.read_bytes() if index.exists() else None
                if current not in (original, after):
                    return
                lock = index.with_name(index.name + '.lock')
                with lock.open('xb') as stream:
                    try:
                        if (index.read_bytes() if index.exists() else None) != current:
                            raise HTTPException(409, '暂存区已被其他进程修改')
                        if before:
                            checked(self.root, 'update-ref', ref, before, commit)
                        else:
                            checked(self.root, 'update-ref', '-d', ref, commit)
                        stream.write(original or b'')
                    except Exception:
                        stream.close()
                        lock.unlink(missing_ok=True)
                        raise
                if original is None:
                    index.unlink(missing_ok=True)
                    lock.unlink()
                else:
                    os.replace(lock, index)
            except (HTTPException, OSError):
                pass
