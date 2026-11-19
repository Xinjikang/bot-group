"""bot-group relay 插件配置解析。

配置来源（优先级）：config.extra 显式字段 > 进程环境 > 插件目录 .env。
凭据（连接凭据）只用于出站 WSS 握手，不落日志。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


PLUGIN_ROOT = Path(__file__).resolve().parent
PACKAGE_VERSION = "1.0.0"  # 源码开发模式兜底；发布包以 VERSION 文件为准（单一事实源）
RELAY_PROTOCOL_VERSION = 1  # 与引擎 engine/relay_server.py RELAY_PROTOCOL_VERSION 一致


def plugin_version() -> str:
    """插件包版本（单一事实源）：随包 VERSION 文件（打包时注入实际发布版本）
    > BOT_GROUP_RELAY_VERSION 环境变量 > 源码常量（开发模式兜底）。
    引擎按此值与自身发布版本同 tag 配套校验，不符 4003。"""
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
    engine_url: str          # 引擎地址（http(s)://host:port，无路径）
    member: str              # 引擎节点注册的成员名
    credential: str          # 节点连接凭据
    mcp_python: str          # 随包 MCP 解释器绝对路径（可空，自动探测）
    home: str                # 本机 gateway 控制 home（取票目标）


def _env(name: str) -> str:
    value = os.environ.get(name) or ""
    return value.strip()


def load(config=None) -> RelayConfig | None:
    """从 PlatformConfig / 环境组装配置；缺必填项返回 None。"""
    extra = getattr(config, "extra", {}) or {}
    engine_url = (extra.get("engine_url") or _env("BOT_GROUP_ENGINE_URL"))
    member = (extra.get("member") or _env("BOT_GROUP_RELAY_MEMBER")
              or _env("HERMES_PROFILE"))
    credential = extra.get("credential") or _env("BOT_GROUP_RELAY_CREDENTIAL")
    mcp_python = extra.get("mcp_python") or _env("BOT_GROUP_MCP_PYTHON")
    home = extra.get("home") or _env("HERMES_HOME") or str(Path.home() / ".hermes")
    if not engine_url or not member or not credential:
        return None
    parsed = urlsplit(engine_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path not in ("", "/"):
        return None
    return RelayConfig(engine_url=engine_url.rstrip("/"), member=member,
                       credential=credential, mcp_python=mcp_python, home=home)


def ws_url(engine_url: str) -> str:
    """引擎 /relay/ws 出站地址：http→ws、https→wss。"""
    return engine_url.replace("https:", "wss:", 1).replace("http:", "ws:", 1) + "/relay/ws"
