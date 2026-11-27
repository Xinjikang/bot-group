"""bot-group relay 插件配置解析。

配置来源（优先级）：config.extra 显式字段 > 进程环境 > 插件目录 .env。
凭据（连接凭据）只用于出站 WSS 握手，不落日志。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

if (Path(__file__).resolve().parent / "engine" / "relay_protocol.py").is_file():
    from .engine import relay_protocol, relay_retry
else:
    from engine import relay_protocol, relay_retry


PLUGIN_ROOT = Path(__file__).resolve().parent
PACKAGE_VERSION = "1.0.0"  # 源码开发模式兜底；发布包以 VERSION 文件为准（单一事实源）
RELAY_PROTOCOL_VERSION = relay_protocol.CURRENT


def plugin_version() -> str:
    """插件包版本（单一事实源）：随包 VERSION 文件（打包时注入实际发布版本）
    > BOT_GROUP_RELAY_VERSION 环境变量 > 源码常量（开发模式兜底）。"""
    version_file = PLUGIN_ROOT / "VERSION"
    try:
        content = version_file.read_text(encoding="utf-8").strip()
        if content:
            return content
    except OSError:
        pass
    env = _env("BOT_GROUP_RELAY_VERSION")
    if env:
        return env
    return PACKAGE_VERSION


@dataclass(frozen=True)
class RelayConfig:
    engine_url: str          # HTTPS origin, or a legacy address awaiting renewal.
    member: str              # 引擎节点注册的成员名
    credential: str          # 节点连接凭据
    mcp_python: str          # 随包 MCP 解释器绝对路径（可空，自动探测）
    home: str                # 本机 gateway 控制 home（取票目标）
    invite: str = ""         # req#175 一次性邀请（云端连本机首次接入；兑换后换持久凭据）
    fingerprint: str = ""


def _env(name: str) -> str:
    value = os.environ.get(name) or ""
    return value.strip()


def platform_mcp_python(config=None) -> str:
    """随包 MCP 解释器配置值：PlatformConfig.extra.mcp_python 或
    BOT_GROUP_MCP_PYTHON。独立于 load() 的必填项（listen 场景不需要
    engine_url/member/credential 也能拿到解释器配置，manager 12793）。"""
    extra = getattr(config, "extra", {}) or {}
    value = extra.get("mcp_python") or _env("BOT_GROUP_MCP_PYTHON")
    return value.strip() if value else ""


def host_member() -> str:
    """Use the active Hermes profile, including a routed gateway profile."""
    try:
        from hermes_cli.profiles import current_profile_name
    except ImportError:
        return _env("HERMES_PROFILE_NAME") or _env("HERMES_PROFILE")
    return current_profile_name() or ""


def host_home() -> str:
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        return _env("HERMES_HOME") or str(Path.home() / ".hermes")
    return str(get_hermes_home())


def load(config=None) -> RelayConfig | None:
    """从 PlatformConfig / 环境组装配置；缺必填项返回 None。"""
    extra = getattr(config, "extra", {}) or {}
    engine_url = (extra.get("engine_url") or _env("BOT_GROUP_ENGINE_URL"))
    invite = extra.get("invite") or _env("BOT_GROUP_RELAY_INVITE")
    member = (host_member() if invite else
              extra.get("member") or _env("BOT_GROUP_RELAY_MEMBER") or host_member())
    credential = extra.get("credential") or _env("BOT_GROUP_RELAY_CREDENTIAL")
    mcp_python = platform_mcp_python(config)
    home = extra.get("home") or host_home()
    if not invite:
        from . import pairing
        saved_url, saved_key = pairing.saved_register(member)
        if saved_url and (not engine_url or saved_url == engine_url.rstrip('/') or
                          pairing.upgrades_url(engine_url, saved_url)):
            engine_url = saved_url
            credential = saved_key or credential
    if not engine_url or not member or not (credential or invite):
        if not invite and pairing.listener_selected(member):
            return RelayConfig("", member, "", mcp_python, home)
        return None
    parsed = urlsplit(engine_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path not in ("", "/"):
        return None
    return RelayConfig(engine_url=engine_url.rstrip("/"), member=member,
                       credential=credential, invite=invite, mcp_python=mcp_python,
                       home=home, fingerprint=extra.get('fingerprint') or _env('BOT_GROUP_RELAY_FINGERPRINT'))


def peer_configs(config=None, *, reserve=False):
    from . import pairing
    primary = load(config)
    result = {}
    extra = getattr(config, "extra", {}) or {}
    member = primary.member if primary else host_member() or extra.get("member") or _env("BOT_GROUP_RELAY_MEMBER")
    for peer in pairing.peer_ids():
        with pairing.using_peer(peer):
            url, key = pairing.saved_register(member)
            if (url and key) or pairing.listener_selected(member):
                result[peer] = RelayConfig(url, member, key, platform_mcp_python(config),
                                           extra.get("home") or host_home())
    if primary:
        upgraded = any(pairing.upgrades_url(primary.engine_url, cfg.engine_url) for cfg in result.values())
        if upgraded:
            return result
        same_peer = next((peer for peer, cfg in result.items()
                          if cfg.engine_url == primary.engine_url), None)
        if same_peer is not None:
            result[same_peer] = primary
        elif primary.engine_url:
            result[pairing.select_peer(primary.member, engine_url=primary.engine_url, reserve=reserve)] = primary
        else:
            result[""] = primary
    return result


def ws_url(engine_url: str) -> str:
    parsed = urlsplit(engine_url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('远程 relay 需要 HTTPS 引擎地址，请在原节点重新签发 WSS 邀请')
    return 'wss://' + parsed.netloc + '/relay/ws'
