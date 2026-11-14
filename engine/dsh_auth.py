"""DSH 认证与凭据接入（req#137 task#265）。

账号路由通过启动 overlay 恢复 ``credentials``、``authorization``、``deepseek-account``
三项；API-key 路由走 ``DSH_HOME/.env`` 的 ``DEEPSEEK_API_KEY`` 或界面保存的原生凭据，
并保持 ``deepseek-account`` 关闭。认证失败识别 ``MISSING_CREDENTIAL`` / ``AUTH`` 事件，
保持所选路由不切换；错误消息脱敏。凭据临时副本限制当前用户可访问，退出时清理，原配置
保持原值。

provider、model、服务配置与凭据来源一起解析（``resolve``）；认证 overlay 条目由
``overlay_entries`` 构建，供 spawn（task 266 引擎集成）以 ``--patch`` 注入。三种来源的
真实调用由本任务 verify 脚本在隔离原生 DSH 验收（账号已实测 DSH_NATIVE_OK，API-key 待
有效 key），task 266 承接引擎 spawn 集成，不重复真实验收。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from . import dsh_profile_config

try:
    import ntsecuritycon
    import win32api
    import win32security
except ImportError:  # 非 Windows 环境，结构化 DACL 核验不可用
    ntsecuritycon = win32api = win32security = None  # type: ignore[assignment]

try:
    import yaml as _yaml
except ImportError:  # 无 YAML 库时退化为正则键名匹配
    _yaml = None  # type: ignore[assignment]


# 凭据脱敏：sk-* 密钥、bearer token、api_key=...、DEEPSEEK_API_KEY=...、authorization=...
_CREDENTIAL = re.compile(
    r'(?i)(sk-[A-Za-z0-9_-]{6,}|bearer\s+\S+|api[_-]?key\s*[:=]\s*\S+'
    r'|DEEPSEEK_API_KEY\s*=\s*\S+|authorization\s*[:=]\s*\S+)')
# 账号路由 provider（desktop 实测 deepseek-account）。
_ACCOUNT_PROVIDER = 'deepseek-account'
# provider/model/服务配置条目，随 overlay 一并透传（desktop 实测 id 集合）。
_PROVIDER_IDS = ('agent-default-model', 'llm-deepseek', 'llm-pi-ai', 'deepseek-account')


# 宽泛 SID（Everyone/Users/Authenticated Users/Guests/ANONYMOUS）——凭据副本不得出现。
_BROAD_SIDS = {'S-1-1-0', 'S-1-5-7', 'S-1-5-11', 'S-1-5-32-545', 'S-1-5-32-546'}
# 系统/管理员/所有者 SID（OS 级，非宽泛、非其他用户）——允许。
_SYSTEM_SIDS = {'S-1-5-18', 'S-1-5-32-544', 'S-1-3-0', 'S-1-3-4'}


class AuthSyncError(ValueError):
    """凭据操作失败，不泄露凭据内容。"""


def _windows_identity() -> str:
    domain = os.environ.get('USERDOMAIN', '')
    user = os.environ.get('USERNAME', '')
    if domain and user:
        return f'{domain}\\{user}'
    return user


def _current_user_sid() -> str:
    """可信进程身份：当前进程令牌用户 SID（非环境变量，防伪造）。"""
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    try:
        sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
        return win32security.ConvertSidToStringSid(sid)
    finally:
        win32api.CloseHandle(token)


def _file_aces(path: Path) -> list[tuple[str, int]]:
    """结构化读取 DACL：返回允许 ACE 的 (SID 字符串, 访问掩码) 列表。"""
    sd = win32security.GetNamedSecurityInfo(str(path), win32security.SE_FILE_OBJECT,
                                             win32security.DACL_SECURITY_INFORMATION)
    dacl = sd.GetSecurityDescriptorDacl()
    if dacl is None:
        raise AuthSyncError('凭据副本无 DACL 保护，已中止')
    aces: list[tuple[str, int]] = []
    for i in range(dacl.GetAceCount()):
        ace = dacl.GetAce(i)
        if ace[0][0] == ntsecuritycon.ACCESS_ALLOWED_ACE_TYPE:
            aces.append((win32security.ConvertSidToStringSid(ace[2]), int(ace[1])))
    if not aces:
        raise AuthSyncError('凭据副本无可解析允许 ACE，已中止')
    return aces


def _verify_private_acl(path: Path, current_sid: str) -> None:
    """结构化核验：无宽泛/其他用户 ACE，且当前进程用户有读权限。

    允许 OS 级主体（SYSTEM/Administrators/Owner Rights），拒绝 Everyone/Users/
    Authenticated Users/Guests 等宽泛 SID 及任何其他具体用户 SID。
    """
    aces = _file_aces(path)
    current_read = False
    for sid, access in aces:
        if sid in _BROAD_SIDS:
            raise AuthSyncError('凭据副本存在宽泛访问权限，已中止')
        if sid == current_sid:
            current_read = bool(access & ntsecuritycon.FILE_READ_DATA)
            continue
        if sid in _SYSTEM_SIDS:
            continue
        raise AuthSyncError('凭据副本存在其他主体访问权限，已中止')
    if not current_read:
        raise AuthSyncError('当前用户无读取权限，已中止')


def credential_sources(home: str) -> dict:
    """只读核验凭据来源存在性（仅报告存在/缺失 + 配置引用，不输出凭据值）。

    返回 ``{account_credentials, api_key_entry, api_key_refs, env_api_key,
    process_env_api_key}``；``api_key_refs`` 为 ``.credentials.yaml`` 内 API-key
    类型条目的键路径（如 ``refs.DEEPSEEK_API_KEY``），仅键名不含值。
    """
    home = Path(home).resolve()
    credentials = home / '.credentials.yaml'
    account = credentials.is_file()
    api_key_refs: list[str] = []
    if account:
        try:
            raw = credentials.read_text(encoding='utf-8-sig')
            if _yaml is not None:
                api_key_refs = _api_key_paths(_yaml.safe_load(raw))
            if not api_key_refs:
                api_key_refs = [m.group(1).strip() for m in re.finditer(
                    r'(?i)^\s*([^#\n]*\b(?:api[_-]?key|DEEPSEEK_API_KEY)\b[^#\n]*)\s*:',
                    raw, re.MULTILINE)]
        except Exception:  # 解析失败/无权限读：只报告存在性，不中断
            pass
    return {'account_credentials': account, 'api_key_entry': bool(api_key_refs),
            'api_key_refs': api_key_refs,
            'env_api_key': bool(_env_key(home)),
            'process_env_api_key': 'DEEPSEEK_API_KEY' in os.environ}


def _api_key_paths(data, prefix: str = '') -> list[str]:
    """递归提取 API-key 类型条目的键路径（不返回值）。"""
    out: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            full = f'{prefix}.{key}' if prefix else str(key)
            if re.search(r'(?i)\b(?:api[_-]?key|DEEPSEEK_API_KEY)\b', str(key)):
                out.append(full)
            out.extend(_api_key_paths(value, full))
    elif isinstance(data, list):
        for i, item in enumerate(data):
            out.extend(_api_key_paths(item, f'{prefix}[{i}]'))
    return out


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


def resolve(bot, *, final_provider: str | None = None) -> dict:
    """解析认证路由 + provider/model + 凭据来源（一起解析，不做实际调用）。

    返回 ``{route, provider, model, api_key_source, credentials_file, selected}``。
    ``route`` ∈ ``{'account', 'apikey'}``；``api_key_source`` ∈ ``{'env', 'native', None}``。
    ``final_provider``：req#138 F3——项目覆盖后的最终 provider；传入时优先于 profile 的
    provider 参与路由判定，避免「运行 provider 与认证路由分裂」。
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
    if final_provider is not None and final_provider != '':
        provider = final_provider
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


def overlay_entries(bot, *, credentials_path: str | None = None,
                    final_provider: str | None = None) -> list:
    """构建启动 overlay 的认证相关条目（供 266 以 ``--patch`` 注入）。

    按 ``id`` 合并：账号路由最终 ``deepseek-account`` 为 ``disabled=False``，API-key 路由
    最终为 ``disabled=True``；所选 provider/model/服务 config（accountId 等）保留，同一
    ``id`` 只输出唯一条目（控制条目的启停状态优先，不依赖 cordis 重复覆盖语义）。
    遥测与会话日志默认关闭；.env 键由 DSH 原生加载，界面保存凭据显式恢复 credentials。
    ``final_provider``（req#138 F3）：项目覆盖后的最终 provider，路由据此判定。
    """
    info = resolve(bot, final_provider=final_provider)
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


def restrict_dir(path: str) -> str | None:
    """写敏感内容前把目录限制为仅当前用户（+OS 级主体）可访问；返回当前用户 SID。

    供 copy_credentials 与 verify 脚本在写 `.env`/`.credentials.yaml` 等敏感副本前调用，
    先限权再写，避免明文敏感字节先落盘后补 ACL 的窗口。
    """
    private = Path(path).resolve()
    if not private.is_dir():
        private.mkdir(parents=True, exist_ok=True)
    if sys.platform != 'win32':
        try:
            os.chmod(private, 0o700)
        except OSError as exc:
            raise AuthSyncError('无法限制凭据目录权限，已中止') from exc
        return None
    current_sid = _current_user_sid()
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
        raise AuthSyncError('无法限制目录权限，已中止') from exc
    if r.returncode != 0:
        raise AuthSyncError(f'无法限制目录权限，已中止（icacls 返回 {r.returncode}）')
    _verify_private_acl(private, current_sid)
    return current_sid


def copy_credentials(home: str, private_dir: str) -> str | None:
    """复制原生凭据到隔离目录并核验私有 ACL；任何限制失败中止并清理。

    先建立/核验本次独占私有目录权限，再写敏感字节；icacls 失败、异常、超时、身份缺失、
    读回失败或宽泛 ACE 均中止并显式报错（不返回可用副本、不回退读取原始凭据）。已有目标
    文件明确拒绝，防止沿用既有宽泛 ACL。核验目录与最终文件 DACL 均无宽泛 ACE；原
    ``.credentials.yaml`` 保持原值。
    """
    source = Path(home).resolve() / '.credentials.yaml'
    if not source.is_file():
        return None
    private = Path(private_dir).resolve()
    dest = private / 'credentials.yaml'
    if dest.exists():
        raise AuthSyncError('凭据副本目标文件已存在，中止（防止沿用既有宽泛 ACL）')
    current_sid = restrict_dir(private)
    try:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        raise AuthSyncError('无法创建独占凭据副本，已中止') from exc
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(source.read_bytes())
    except Exception:
        dest.unlink(missing_ok=True)
        raise AuthSyncError('无法写入凭据副本，已中止') from None
    if sys.platform == 'win32':
        try:
            _verify_private_acl(dest, current_sid)
        except AuthSyncError:
            # 写后核验失败：清理本次副本，清理失败亦显式可见
            cleanup_failed = False
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                cleanup_failed = True
            if cleanup_failed:
                raise AuthSyncError('写后 ACL 核验失败且副本清理失败，请手动清理') from None
            raise AuthSyncError('写后 ACL 核验失败，副本已清理，已中止') from None
    return dest


def classify_auth_error(event, stderr: str = '') -> str | None:
    """分类认证/初始化失败：'missing_credential' / 'auth' / 'init' / None（区分具体失败）。

    是 ``is_auth_error`` 的细分：MISSING_CREDENTIAL（凭据缺失）、AUTH（401/invalid 等 key 被
    端点拒绝）、init（初始化失败，非认证失败）。诊断层据此区分，避免把任意 auth_error /
    invalid 字符串一律归因 key-invalid。
    """
    if isinstance(event, dict):
        kind = str(event.get('type') or '').lower()
        msg = event.get('message') or event.get('text') or ''
    else:
        kind = ''
        msg = ''
    text = (kind + ' ' + str(msg) + ' ' + (stderr or '')).lower()
    if kind == 'missing_credential' or 'missing_credential' in text or 'missing credential' in text:
        return 'missing_credential'
    if kind == 'auth' or 'authentication fails' in text or (
            '401' in text and ('unauthorized' in text or 'authentication' in text or 'invalid' in text)):
        return 'auth'
    if ('initialization' in text or 'initialize' in text or 'load_failed' in text
            or 'failed to load' in text or 'failed to initialize' in text
            or 'failed to parse' in text):
        return 'init'
    return None


def is_auth_error(event) -> bool:
    """识别 MISSING_CREDENTIAL / AUTH 认证失败事件（保持所选路由不切换）。"""
    return classify_auth_error(event) in ('missing_credential', 'auth')
