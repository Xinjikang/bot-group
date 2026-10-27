"""#253 测试 MCP server（stdio，JSON-RPC 2.0），仅用于网关 MCP 注入验证。

工具：
  probe_ping   返回固定标记，验证工具可调用；
  probe_whoami 返回本进程 pid / HERMES_HOME / HERMES_PROFILE / argv / PROBE_*，
               用于核对"每次执行注入的 MCP 子进程拿到的是哪个 home、哪个 tag"；
  probe_engine 访问 ${PROBE_ENGINE_URL}/whoami，返回隔离测试引擎的回执，
               用于证明 MCP 配置里的引擎地址指向隔离实例而不是生产 8180。

设计约束：只读、无生产访问能力。引擎地址只从环境变量取，默认空（未设置时返回错误
而不是去猜 8180）。stdout 只输出 JSON-RPC 行，日志一律不写，避免污染 MCP 通道。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

TOOLS = [
    {
        "name": "probe_ping",
        "description": "Return a fixed probe marker; used to prove the MCP tool is callable.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "probe_extra",
        "description": "Extra tool exposed only when PROBE_EXTRA_TOOL=1 (schema-change probe).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "probe_whoami",
        "description": "Return this MCP process identity and probe environment.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "probe_engine",
        "description": "Call the isolated probe engine /whoami and return its receipt.",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
]


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _text(payload: object, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": is_error}


def _whoami() -> dict:
    return {
        "marker": "probe-mcp-v1",
        "pid": os.getpid(),
        "argv": sys.argv,
        "env": {k: os.environ.get(k) for k in (
            "HERMES_HOME", "HERMES_PROFILE", "PROBE_MCP_TAG",
            "PROBE_ENGINE_URL", "GROUP_ENGINE_URL", "XDG_GROUP_ENGINE_URL")},
    }


def _call_engine(path: str) -> dict:
    base = (os.environ.get("PROBE_ENGINE_URL") or "").strip()
    if not base:
        return {"error": "PROBE_ENGINE_URL not set", "isolated": False}
    url = base.rstrip("/") + (path or "/whoami")
    req = urllib.request.Request(url, headers={
        "X-Probe-Agent": "mcp", "X-Probe-Tag": os.environ.get("PROBE_MCP_TAG", "")})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return {"status": resp.status, "body": json.loads(resp.read().decode("utf-8"))}
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "url": url}


def _tools() -> list:
    """probe_extra 只在 PROBE_EXTRA_TOOL=1 时暴露，用来构造 tools/list 变化。"""
    if os.environ.get("PROBE_EXTRA_TOOL") == "1":
        return TOOLS
    return [t for t in TOOLS if t["name"] != "probe_extra"]


def _handle(msg: dict) -> dict | None:
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "probe-mcp", "version": "1.0.0"},
        }}
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": _tools()}}
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "probe_ping":
            return {"jsonrpc": "2.0", "id": mid, "result": _text({"pong": True, "marker": "probe-mcp-v1"})}
        if name == "probe_whoami":
            return {"jsonrpc": "2.0", "id": mid, "result": _text(_whoami())}
        if name == "probe_engine":
            return {"jsonrpc": "2.0", "id": mid,
                    "result": _text(_call_engine(str(args.get("path") or "/whoami")))}
        return {"jsonrpc": "2.0", "id": mid, "result": _text(
            {"error": f"unknown tool {name}"}, is_error=True)}
    if mid is None:
        return None
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        reply = _handle(msg)
        if reply is not None:
            _send(reply)


if __name__ == "__main__":
    main()
