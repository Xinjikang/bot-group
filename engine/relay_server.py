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

# 握手拒绝码（插件侧安装指引按码提示）。
CLOSE_BAD_CREDENTIAL = 4001
CLOSE_MCP_INCOMPATIBLE = 4002
CLOSE_STALE_GENERATION = 4003


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

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._inbound.put(_CLOSED_SENTINEL)


_CLOSED_SENTINEL = object()


class NodeLink:
    """一条成员连接的活跃状态（内存注册表项）。"""

    def __init__(self, member: str, ws: WebSocket, generation: int):
        self.member = member
        self.ws = ws
        self.generation = generation
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None  # 纯内存路由测试场景：不 send
        self._send_lock = threading.Lock()
        self._stream_seq = 0
        self.transports: dict[str, RelayTransport] = {}
        self.last_heartbeat = time.time()
        self.closed = False

    def next_stream(self) -> str:
        self._stream_seq += 1
        return f"{self.member}-{self._stream_seq}"

    def open_transport(self) -> RelayTransport:
        transport = RelayTransport(self, self.next_stream())
        self.transports[transport.stream] = transport
        return transport

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


def _shutdown_link(link: NodeLink, reason: str) -> None:
    """断开一条成员连接：关 WS、所有流转发哨兵、节点标离线。"""
    if link.closed:
        return
    link.closed = True
    with _LINKS_LOCK:
        if _LINKS.get(link.member) is link:
            _LINKS.pop(link.member, None)
    for transport in list(link.transports.values()):
        transport.close()
    conn = db.get_connection()
    try:
        relay.mark_offline(conn, link.member)
    finally:
        conn.close()
    logger.warning("relay link down member=%s reason=%s", link.member, reason)
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
    """插件→引擎帧按 stream 路由到对应 transport；返回是否已路由。"""
    meta = envelope.get("relay") or {}
    kind = meta.get("kind")
    if kind == "heartbeat":
        link.last_heartbeat = time.time()
        conn = db.get_connection()
        try:
            relay.heartbeat(conn, link.member)
        finally:
            conn.close()
        return True
    stream = meta.get("stream")
    frame_gen = meta.get("generation")
    if frame_gen is not None and frame_gen != link.generation:
        logger.warning("relay stale-generation frame dropped member=%s stream=%s gen=%s",
                       link.member, stream, frame_gen)
        return True  # 旧代帧拒收（丢弃即拒）
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
    try:
        raw = await websocket.receive_text()
        hello = json.loads(raw)
        member = (hello.get("member") or "").strip()
        credential = hello.get("credential") or ""
        code_version = hello.get("code_version") or ""
        mcp_fingerprint = hello.get("mcp_fingerprint") or ""
        conn = db.get_connection()
        try:
            if not member or not relay.verify_credential(conn, member, credential):
                await websocket.close(code=CLOSE_BAD_CREDENTIAL)
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

    link = NodeLink(member, websocket, generation)
    with _LINKS_LOCK:
        previous = _LINKS.get(member)
        if previous is not None and not previous.closed:
            previous.close()  # 重连：旧连接立即失效（重连后只让当前连接接新消息）
        _LINKS[member] = link
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
