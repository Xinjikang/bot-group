"""方向 B 插件监听（req#173 验收 1/3）：listen 工具核心。

- 绑本机具体网卡地址监听（禁 0.0.0.0/:: 通配），绑定失败报错保持原状；
- TLS 证书：优先既有 PEM（--cert/--key 或 state/ 下文件），缺省自动生成
  自签（openssl CLI 优先，cryptography 兜底）；
- 监听成功后签发一次性配对链接（入口地址用对端可达的 advertise 地址/域名，
  云上公网 IP 是 NAT 映射不当 bind）；
- 引擎拨入握手：hello{direction:'dial', member, credential=配对凭据,
  code_version, protocol_version} → 兑换配对（原子单次）→ hello_ok{generation}
  → 帧路由（与出站方向共用 streams 转发本机 gateway、mcp_snapshot 实时
  问询、http_request 转发给 loopback MCP 代理回引擎）；
- 单活动通道：同一时刻只保留一条已认证连接（新连接认证成功才接管，
  旧连接主动关闭不留孤儿）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import socket
import subprocess
import threading
import time
from pathlib import Path

from . import config as relay_config
from . import mcp_digest
from . import pairing
from . import streams as streams_mod

logger = logging.getLogger(__name__)

STATE_DIR = Path(__file__).resolve().parent / "state"
HEARTBEAT_INTERVAL = 45.0


def _ensure_cert(cert_path: str | None, key_path: str | None) -> tuple[str, str]:
    """取得/生成 TLS 证书 PEM 路径。优先显式提供，其次 state/ 既有文件，
    否则自动生成自签（openssl CLI 优先，cryptography 兜底）。"""
    state_dir = STATE_DIR
    state_dir.mkdir(parents=True, exist_ok=True)
    default_cert = state_dir / "cert.pem"
    default_key = state_dir / "key.pem"
    if cert_path and key_path:
        return str(Path(cert_path).resolve()), str(Path(key_path).resolve())
    if default_cert.is_file() and default_key.is_file():
        return str(default_cert), str(default_key)
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(default_key), "-out", str(default_cert),
             "-days", "825", "-subj", "/CN=bot-group-relay-plugin"],
            check=True, capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        _generate_cert_with_cryptography(default_cert, default_key)
    return str(default_cert), str(default_key)


def _generate_cert_with_cryptography(cert_path: Path, key_path: Path) -> None:
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
        from datetime import datetime, timedelta, timezone
    except ImportError:
        raise RuntimeError(
            "TLS 证书生成失败：请提供 --cert/--key，或安装 openssl/cryptography") from None
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bot-group-relay-plugin")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=825))
            .sign(key, hashes.SHA256()))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def certificate_fingerprint(cert_path: str) -> str:
    """证书 DER 的 sha256 指纹（配对链接携带、引擎拨号校验）。"""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        der = x509.load_pem_x509_certificate(Path(cert_path).read_bytes()) \
            .public_bytes(serialization.Encoding.DER)
        return hashlib.sha256(der).hexdigest()
    except Exception:  # noqa: BLE001 cryptography 不可用时用 openssl 输出
        out = subprocess.run(
            ["openssl", "x509", "-in", cert_path, "-outform", "DER"],
            check=True, capture_output=True)
        return hashlib.sha256(out.stdout).hexdigest()


def validate_bind_address(address: str) -> str:
    """监听地址校验：必须是本机网卡具体单播地址，禁通配/回环/组播。"""
    import ipaddress
    try:
        addr = ipaddress.ip_address(str(address).strip())
    except ValueError:
        raise ValueError(f"无效的 IP 地址：{address}") from None
    if addr in (ipaddress.IPv4Address("0.0.0.0"), ipaddress.IPv6Address("::")):
        raise ValueError("禁止通配监听，只绑定本机具体网卡地址")
    if addr.is_loopback:
        raise ValueError("listen 用于对端可达的远程监听，请指定本机具体网卡地址")
    if addr.is_multicast:
        raise ValueError("组播地址不可监听")
    import psutil
    local = {
        a.address for addrs in psutil.net_if_addrs().values()
        for a in addrs if a.family in (socket.AF_INET, socket.AF_INET6)}
    if str(addr) not in local:
        raise ValueError("地址不是本机网卡的具体单播地址")
    return str(addr)


class ListenServer:
    """方向 B 监听服务器：一条已认证连接（单活动通道）。"""

    def __init__(self, *, address: str, port: int, cert_path: str, key_path: str,
                 advertise: str, member: str, home: str, mcp_python: str,
                 scheme: str = "wss"):
        self.address = address
        self.port = port
        self.cert_path = cert_path
        self.key_path = key_path
        self.advertise = advertise or address
        self.member = member
        self.home = home
        self.mcp_python = mcp_python
        self.scheme = scheme
        self._streams = streams_mod.StreamRegistry(home, on_event=self._on_local_event)
        self._stop = threading.Event()
        self._server = None
        self._thread: threading.Thread | None = None
        self._active_ws = None
        self._active_generation = 0
        self._ws_lock = threading.Lock()
        self._send_queue = queue.Queue()
        self._last_beat = 0.0

    # ---------- 生命周期 ----------

    def start(self) -> None:
        from websockets.sync.server import serve as ws_serve
        import ssl
        ssl_context = None
        if self.scheme == "wss":
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(self.cert_path, self.key_path)
        try:
            # 绑定失败（地址不可用/端口占用）抛异常：保持原状，不残留监听
            self._server = ws_serve(self._handler, self.address, self.port,
                                    ssl=ssl_context, max_size=8 * 1024 * 1024)
        except OSError as exc:
            raise RuntimeError(f"监听绑定失败 {self.address}:{self.port}：{exc}") from exc
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="bot-group-relay-listen", daemon=True)
        self._thread.start()
        logger.info("bot-group relay listening member=%s on %s:%s scheme=%s",
                    self.member, self.address, self.port, self.scheme)

    def stop(self) -> None:
        self._stop.set()
        with self._ws_lock:
            ws = self._active_ws
            self._active_ws = None
        if ws is not None:
            try:
                ws.close(code=1000, reason="listen stopped")
            except Exception:  # noqa: BLE001
                pass
        if self._server is not None:
            self._server.shutdown()
        self._streams.close_all()

    def bound_port(self) -> int:
        if self._server is None:
            return self.port
        return self._server.socket.getsockname()[1]

    # ---------- 握手与帧循环 ----------

    def _handler(self, conn) -> None:
        from websockets.exceptions import ConnectionClosed
        try:
            raw = conn.recv(timeout=10)
            hello = json.loads(raw)
            member = (hello.get("member") or "").strip()
            credential = hello.get("credential") or ""
            fingerprint = hello.get("fingerprint") or ""
            protocol_version = hello.get("protocol_version")
            if hello.get("direction") != "dial":
                conn.close(code=4005, reason="wrong direction")
                return
            if member != self.member:
                conn.close(code=4001, reason="unknown member")
                return
            if protocol_version != relay_config.RELAY_PROTOCOL_VERSION:
                conn.close(code=4003, reason="protocol mismatch")
                return
            # 配对凭据兑换：原子、首次成功即作废（重连不再有效由引擎侧
            # 单活动通道保证——已认证连接不受重生成影响，吊销才断）
            if not pairing.redeem_pairing(member=member, token=credential,
                                          fingerprint=fingerprint):
                conn.close(code=4001, reason="pairing token invalid")
                return
            # 接替旧连接：单活动通道（新连接认证成功才接管）
            with self._ws_lock:
                previous = self._active_ws
                self._active_ws = conn
                self._active_generation += 1
                generation = self._active_generation
            if previous is not None and previous is not conn:
                try:
                    previous.close(code=1000, reason="replaced")
                except Exception:  # noqa: BLE001
                    pass
            conn.send(json.dumps({"relay": {"kind": "hello_ok",
                                            "generation": generation}},
                                 ensure_ascii=False))
            logger.info("bot-group relay dial link up member=%s generation=%d",
                        member, generation)
            self._serve_loop(conn, generation)
        except (ValueError, KeyError, TypeError, ConnectionClosed):
            return

    def _serve_loop(self, conn, generation: int) -> None:
        import time as time_mod
        last_beat = time_mod.monotonic()
        while not self._stop.is_set():
            # 本地事件回传（流线程塞入，监听线程统一发送）
            while True:
                try:
                    pending = self._send_queue.get_nowait()
                except queue.Empty:
                    break
                conn.send(pending)
            try:
                raw = conn.recv(timeout=0.5)
                envelope = json.loads(raw)
                self._route_inbound(conn, envelope, generation)
            except TimeoutError:
                pass
            except Exception:  # noqa: BLE001 断链
                break
            if time_mod.monotonic() - last_beat >= HEARTBEAT_INTERVAL:
                conn.send(json.dumps({"relay": {"kind": "heartbeat",
                                                "generation": generation}},
                                     ensure_ascii=False))
                last_beat = time_mod.monotonic()
        with self._ws_lock:
            if self._active_ws is conn:
                self._active_ws = None
        self._streams.close_all()

    def _route_inbound(self, ws, envelope: dict, generation: int) -> None:
        """引擎→插件帧：与出站方向同口径（frame 转发本机 gateway、
        mcp_snapshot 实时问询、http_response 回传 loopback MCP 代理）。"""
        meta = envelope.get("relay") or {}
        kind = meta.get("kind")
        frame_gen = meta.get("generation")
        if kind not in ("hello",) and frame_gen != generation:
            logger.warning("bot-group relay stale dial frame dropped kind=%s gen=%s",
                           kind, frame_gen)
            return
        if kind == "mcp_snapshot_request":
            snapshot = mcp_digest.snapshot(self.mcp_python,
                                           code_version=relay_config.plugin_version())
            ws.send(json.dumps({
                "relay": {"kind": "mcp_report", "request_id": meta.get("request_id"),
                          "generation": generation},
                "mcp_fingerprint": snapshot["mcp_fingerprint"],
                "code_version": snapshot["code_version"],
                "mcp_python": snapshot["mcp_python"],
                "mcp_entry": snapshot["mcp_entry"],
            }, ensure_ascii=False))
            return
        if kind == "http_response":
            # loopback MCP 代理的应答：交还代理线程
            from . import mcpproxy
            mcpproxy.deliver_http_response(meta.get("request_id"),
                                           envelope.get("status"),
                                           envelope.get("headers") or {},
                                           envelope.get("body") or "")
            return
        if kind != "frame":
            return
        stream = meta.get("stream")
        frame = envelope.get("frame")
        if not isinstance(stream, str) or frame is None:
            return
        try:
            local = self._streams.open(stream)
            local.send_text(json.dumps(frame, ensure_ascii=False))
        except streams_mod.StreamClosed:
            logger.warning("bot-group relay dial frame dropped stream=%s (closed)", stream)

    def _on_local_event(self, local: streams_mod.LocalGatewayStream, frame: dict) -> None:
        envelope = json.dumps({
            "relay": {"kind": "frame", "stream": local.stream,
                      "generation": self._active_generation},
            "frame": frame,
        }, ensure_ascii=False)
        self._send_queue.put(envelope)

    def forward_http_request(self, request_id: str, method: str, path: str,
                             headers: dict, body: str) -> None:
        """MCP loopback 代理请求 → 沿已认证通道发给引擎。"""
        with self._ws_lock:
            ws = self._active_ws
        if ws is None:
            raise ConnectionError("listen 通道未认证，无法转发 MCP 请求")
        ws.send(json.dumps({
            "relay": {"kind": "http_request", "request_id": request_id,
                      "generation": self._active_generation},
            "method": method, "path": path, "headers": headers, "body": body,
        }, ensure_ascii=False))


def listen_serve(*, address: str, port: int, advertise: str = "",
                 member: str = "", home: str = "", mcp_python: str = "",
                 cert_path: str | None = None, key_path: str | None = None,
                 tls: bool = True, link_to_file: bool = False) -> dict:
    """listen 工具入口：校验→证书→监听→签发配对链接。返回结果 dict。

    失败（地址非法/绑定失败/证书缺失）抛 ValueError/RuntimeError，调用方
    如实报错；成功前无任何状态残留。"""
    bind = validate_bind_address(address)
    scheme = "wss" if tls else "ws"
    cert, key = "", ""
    fingerprint = ""
    if tls:
        cert, key = _ensure_cert(cert_path, key_path)
        fingerprint = certificate_fingerprint(cert)
    member = member or os.environ.get("HERMES_PROFILE") or ""
    if not member:
        raise ValueError("必须提供 member（本机成员名）")
    home = home or os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    server = ListenServer(address=bind, port=port, cert_path=cert, key_path=key,
                          advertise=advertise or bind, member=member, home=home,
                          mcp_python=mcp_python, scheme=scheme)
    server.start()  # 绑定失败抛 RuntimeError，保持原状
    issued = pairing.issue_pairing(member=member, advertise=server.advertise,
                                   port=server.bound_port(),
                                   fingerprint=fingerprint, scheme=scheme)
    result = {
        "status": "listening",
        "address": bind,
        "port": server.bound_port(),
        "advertise": server.advertise,
        "scheme": scheme,
        "fingerprint": fingerprint,
        "fingerprint_short": fingerprint[:8],
        "pairing_url": issued["pairing_url"],
        "expires_in_seconds": pairing.PAIRING_TTL_SECONDS,
    }
    if link_to_file:
        result["link_file"] = pairing.write_link_file(issued["pairing_url"])
    return result
