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
            relay.mark_offline(conn, link.member, generation=link.generation)
        finally:
            conn.close()
    logger.warning("relay link down member=%s generation=%d reason=%s",
                   link.member, link.generation, reason)
    try:
        asyncio.run_coroutine_threadsafe(link.ws.close(code=1000), link.loop)
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
    if kind == "heartbeat":
        # 心跳只判存活（写 online 以当前代为准——上方代次校验已保证，SQL 再带
        # 代条件防处理瞬间被接替的跨代窗口，manager node1981 ②）
        link.last_heartbeat = time.time()
        conn = db.get_connection()
        try:
            relay.heartbeat(conn, link.member, generation=link.generation)
        finally:
            conn.close()
        return True
    if kind == "mcp_report":
        # 连接存续中插件换包后上报新摘要（manager node1977 ③：configure 前
        # 必须能取到本次配置的远端实际摘要，不能只靠握手缓存）
        fingerprint = envelope.get("mcp_fingerprint") or ""
        request_id = meta.get("request_id")
        if request_id:
            # mcp_snapshot 问询的同步响应（manager node1981 ③）
            link._on_snapshot_response(request_id, {
                "mcp_fingerprint": fingerprint,
                "code_version": envelope.get("code_version") or "",
            })
            return True
        conn = db.get_connection()
        try:
            relay.mcp_report(conn, link.member, fingerprint,
                             generation=link.generation)
        finally:
            conn.close()
        logger.info("relay mcp_report member=%s fingerprint=%s", link.member,
                    fingerprint[:16] if fingerprint else "")
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
        raw = await websocket.receive_text()
        hello = json.loads(raw)
        member = (hello.get("member") or "").strip()
        credential = hello.get("credential") or ""
        code_version = hello.get("code_version") or ""
        plugin_version = hello.get("plugin_version") or code_version
        mcp_fingerprint = hello.get("mcp_fingerprint") or ""
        protocol_version = hello.get("protocol_version")
        conn = db.get_connection()
        try:
            if not member or not relay.verify_credential(conn, member, credential):
                await websocket.close(code=CLOSE_BAD_CREDENTIAL)
                return
            # req#171 第二层：凭据验证通过后核对 socket peer IP 属于该成员
            # 绑定集合——B 成员凭据从 A 成员已绑定 IP 连接被拒（防交换来源
            # 冒用）。回环恒放行。
            from . import ip_allowlist
            peer_raw = websocket.client.host if websocket.client else None
            peer = ip_allowlist._normalize(peer_raw or "") or peer_raw
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
            expected = row["expected_mcp_sha"] if row else ""
            if expected and expected != mcp_fingerprint:
                logger.warning("relay mcp fingerprint mismatch member=%s expected=%s got=%s",
                               member, expected, mcp_fingerprint)
                await websocket.close(code=CLOSE_MCP_INCOMPATIBLE)
                return
            generation = relay.generation_advance(conn, member)
            relay.mark_online(conn, member, code_version=code_version,
                              mcp_fingerprint=mcp_fingerprint, generation=generation)
        finally:
            conn.close()
    except (ValueError, KeyError, TypeError):
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
        await websocket.send_text(json.dumps(
            {"relay": {"kind": "hello_ok", "generation": generation},
             "engine_url": _engine_url()},
            ensure_ascii=False))
        logger.info("relay link up member=%s generation=%d", member, generation)
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
