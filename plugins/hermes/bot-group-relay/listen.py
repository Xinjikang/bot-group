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
import sys
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
                 scheme: str = "wss", mcp_entry: str = ""):
        self.address = address
        self.port = port
        self.cert_path = cert_path
        self.key_path = key_path
        self.advertise = advertise or address
        self.member = member
        self.home = home
        self.mcp_python = mcp_python
        self.mcp_entry = mcp_entry
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
            kind = (hello.get('relay') or {}).get('kind')
            if kind == 'pairing_probe':
                version = relay_config.plugin_version()
                if credential or hello.get('commitment') or (version and hello.get('code_version') != version):
                    conn.close(code=4003, reason='version mismatch')
                    return
                expiry = pairing.verify_dial_offer(self.member, hello.get('pairing_ticket'), fingerprint)
                if expiry is None:
                    conn.close(code=4001, reason='pairing not available')
                    return
                conn.send(json.dumps({'relay': {'kind': 'pairing_verified', 'member': self.member,
                                                 'expires_at': expiry, 'activation_mode': 'after_confirm'}}))
                return
            # No first-join credential becomes active during the independent probe.
            if pairing.verify_persistent(member, credential):
                persistent = ''
            elif hello.get('activate_pairing') is True:
                if not pairing.activate_dial_offer(member, credential, hello.get('pairing_ticket'), fingerprint):
                    conn.close(code=4001, reason='activation unavailable')
                    return
                persistent = ''
            elif pairing.redeem_pairing(member=member, token=credential, fingerprint=fingerprint):
                persistent = pairing.issue_persistent(member)
            else:
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
            # hello_ok 下发帧代 + 持久身份凭据（首次配对成功才下发新凭据，
            # 引擎侧轮换进 dial_endpoint；重连认证不再下发）。member=本插件
            # 宿主实际成员（HERMES_PROFILE）主动上报（req#175 验收 5/6）：
            # 引擎核验与 dial_endpoint 归属一致（防旧连接冒名点亮新节点）。
            hello_ok = {"relay": {"kind": "hello_ok", "generation": generation,
                                  "member": self.member}}
            if persistent:
                hello_ok["relay"]["credential"] = persistent
            conn.send(json.dumps(hello_ok, ensure_ascii=False))
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
            snapshot = mcp_digest.snapshot(self._mcp_python(),
                                           code_version=relay_config.plugin_version())
            # manager node2084：随包 MCP 的 GROUP_ENGINE_URL 必须指向本次
            # loopback 代理实际端口——快照带上当前代理端口，引擎侧落库后
            # 按节点方向渲染。
            from . import mcpproxy
            proxy_port = (mcpproxy._ACTIVE_SERVER.port
                          if mcpproxy._ACTIVE_SERVER is not None else 0)
            ws.send(json.dumps({
                "relay": {"kind": "mcp_report", "request_id": meta.get("request_id"),
                          "generation": generation},
                "mcp_fingerprint": snapshot["mcp_fingerprint"],
                "code_version": snapshot["code_version"],
                "mcp_python": snapshot["mcp_python"],
                "mcp_entry": snapshot["mcp_entry"],
                "proxy_port": proxy_port,
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
        if kind == "mcp_verify_request":
            # manager node2102：引擎发起的实际受限只读验证——本机经 loopback
            # 代理发起 GET（代理打包 http_request 帧沿已认证通道回引擎执行
            # 真实鉴权），状态回传。**必须独立线程**：本分支同步 urlopen 会
            # 阻塞收帧循环，http_response 帧无法派发给代理 handler（20s 超时
            # 链——收帧线程与代理应答同链路，不能同线程同步等）。
            threading.Thread(target=self._handle_mcp_verify_request,
                             args=(ws, meta, envelope, generation),
                             name="bot-group-mcp-verify", daemon=True).start()
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

    def _mcp_entry(self) -> str | None:
        """随包 MCP stdio 入口 mcp/server.py：发布态在插件包根/mcp/，
        源码态回退仓库根/mcp/；测试可经 listen_serve(mcp_entry=) 显式给。"""
        if self.mcp_entry:
            return self.mcp_entry
        for base in (Path(__file__).resolve().parent,
                     Path(__file__).resolve().parent.parents[2]):
            entry = base / "mcp" / "server.py"
            if entry.is_file():
                return str(entry)
        return None

    def _mcp_python(self) -> str:
        """随包 MCP 解释器解析（manager 12793）：显式参数/PlatformConfig
        配置 → 插件目录 venv（安装脚本生成的解释器）→ 宿主 sys.executable
        兜底。宿主 Python 没装 mcp 依赖时，常规 listen 入口必须仍用
        插件 venv 解释器（install.sh 安装目标），不落回宿主解释器。"""
        configured = self.mcp_python or relay_config.platform_mcp_python()
        resolved = mcp_digest.mcp_python(configured)
        if resolved and os.path.isfile(resolved):
            return resolved
        return sys.executable

    def _handle_mcp_verify_request(self, ws, meta: dict, envelope: dict,
                                   generation: int) -> None:
        """mcp_verify_request 的独立处理线程（manager node2102/2106）：用
        实际配置的随包程序调用 group_list_members 工具（spawn mcp/server.py
        stdio，JSON-RPC initialize+tools/call），工具经 GROUP_ENGINE_URL=
        本机 loopback 代理走已认证隧道回引擎鉴权。结果含成员名单回传。
        入口缺失/代理未启/调用失败如实回传错误。收帧线程不阻塞（同步
        调用会卡死同链路 http_response 派发）。"""
        request_id = meta.get("request_id") or ""
        status = 502
        body = '{"error": "proxy not running"}'
        from . import mcpproxy
        proxy = mcpproxy._ACTIVE_SERVER
        entry = self._mcp_entry()
        if proxy is not None and entry and os.path.isfile(entry):
            try:
                status, body = self._call_group_list_members(
                    entry, proxy.port, envelope, request_id)
            except Exception as exc:  # noqa: BLE001 如实回传
                logger.warning("bot-group relay verify tool call failed: %s", exc)
                status = 502
                body = json.dumps({"error": f"tool call failed: {exc}"},
                                  ensure_ascii=False)
        elif proxy is None:
            body = '{"error": "proxy not running"}'
        else:
            body = json.dumps({"error": "packaged mcp entry unavailable"},
                              ensure_ascii=False)
        try:
            ws.send(json.dumps({
                "relay": {"kind": "mcp_verify_response", "request_id": request_id,
                          "generation": generation},
                "status": status, "body": body,
            }, ensure_ascii=False))
        except Exception:  # noqa: BLE001 断链时验证结果作废（换代清）
            logger.warning("bot-group relay verify response send failed")

    def _call_group_list_members(self, entry: str, proxy_port: int,
                                 envelope: dict, request_id: str) -> tuple[int, str]:
        """spawn 随包 mcp/server.py（stdio）调 group_list_members 工具。

        子进程环境白名单构造（不继承宿主 env）：GROUP_ENGINE_URL 指向本机
        代理、凭据用本次会话凭据、执行范围用引擎下发的 XDG_GROUP_ENGINE_
        EXECUTION——工具沿代理隧道回引擎按成员会话范围鉴权。"""
        import subprocess
        env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", "") or "C:\\Windows",
            "TEMP": os.environ.get("TEMP", "") or os.environ.get("TMP", ""),
            "TMP": os.environ.get("TMP", "") or os.environ.get("TEMP", ""),
            "PYTHONUTF8": "1",
            "GROUP_ENGINE_URL": f"http://127.0.0.1:{proxy_port}",
            "XDG_GROUP_ENGINE_URL": "",
            "GROUP_ENGINE_TOKEN": str(envelope.get("x_group_key") or ""),
            "XDG_GROUP_ENGINE_EXECUTION":
                json.dumps(envelope.get("execution") or {}, ensure_ascii=False),
            "NO_PROXY": "127.0.0.1",
            "HERMES_PROFILE": self.member,
        }
        python = self._mcp_python()
        # 二进制管道（text 模式 universal newlines 会把 '\n' 转 '\r\n'，
        # 与 mcp 库按 b'\n' 分帧不一致——曾致 initialize 静默无响应）
        proc = subprocess.Popen(
            [python, "-B", entry],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env,
            cwd=str(Path(entry).resolve().parent))

        def rpc(payload: dict) -> dict:
            if proc.stdin is None or proc.stdout is None:
                raise RuntimeError("stdio closed")
            proc.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError("stdio closed before reply")
            return json.loads(line)

        def notify(payload: dict) -> None:
            # 通知无响应：只写不读（读过会永久阻塞）
            if proc.stdin is None:
                raise RuntimeError("stdio closed")
            proc.stdin.write(json.dumps(payload).encode("utf-8") + b"\n")
            proc.stdin.flush()

        try:
            rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "bot-group-relay-verify",
                                           "version": "1.0"}}})
            notify({"jsonrpc": "2.0", "method": "notifications/initialized",
                    "params": {}})
            reply = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": "group_list_members",
                                    "arguments": {}}})
        finally:
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                proc.kill()
        if reply.get("error"):
            return 502, json.dumps({"error": str(reply["error"])[:500]},
                                   ensure_ascii=False)
        result = reply.get("result") or {}
        content = result.get("content") or []
        if result.get("isError"):
            text = (content[0].get("text", "") if content else "")
            return 502, text[:2000] or '{"error": "tool reported error"}'
        text = content[0].get("text", "{}") if content else "{}"
        try:
            data = json.loads(text)
        except ValueError:
            return 502, json.dumps(
                {"error": f"unexpected tool result: {text[:300]}"},
                ensure_ascii=False)
        if not isinstance(data, dict):
            return 502, json.dumps({"error": "unexpected tool result shape"},
                                   ensure_ascii=False)
        if data.get("error"):
            # 工具层错误（如 engine 403: Session project mismatch）如实回传
            error_text = str(data["error"])
            if "engine 403" in error_text or "mismatch" in error_text:
                return 403, json.dumps({"error": error_text[:500]},
                                       ensure_ascii=False)
            return 502, json.dumps({"error": error_text[:500]},
                                   ensure_ascii=False)
        return 200, text

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
                 tls: bool = True, link_to_file: bool = False,
                 proxy_port: int = 0, no_proxy: bool = False,
                 mcp_entry: str = "") -> dict:
    """listen 工具入口：校验→证书→监听→签发配对链接→启动 MCP loopback
    代理。返回结果 dict（含 proxy_port——ECS 本地 GROUP_ENGINE_URL 指向
    http://127.0.0.1:<proxy_port>）。

    manager node2060 ②：正常 listen 路径必须启动随包 MCP 代理
    （no_proxy=True 才跳过）；失败（地址非法/绑定失败/证书缺失）抛
    ValueError/RuntimeError/OSError，调用方如实报错；成功前无状态残留。

    存活周期（manager node2060 ①）：由调用方进程持有——私聊入口由网关
    进程持有 daemon 线程；CLI 入口 dispatch 前台运行（Ctrl-C 停止）。"""
    bind = validate_bind_address(address)
    scheme = "wss" if tls else "ws"
    cert, key = "", ""
    fingerprint = ""
    if tls:
        cert, key = _ensure_cert(cert_path, key_path)
        fingerprint = certificate_fingerprint(cert)
    member = member or relay_config.host_member()
    if not member:
        raise ValueError("无法识别当前 Hermes 成员，请从对应的 Hermes 配置启动插件")
    home = home or relay_config.host_home()
    server = ListenServer(address=bind, port=port, cert_path=cert, key_path=key,
                          advertise=advertise or bind, member=member, home=home,
                          mcp_python=mcp_python, scheme=scheme, mcp_entry=mcp_entry)
    try:
        server.start()  # 绑定失败抛 RuntimeError，保持原状
        issued = pairing.issue_pairing(member=member, advertise=server.advertise,
                                       port=server.bound_port(),
                                       fingerprint=fingerprint, scheme=scheme)
        proxy = None
        if not no_proxy:
            # 随包 MCP loopback 代理（manager node2060 ②）：正常路径必启动
            from . import mcpproxy
            proxy = mcpproxy.start_proxy(server, proxy_port)
        if link_to_file:
            link_file = pairing.write_link_file(issued["pairing_url"])
    except Exception:
        # 任何一步失败都清理已建资源（server/代理），保持原状如实报错
        from . import mcpproxy
        if not no_proxy:
            mcpproxy.stop_proxy()
        server.stop()
        raise
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
        "proxy_port": proxy.port if proxy is not None else 0,
        # ECS 本地 MCP 进程的 GROUP_ENGINE_URL 指向值（代理未启时为空）
        "proxy_url": f"http://127.0.0.1:{proxy.port}" if proxy is not None else "",
    }
    if link_to_file:
        result["link_file"] = link_file
    # 存活周期：调用方进程持有。CLI 前台运行由 cli.dispatch 负责等待信号；
    # 私聊由网关进程持有 daemon 线程。返回 server 引用供 CLI 停止清理。
    result["_server"] = server
    return result
