"""DSH 认证与凭据接入（req#137 task#265）。

账号路由通过启动 overlay 恢复 ``credentials``、``authorization``、``deepseek-account``
三项；API-key 路由走 ``DSH_HOME/.env`` 的 ``DEEPSEEK_API_KEY`` 或界面保存的原生凭据，
并保持 ``deepseek-account`` 关闭。认证失败识别 ``MISSING_CREDENTIAL`` / ``AUTH`` 事件，
保持所选路由不切换；错误消息脱敏。凭据临时副本限制当前用户可访问，退出时清理，原配置
保持原值。

provider、model、服务配置与凭据来源一起解析（``resolve``）；认证 overlay 条目由
``overlay_entries`` 构建，供 spawn（task 266）以 ``--patch`` 注入。三种来源的真实模型
调用成功由 task 266 起真实 dsh 验收，本模块提供路由判定与 overlay 构造的隔离可测闭环。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from . import dsh_profile_config


# 凭据脱敏：sk-* 密钥、bearer token、api_key=...、DEEPSEEK_API_KEY=...、authorization=...
_CREDENTIAL = re.compile(
    r'(?i)(sk-[A-Za-z0-9_-]{6,}|bearer\s+\S+|api[_-]?key\s*[:=]\s*\S+'
    r'|DEEPSEEK_API_KEY\s*=\s*\S+|authorization\s*[:=]\s*\S+)')
# 账号路由 provider（desktop 实测 deepseek-account）。
_ACCOUNT_PROVIDER = 'deepseek-account'
# provider/model/服务配置条目，随 overlay 一并透传（desktop 实测 id 集合）。
_PROVIDER_IDS = ('agent-default-model', 'llm-deepseek', 'llm-pi-ai', 'deepseek-account')


def sanitize(message: str) -> str:
    return _CREDENTIAL.sub('[REDACTED]', str(message))


def _read_patch(bot) -> list:
    home = Path(bot['config_root']).resolve()
    patch = home / 'profiles' / bot['profile'] / 'cordis.patch.yml'
    if not patch.is_file():
        return []
    return dsh_profile_config._parse(patch.read_text(encoding='utf-8-sig'))


def _env_key(home: Path) -> str:
    env = home / '.env'
    if not env.is_file():
        return ''
    for line in env.read_text(encoding='utf-8-sig').splitlines():
        key, _, value = line.partition('=')
        if key.strip() == 'DEEPSEEK_API_KEY':
            return value.strip()
    return ''


def resolve(bot) -> dict:
    """解析认证路由 + provider/model + 凭据来源（一起解析，不做实际调用）。

    返回 ``{route, provider, model, api_key_source, credentials_file, selected}``。
    ``route`` ∈ ``{'account', 'apikey'}``；``api_key_source`` ∈ ``{'env', 'native', None}``。
    """
    home = Path(bot['config_root']).resolve()
    seq = _read_patch(bot)
    provider = ''
    model = ''
    selected = []
    for entry in seq:
        if not isinstance(entry, dict):
            continue
        eid = str(entry.get('id') or '')
        if eid == 'agent-default-model':
            config = entry.get('config') if isinstance(entry.get('config'), dict) else {}
            provider = str(config.get('provider') or '')
            model = str(config.get('model') or '')
        if eid in _PROVIDER_IDS:
            selected.append(entry)
    route = 'account' if provider == _ACCOUNT_PROVIDER else 'apikey'
    api_key_source = None
    if route == 'apikey':
        if _env_key(home):
            api_key_source = 'env'
        elif (home / '.credentials.yaml').is_file():
            api_key_source = 'native'
    credentials_file = home / '.credentials.yaml'
    return {
        'route': route,
        'provider': provider,
        'model': model,
        'api_key_source': api_key_source,
        'credentials_file': str(credentials_file) if credentials_file.is_file() else None,
        'selected': selected,
    }


def overlay_entries(bot, *, credentials_path: str | None = None) -> list:
    """构建启动 overlay 的认证相关条目（供 266 以 ``--patch`` 注入）。

    账号路由恢复 credentials/authorization/deepseek-account；API-key 路由保持
    deepseek-account 关闭，界面保存凭据时显式恢复 credentials 服务，.env 键由 DSH
    原生加载。遥测与会话日志默认关闭；provider/model 选择随 overlay 透传。
    """
    info = resolve(bot)
    entries: list = []
    if info['route'] == 'account':
        path = credentials_path or info['credentials_file']
        entries.append({'id': 'credentials', 'disabled': False,
                        'config': {'path': path, 'watch': False}})
        entries.append({'id': 'authorization', 'disabled': False})
        entries.append({'id': 'deepseek-account', 'disabled': False})
    else:
        entries.append({'id': 'deepseek-account', 'disabled': True})
        if info['api_key_source'] == 'native':
            path = credentials_path or info['credentials_file']
            entries.append({'id': 'credentials', 'disabled': False,
                            'config': {'path': path, 'watch': False}})
        # .env 路由：DSH 原生加载 .env 的 DEEPSEEK_API_KEY，无需 credentials 服务。
    entries.append({'id': 'session-log-deepseek', 'disabled': True})
    entries.append({'id': 'session-telemetry-otel', 'disabled': True})
    entries.extend(info['selected'])
    return entries


def copy_credentials(home: str, private_dir: str) -> str | None:
    """复制原生凭据到隔离目录并限制当前用户可访问；返回副本路径或 None。

    原 ``.credentials.yaml`` 保持原值；副本目录取消继承权限后仅授予当前用户。
    调用方（spawn）负责在退出时删除 ``private_dir``。
    """
    source = Path(home).resolve() / '.credentials.yaml'
    if not source.is_file():
        return None
    private = Path(private_dir).resolve()
    dest = private / 'credentials.yaml'
    dest.write_bytes(source.read_bytes())
    if sys.platform == 'win32':
        domain = os.environ.get('USERDOMAIN', '')
        user = os.environ.get('USERNAME', '')
        identity = f'{domain}\\{user}' if domain and user else user
        if identity:
            subprocess.run(
                ['icacls', str(private), '/inheritance:r', '/grant:r',
                 identity + ':(OI)(CI)F'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return dest


def is_auth_error(event) -> bool:
    """识别 MISSING_CREDENTIAL / AUTH 认证失败事件（保持所选路由不切换）。"""
    if not isinstance(event, dict):
        return False
    kind = str(event.get('type') or '').lower()
    if kind in ('missing_credential', 'auth'):
        return True
    if kind in ('error', 'final', 'turn.failed'):
        message = event.get('message') or event.get('text') or ''
        if not isinstance(message, str):
            return False
        text = message.lower()
        return ('authentication fails' in text or 'missing credential' in text
                or 'missing_credential' in text
                or ('401' in text and ('unauthorized' in text or 'authentication' in text
                                       or 'invalid' in text)))
    return False
