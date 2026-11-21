"""本机 gateway 控制通道取票（req#168 验收 2/3）。

identify → session-ticket 三步对齐（profile_id/instance_id/runtime_protocol
相等校验；api_origin 在端点发现时检查必须为 loopback；authority_epoch 做
正整数/语义检查并用于回执 fencing，取票时不比对）。票据单次短时效、每次建
本地连接重取、不缓存；identify 身份与 ticket 请求一致（同一 profile_id）。

实现与引擎侧 hermes_gateway_client 的 Linux 路径同协议（unix socket
JSON 行），不 import Hermes 内部模块——全部经公开控制通道。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

IDENTIFY_TIMEOUT = 5.0


class TicketError(RuntimeError):
    """取票失败；结果未知时调用方按断线语义处理（不重投）。"""


def _socket_exchange(home: str, data: bytes, timeout: float) -> str:
    """与 gateway.sock 的一次请求-响应（每行一个 JSON）。"""
    deadline = time.monotonic() + timeout
    home_path = Path(home)
    path = home_path / "gateway.sock"
    if not path.exists():
        pointer = home_path / "gateway.sock.path"
        stat = pointer.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise TicketError("Unsafe Hermes control pointer")
        path = Path(pointer.read_text(encoding="utf-8").strip())
    stat = path.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise TicketError("Unsafe Hermes control socket")
    with socket.socket(socket.AF_UNIX) as peer:
        peer.settimeout(max(0.1, deadline - time.monotonic()))
        peer.connect(str(path))
        peer.settimeout(max(0.1, deadline - time.monotonic()))
        peer.sendall(data)
        response = bytearray()
        while b"\n" not in response:
            peer.settimeout(max(0.1, deadline - time.monotonic()))
            chunk = peer.recv(4096)
            if not chunk or len(response) + len(chunk) > 65536:
                raise TicketError("Invalid Hermes control response")
            response.extend(chunk)
    return bytes(response).split(b"\n", 1)[0].decode("utf-8", errors="replace")


def identify(home: str, timeout: float = IDENTIFY_TIMEOUT) -> dict:
    """发现本机 gateway 控制身份（identify 只读，不消耗票据）。"""
    raw = _socket_exchange(home, b'{"protocol":1,"verb":"identify","id":1}\n', timeout)
    try:
        response = json.loads(raw)
        payload = response["result"]
        if (response.get("ok") is not True or response.get("protocol") != 1
                or response.get("id") != 1 or payload.get("state") != "ready"):
            raise TicketError("Hermes gateway is unavailable")
        matches = [p for p in payload["served_profiles"] if Path(p["home"]).resolve() == Path(home).resolve()]
        if len(matches) != 1 or Path(matches[0]["profile_id"]).resolve() != Path(home).resolve():
            raise TicketError("Hermes gateway profile mismatch")
        endpoint = {key: payload[key] for key in
                    ("instance_id", "authority_epoch", "runtime_protocol", "api_origin")}
        endpoint.update(profile_id=matches[0]["profile_id"], control_home=str(home))
        # 端点发现时检查 api_origin：必须为 loopback、无凭据/查询/片段
        origin = urlsplit(endpoint["api_origin"])
        if (origin.scheme not in ("http", "https") or origin.username or origin.password
                or origin.query or origin.fragment or origin.path not in ("", "/")
                or not ipaddress.ip_address(origin.hostname).is_loopback):
            raise TicketError("Hermes gateway must publish a loopback origin")
        # authority_epoch：正整数语义检查（用于回执 fencing；取票时不比对）
        if (not isinstance(endpoint["instance_id"], str) or not endpoint["instance_id"]
                or type(endpoint["authority_epoch"]) is not int or endpoint["authority_epoch"] <= 0
                or endpoint["runtime_protocol"] != 1):
            raise TicketError("Hermes gateway identity is invalid")
        return endpoint
    except (ValueError, KeyError, TypeError) as exc:
        raise TicketError("Hermes gateway discovery returned invalid data") from exc


def fetch_ticket(home: str, *, purpose: str = "interactive", timeout: float = IDENTIFY_TIMEOUT) -> tuple[dict, str]:
    """identify → session-ticket，返回 (endpoint, ticket)。每次建本地连接重取、
    不缓存；identify 身份与 ticket 请求一致（同一 profile_id/instance_id）。"""
    endpoint = identify(home, timeout=timeout)
    request = {"protocol": 1, "id": 1, "verb": "session-ticket", "params": {
        "profile_id": endpoint["profile_id"], "instance_id": endpoint["instance_id"],
        "purpose": purpose}}
    raw = _socket_exchange(endpoint["control_home"],
                           json.dumps(request).encode("utf-8") + b"\n", timeout)
    try:
        response = json.loads(raw)
        result = response.get("result") or {}
        if (response.get("ok") is not True or response.get("protocol") != 1
                or response.get("id") != 1
                or any(result.get(key) != endpoint[key] for key in ("profile_id", "instance_id", "runtime_protocol"))
                or not isinstance(result.get("ticket"), str) or not result["ticket"]):
            raise TicketError("Hermes control identity mismatch")
        return endpoint, result["ticket"]
    except (ValueError, KeyError, TypeError) as exc:
        raise TicketError("Hermes ticket response invalid") from exc


def gateway_ws_url(endpoint: dict) -> str:
    """本机 gateway /api/ws 地址（从 api_origin 派生）。"""
    origin = endpoint["api_origin"].rstrip("/")
    return origin.replace("https:", "wss:", 1).replace("http:", "ws:", 1) + "/api/ws"
