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
# 宽泛 ACE（Everyone/Users/Authenticated Users），凭据副本目录不得残留。
_BROAD_ACE = re.compile(r'(?i)\b(Everyone|BUILTIN\\Users|NT AUTHORITY\\Authenticated Users)\b')


class AuthSyncError(ValueError):
    """凭据操作失败，不泄露凭据内容。"""


def _windows_identity() -> str:
    domain = os.environ.get('USERDOMAIN', '')
    user = os.environ.get('USERNAME', '')
    if domain and user:
        return f'{domain}\\{user}'
    return user


def _read_acl(path: Path) -> str:
    r = subprocess.run(['icacls', str(path)], stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL, text=True,
                       encoding='utf-8', errors='replace', timeout=30, check=False)
    return r.stdout or ''


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

    按 ``id`` 合并：账号路由最终 ``deepseek-account`` 为 ``disabled=False``，API-key 路由
    最终为 ``disabled=True``；所选 provider/model/服务 config（accountId 等）保留，同一
    ``id`` 只输出唯一条目（控制条目的启停状态优先，不依赖 cordis 重复覆盖语义）。
    遥测与会话日志默认关闭；.env 键由 DSH 原生加载，界面保存凭据显式恢复 credentials。
    """
    info = resolve(bot)
    controls: list[dict] = []
    if info['route'] == 'account':
        path = credentials_path or info['credentials_file']
        controls += [
            {'id': 'credentials', 'disabled': False, 'config': {'path': path, 'watch': False}},
            {'id': 'authorization', 'disabled': False},
            {'id': 'deepseek-account', 'disabled': False},
        ]
    else:
        controls.append({'id': 'deepseek-account', 'disabled': True})
        if info['api_key_source'] == 'native':
            path = credentials_path or info['credentials_file']
            controls.append({'id': 'credentials', 'disabled': False,
                             'config': {'path': path, 'watch': False}})
        # .env 路由：DSH 原生加载 .env 的 DEEPSEEK_API_KEY，无需 credentials 服务。
    controls += [
        {'id': 'session-log-deepseek', 'disabled': True},
        {'id': 'session-telemetry-otel', 'disabled': True},
    ]
    merged: dict[str, dict] = {e['id']: dict(e) for e in controls}
    for entry in info['selected']:
        if not isinstance(entry, dict) or not entry.get('id'):
            continue
        eid = entry['id']
        if eid in merged:
            target = merged[eid]
            for key, value in entry.items():
                if key == 'id':
                    continue
                # 控制条目的 disabled 等既有字段优先，selected 仅补缺失的 config/name/未知字段
                if key not in target:
                    target[key] = value
        else:
            merged[eid] = dict(entry)
    return list(merged.values())


def copy_credentials(home: str, private_dir: str) -> str | None:
    """复制原生凭据到隔离目录并核验私有 ACL；任何限制失败中止并清理。

    先建立/核验本次独占私有目录权限，再写敏感字节；icacls 失败、异常、超时或身份缺失
    均中止并显式报错（不返回可用副本、不回退读取原始凭据）。失败清理本次副本并报告。
    核验最终 DACL 无宽泛 ACE；原 ``.credentials.yaml`` 保持原值。
    """
    source = Path(home).resolve() / '.credentials.yaml'
    if not source.is_file():
        return None
    private = Path(private_dir).resolve()
    dest = private / 'credentials.yaml'
    if sys.platform == 'win32':
        identity = _windows_identity()
        if not identity:
            raise AuthSyncError('无法解析当前用户身份，中止凭据副本')
        try:
            r = subprocess.run(
                ['icacls', str(private), '/inheritance:r', '/grant:r',
                 identity + ':(OI)(CI)F'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AuthSyncError('无法限制凭据副本目录权限，已中止') from exc
        if r.returncode != 0:
            raise AuthSyncError(f'无法限制凭据副本目录权限，已中止（icacls 返回 {r.returncode}）')
        acl = _read_acl(private)
        if _BROAD_ACE.search(acl):
            raise AuthSyncError('凭据副本目录仍存在宽泛访问权限，已中止')
    try:
        dest.write_bytes(source.read_bytes())
    except Exception:
        dest.unlink(missing_ok=True)
        raise AuthSyncError('无法写入凭据副本，已中止') from None
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
