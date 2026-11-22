"""Prepare Git workspaces on the machine that owns the Hermes gateway."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import subprocess
import tempfile
import threading
from weakref import WeakValueDictionary
from urllib.parse import urlsplit


class WorkspaceError(ValueError):
    pass


_lock = threading.Lock()
_repositories = WeakValueDictionary()
_workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix='relay-workspace')


def root_directory(value):
    if (not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value)
            or not Path(value).is_absolute()):
        raise WorkspaceError('请输入远程主机上的绝对路径')
    root = Path(value).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=root):
        pass
    return root


def _git(directory, *args, check=True):
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0'}
    for key in ('GIT_DIR', 'GIT_COMMON_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_OBJECT_DIRECTORY'):
        env.pop(key, None)
    env.setdefault('GIT_ALLOW_PROTOCOL', 'https:ssh')
    env.setdefault('GIT_SSH_COMMAND', 'ssh -o BatchMode=yes')
    result = subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull, *args],
                            cwd=directory, env=env, capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=90)
    if check and result.returncode:
        raise WorkspaceError(f'远端 Git {args[0]} 失败，请检查仓库、访问权限和工作区状态')
    return result


def _component(name):
    if not isinstance(name, str) or not re.fullmatch(r'[\w][\w.-]{0,127}', name) or name.endswith('.'):
        raise WorkspaceError('项目或成员标识不能用作目录名')
    return name


def _origin(value):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise WorkspaceError('仓库需要可由远端访问的 HTTPS 或 SSH 地址')
    if re.fullmatch(r'[\w.-]+@[\w.-]+:[\w./-]+', value):
        return value
    url = urlsplit(value)
    if (url.scheme not in ('https', 'ssh') or not url.hostname or not url.path or url.query
            or url.fragment or url.password or (url.scheme == 'https' and url.username)):
        raise WorkspaceError('仓库需要不含凭据的 HTTPS 或 SSH 地址')
    return value


def _child(parent, name):
    path = parent / name
    if path.resolve().parent != parent or path.is_symlink():
        raise WorkspaceError('远端目录指向了其他位置')
    return path


def _checkout(path, branch):
    if (Path(_git(path, 'rev-parse', '--show-toplevel').stdout.strip()).resolve() != path
            or _git(path, 'branch', '--show-current').stdout.strip() != branch):
        raise WorkspaceError('已有目录的仓库或分支不匹配，请核对远端工作区')


def _advance(path, baseline):
    if _git(path, 'merge-base', '--is-ancestor', baseline, 'HEAD', check=False).returncode == 0:
        return
    if _git(path, 'status', '--porcelain').stdout:
        raise WorkspaceError('远端有未提交改动，请处理后再更新基线')
    if _git(path, 'merge-base', '--is-ancestor', 'HEAD', baseline, check=False).returncode:
        raise WorkspaceError('远端分支与项目基线已分叉，请完成分支合并后重试')
    _git(path, 'merge', '--ff-only', '--no-overwrite-ignore', baseline)


def prepare(spec):
    root = root_directory(spec['root'])
    if spec['action'] == 'validate':
        return {'root': str(root)}
    if spec['action'] != 'prepare' or spec.get('mode') not in ('worktree', 'shared'):
        raise WorkspaceError('远端工作区模式无效')
    project, member = _component(spec['project']), _component(spec['member'])
    baseline, origin = spec['baseline'], _origin(spec['origin'])
    if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', baseline):
        raise WorkspaceError('项目基线无效')
    main = _child(root, project)
    with _lock:
        guard = _repositories.get(main)
        if guard is None:
            guard = _repositories[main] = threading.Lock()
    with guard:
        return _prepare_checkout(root, main, project, member, spec['mode'], origin, baseline)


def _prepare_checkout(root, main, project, member, mode, origin, baseline):
    if not main.exists():
        with tempfile.TemporaryDirectory(dir=root, prefix='.relay-clone-') as stage:
            clone = Path(stage) / 'repo'
            _git(root, 'clone', '--no-checkout', '--branch', 'master', '--single-branch', '--', origin, str(clone))
            if _git(clone, 'merge-base', '--is-ancestor', baseline, 'origin/master', check=False).returncode:
                raise WorkspaceError('远端 master 缺少办公机基线 ' + baseline[:12] + '，请先同步项目仓库')
            _git(clone, 'checkout', '-B', 'master', baseline)
            clone.rename(main)
    _checkout(main, 'master')
    if _git(main, 'config', '--get', 'remote.origin.url').stdout.strip() != origin:
        raise WorkspaceError('远端已有同名目录，其 origin 与项目不一致')
    if _git(main, 'merge-base', '--is-ancestor', baseline, 'HEAD', check=False).returncode:
        _git(main, 'fetch', 'origin', 'refs/heads/master')
        if _git(main, 'merge-base', '--is-ancestor', baseline, 'FETCH_HEAD', check=False).returncode:
            raise WorkspaceError('远端 master 缺少办公机基线 ' + baseline[:12] + '，请先同步项目仓库')
        _advance(main, baseline)
    path, branch = main, 'master'
    if mode == 'worktree':
        parent = _child(root, '.bot-group-worktrees')
        parent = _child(parent, project)
        parent.mkdir(parents=True, exist_ok=True)
        path = _child(parent, member)
        branch = 'bot-group/' + member
        if not path.exists():
            exists = _git(main, 'show-ref', '--verify', '--quiet', 'refs/heads/' + branch, check=False)
            args = [str(path), branch] if exists.returncode == 0 else ['-b', branch, str(path), baseline]
            _git(main, 'worktree', 'add', *args)
        _checkout(path, branch)
        common = ('rev-parse', '--path-format=absolute', '--git-common-dir')
        if _git(path, *common).stdout.strip() != _git(main, *common).stdout.strip():
            raise WorkspaceError('远端 worktree 属于其他仓库')
        _advance(path, baseline)
    return {'root': str(root), 'main': str(main), 'path': str(path), 'branch': branch,
            'head': _git(path, 'rev-parse', 'HEAD').stdout.strip(),
            'dirty': bool(_git(path, 'status', '--porcelain').stdout), 'valid': True}


def respond(ws, meta, spec):
    def run():
        try:
            result = prepare(spec)
        except WorkspaceError as exc:
            result = {'error': str(exc)}
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
            result = {'error': '远端工作区准备失败，请检查路径权限、Git 安装和连接超时'}
        try:
            ws.send(json.dumps({'relay': {**meta, 'kind': 'workspace_result'}, 'workspace': result}))
        except Exception:
            pass  # The next connection revalidates the on-disk checkout.
    _workers.submit(run)
