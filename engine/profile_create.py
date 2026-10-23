"""Create independent profile homes, with optional project enrollment."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

from fastapi import HTTPException
import tomlkit

from . import bot_identity, db, discovery, profile_config as files


_MARKER = '.bot-group-profile.json'
_CODEX_KEYS = (
    'model', 'model_provider', 'model_providers', 'openai_base_url',
    'model_reasoning_effort', 'model_reasoning_summary', 'model_verbosity',
    'shell_environment_policy', 'windows',
)
_CODEX_SANDBOX_FEATURES = (
    'prefer_mxc', 'experimental_windows_sandbox', 'elevated_windows_sandbox',
)
_DEVICE = re.compile(r'^(con|prn|aux|nul|com[0-9]|lpt[0-9])$', re.I)


def _read(home, name):
    return files._snapshot(home, [name])[name]


def _source(body):
    selected = body.get('source')
    if not selected:
        return None, None
    candidates = discovery._scan_codex() if body['host_type'] == 'codex' else discovery._scan_hermes()
    match = next((c for c in candidates if c['profile'] == selected['profile']
                  and c['config_root'] == selected['config_root']), None)
    if not match:
        raise HTTPException(409, '配置来源已变化，请重新选择 profile')
    root = Path(match['config_root']).resolve()
    if body['host_type'] == 'codex':
        home, selector = bot_identity.codex_settings(match)
        return Path(home).resolve(), selector
    return (root if match['profile'] == 'default' else root / 'profiles' / match['profile']), None


def _merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def _codex(home, selector, body):
    config = tomlkit.document()
    if home:
        parent = tomlkit.parse(files._text(_read(home, 'config.toml')))
        selector = selector or parent.get('profile')
        if selector:
            if not isinstance(selector, str) or any(c in selector for c in '/\\\0') or selector in ('.', '..'):
                raise ValueError('Invalid profile selector')
            named = _read(home, f'{selector}.config.toml')
            overrides = tomlkit.parse(files._text(named)) if named is not None else parent.get('profiles', {}).get(selector, {})
            _merge(parent, overrides)
        for key in _CODEX_KEYS:
            if key in parent:
                config[key] = parent[key]
        features = parent.get('features', {})
        if not isinstance(features, dict):
            raise ValueError('Invalid features table')
        selected_features = {key: features[key] for key in _CODEX_SANDBOX_FEATURES if key in features}
        if selected_features:
            config['features'] = selected_features
    # New homes use one execution policy, independently of the selected source.
    config['approval_policy'] = 'on-request'
    config['approvals_reviewer'] = 'auto_review'
    config['sandbox_mode'] = 'workspace-write'
    if sys.platform == 'win32':
        windows = config.setdefault('windows', tomlkit.table())
        if not isinstance(windows, dict):
            raise ValueError('Invalid windows table')
        # Independent background homes need a sandbox selection that requires no UAC setup.
        features = config.get('features', {})
        if windows.get('sandbox') not in ('elevated', 'unelevated', 'mxc'):
            windows['sandbox'] = 'elevated' if features.get('elevated_windows_sandbox') is True else 'unelevated'
        # The explicit implementation replaces legacy switches, including disabled ones.
        for key in ('experimental_windows_sandbox', 'elevated_windows_sandbox'):
            features.pop(key, None)
        if not features:
            config.pop('features', None)
    if body['model']:
        config['model'] = body['model']
    result = {}
    from . import codex_auth
    raw, _ = codex_auth.read_auth(discovery._codex_root())
    if raw:
        result['auth.json'] = raw
        config['cli_auth_credentials_store'] = 'file'
    result['config.toml'] = tomlkit.dumps(config).encode('utf-8')
    result['AGENTS.md'] = _instructions(body).encode('utf-8')
    inline_auth = any(isinstance(p, dict) and (p.get('experimental_bearer_token') or p.get('api_key'))
                      for p in config.get('model_providers', {}).values())
    return result, bool(result.get('auth.json') or inline_auth)


def _instructions(body):
    return body['instructions'] or f"# {body['display_name'] or body['profile']}\n\n按当前项目的职责、权限和协作约定完成工作。\n"


def _hermes(home, body, member):
    parser, source = files._yaml(_read(home, 'config.yaml') if home else None)
    # Seed model/runtime settings only; channels and personal history belong to the source.
    config = {key: source[key] for key in ('model', 'terminal', 'compression', 'max_turns') if key in source}
    if not body['reuse_auth'] and isinstance(config.get('model'), dict):
        config['model'].pop('api_key', None)
    if body['model']:
        config.setdefault('model', {})['default'] = body['model']
    env = ''
    if home and body['reuse_auth']:
        # Only provider keys/endpoints; never duplicate messaging-channel credentials.
        raw = files._text(_read(home, '.env'))
        env = '\n'.join(line for line in raw.splitlines() if re.match(
            r'^(?:OPENAI|ANTHROPIC|OPENROUTER|NOUS|DEEPSEEK|GROQ|GEMINI|GOOGLE|XAI|MISTRAL|TOGETHER|FIREWORKS)_(?:API_KEY|BASE_URL|API_BASE)=', line))
    has_key = any(line.split('=', 1)[0].endswith('_API_KEY') and line.split('=', 1)[1].strip().strip('\'"')
                  for line in env.splitlines())
    inline_auth = isinstance(config.get('model'), dict) and config['model'].get('api_key')
    return {'config.yaml': files._dump(parser, config, None) if config else b"# Hermes member configuration.\n",
            'profile.yaml': files._dump(parser, {'display_name': body['display_name'] or body['profile']}, None),
            'SOUL.md': _instructions(body).encode('utf-8'), '.env': (env + '\n').encode('utf-8')}, bool(has_key or inline_auth)


def create(conn, body):
    """Publish files and register a Bot, optionally enrolling it, with request recovery."""
    if body['host_type'] == 'codex':
        # Older clients may still submit the former opt-out field.
        body = {**body, 'reuse_auth': True}
    name, host = body['profile'], body['host_type']
    raw_project = body.get('project')
    project = (raw_project or '').strip()
    if raw_project and not project:
        raise HTTPException(400, '项目标识不能为空白')
    body = {**body, 'project': project or None}
    if (not discovery._PROFILE_RE.fullmatch(name) or name in ('default', 'user', 'profiles')
            or _DEVICE.fullmatch(name)):
        raise HTTPException(400, 'Profile 标识须为 1–64 位小写字母、数字、短横线或下划线，并避开系统保留名')
    root = discovery._codex_root().resolve() if host == 'codex' else Path(discovery._hermes_root()).resolve()
    container = root / 'profiles'
    target = container / name
    if container.resolve() != container or target.resolve() != target:
        raise HTTPException(400, 'Profile 目录须位于宿主的 profiles 目录内')
    fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    stage = container / ('.create-' + uuid.uuid4().hex)
    published = False
    written = []
    with files._LOCK:
        try:
            conn.execute('BEGIN IMMEDIATE')
            if project and not conn.execute('SELECT 1 FROM projects WHERE name=? AND archived=0 AND removed=0', (project,)).fetchone():
                raise HTTPException(404, '请选择有效的未归档项目')
            if target.exists():
                marker = _read(target, _MARKER)
                saved = json.loads(marker) if marker else {}
                if saved.get('fingerprint') != fingerprint:
                    raise HTTPException(409, '同名 profile 已存在，请选择已有 profile 或更换标识')
                provisioned = saved['auth_copied']
            else:
                home, selector = _source(body)
                config_root = str(target) if host == 'codex' else str(root)
                reg = discovery.register_bot(conn, host, name, config_root, body['display_name'])
                member = bot_identity.enroll(conn, reg['bot_id'], project) if project else None
                content, provisioned = (_codex(home, selector, body) if host == 'codex'
                                        else _hermes(home, body, member))
                if host == 'codex' and project:
                    from . import codex_project_access as access, project_workspaces as ws
                    config = tomlkit.parse(content['config.toml'].decode('utf-8'))
                    project_config = ws.project(conn, project)
                    directory = ws.selected_path(conn, project_config, member)
                    for granted in access.project_directories(project_config, directory):
                        access.grant(config, config, granted)
                    content['config.toml'] = tomlkit.dumps(config).encode('utf-8')
                content[_MARKER] = json.dumps({'fingerprint': fingerprint, 'auth_copied': provisioned}).encode()
                container.mkdir(parents=True, exist_ok=True)
                stage.mkdir(mode=0o700)
                for filename, data in content.items():
                    path = stage / filename
                    with path.open('xb') as stream:
                        written.append(filename)
                        os.chmod(path, 0o600)
                        stream.write(data)
                for folder in ('memories', 'skills'):
                    (stage / folder).mkdir()
                # A reserved final name prevents overwriting a concurrently created home.
                target.mkdir(mode=0o700)
                published = True
                for path in sorted(stage.iterdir(), key=lambda p: p.name == _MARKER):
                    path.rename(target / path.name)
                stage.rmdir()
            config_root = str(target) if host == 'codex' else str(root)
            reg = discovery.register_bot(conn, host, name, config_root, body['display_name'])
            member = bot_identity.enroll(conn, reg['bot_id'], project) if project else None
            conn.commit()
            notice = '已创建 profile 并加入项目' if project else '已创建 profile 并注册 Bot；可在成员列表选择后加入项目'
            return {'created': True, 'bot_id': reg['bot_id'], 'member_profile': member,
                    'host_type': host, 'config_root': config_root, 'project': project or None,
                    'profile': name, 'home': str(target), 'auth_copied': provisioned,
                    'notice': notice + ('；已复制认证配置，实际可用性以首次运行结果为准。' if provisioned
                               else '；请完成主账户登录并提供文件认证后再唤醒。' if host == 'codex'
                               else '；请完成该 profile 的登录或模型认证后再唤醒。'),
                    'setup_command': _setup_command(host, root if host == 'codex' else target, name) if not provisioned else ''}
        except Exception as exc:
            conn.rollback()
            # Remove only files made by this request; never recursively delete a user home.
            for folder in ([target] if published else []) + [stage]:
                if folder.exists() and folder.resolve().parent == container:
                    for filename in written:
                        (folder / filename).unlink(missing_ok=True)
                    for child in ('memories', 'skills'):
                        if (folder / child).is_dir():
                            (folder / child).rmdir()
                    folder.rmdir()
            if isinstance(exc, HTTPException):
                raise
            if isinstance(exc, FileExistsError):
                raise HTTPException(409, '同名 profile 已存在，请更换标识') from None
            # Parser errors can contain credential values; keep them out of HTTP responses.
            raise HTTPException(400, '创建失败，请检查来源配置格式、文件权限及可用磁盘空间') from None


def _setup_command(host, home, name):
    variable = 'HERMES_HOME' if host == 'hermes' else 'CODEX_HOME'
    command = f'hermes -p {name} setup' if host == 'hermes' else 'codex login'
    root = home.parent.parent if host == 'hermes' else home
    escaped = str(root).replace("'", "''" if os.name == 'nt' else "'\"'\"'")
    if os.name == 'nt':
        return f"$env:{variable}='{escaped}'; {command}"
    return f"{variable}='{escaped}' {command}"
