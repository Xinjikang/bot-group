"""随包 MCP 摘要与入口解析（req#168 验收 5/9）。

摘要口径 = 随包 MCP 入口（mcp/server.py）内容 sha256 + 依赖清单
（mcp/requirements.txt 与发布 requirements.lock）内容 sha256 的组合 digest。
只覆盖 MCP 包与依赖清单，不含 env 或令牌（防摘要成为凭据侧信道）。

连接存续中每次被问询（mcp_snapshot_request）与握手时都实时重算——
「连接不断、磁盘 MCP 包原地替换」后重核摘要再配会话（缓存 bug 场景）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent
MCP_DIR = PLUGIN_ROOT / "mcp"


def _file_sha(path: Path) -> str:
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def mcp_entry() -> Path | None:
    """随包 MCP 入口绝对路径（mcp/server.py）。"""
    entry = MCP_DIR / "server.py"
    return entry if entry.is_file() else None


def mcp_python(mcp_python_env: str = "") -> str:
    """随包 MCP 解释器绝对路径：配置优先，其次插件目录 venv，最后 None。"""
    if mcp_python_env and Path(mcp_python_env).is_file():
        # Preserve the venv launcher path even when it links to the base interpreter.
        return str(Path(mcp_python_env).absolute())
    for candidate in (
        PLUGIN_ROOT / "venv" / "bin" / "python",
        PLUGIN_ROOT / "venv" / "Scripts" / "python.exe",
    ):
        if candidate.is_file():
            return str(candidate)
    return ""


def mcp_fingerprint() -> str:
    """随包 MCP 入口 + 依赖清单的内容摘要（包级 digest）。

    与引擎节点配置的 _package_sha 同口径比对：引擎按版本拒绝不兼容节点。
    """
    parts = {
        "entry_sha256": _file_sha(MCP_DIR / "server.py"),
        "requirements_sha256": _file_sha(MCP_DIR / "requirements.txt"),
        "lock_sha256": _file_sha(PLUGIN_ROOT / "requirements.lock"),
    }
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def snapshot(mcp_python_env: str = "", *, code_version: str = "") -> dict:
    """握手/问询上报的一次摘要快照：解释器、入口、版本、内容摘要。"""
    entry = mcp_entry()
    return {
        "mcp_fingerprint": mcp_fingerprint(),
        "mcp_python": mcp_python(mcp_python_env),
        "mcp_entry": str(entry) if entry else "",
        "code_version": code_version,
    }
