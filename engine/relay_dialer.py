"""req#173 方向 B：引擎出站拨号器（按成员认证并接替活动通道）。

按配对记录（relay_nodes.dial_endpoint）向插件监听端点拨入 WSS：
- TLS/证书指纹校验（配对记录 fingerprint，自签证书必填指纹）；
- 认证复用配对公钥（成员凭据，与方向 A 同表同口径）；
- 单活动通道：与 relay_server 共用 _LINKS 注册表，新通道认证成功才接管，
  旧连接主动关闭不留孤儿；
- 重连指数退避（1/2/4/8/16/30s 封顶）+ 次数上限，耗尽后停，等管理员
  重试/切回；不设计时器、不做 draining/双通道。

切换状态机（relay.begin_switch/complete_switch）：用户点切换 → 记意图+目标
方向（持久化）→ 暂停派发（queue._relay_node_gate 看 switch_state）→ 新方向认证接管 → 清切换态（relay_ws 或本模块握手成功时 complete_switch）。
offline 原因动作驱动：切换中=switch（不告警不重试任务），其余=unexpected。
"""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import socket
import ssl
import threading
import time
import urllib.parse
from contextlib import closing
from websockets.exceptions import ConnectionClosed

from . import db, relay, relay_server, relay_retry

logger = logging.getLogger(__name__)

# 最近一次拨号失败（页面按 DNS/TCP/TLS/认证阶段显示；成功即清）。只保留
# 阶段与地址，不保存配对凭据。
_FAILURES: dict[str, dict] = {}
_FAILURES_LOCK = threading.Lock()

# 重连退避（指数封顶）与尝试上限：耗尽后 abandoned，等管理员动作。
BACKOFF_SEQUENCE = relay_retry.DELAYS
MAX_ATTEMPTS = len(BACKOFF_SEQUENCE)
HEARTBEAT_INTERVAL = 30.0
RECV_TIMEOUT = 15.0


def _record_failure(member: str, stage: str, address: str, error: str, *, terminal=False) -> None:
    with _FAILURES_LOCK:
        _FAILURES[member] = {
            'stage': stage, 'address': address, 'error': str(error)[:300],
            'ts': time.time(), 'terminal': terminal,
        }


def dial_failure(member: str) -> dict | None:
    """页面展示用：最近一次拨号失败阶段/地址/原因（成功或停用后清除）。"""
    with _FAILURES_LOCK:
        item = _FAILURES.get(member)
        return dict(item) if item else None


def _clear_failure(member: str) -> None:
    with _FAILURES_LOCK:
        _FAILURES.pop(member, None)


def _classify_connect_error(exc: Exception) -> str:
    text = str(exc).lower()
    if isinstance(exc, socket.gaierror) or any(part in text for part in (
            'getaddrinfo', 'name or service not known', 'nodename nor servname',
            'temporary failure in name resolution', 'name does not resolve')):
        return 'dns'
    if isinstance(exc, ssl.SSLError) or any(part in text for part in (
            'certificate', 'ssl', 'tls')):
        return 'tls'
    return 'tcp'


def _endpoint_address(endpoint: dict) -> str:
    host = str(endpoint.get('host') or '')
    return f'{host}:{endpoint.get("port") or ""}' if host else ''


def probe_endpoint(host: str, port: int, fingerprint: str,
                   timeout: float = 5.0) -> dict:
    """只读预检：解析/连通/证书握手并核对指纹，不兑换配对凭据、不保存。

    用于配对导入时先校验可达性与证书；认证与 MCP 阶段由实际拨号完成。
    返回 {ok, stage, address, error}；stage 为 dns/tcp/tls/ok。
    """
    host = str(host or '').strip()
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 0
    address = f'{host}:{port}'
    if not host or not 1 <= port <= 65535:
        return {'ok': False, 'stage': 'dns', 'address': address,
                'error': '地址或端口无效'}
    expected = str(fingerprint or '').strip().lower().replace(':', '')
    if len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
        return {'ok': False, 'stage': 'tls', 'address': address,
                'error': '证书指纹必须是 64 位十六进制'}
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        return {'ok': False, 'stage': 'dns', 'address': address,
                'error': str(exc)[:200]}
    if not infos:
        return {'ok': False, 'stage': 'dns', 'address': address,
                'error': '域名没有可用地址'}
    sock = None
    try:
        last = None
        for family, socktype, proto, _canon, sockaddr in infos:
            try:
                sock = socket.socket(family, socktype, proto)
                sock.settimeout(timeout)
                sock.connect(sockaddr)
                break
            except OSError as exc:
                last = exc
                if sock is not None:
                    sock.close()
                sock = None
        if sock is None:
            raise last or OSError('连接失败')
    except OSError as exc:
        return {'ok': False, 'stage': _classify_connect_error(exc), 'address': address,
                'error': str(exc)[:200]}
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        tls_sock = context.wrap_socket(sock, server_hostname=host)
        sock = tls_sock
        cert = tls_sock.getpeercert(binary_form=True) or b''
        actual = hashlib.sha256(cert).hexdigest()
        if actual != expected:
            return {'ok': False, 'stage': 'tls', 'address': address,
                    'error': f'证书指纹不匹配（实际 {actual[:16]}…，链接 {expected[:16]}…）'}
        return {'ok': True, 'stage': 'ok', 'address': address, 'error': ''}
    except ssl.SSLError as exc:
        return {'ok': False, 'stage': 'tls', 'address': address,
                'error': str(exc)[:200]}
    except OSError as exc:
        return {'ok': False, 'stage': 'tcp', 'address': address,
                'error': str(exc)[:200]}
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


class DialLink:
    """引擎出站连接（方向 B）的活跃通道：与 NodeLink 接口兼容，注册进
    relay_server._LINKS——send_text/close/transports/request_mcp_snapshot 同构。

    双代（manager node2057 ②）：generation=插件 hello_ok 下发的帧代
    （帧路由/心跳出站用）；db_generation=引擎本地 generation_advance 后的
    状态代（心跳/mcp_report/离线写库用，本地代贯穿状态写入）。"""

    protocol_version = relay_server.RELAY_PROTOCOL_VERSION

    def __init__(self, member: str, ws, generation: int, peer_ip: str | None,
                 db_generation: int = 0):
        self.member = member
        self.ws = ws                  # websockets.sync ClientConnection（sync send/recv/close）
        self.generation = generation
        self.db_generation = db_generation or generation
        self.peer_ip = peer_ip
        self.loop = None              # sync 客户端无 asyncio loop；close 走 sync 分支
        self._send_lock = threading.Lock()
        self.send_failure = ""
        self._stream_seq = 0
        self.transports: dict[str, relay_server.RelayTransport] = {}
        self._snapshot_seq = 0
        self._snapshot_waiters: dict[str, queue.Queue] = {}
        self._snapshot_waiters_lock = threading.Lock()
        self.last_heartbeat = time.time()
        self.closed = False

    def next_stream(self) -> str:
        self._stream_seq += 1
        return f"{self.member}-{self._stream_seq}"

    def open_transport(self) -> relay_server.RelayTransport:
        transport = relay_server.RelayTransport(self, self.next_stream())
        self.transports[transport.stream] = transport
        return transport

    def request_mcp_snapshot(self, timeout: float) -> dict | None:
        if self.closed:
            return None
        with self._snapshot_waiters_lock:
            self._snapshot_seq += 1
            request_id = f"snap-{self._snapshot_seq}"
            waiter: queue.Queue = queue.Queue(maxsize=1)
            self._snapshot_waiters[request_id] = waiter
        try:
            self.send_text(json.dumps({
                "relay": {"kind": "mcp_snapshot_request", "request_id": request_id,
                          "generation": self.generation},
            }, ensure_ascii=False))
        except ConnectionError:
            with self._snapshot_waiters_lock:
                self._snapshot_waiters.pop(request_id, None)
            return None
        try:
            return waiter.get(timeout=timeout)
        except queue.Empty:
            with self._snapshot_waiters_lock:
                self._snapshot_waiters.pop(request_id, None)
            return None

    def _on_snapshot_response(self, request_id: str, report: dict) -> None:
        # manager node2088：写库代必须用本连接的本地状态代（db_generation）
        # ——首次真实拨号帧代=1、库代=2，用帧代写库（WHERE generation=?）
        # 不命中，快照代理端口落库 0、GROUP_ENGINE_URL 回落错误地址。
        # 线上帧校验仍用插件帧代（generation），旧连接不得借用新代。
        report["generation"] = self.db_generation or self.generation
        with self._snapshot_waiters_lock:
            waiter = self._snapshot_waiters.pop(request_id, None)
        if waiter is not None:
            waiter.put(report)

    def request_mcp_verify(self, x_group_key: str, project: str,
                           execution: dict | None = None,
                           timeout: float = 20.0) -> dict | None:
        """manager node2102：配对导入并认证后的实际受限只读 MCP 验证——
        发 mcp_verify_request 帧给插件（带成员会话凭据+执行范围，仅沿已
        认证通道下发），插件用随包 MCP 程序实际调用 group_list_members
        （沿 loopback 代理回引擎鉴权），状态回传。返回 {status, body,
        generation} 或 None（超时/断链）；generation 是发起时的本地状态
        代——写库必须用它，旧连接迟到的回执不得点亮新代。"""
        if self.closed:
            return None
        verify_generation = self.db_generation or self.generation
        with self._snapshot_waiters_lock:
            self._snapshot_seq += 1
            request_id = f"verify-{self._snapshot_seq}"
            waiter: queue.Queue = queue.Queue(maxsize=1)
            self._snapshot_waiters[request_id] = waiter
        try:
            self.send_text(json.dumps({
                "relay": {"kind": "mcp_verify_request", "request_id": request_id,
                          "generation": self.generation},
                "path": "/members", "query": f"project={urllib.parse.quote(project)}",
                "x_group_key": x_group_key,
                "project": project,
                "execution": execution or {},
            }, ensure_ascii=False))
        except ConnectionError:
            with self._snapshot_waiters_lock:
                self._snapshot_waiters.pop(request_id, None)
            return None
        try:
            report = waiter.get(timeout=timeout)
        except queue.Empty:
            with self._snapshot_waiters_lock:
                self._snapshot_waiters.pop(request_id, None)
            return None
        return dict(report, generation=verify_generation)

    def _on_verify_response(self, request_id: str, report: dict) -> None:
        with self._snapshot_waiters_lock:
            waiter = self._snapshot_waiters.pop(request_id, None)
        if waiter is not None:
            waiter.put(report)

    def send_text(self, text: str) -> None:
        if self.closed:
            raise ConnectionError("relay link closed")
        with self._send_lock:
            try:
                self.ws.send(text)
            except Exception as exc:  # noqa: BLE001 断链即离线
                self.send_failure = f"send failed: {type(exc).__name__}"
                relay_server._shutdown_link(self, self.send_failure, announce=False)
                raise ConnectionError("relay link send failed") from exc

    def close(self) -> None:
        relay_server._shutdown_link(self, "dial link closed")


# ---------------- 拨号任务管理（per-member 后台线程） ----------------

_TASKS: dict[str, dict] = {}
_TASKS_LOCK = threading.Lock()


def _task_state(member: str) -> str:
    """拨号器运行态（页面显示）：idle/connecting/connected/backoff/abandoned。"""
    with _TASKS_LOCK:
        task = _TASKS.get(member)
        if task is None:
            return "idle"
        link = relay_server.active_link(member)
        if link is not None and isinstance(link, DialLink):
            return "connected"
        if task["abandoned"]:
            return "abandoned"
        if task["thread"] is not None and task["thread"].is_alive():
            return "backoff" if task["attempt"] > 0 else "connecting"
        return "idle"


def stop_dial(member: str) -> None:
    """停该成员的拨号任务与 dial 连接（切回/停用动作）。

    置停止标志 + 断活跃连接后 join 任务线程（帧循环在连接断开后很快退出），
    避免残留线程让下一个测试/操作误复用旧任务状态。"""
    with _TASKS_LOCK:
        task = _TASKS.get(member)
        if task is not None:
            task["stop"].set()
    link = relay_server.active_link(member)
    if link is not None and isinstance(link, DialLink):
        link.close()
    if task is not None and task["thread"] is not None and \
            task["thread"] is not threading.current_thread():
        task["thread"].join(timeout=10)
    _clear_failure(member)


def _hello_payload(conn, member: str) -> dict | None:
    """构造拨号 hello：成员凭据（一次性配对凭据，随 dial_endpoint 保存，
    拨号 hello 出示、插件侧兑换）+ 引擎代码版本 + 协议版本 + 方向。"""
    row = relay.read_node(conn, member)
    if row is None or row["credential_revoked"]:
        return None
    endpoint = relay.read_dial_endpoint(conn, member) or {}
    first_join = bool(endpoint.get('activate_pairing'))
    ticket = endpoint.get('activation_ticket')
    activation = {'activate_pairing': True} if first_join else {}
    if first_join and ticket and endpoint['activation_expires_at'] > relay._now():
        activation.update(pairing_ticket=ticket, fingerprint=endpoint['fingerprint'])
    return {
        "relay": {"kind": "hello"},
        "member": row['host_profile'],
        "credential": str(endpoint.get("pairing_token") or ""),
        **activation,
        "code_version": relay_server._engine_release_version() or "",
        **relay_server.relay_protocol.offer(),
        "direction": "dial",
    }


def _verify_fingerprint(ws, expected: str, member: str) -> bool:
    if not expected:
        return True
    cert = ws.socket.getpeercert(binary_form=True)
    actual = hashlib.sha256(cert).hexdigest()
    return actual == expected


class PairingProbeError(ValueError):
    def __init__(self, stage, reason):
        self.stage, self.reason = stage, reason
        super().__init__(reason)


def _authenticate_dial(member: str, endpoint: dict, payload: dict, *, probe=False):
    """Verify the pinned peer before sending a pairing ticket; close on failure."""
    from websockets.sync.client import connect as ws_connect
    scheme = "wss" if endpoint.get("tls") else "ws"
    host = endpoint["host"]
    uri = f"{scheme}://{f'[{host}]' if ':' in host else host}:{endpoint['port']}"
    fingerprint = str(endpoint.get("fingerprint") or "").strip().lower().replace(":", "")
    context = None
    if scheme == "wss":
        context = ssl.create_default_context()
        if fingerprint:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
    stage = "tcp"
    ws = None
    try:
        ws = ws_connect(uri, ssl=context, max_size=16 * 1024 * 1024, open_timeout=8, close_timeout=1, proxy=None)
        stage = "tls"
        if fingerprint and not _verify_fingerprint(ws, fingerprint, member):
            raise PairingProbeError(stage, "证书指纹不匹配")
        stage = "auth"
        ws.send(json.dumps(payload, ensure_ascii=False))
        meta = json.loads(ws.recv(timeout=8))["relay"]
        ws.relay_protocol_version = relay_server.relay_protocol.selected(meta)
        if probe:
            import math
            expiry = meta.get('expires_at')
            if (meta.get('kind') != 'pairing_verified' or meta.get('activation_mode') != 'after_confirm'
                    or type(expiry) not in (int, float) or not math.isfinite(expiry)):
                raise PairingProbeError('auth', 'invalid_receipt')
            if meta.get('member') != member:
                raise PairingProbeError('identity', 'member_mismatch')
            ws.close()
            return meta['member'], expiry
        generation = int(meta.get("generation") or 0)
        if meta.get("kind") != "hello_ok" or generation <= 0:
            raise PairingProbeError(stage, "插件未返回有效认证回执")
        reported = meta.get("member")
        if (reported or payload.get('activate_pairing')) and reported != payload['member']:
            raise PairingProbeError(stage, "成员上报与配对链接不一致")
        persistent = meta.get('credential') or ''
        if not isinstance(persistent, str) or (persistent and payload.get('activate_pairing')):
            raise PairingProbeError(stage, '插件返回了非预期的连接凭据')
        return ws, generation, persistent
    except Exception as exc:  # network, protocol or authentication failure
        if ws is not None:
            ws.close()
        refused = relay_retry.rejection(exc)
        if refused:
            stage, detail = refused
        elif stage == "tcp":
            stage = _classify_connect_error(exc)
        invalid = isinstance(exc, (ValueError, KeyError, TypeError, AttributeError))
        if probe:
            if isinstance(exc, PairingProbeError) and stage != "tls":
                raise
            reason = detail if refused and stage == "protocol" else {
                'tls': 'fingerprint_mismatch', 'auth': 'pairing_refused'}.get(stage, 'unreachable')
            raise PairingProbeError(stage, reason) from None
        if not refused:
            detail = exc.reason if isinstance(exc, PairingProbeError) else relay_retry.transient_reason(stage, exc)
        _record_failure(member, stage, uri, detail, terminal=bool(refused or invalid))
        return None


def verify_first_pairing(member: str, endpoint: dict):
    """Ask the pinned plugin for its verified identity without reserving its ticket."""
    return _authenticate_dial(member, endpoint, {
        "relay": {"kind": "pairing_probe"}, "member": member,
        "pairing_ticket": endpoint["pairing_token"], "fingerprint": endpoint['fingerprint'],
        "direction": "dial",
        "code_version": relay_server._engine_release_version() or "",
        **relay_server.relay_protocol.offer(),
    }, probe=True)


def connect_endpoint(conn, member, endpoint):
    row = relay.read_node(conn, member)
    payload = _hello_payload(conn, member)
    if not payload or not payload["credential"]:
        _record_failure(member, "auth", _endpoint_address(endpoint),
                        "缺少配对凭据，请重新导入配对链接", terminal=True)
        return None
    has_ticket = bool(endpoint.get('activation_ticket'))
    if has_ticket and not payload.get('pairing_ticket'):
        relay.discard_dial_activation_ticket(conn, member, endpoint, row['dial_endpoint'])
        has_ticket = False
    authenticated = _authenticate_dial(member, endpoint, payload)
    if authenticated is None:
        return None
    ws, remote_generation, persistent_credential = authenticated
    if has_ticket:
        relay.discard_dial_activation_ticket(conn, member, endpoint, row['dial_endpoint'])
    if persistent_credential:
        relay.update_dial_pairing_token(conn, member, persistent_credential)
    return ws, remote_generation


def dial_once(member: str, *, initial=None, round_state=None) -> bool:
    """Authenticate or reuse a first-join connection, then run the frame loop."""
    conn = db.get_connection()
    try:
        endpoint = relay.read_dial_endpoint(conn, member)
        row = relay.read_node(conn, member)
    finally:
        conn.close()
    if endpoint is None or row is None or row["credential_revoked"]:
        if initial is not None:
            initial[0].close()
        _record_failure(member, "auth", _endpoint_address(endpoint or {}),
                        "未配置拨号端点或凭据已吊销", terminal=True)
        return False
    if row["direction"] != "dial" and row["switch_target"] != "dial":
        if initial:
            initial[0].close()
        _record_failure(member, "direction", _endpoint_address(endpoint), "当前方向为监听", terminal=True)
        return False
    if initial is None:
        with closing(db.get_connection()) as conn:
            initial = connect_endpoint(conn, member, endpoint)
        if initial is None:
            return False
    ws, remote_generation = initial


    # 注册前复核（动作驱动，防切回竞态）：切换目标已不是 dial（用户切回
    # listen）或非切换时方向已回 listen → 本连接不再被需要，主动关闭不接管。
    conn = db.get_connection()
    try:
        _row = relay.read_node(conn, member)
        _switching = bool(_row and _row["switch_state"] == "switching")
        _target = _row["switch_target"] if _row else ""
        _direction = _row["direction"] if _row else ""
    finally:
        conn.close()
    if _switching:
        if _target != "dial":
            try:
                if _direction == "dial" and _target == "listen":
                    from . import relay_switch
                    with closing(db.get_connection()) as conn:
                        relay_switch.send_return(conn, member, remote_generation, ws.send)
                    return True
                return False
            except (OSError, ConnectionClosed):
                return False
            finally:
                ws.close()
    elif _direction != "dial":
        ws.close(code=4005, reason="direction is listen")
        return False

    # 本地状态代 +1（与方向 A relay_ws 握手同口径），mark_online 按本地代写库
    conn = db.get_connection()
    try:
        db_generation = relay.generation_advance(conn, member)
    finally:
        conn.close()

    # 认证成功：注册通道（单活动通道——接替旧连接，旧连接主动关闭不留孤儿）。
    # link.generation=插件帧代（hello_ok 下发），帧路由/心跳出站用；
    # link.db_generation=本地状态代（心跳/mcp_report/离线写库用）。
    _clear_failure(member)
    link = DialLink(member, ws, remote_generation, peer_ip=endpoint["host"],
                    db_generation=db_generation)
    link.protocol_version = ws.relay_protocol_version
    link.profile_tls = bool(endpoint.get("tls"))
    with relay_server._LINKS_LOCK:
        previous = relay_server._LINKS.get(member)
        relay_server._LINKS[member] = link
    if previous is not None and not previous.closed:
        previous.close()
    conn = db.get_connection()
    try:
        relay.mark_online(conn, member, code_version=row["code_version"] or "",
                          mcp_fingerprint=row["mcp_fingerprint"] or "",
                          generation=db_generation)
        # 切换完成：切换目标 dial 且本连接认证成功——清切换态放异步恢复
        # 步骤（manager node2073：link 已注册能收发帧，从新链 relay lease
        # 做首次在途问询后按仍有效的本地代与目标恢复派发）。
        _row = relay.read_node(conn, member)
        switching = bool(_row and _row["switch_state"] == "switching"
                         and _row["switch_target"] == "dial")
    finally:
        conn.close()
    if _row is None or _row['credential_revoked']:
        link.close()
        return False
    if switching:
        relay.restore_switch_after_handshake(
            member, target="dial", generation=db_generation)
    elif _row and _row["switch_state"] == "switching" and _row["switch_target"] == "listen":
        # The request arrived between the pre-registration check and publication.
        from . import relay_switch
        try:
            with closing(db.get_connection()) as conn:
                relay_switch.send_return(conn, member, remote_generation, link.send_text)
        except ConnectionError:
            pass
    if round_state is not None and round_state.reasons:
        round_state.finish(logger, "relay dial restored", member, "restored")
    else:
        logger.info("relay dial link up member=%s frame_generation=%d db_generation=%d",
                    member, remote_generation, db_generation)
    # 帧循环：收插件帧（heartbeat/mcp_report/frame/mcp_snapshot 响应），
    # 每 HEARTBEAT_INTERVAL 发心跳；曾认证的连接断开后另起一轮。
    last_sent = time.time()
    close_reason = "dial connection closed"
    refused = None
    while True:
        try:
            raw = ws.recv(timeout=RECV_TIMEOUT)
        except TimeoutError:
            raw = None
        except Exception as exc:  # noqa: BLE001 断链
            refused = relay_retry.rejection(exc)
            if refused:
                stage, reason = refused
                _record_failure(member, stage, _endpoint_address(endpoint), reason, terminal=True)
            close_reason = refused[1] if refused else f"dial receive failed: {type(exc).__name__}"
            break
        if raw is not None:
            try:
                envelope = json.loads(raw)
            except ValueError:
                continue
            try:
                relay_server._route_inbound(link, envelope)
            except Exception:  # noqa: BLE001
                logger.exception("relay dial inbound routing failed member=%s", member)
        if time.time() - last_sent >= HEARTBEAT_INTERVAL:
            try:
                link.send_text(json.dumps({
                    "relay": {"kind": "heartbeat", "generation": link.generation}},
                    ensure_ascii=False))
                last_sent = time.time()
            except ConnectionError:
                break
    relay_server._shutdown_link(link, close_reason, announce=False)
    if not refused:
        _record_failure(member, "connection", _endpoint_address(endpoint), link.send_failure or close_reason)
    return True


def dial_loop(member: str, task: dict, initial=None) -> None:
    """Run one bounded retry window, resetting its budget after authentication."""
    window = relay_retry.RetryRound()
    while not task["stop"].is_set():
        window.attempts += 1
        ok = dial_once(member, initial=initial, round_state=window)
        initial = None
        if task["stop"].is_set():
            return
        failure = dial_failure(member) or {}
        with closing(db.get_connection()) as conn:
            row = relay.read_node(conn, member)
        if row is None:
            return
        target = row["switch_target"] or row["direction"]
        if target != "dial" and (ok or failure.get("stage") == "direction" or row["direction"] != "dial"):
            return
        if ok:
            window = relay_retry.RetryRound()
            task["attempt"] = 0
        reason = failure.get("error") or "连接中断"
        if not window.reasons:
            logger.info("relay dial unavailable member=%s stage=%s reason=%s",
                        member, failure.get("stage"), reason)
        window.reasons[reason] += 1
        terminal = failure.get("terminal")
        if ok and not terminal:
            continue
        if terminal or window.attempts > MAX_ATTEMPTS:
            message = reason if terminal else "拨号重连次数耗尽，等待手动重试"
            with _TASKS_LOCK:
                if task["stop"].is_set():
                    return
                with closing(db.get_connection()) as conn, conn:
                    conn.execute("UPDATE relay_nodes SET dial_stopped=? WHERE member=? AND removed=0",
                                 (message, member))
                task["abandoned"] = True
            window.finish(logger, "relay dial abandoned", member, "rejected" if terminal else "exhausted")
            return
        task["attempt"] = window.attempts
        delay = BACKOFF_SEQUENCE[min(window.attempts, len(BACKOFF_SEQUENCE)) - 1]
        task["stop"].wait(delay)
    if initial is not None:
        initial[0].close()


def start_dial(member: str, *, initial=None) -> None:
    """Start one round for this member; repeated requests reuse the active worker."""
    with _TASKS_LOCK:
        task = _TASKS.get(member)
        if task and not task["abandoned"] and task["thread"].is_alive():
            if initial is not None:
                initial[0].close()
            return
        with closing(db.get_connection()) as conn, conn:
            conn.execute("UPDATE relay_nodes SET dial_stopped='' WHERE member=? AND dial_stopped!=''", (member,))
            # Earlier engines kept retry exhaustion in the direction-switch error field.
            conn.execute("UPDATE relay_nodes SET switch_error='' WHERE member=? AND switch_error=?",
                         (member, "拨号重连次数耗尽，等待重试或切回"))
        _clear_failure(member)
        task = {"thread": None, "attempt": 0, "abandoned": False,
                "stop": threading.Event()}
        _TASKS[member] = task
        thread = threading.Thread(target=dial_loop, args=(member, task, initial),
                                  name=f"relay-dial-{member}", daemon=True)
        task["thread"] = thread
        thread.start()


def restore_on_startup() -> None:
    """引擎重启恢复：切换态读回——目标未达成继续切换，已达成清除；
    方向为 dial 且无活跃通道的节点恢复拨号（普通重连路径）。"""
    conn = db.get_connection()
    try:
        rows = relay.list_nodes(conn)
    finally:
        conn.close()
    for row in rows:
        member = row["member"]
        if row["dial_stopped"] or row["credential_revoked"]:
            continue
        if row["switch_state"] == "switching":
            if row["direction"] == row["switch_target"]:
                # 重启清除：切换目标已达成（断链窗口内完成），清切换态
                conn = db.get_connection()
                try:
                    relay.complete_switch(conn, member)
                finally:
                    conn.close()
            elif row["switch_target"] == "dial":
                start_dial(member)
            elif row["direction"] == "dial":
                from . import relay_switch
                relay_switch.switch(member, "listen")
        elif row["direction"] == "dial":
            start_dial(member)
