"""Identify the running source tree without changing Git refs or the worktree."""
import os
from pathlib import Path
import re
import subprocess
import time

from . import config


def version_key(value):
    match = re.fullmatch(r'[vV]?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value)
    return tuple(map(int, match.groups())) if match else None


def _git(root, *args, timeout=5):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=timeout,
                            env=env, **options)
    if result.returncode:
        raise ValueError('Git metadata unavailable')
    return result.stdout.strip()


def version_info():
    root = Path(config.program_root()).resolve()
    # A worktree has a .git file; requiring it prevents inheriting a parent repository.
    if (root / '.git').exists():
        try:
            deadline = time.monotonic() + 4
            def git(*args):
                return _git(root, *args, timeout=max(.01, deadline - time.monotonic()))
            commit = git('rev-parse', '--short=12', 'HEAD')
            dirty = bool(git('status', '--porcelain', '--untracked-files=normal'))
            tags = [tag for tag in git('tag', '--merged', 'HEAD').splitlines() if version_key(tag)]
            base, distance = None, None
            if tags:
                patterns = [arg for tag in tags for arg in ('--match', tag)]
                description = git('describe', '--tags', '--long', '--abbrev=12', *patterns, 'HEAD')
                base, count, _ = description.rsplit('-', 2)
                distance = int(count)
            development = not base or bool(distance) or dirty
            label = base if base and not distance else (f'{base} +{distance} · {commit}' if base else f'开发版 · {commit}')
            if dirty:
                label += ' · 本地有修改'
            return {'current_version': base, 'version_label': label, 'version_source': 'git',
                    'commit': commit, 'commits_since_tag': distance, 'dirty': dirty,
                    'development': development}
        except (OSError, ValueError, subprocess.SubprocessError):
            # A stale package VERSION cannot identify a Git tree whose metadata failed.
            return {'current_version': None, 'version_label': 'Git 源码 · 版本信息读取失败',
                    'version_source': 'git', 'development': True}
    try:
        value = (root / 'VERSION').read_text(encoding='utf-8').strip()
        if version_key(value):
            return {'current_version': value, 'version_label': value,
                    'version_source': 'release', 'development': False}
    except (OSError, UnicodeError):
        pass
    return {'current_version': None, 'version_label': '源码副本 · 缺少版本信息',
            'version_source': 'unknown', 'development': False}
