"""Persist directory grants on explicit enrollment and workspace changes."""
from copy import deepcopy
from pathlib import Path

from fastapi import HTTPException
import tomlkit
from tomlkit.exceptions import TOMLKitError

from . import bot_identity, profile_config as files, project_workspaces as ws


def grant(config, inherited, directory):
    """Change only filesystem access; approval and network settings stay intact."""
    path = str(Path(directory).resolve()).replace('\\', '/')
    mode = config.get('sandbox_mode', inherited.get('sandbox_mode'))
    if mode:
        if mode == 'danger-full-access':
            return
        if mode != 'workspace-write':
            raise HTTPException(409, '该 Codex profile 使用旧式只读沙箱；请先选择支持目录授权的权限配置后再加入项目')
        legacy = config.setdefault('sandbox_workspace_write', tomlkit.table())
        roots = legacy.setdefault('writable_roots', deepcopy(inherited.get('sandbox_workspace_write', {}).get('writable_roots', [])))
        if not any(Path(p).resolve() == Path(directory).resolve() for p in roots):
            roots.append(path)
        return
    active = config.get('default_permissions', inherited.get('default_permissions'))
    if active == ':danger-full-access':
        return
    if not active:
        active = ':read-only'
    if active.startswith(':'):
        if active not in (':workspace', ':read-only'):
            raise HTTPException(400, '未知的 Codex 权限配置，请检查 profile')
        name = 'bot-group-project-access'
        if name in config.get('permissions', {}) or name in inherited.get('permissions', {}):
            raise HTTPException(409, 'Codex 权限配置名称冲突，请检查 profile')
        policy = tomlkit.table()
        policy['extends'] = active
        config.setdefault('permissions', tomlkit.table())[name] = policy
        config['default_permissions'] = name
        active = name
    policies = config.setdefault('permissions', tomlkit.table())
    policy = policies.setdefault(active, tomlkit.table())
    filesystem = policy.setdefault('filesystem', tomlkit.table())
    parent_fs = inherited.get('permissions', {}).get(active, {}).get('filesystem', {})
    key = next((p for p in [*filesystem, *parent_fs]
                if not p.startswith(':') and Path(p).is_absolute() and Path(p).resolve() == Path(directory).resolve()), path)
    existing = deepcopy(filesystem.get(key, parent_fs.get(key)))
    if existing == 'deny' or isinstance(existing, dict) and existing.get('.') == 'deny':
        raise HTTPException(409, '该项目目录被 Codex profile 显式禁止访问，请先核对权限配置')
    if isinstance(existing, dict):
        existing['.'] = 'write'
        filesystem[key] = existing
    else:
        filesystem[key] = 'write'


def project_directories(project, directory):
    """Keep versioned documents in the workspace and share project reports."""
    reports = Path(project['root']).resolve() / 'reports'
    return (directory,) if reports.is_relative_to(Path(directory).resolve()) else (directory, reports)


def config_change(bot, directory, extra_directories=()):
    home, selector = bot_identity.codex_settings(bot)
    home = Path(home)
    if not home.is_dir():
        raise HTTPException(400, 'Codex profile 目录不存在，请重新选择')
    path = home / (f'{selector}.config.toml' if selector else 'config.toml')
    before = files._snapshot(home, [path.name])[path.name]
    try:
        config = tomlkit.parse(files._text(before))
        inherited = tomlkit.parse(files._text(files._snapshot(home, ['config.toml'])['config.toml'])) if selector else config
        if selector and before is None and selector in inherited.get('profiles', {}):
            # Keep inline overrides when supplying the CLI's named configuration file.
            config.update(deepcopy(inherited['profiles'][selector]))
        grant(config, inherited, directory)
        for extra in extra_directories:
            grant(config, inherited, extra)
        content = tomlkit.dumps(config)
        if before and b'\r\n' in before:
            content = content.replace('\r\n', '\n').replace('\n', '\r\n')
        encoding = 'utf-8-sig' if before and before.startswith(b'\xef\xbb\xbf') else 'utf-8'
        return path, before, content.encode(encoding)
    except (TOMLKitError, UnicodeError, TypeError, AttributeError):
        raise HTTPException(400, 'Codex 配置必须是有效的 UTF-8 TOML，请检查 profile') from None


def commit_enrollment(conn, project, members):
    """Commit membership together with the assigned directories' grants."""
    commit_directory_access(conn, project, members)


def commit_directory_access(conn, project, members):
    """Commit directory changes and grants together; restore grants on failure."""
    changes = []
    try:
        with files._LOCK:
            p = ws.project(conn, project)
            for member in dict.fromkeys(members):
                bot = bot_identity.resolve(conn, project, member)
                if not bot or bot['runtime'] != 'codex':
                    continue
                directory = ws.selected_path(conn, p, member)
                directories = project_directories(p, directory)
                path, before, after = config_change(bot, directories[0], directories[1:])
                if before == after:
                    continue
                files._commit(path.parent, {path.name: before}, {path.name: after})
                changes.append((path, before, after))
            conn.commit()
    except Exception:
        conn.rollback()
        for path, before, after in reversed(changes):
            if path.read_bytes() != after:
                raise HTTPException(409, '目录授权失败且 Codex 配置已被其他操作修改，请核对备份') from None
            if before is None:
                path.unlink()
            else:
                staged = files._stage(path.parent, before)
                try:
                    staged.replace(path)
                finally:
                    staged.unlink(missing_ok=True)
        raise
