"""Wake-time copies of the main project's rules; Hermes owns prompt refresh."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import tempfile

from . import db
from .project_workspaces import LOCK, git, key

LIMIT = 2 * 1024 * 1024


def _read(path):
    with path.open('rb') as stream:
        content = stream.read(LIMIT + 4097)
    if len(content) > LIMIT + 4096:
        raise ValueError(f'规则文件超过大小限制：{path}')
    return content


def sync(project, workdir):
    """Called under the spawn lock. Never change the main file or session identity."""
    with LOCK:
        conn = db.get_connection()
        try:
            row = conn.execute('SELECT root FROM projects WHERE name=?', (project,)).fetchone()
            records = {r['path']: dict(r) for r in conn.execute('SELECT * FROM project_rule_copies')}
        finally:
            conn.close()
        if row is None:
            raise ValueError('项目不存在')
        root, directory = Path(row['root']).resolve(), Path(workdir).resolve()
        if key(root) == key(directory):
            return  # Main-directory bots continue using native discovery directly.
        source = root / 'AGENTS.md'
        body = _read(source)
        if len(body) > LIMIT:
            raise ValueError(f'主目录 AGENTS.md 超过 2 MB：{source}')
        body = body.decode('utf-8-sig').encode('utf-8')
        if not body.strip():
            raise ValueError('主目录 AGENTS.md 为空，无法同步项目规则')
        # Linked worktrees have their own Git root; Hermes stops discovery there.
        if key(git(directory, 'rev-parse', '--show-toplevel')) != key(directory):
            raise ValueError('规则同步需要使用 worktree 根目录')
        for name in ('.hermes.md', 'HERMES.md'):
            if (directory / name).exists():
                raise ValueError(f'{directory / name} 优先于 AGENTS 规则，请先处理冲突')
        target = directory / 'AGENTS.override.md'
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError(f'保留已有规则文件，请先处理冲突：{target}')
        if git(directory, 'ls-files', '--', 'AGENTS.override.md'):
            raise ValueError(f'保留 Git 已跟踪的规则文件：{target}')
        previous = _read(target) if target.exists() else None
        record = records.get(key(target))
        if record and (record['source'] != key(source) or record['project'] != project):
            raise ValueError(f'override 非本项目受管理副本，保留原文件：{target}')
        if previous is not None:
            if not record or hashlib.sha256(previous).hexdigest() not in (record['digest'], record['pending_digest']):
                raise ValueError(f'override 非本项目受管理副本或被手动修改，保留原文件：{target}')
        content = body
        # Local Git exclude keeps engine-owned copies out of commits and dirty checks.
        exclude = Path(git(directory, 'rev-parse', '--path-format=absolute', '--git-path', 'info/exclude'))
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_bytes() if exclude.exists() else b''
        if b'/AGENTS.override.md' not in existing.splitlines():
            with exclude.open('ab') as stream:
                stream.write((b'\n' if existing and not existing.endswith(b'\n') else b'') + b'/AGENTS.override.md\n')
        if previous == content:
            return
        # Record both old and intended bytes before publication so a crash between
        # SQLite and filesystem writes can be retried without taking over other data.
        digest = hashlib.sha256(content).hexdigest()
        conn = db.get_connection()
        try:
            conn.execute('INSERT INTO project_rule_copies(path,project,source,digest,pending_digest) VALUES(?,?,?,?,?) '
                         'ON CONFLICT(path) DO UPDATE SET digest=excluded.digest,pending_digest=excluded.pending_digest',
                         (key(target), project, key(source), hashlib.sha256(previous).hexdigest() if previous is not None else '', digest))
            conn.commit()
        finally:
            conn.close()
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, prefix='.group-rules-', delete=False) as stream:
                temp_path = Path(stream.name)
                stream.write(content)
            if previous is None:
                # Atomic exclusive publication of the finished temporary file.
                os.link(temp_path, target)
            else:
                if target.is_symlink() or _read(target) != previous:
                    raise ValueError(f'同步期间 override 被修改，保留原文件：{target}')
                os.replace(temp_path, target)
            conn = db.get_connection()
            try:
                conn.execute('UPDATE project_rule_copies SET digest=?,pending_digest=? WHERE path=?',
                             (digest, '', key(target)))
                conn.commit()
            finally:
                conn.close()
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
