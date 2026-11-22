"""引擎侧 relay WS 服务端入口（req#168 task#315 验收 1/2/4）。

插件（ECS 上 Hermes gateway 内）主动出站 WSS 连本端点。一条成员连接承载多条
「流」（stream = 插件↔本机 gateway 的一条本地连接），relay 帧带 stream 标识路由；
引擎侧每个 GatewayClient(remote) 独占一条流。握手含成员级认证（凭据绑定成员、
可单独吊销）与 generation fencing（连接代随 hello_ok 下发、随派发帧下发，旧代帧拒收）。

心跳 90s TTL 只作存活判断（relay.is_online），绝不据此判任务结束重跑。
节点断开：所有流转发哨兵 → GatewayClient rpc 抛 transport error → 既有
reconcile 链接管（不伪造进程退出）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time

from fastapi import WebSocket, WebSocketDisconnect

from . import db, relay

logger = logging.getLogger(__name__)

# relay 帧协议版本：hello 必须上报且与引擎一致，不一致按版本拒绝（验收 6）。
RELAY_PROTOCOL_VERSION = 1

# 握手/帧拒绝码（插件侧安装指引按码提示）。
CLOSE_BAD_CREDENTIAL = 4001
CLOSE_MCP_INCOMPATIBLE = 4002
CLOSE_BAD_PROTOCOL = 4003
CLOSE_BAD_SOURCE = 4004   # req#171：peer IP 未绑定到该成员（来源不符）
CLOSE_WRONG_DIRECTION = 4005  # req#173：节点方向为拨号时插件不得主动连入


class RelayTransport:
    """引擎侧一条流的 transport：实现 GatewayClient 需要的 send/迭代/close。"""

    def __init__(self, link: "NodeLink", stream: str):
        self._link = link
        self.stream = stream
        self._inbound: queue.Queue = queue.Queue()
        self._closed = False

    def send(self, text: str) -> None:
        if self._closed:
            raise ConnectionError("relay stream closed")
        envelope = json.dumps({
            "relay": {"kind": "frame", "stream": self.stream,
                      "generation": self._link.generation},
            "frame": json.loads(text),
        }, ensure_ascii=False)
        self._link.send_text(envelope)

    def __iter__(self):
        return self

    def __next__(self):
        item = self._inbound.get()
        if item is _CLOSED_SENTINEL:
            self._closed = True
            raise StopIteration
        return item

    def push(self, raw: str) -> None:
        if not self._closed:
            self._inbound.put(raw)

    def request_mcp_snapshot(self, timeout: float = 10.0) -> dict | None:
        """configure 前同步问询远端 MCP 实际摘要（manager node1981 ③）。"""
        return self._link.request_mcp_snapshot(timeout)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._inbound.put(_CLOSED_SENTINEL)


_CLOSED_SENTINEL = object()


_RELEASE_SENTINEL = object()
_RELEASE_VERSION: str | None | object = _RELEASE_SENTINEL


def _engine_release_version() -> str | None:
    """引擎发布版本（插件必须与引擎同 tag 配套，设计 2.1）。惰性缓存：
    version_info 在 git 树上要跑 git（握手路径不重复开销）。"""
    global _RELEASE_VERSION
    if _RELEASE_VERSION is _RELEASE_SENTINEL:
        from . import version
        _RELEASE_VERSION = version.version_info().get("current_version")
    return _RELEASE_VERSION


class NodeLink:
    """一条成员连接的活跃状态（内存注册表项）。"""

    def __init__(self, member: str, ws: WebSocket, generation: int,
                 peer_ip: str | None = None):
        self.member = member
        self.ws = ws
        self.generation = generation
        self.peer_ip = peer_ip
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None  # 纯内存路由测试场景：不 send
        self._send_lock = threading.Lock()
        self._stream_seq = 0
        self.transports: dict[str, RelayTransport] = {}
        # mcp_snapshot 同步问询（configure 前取本次实际摘要，manager node1981 ③）
        self._snapshot_seq = 0
        self._snapshot_waiters: dict[str, queue.Queue] = {}
        self._snapshot_waiters_lock = threading.Lock()
        self.last_heartbeat = time.time()
        self.closed = False

    def next_stream(self) -> str:
        self._stream_seq += 1
        return f"{self.member}-{self._stream_seq}"

    def open_transport(self) -> RelayTransport:
        transport = RelayTransport(self, self.next_stream())
        self.transports[transport.stream] = transport
        return transport

    def request_mcp_snapshot(self, timeout: float) -> dict | None:
        """向插件同步问询当前 MCP 实际摘要（换包未异步上报时也能取到本次值）。
        连接已关闭或超时返回 None（configure 走缓存复核或拒绝，绝不误通过）。"""
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
        # 附本连接代：configure 写库按代条件，旧代迟到的问询响应不得
        # 覆盖新代摘要（reviewer 返修①）。
        report["generation"] = self.generation
        with self._snapshot_waiters_lock:
            waiter = self._snapshot_waiters.pop(request_id, None)
        if waiter is not None:
            waiter.put(report)

    def send_text(self, text: str) -> None:
        if self.closed:
            raise ConnectionError("relay link closed")
        with self._send_lock:
            try:
                future = asyncio.run_coroutine_threadsafe(self.ws.send_text(text), self.loop)
                future.result(timeout=10)
            except Exception as exc:  # noqa: BLE001 断链即离线
                _shutdown_link(self, f"send failed: {type(exc).__name__}")
                raise ConnectionError("relay link send failed") from exc

    def close(self) -> None:
        _shutdown_link(self, "link closed")


_LINKS: dict[str, NodeLink] = {}
_LINKS_LOCK = threading.Lock()
_LAST_PEERS: dict[str, str] = {}


def last_peer(member: str) -> str | None:
    """最近一次成功握手的 socket peer IP；当前连接优先。

    仅用于管理界面展示实际连接来源，不参与访问判断。连接关闭后保留最后
    一次值，供页面区分「从未连接」和「离线但曾连接」；引擎重启后清空。
    """
    with _LINKS_LOCK:
        link = _LINKS.get(member)
        if link is not None and not link.closed and link.peer_ip:
            return link.peer_ip
        return _LAST_PEERS.get(member)


def _db_generation(link: NodeLink) -> int:
    """状态写库用代：DialLink=本地状态代（manager node2057 ②），
    NodeLink=连接代（同值）。"""
    return getattr(link, "db_generation", None) or link.generation


def _shutdown_link(link: NodeLink, reason: str) -> None:
    """断开一条成员连接：关 WS、所有流转发哨兵。

    只有「当前注册的连接」断开才把节点标 offline——旧代连接被新连接接替时
    的清理不覆盖新连接已写入的 online 状态（manager node1977 ①）。
    注意：本函数自取 _LINKS_LOCK，调用方不得持锁调用。
    """
    if link.closed:
        return
    link.closed = True
    current = None
    with _LINKS_LOCK:
        if _LINKS.get(link.member) is link:
            _LINKS.pop(link.member, None)
            current = link
    for transport in list(link.transports.values()):
        transport.close()
    if current is not None:
        conn = db.get_connection()
        try:
            # 带代条件写 offline：本函数锁外执行，清理期间可能已被新连接接替，
            # 旧代清理不得覆盖新代状态（manager node1981 ②跨代窗口）。
            # 状态写库用本地状态代（manager node2057 ②：本地代贯穿离线写入）。
            relay.mark_offline(conn, link.member, generation=_db_generation(link))
        finally:
            conn.close()
    logger.warning("relay link down member=%s generation=%d reason=%s",
                   link.member, link.generation, reason)
    try:
        if link.loop is not None:
            asyncio.run_coroutine_threadsafe(link.ws.close(code=1000), link.loop)
        else:
            # 拨号侧 DialLink（sync websockets 客户端）在调用线程同步关闭
            link.ws.close(code=1000)
    except Exception:  # noqa: BLE001
        pass


def active_link(member: str) -> NodeLink | None:
    with _LINKS_LOCK:
        link = _LINKS.get(member)
        if link is not None and not link.closed:
            return link
        return None


def open_transport(member: str) -> RelayTransport | None:
    """为该成员开一条新流（GatewayClient 远端 transport）。节点离线返回 None。"""
    link = active_link(member)
    if link is None:
        return None
    return link.open_transport()


def close_all() -> None:
    with _LINKS_LOCK:
        links = list(_LINKS.values())
    for link in links:
        _shutdown_link(link, "engine shutdown")


def _route_inbound(link: NodeLink, envelope: dict) -> bool:
    """插件→引擎帧：一律先按当前连接代校验（缺代/旧代拒收），再按 kind 路由。"""
    meta = envelope.get("relay") or {}
    kind = meta.get("kind")
    frame_gen = meta.get("generation")
    if kind != "hello" and frame_gen != link.generation:
        # 缺 generation 或旧代：拒收（旧连接迟到帧不得影响当前代状态或流）
        logger.warning("relay generation-mismatch frame dropped member=%s kind=%s gen=%s",
                       link.member, kind, frame_gen)
        return True
    if kind == 'workspace_result':
        from . import remote_workspaces
        remote_workspaces.receive(link, meta.get('request_id'), envelope.get('workspace'))
        return True
    if kind == "heartbeat":
        # 心跳只判存活（写 online 以当前代为准——上方代次校验已保证，SQL 再带
        # 代条件防处理瞬间被接替的跨代窗口，manager node1981 ②）。状态写库
        # 用本地状态代（manager node2057 ②：帧代≠本地代时心跳必须更新本地
        # 代状态，否则心跳对 online/4 的 DB 不生效）。
        link.last_heartbeat = time.time()
        conn = db.get_connection()
        try:
            relay.heartbeat(conn, link.member, generation=_db_generation(link))
        finally:
            conn.close()
        return True
    if kind == "mcp_report":
        # 连接存续中插件换包后上报新摘要（manager node1977 ③：configure 前
        # 必须能取到本次配置的远端实际摘要，不能只靠握手缓存）
        fingerprint = envelope.get("mcp_fingerprint") or ""
        request_id = meta.get("request_id")
        proxy_port = envelope.get("proxy_port")
        proxy_port = proxy_port if isinstance(proxy_port, int) and proxy_port > 0 else 0
        if request_id:
            # mcp_snapshot 问询的同步响应（manager node1981 ③）
            link._on_snapshot_response(request_id, {
                "mcp_fingerprint": fingerprint,
                "code_version": envelope.get("code_version") or "",
                "proxy_port": proxy_port,
                **{key: envelope[key] for key in ('mcp_python', 'mcp_entry') if key in envelope},
            })
            return True
        conn = db.get_connection()
        try:
            relay.mcp_report(conn, link.member, fingerprint,
                             generation=_db_generation(link),
                             proxy_port=proxy_port)
        finally:
            conn.close()
        logger.info("relay mcp_report member=%s fingerprint=%s", link.member,
                    fingerprint[:16] if fingerprint else "")
        return True
    if kind == "http_request":
        # req#173 方向 B 的 MCP loopback 隧道：插件代理转来的 HTTP 请求沿
        # 已认证通道回引擎——引擎转发到自身回环 API（按成员头凭据执行），
        # 响应以 http_response 帧回传。成员身份=配对连接成员（握手已认证）。
        _serve_relay_http_request(link, envelope)
        return True
    if kind == "mcp_verify_response":
        # manager node2102：插件完成本机经代理的真实只读调用后回传状态——
        # 交还 request_mcp_verify 的等待线程（DialLink 持有 waiter）。
        request_id = meta.get("request_id") or ""
        handler = getattr(link, "_on_verify_response", None)
        if handler is not None and request_id:
            handler(request_id, {
                "status": envelope.get("status"),
                "body": str(envelope.get("body") or "")[:4000],
            })
        return True
    stream = meta.get("stream")
    transport = link.transports.get(stream) if isinstance(stream, str) else None
    frame = envelope.get("frame")
    if transport is None or frame is None:
        logger.warning("relay unknown stream frame dropped member=%s stream=%s",
                       link.member, stream)
        return True
    transport.push(json.dumps(frame, ensure_ascii=False))
    return True


async def relay_ws(websocket: WebSocket) -> None:
    """/relay/ws 端点：成员级认证握手 → 帧路由循环。"""
    await websocket.accept()
    member = None
    peer = None
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=8)
        hello = json.loads(raw)
        member = (hello.get("member") or "").strip()
        credential = hello.get("credential") or ""
        invite_token = (hello.get("invite") or "").strip()
        code_version = hello.get("code_version") or ""
        plugin_version = hello.get("plugin_version") or code_version
        mcp_fingerprint = hello.get("mcp_fingerprint") or ""
        protocol_version = hello.get("protocol_version")
        # req#171 第二层：凭据验证通过后核对 socket peer IP 属于该成员
        # 绑定集合——B 成员凭据从 A 成员已绑定 IP 连接被拒（防交换来源
        # 冒用）。回环恒放行。邀请路径（req#175）在兑换时把 peer 首绑进
        # 该新成员来源集合，之后同口径核验。
        from . import ip_allowlist
        peer_raw = websocket.client.host if websocket.client else None
        peer = ip_allowlist._normalize(peer_raw or "") or peer_raw
        conn = db.get_connection()
        try:
            if invite_token:
                if credential or not member or not ip_allowlist._normalize(peer_raw or ''):
                    await websocket.close(code=CLOSE_BAD_CREDENTIAL)
                    return
                engine_version = _engine_release_version()
                if (protocol_version != RELAY_PROTOCOL_VERSION
                        or (engine_version and plugin_version != engine_version)):
                    await websocket.close(code=CLOSE_BAD_PROTOCOL)
                    return
                offer = relay.offer_invitation(conn, invite_token, member, peer,
                                               hello.get('join_proof', ''), hello.get('credential_commitment', ''))
                if offer is None:
                    await websocket.close(code=CLOSE_BAD_CREDENTIAL)
                    return
                # No relay link is registered on a bootstrap connection, even after approval.
                await websocket.send_json({'relay': {'kind': 'hello_confirmed' if offer['state'] == 'confirmed'
                                                           else 'hello_pending'}})
                await websocket.close()
                return
            member = relay.authenticated_member(conn, member, credential)
            if member is None:
                await websocket.close(code=CLOSE_BAD_CREDENTIAL)
                return
            if not ip_allowlist.member_allowed(conn, member, peer_raw):
                logger.warning("relay source not bound to member=%s peer=%s", member, peer)
                await websocket.close(code=CLOSE_BAD_SOURCE)
                return
            if protocol_version != RELAY_PROTOCOL_VERSION:
                logger.warning("relay protocol version mismatch member=%s got=%s",
                               member, protocol_version)
                await websocket.close(code=CLOSE_BAD_PROTOCOL)
                return
            # 插件包版本与引擎同 tag 配套（设计 2.1，manager node1981 ③）：
            # 插件包版本必须等于引擎发布版本，否则按版本拒绝。
            engine_version = _engine_release_version()
            if engine_version and plugin_version != engine_version:
                logger.warning("relay plugin version mismatch member=%s engine=%s got=%s",
                               member, engine_version, plugin_version)
                await websocket.close(code=CLOSE_BAD_PROTOCOL)
                return
            row = relay.read_node(conn, member)
            # req#173 单活动通道：插件主动连入（方向 A）只在「方向为 listen」
            # 或「切换目标是 listen」时接受；方向拨号/切换目标拨号时插件
            # 旧方向重试连入一律拒（防 dial↔listen 乒乓顶替）。
            if row is not None:
                if row["switch_state"] == "switching":
                    if row["switch_target"] != "listen":
                        logger.warning("relay wrong direction (switching to dial) member=%s",
                                       member)
                        await websocket.close(code=CLOSE_WRONG_DIRECTION)
                        return
                elif row["direction"] == "dial":
                    logger.warning("relay wrong direction member=%s", member)
                    await websocket.close(code=CLOSE_WRONG_DIRECTION)
                    return
            expected = row["expected_mcp_sha"] if row else ""
            if expected and expected != mcp_fingerprint:
                logger.warning("relay mcp fingerprint mismatch member=%s expected=%s got=%s",
                               member, expected, mcp_fingerprint)
                await websocket.close(code=CLOSE_MCP_INCOMPATIBLE)
                return
            generation = relay.generation_advance(conn, member)
            relay.mark_online(conn, member, code_version=code_version,
                              mcp_fingerprint=mcp_fingerprint, generation=generation)
            # req#173：切换目标为 listen 时，本连接（方向 A）认证成功即
            # 切换完成——但清切换态放在 link 注册（能收发帧）之后的异步
            # 恢复步骤（manager node2073：新链先注册，再经 relay lease
            # 做首次在途问询，完成后按仍有效的本地代与目标恢复派发）。
            switching = relay.read_node(conn, member)["switch_state"] == "switching"
        finally:
            conn.close()
    except (ValueError, KeyError, TypeError, asyncio.TimeoutError):
        await websocket.close(code=CLOSE_BAD_CREDENTIAL)
        return
    except WebSocketDisconnect:
        return

    link = NodeLink(member, websocket, generation, peer_ip=peer)
    # 接替：先注册新连接（新连接成为当前代），旧连接的清理在锁外进行——
    # _shutdown_link 自取 _LINKS_LOCK，持锁调用会死锁；旧代清理因「非当前
    # 注册连接」不写 offline，不覆盖新连接状态（manager node1977 ①）。
    with _LINKS_LOCK:
        previous = _LINKS.get(member)
        _LINKS[member] = link
        if peer:
            _LAST_PEERS[member] = peer
    if previous is not None and not previous.closed:
        previous.close()
    try:
        hello_ok = {"relay": {"kind": "hello_ok", "generation": generation},
                    "engine_url": _engine_url()}
        await websocket.send_text(json.dumps(hello_ok, ensure_ascii=False))
        logger.info("relay link up member=%s generation=%d", member, generation)
        if switching:
            # manager node2073：link 已注册且 hello_ok 已发（能收发帧），
            # 异步从新链取得 relay lease 做首次在途问询后再恢复派发；
            # 问询期间 switch_state 仍 switching → 闸门 hold。
            relay.restore_switch_after_handshake(
                member, target="listen", generation=generation)
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break
            try:
                envelope = json.loads(raw)
            except ValueError:
                logger.warning("relay malformed frame member=%s", member)
                continue
            try:
                _route_inbound(link, envelope)
            except Exception:  # noqa: BLE001
                logger.exception("relay inbound routing failed member=%s", member)
    finally:
        _shutdown_link(link, "connection closed")


def _engine_url() -> str:
    from . import wake
    try:
        return wake._engine_url()
    except Exception:  # noqa: BLE001
        from . import config
        return f"http://{config.host()}:{config.port()}"


# manager 12774/12781/12796：写端点必须**精确匹配 method+path**
# （startswith 会扩大匹配范围）——POST /msg 是随包 group_send 的业务端点
# （按成员会话凭据+执行范围鉴权，不属管理接口）。回执查询 /mcp-operations/
# 是 recorded 写入链的只读核对（mcp_http 对 POST /msg 挂
# X-Group-Operation-Id，结果不明时靠它按成员/凭据作用域核对——消息落库
# 一次的幂等依赖）：仅 GET + 单个非空操作号（空路径/多段/其他方法拒）。
# 心跳 /heartbeat 不属发送依赖，保持拒绝。
_HTTP_TUNNEL_EXACT = (("POST", "/msg"),)
_HTTP_TUNNEL_PREFIXES = ("/health", "/projects/", "/tasks/", "/group/",
                         "/members", "/auth/status")
_RECEIPT_PREFIX = "/mcp-operations/"


def _tunnel_allowed(method: str, path: str) -> bool:
    """白名单判定：写端点精确匹配 method+path；回执查询仅 GET 且单个
    非空操作号；其余只读/业务端点前缀匹配（manager 12796）。"""
    if method == "GET" and path.startswith(_RECEIPT_PREFIX):
        operation_id = path[len(_RECEIPT_PREFIX):].strip()
        return bool(operation_id) and "/" not in operation_id
    return ((method, path) in _HTTP_TUNNEL_EXACT
            or any(path.startswith(prefix)
                   for prefix in _HTTP_TUNNEL_PREFIXES))


def _serve_relay_http_request(link: NodeLink, envelope: dict) -> None:
    """已认证成员通道上的 HTTP 请求 → 转发到引擎自身回环 API →
    http_response 帧回传。路径白名单外一律 403；响应脱敏记录。

    manager node2060 ②：下游授权上下文绑定实际通道成员——外部身份头
    （authorization/x-group-key/x-group-profile）一律过滤丢弃，注入
    X-Group-Profile=<link.member>（配对通道成员身份，绝不以本地管理员
    或外部身份执行）；只透传内容头（content-type/accept/user-agent）。

    manager node2084：凭据保留原会话/执行范围——随包 MCP 进程自带
    hermes_session_auth 注入的受限凭据（session./run. 带项目/会话/执行
    范围），不再替换成 auth.bot_token（该类型 session_scope 与
    execution_scope 均为空，会丢权限链）。原请求凭据必须存在且经
    verify_bot 核验属于通道成员本人，原样转发保留原授权链；无凭据或
    凭据不属于通道成员一律 401，绝不注入空 scope 的通用成员密钥。"""
    import urllib.error
    import urllib.request
    request_id = (envelope.get("relay") or {}).get("request_id") or ""
    method = str(envelope.get("method") or "GET").upper()
    path = str(envelope.get("path") or "/")
    raw_headers = {str(k).lower(): str(v) for k, v in
                   (envelope.get("headers") or {}).items()}
    # 过滤外部身份头（manager node2060 ②）：隧道请求的身份由通道成员决定
    # manager 12774：x-group-operation-id 必须透传——POST /msg 是 recorded
    # 写入，mcp_http 挂的幂等操作号随请求到引擎，落库一次的幂等依赖它。
    forward_headers = {k: v for k, v in raw_headers.items()
                       if k in ("content-type", "accept", "user-agent",
                                "x-group-operation-id")}
    # 下游授权上下文：实际配对通道成员（引擎 API 按 X-Group-Profile 解析）
    forward_headers["x-group-profile"] = link.member
    from . import auth
    # manager node2084：识别既有受限凭据并按通道成员校验——保留原会话/
    # 执行权限（session./run. 凭据随行，access 侧照常施加项目/会话/执行
    # 限制）；凭据缺失或不属于通道成员则拒绝，绝不降级为无 scope 的
    # bot_token 通用密钥。
    credential = raw_headers.get("x-group-key") or ""
    if not credential and raw_headers.get("authorization", "").startswith("Bearer "):
        credential = raw_headers["authorization"][7:].strip()
    body = envelope.get("body") or ""
    response = None
    if not credential or not auth.verify_bot(link.member, credential):
        status, resp_headers, resp_body = 401, {}, \
            '{"error": "member credential required for tunnel"}'
        logger.warning("relay http_request rejected member=%s %s %s "
                       "credential_missing_or_mismatched", link.member, method, path)
    elif method not in ("GET", "POST"):
        status, resp_headers, resp_body = 405, {}, '{"error": "method not allowed"}'
    elif not _tunnel_allowed(method, path):
        logger.warning("relay http_request blocked member=%s path=%s", link.member, path)
        status, resp_headers, resp_body = 403, {}, '{"error": "path not allowed"}'
    else:
        # 凭据原样保留（session./run. 范围随行，access.resolve_identity 的
        # verify_bot 分支解析 session_scope/execution_scope 施加成员限制）
        forward_headers["x-group-key"] = credential
        from . import config
        url = f"http://{config.host()}:{config.port()}{path}"
        request = urllib.request.Request(
            url, data=body.encode() if body else None, method=method)
        for key, value in forward_headers.items():
            request.add_header(key, value)
        try:
            response = urllib.request.urlopen(request, timeout=120)
            resp_body = response.read().decode("utf-8", errors="replace")
            status = response.status
            resp_headers = {k: v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            status = exc.code
            resp_headers = {k: v for k, v in exc.headers.items()}
            resp_body = exc.read().decode("utf-8", errors="replace")
        except OSError as exc:
            status, resp_headers, resp_body = 502, {}, \
                json.dumps({"error": f"engine loopback unreachable: {exc}"})
        finally:
            if response is not None:
                response.close()
    logger.info("relay http_request member=%s %s %s -> %s",
                link.member, method, path, status)
    try:
        link.send_text(json.dumps({
            "relay": {"kind": "http_response", "request_id": request_id,
                      "generation": link.generation},
            "status": status, "headers": resp_headers, "body": resp_body,
        }, ensure_ascii=False))
    except ConnectionError:
        logger.warning("relay http_response send failed member=%s", link.member)
