"""Read-only Windows sandbox configuration hints for discovered Codex homes."""
import sys
import tomllib
from pathlib import Path

from . import bot_identity


_GUIDANCE = '建议先检查配置，或使用本页的“创建新 profile”准备独立 Bot 并核对配置。你也可以继续加入已有 Bot。'


def _read(path):
    try:
        with path.open('rb') as stream:
            raw = stream.read(2 * 1024 * 1024 + 1)
    except FileNotFoundError:
        return {}
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('Configuration exceeds the read limit')
    return tomllib.loads(raw.decode('utf-8-sig'))


def _merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def for_candidate(candidate):
    """Describe explicit local settings; runtime defaults and setup remain untested."""
    if sys.platform != 'win32' or candidate['host_type'] != 'codex':
        return None
    try:
        home, selector = bot_identity.codex_settings(candidate)
        home = Path(home)
        config = _read(home / 'config.toml')
        selector = selector or config.get('profile')
        if selector:
            if not isinstance(selector, str) or not selector or any(c in selector for c in '/\\\0') or selector in ('.', '..'):
                raise ValueError('Invalid profile selector')
            path = home / f'{selector}.config.toml'
            override = _read(path) if path.is_file() else config.get('profiles', {}).get(selector, {})
            if not isinstance(override, dict):
                raise ValueError('Invalid profile table')
            _merge(config, override)
        windows = config.get('windows', {})
        features = config.get('features', {})
        if not isinstance(windows, dict) or not isinstance(features, dict):
            raise ValueError('Invalid sandbox settings')
    except (OSError, ValueError, TypeError, AttributeError):
        return {'code': 'unreadable', 'message': '沙箱配置待确认：读取该 Bot 的 Codex 配置失败，请检查文件格式和读取权限。' + _GUIDANCE}

    if config.get('sandbox_mode') == 'danger-full-access' or config.get('default_permissions') == ':danger-full-access':
        return {'code': 'disabled', 'message': '沙箱配置提醒：该 Bot 配置了完全访问，命令执行可能访问项目以外的文件和资源。' + _GUIDANCE}
    implementation = windows.get('sandbox')
    if implementation in ('elevated', 'unelevated', 'mxc'):
        return None
    if implementation is not None:
        return {'code': 'unrecognized', 'message': '沙箱配置待确认：该 Bot 的 windows.sandbox 值需要按所用 Codex 版本核对，加入后可能出现命令执行失败。' + _GUIDANCE}
    if any(features.get(key) is True for key in (
            'prefer_mxc', 'experimental_windows_sandbox', 'elevated_windows_sandbox')):
        return None
    return {'code': 'unspecified', 'message': '沙箱配置待确认：该 Bot 未显式指定 Windows 沙箱实现，实际启用情况取决于 Codex 版本和默认设置，加入后可能出现命令执行失败。' + _GUIDANCE}
