"""Bounded project evidence and fixed read-only environment checks for the HUD."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import unquote, urlsplit

EXCLUDED = {'node_modules', 'reports', 'targets', 'dist', 'build', '__pycache__', 'venv', 'vendor'}
ENTRY_DOCS = ('README.md', 'README.zh-CN.md', 'AGENTS.md', 'docs/INDEX.md',
              'docs/engineering/ENGINEERING.md', 'docs/ENGINEERING.md', 'ENGINEERING.md')
LINK = re.compile(r'(?<!!)\[[^\]\n]+\]\(([^)\n]+)\)')


def safe_path(base, path):
    try:
        if not path.resolve().is_relative_to(base):
            return False
        current = path
        while current != base:
            if current.is_symlink():
                return False
            try:
                if getattr(current.lstat(), 'st_file_attributes', 0) & 1024:
                    return False
            except FileNotFoundError:
                pass
            current = current.parent
        return True
    except OSError:
        return False


def documents(root, *, include_contents=True, preferred=()):
    result = {'files': [], 'documents': {}, 'signatures': {}, 'structure': [], 'structure_scope': 'limited_overview',
              'coverage': {'scan_complete': True, 'unread': [], 'limits': '160 documents; 60000 characters; 2000 paths'}}
    if not root or not Path(root).is_dir():
        result['coverage']['scan_complete'] = False
        return result
    base = Path(root).resolve()
    candidates = {}
    visited = 0
    def walk_error(_):
        result['coverage']['scan_complete'] = False
    for folder, dirs, names in os.walk(base, followlinks=False, onerror=walk_error):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d not in EXCLUDED
                         and safe_path(base, Path(folder) / d))
        depth = len(Path(folder).relative_to(base).parts)
        if depth <= 2:
            for directory in dirs:
                if len(result['structure']) < 180:
                    result['structure'].append((Path(folder) / directory).relative_to(base).as_posix() + '/')
        if depth >= 5:
            if dirs:
                result['coverage']['scan_complete'] = False
            dirs[:] = []
        for name in sorted(names):
            visited += 1
            if visited > 2000:
                result['coverage']['scan_complete'] = False
                break
            path = Path(folder) / name
            if name.startswith('.') or any(s in name.lower() for s in ('secret', 'credential', 'password', 'token')):
                continue
            if not safe_path(base, path):
                continue
            relative = path.relative_to(base).as_posix()
            if depth <= 2 and len(result['structure']) < 180:
                result['structure'].append(relative)
            if path.suffix.lower() in ('.md', '.txt'):
                try:
                    stat = path.stat()
                    candidates[relative] = [stat.st_size, stat.st_mtime_ns]
                except OSError:
                    walk_error(None)
        if visited > 2000:
            break
    # Entry documents remain available even when a large source tree exhausts the scan.
    for name in (*ENTRY_DOCS, *preferred):
        path = base / name
        if safe_path(base, path) and path.is_file():
            stat = path.stat()
            candidates[name] = [stat.st_size, stat.st_mtime_ns]
    def rank(name):
        return (ENTRY_DOCS.index(name) if name in ENTRY_DOCS else len(ENTRY_DOCS), name)
    ordered = sorted(candidates, key=rank)
    # Prioritize directly referenced documents over unrelated archives.
    linked = []
    for name in ENTRY_DOCS:
        if name not in candidates or candidates[name][0] > 150000:
            continue
        try:
            text = (base / name).read_text(encoding='utf-8-sig')[:14000]
            for target in LINK.findall(text):
                path = local_link(base, name, target)
                if path and path.as_posix() in candidates:
                    linked.append(path.as_posix())
        except (OSError, UnicodeError):
            pass
    ordered = list(dict.fromkeys([n for n in preferred if n in candidates] +
                                [n for n in ENTRY_DOCS if n in candidates] + linked + ordered))
    result['files'] = ordered[:160]
    result['signatures'] = {n: candidates[n] for n in ordered}
    if len(ordered) > 160:
        result['coverage']['scan_complete'] = False
    remaining = 60000
    if include_contents:
        for name in result['files']:
            if remaining <= 0 or candidates[name][0] > 150000:
                result['coverage']['unread'].append(name)
                continue
            try:
                raw = (base / name).read_bytes()
                full = raw.decode('utf-8-sig')
                limit = 30000 if name in preferred else 14000 if name in ENTRY_DOCS else 6000
                excerpt = full[:min(limit, remaining)]
                result['documents'][name] = {'text': excerpt, 'sha256': hashlib.sha256(raw).hexdigest(),
                                              'truncated': len(excerpt) < len(full), 'source': 'project_file'}
                remaining -= len(excerpt)
            except (OSError, UnicodeError):
                result['coverage']['unread'].append(name)
    return result


def local_link(base, source, target):
    target = target.strip().split(' "', 1)[0].strip('<>')
    try:
        parsed = urlsplit(target)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    path = (base / source).parent / unquote(parsed.path)
    try:
        return path.resolve().relative_to(base)
    except (OSError, ValueError):
        return None


def run(argv, root):
    """Run fixed probes with no shell and a bounded result, without exposing stderr."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0', NO_COLOR='1')
    try:
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen(argv, cwd=root, env=env, stdout=output, stderr=subprocess.DEVNULL,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                from .hud_runtime import _terminate
                _terminate(proc)
                proc.wait(timeout=3)
                raise
            output.seek(0)
            raw = output.read(262145)
        if len(raw) > 262144:
            return None, '检查输出超过上限'
        return proc.returncode, raw.decode('utf-8', errors='replace').rstrip('\r\n')
    except (OSError, subprocess.TimeoutExpired):
        return None, '检查未完成，可能是工具不可用、超时或目录权限不足'


def codegraph_command():
    command = shutil.which('codegraph')
    if not command:
        return None
    # Invoke the npm entry directly to avoid cmd.exe argument interpretation on Windows.
    if Path(command).suffix.lower() in ('.cmd', '.bat', '.ps1'):
        entry = Path(command).parent / 'node_modules/@colbymchenry/codegraph/npm-shim.js'
        node = shutil.which('node')
        return [node, str(entry)] if node and entry.is_file() else None
    return [command]


def inspect(project, materials):
    root = project.get('root')
    checks = []
    result = {'checks': checks, 'scope': '引擎所在机器的项目目录；远程连通性与正式发布状态待核实'}
    def add(key, status, text, **evidence):
        checks.append({'key': key, 'status': status, 'summary': text, 'evidence': evidence})
    if not root or not Path(root).is_dir():
        add('root', 'missing', '项目目录尚未设置或当前无法访问')
        return result
    base = Path(root).resolve()
    add('root', 'ok', '项目目录可以读取', path=str(base))
    git = shutil.which('git')
    if not git:
        add('git', 'unknown', '当前环境未找到 Git，仓库状态待检查')
    else:
        prefix = [git, '-c', 'core.fsmonitor=false', '-C', str(base)]
        code, top = run(prefix + ['rev-parse', '--show-toplevel'], base)
        if code != 0:
            add('git', 'unknown' if code is None else 'missing', '尚未确认此目录是可用的 Git 仓库')
        elif Path(top).resolve() != base:
            add('git', 'conflict', '项目目录位于另一个 Git 仓库内，请核对项目范围', repository=top)
        else:
            code, urls = run(prefix + ['config', '--get-regexp', r'^remote\..*\.url$'], base)
            names = [line.split()[0][7:-4] for line in urls.splitlines() if len(line.split()) >= 2] if code == 0 else []
            known = code in (0, 1)
            add('git_remote', 'unknown' if not known else 'ok' if names else 'missing',
                '远程仓库检查未完成' if not known else '已配置远程仓库' if names else '尚未配置代码远程仓库',
                remotes=names[:20], connectivity='not_checked')
            code, changes = run(prefix + ['status', '--porcelain', '--untracked-files=normal'], base)
            add('git_changes', 'unknown' if code != 0 else 'changed' if changes else 'ok',
                '工作区检查未完成' if code != 0 else '本地有待提交的修改，文档可能包含准备中的内容' if changes else '工作区与提交一致',
                files=[line[3:] for line in changes.splitlines()[:80]] if code == 0 else [])
    command = codegraph_command()
    if not command:
        add('codegraph', 'unknown', '当前引擎环境未找到可用的 CodeGraph；是否需要启用取决于项目约定')
    else:
        code, output = run(command + ['status', '--json', str(base)], base)
        try:
            status = json.loads(output) if code == 0 else None
            if not isinstance(status, dict) or not isinstance(status.get('initialized'), bool):
                raise ValueError('unsupported status')
            if not status['initialized']:
                add('codegraph', 'missing', 'CodeGraph 可用，此项目尚未建立代码索引')
            elif status.get('worktreeMismatch') or Path(status.get('projectPath', '')).resolve() != base:
                add('codegraph', 'conflict', '代码索引的项目目录与当前项目不一致')
            elif not isinstance(status.get('pendingChanges'), dict):
                add('codegraph', 'unknown', '已有代码索引，当前版本无法确认索引是否最新')
            else:
                pending = status['pendingChanges']
                stale = any(pending.get(k, 0) for k in ('added', 'modified', 'removed'))
                add('codegraph', 'changed' if stale else 'ok', '代码索引需要更新' if stale else '代码索引与当前文件一致',
                    pending=pending, files=status.get('fileCount'))
        except (ValueError, TypeError):
            add('codegraph', 'unknown', 'CodeGraph 检查未完成，安装与索引状态待核实')
    for source, doc in materials['documents'].items():
        for target in LINK.findall(doc['text']):
            relative = local_link(base, source, target)
            if relative is not None and not (base / relative).exists():
                add('document_link', 'missing', '文档引用的本地文件未找到', source=source, target=relative.as_posix())
                if sum(c['key'] == 'document_link' for c in checks) >= 20:
                    break
        if sum(c['key'] == 'document_link' for c in checks) >= 20:
            break
    if 'members' in project:
        active = [m for m in project['members'] if m.get('active')]
        add('members', 'ok' if active and all(m.get('role') for m in active) else 'missing',
            '成员已分配职责' if active and all(m.get('role') for m in active) else '项目成员或职责分配尚未齐备',
            active=len(active))
    if project.get('directory_mode') == 'worktree':
        worktree_root = project.get('worktree_root')
        add('worktree_root', 'ok' if worktree_root and Path(worktree_root).is_dir() else 'missing',
            '独立工作区根目录可以访问' if worktree_root and Path(worktree_root).is_dir() else '独立工作区根目录尚未准备好')
    if 'workspace_resources' in project:
        workspaces = project['workspace_resources']
        saved = [w for w in workspaces if w['revision'] > 0]
        missing = [w['path'] for w in workspaces if not w['revision']]
        errors = project.get('workspace_errors', [])
        status = 'unknown' if errors else 'missing' if missing or not workspaces else 'ok'
        summary = ('部分成员的工作目录尚未确认，资源配置待核实' if errors else
                   '部分工作目录尚未保存资源配置' if missing and saved else
                   '尚未保存工作区资源配置' if not saved else
                   '已保存工作区资源配置；共用目录的成员使用同一份配置，实际服务运行情况待核实')
        add('resources', status, summary, configured=len(saved), workspaces=len(workspaces),
            missing_paths=missing, unresolved=errors)
    return result
