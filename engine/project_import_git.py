"""Local Git discovery and isolated initialization for reviewed imports."""
import os
from pathlib import Path
import subprocess
import tempfile

from fastapi import HTTPException
from . import project_initialization as files

GITIGNORE = '# Test and technical research reports\n/reports/\n\n# Local environment and credentials\n.env\n.env.*\n!.env.example\n!.env.sample\n\n# Dependencies and caches\nnode_modules/\n.venv/\nvenv/\n__pycache__/\n*.py[cod]\n.pytest_cache/\n\n# Local OS files\n.DS_Store\nThumbs.db\n'


def run(root, *arguments, env=None, input=None):
    environment = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    environment['GIT_OPTIONAL_LOCKS'] = '0'
    environment['GIT_TERMINAL_PROMPT'] = '0'
    environment.update(env or {})
    return subprocess.run(['git', '-C', str(root), *arguments], env=environment,
                          input=input, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def inspect(root):
    root = Path(root)
    try:
        if not root.exists():
            parent = inspect(root.parent)
            if parent['status'] == 'existing':
                return {**parent, 'status': 'parent'}
            return parent if parent['status'] in ('parent', 'invalid', 'unavailable') else {'status': 'missing', 'has_commit': False}
        result = run(root, 'rev-parse', '--show-toplevel')
        if result.returncode:
            marker = any(os.path.lexists(parent / '.git') for parent in (root, *root.parents))
            marker = marker or run(root, 'rev-parse', '--is-bare-repository').stdout.strip() == 'true'
            return {'status': 'invalid' if marker else 'missing', 'has_commit': False}
        top = Path(result.stdout.strip()).resolve()
        return {'status': 'existing' if top == root.resolve() else 'parent', 'root': str(top),
                'has_commit': run(root, 'rev-parse', '--verify', 'HEAD^{commit}').returncode == 0}
    except (OSError, subprocess.TimeoutExpired):
        return {'status': 'unavailable', 'has_commit': False}


def initialize(publication, branch=None):
    root = publication.root
    if inspect(root)['status'] != 'missing':
        raise HTTPException(409, 'Git 状态已变化，请重新预览')
    # Build metadata away from the project, then exclusively publish each file.
    with tempfile.TemporaryDirectory(prefix='import-git-') as temporary:
        staging = Path(temporary)
        empty = staging / 'empty-template'
        empty.mkdir()
        try:
            result = run(staging, 'init', '--template=' + str(empty), *(['-b', branch] if branch else []))
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(409, 'Git 初始化超时，请重新预览') from exc
        if result.returncode:
            raise HTTPException(409, '本地 Git 初始化失败，请检查 Git 配置')
        target = files._safe(root / '.git')
        target.mkdir()
        publication.directories.append(target)
        for source in sorted((staging / '.git').rglob('*'), key=lambda p: (len(p.parts), str(p))):
            destination = files._safe(target / source.relative_to(staging / '.git'))
            if source.is_dir():
                destination.mkdir()
                publication.directories.append(destination)
            else:
                content = source.read_bytes()
                fd, temporary_file = tempfile.mkstemp(prefix='import-', dir=destination.parent)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.link(temporary_file, destination)
                    publication.created.append((destination, files._sha(content), files._stamp(destination)))
                finally:
                    Path(temporary_file).unlink(missing_ok=True)
        if inspect(root)['status'] != 'existing':
            raise HTTPException(409, 'Git 初始化验证失败，请重新预览')
