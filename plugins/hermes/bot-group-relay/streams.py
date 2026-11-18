"""本机 gateway /api/ws 流管理（req#168 验收 3）。

一条引擎流 = 一条本机 gateway 连接，各自独立线程/事件循环——取票与阻塞
RPC 不卡 gateway 事件循环（插件与 gateway 同进程）。票据单次短时效、
每次建本地连接重取、不缓存。JSON-RPC id 由引擎侧每条流各自的
GatewayClient 维护（各从 1 起），插件原样转发帧文本不改写 id；事件帧
无 id，靠流标识归属。
"""

from __future__ import annotations

import json
import logging
import queue
import threading

logger = logging.getLogger(__name__)


class StreamClosed(RuntimeError):
    """流已关闭（引擎断链/本机连接断开）；调用方按断线语义处理，不重投。"""


class LocalGatewayStream:
    """一条流：本机 gateway /api/ws 连接（独立线程 + 同步 websockets 客户端）。"""

    def __init__(self, stream: str, home: str, *, on_event):
        from . import tickets
        self.stream = stream
        self.home = home
        self._on_event = on_event
        self._outbound: queue.Queue = queue.Queue(maxsize=256)
        self._closed = False
        self._tickets = tickets
        self._thread = threading.Thread(target=self._run, name=f"bot-group-stream-{stream}",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            import websockets.sync.client as ws_sync
        except ImportError:
            logger.error("bot-group relay: websockets unavailable for stream %s", self.stream)
            self._finish()
            return
        try:
            endpoint, ticket = self._tickets.fetch_ticket(self.home, purpose="relay")
            url = self._tickets.gateway_ws_url(endpoint)
            with ws_sync.connect(
                    url,
                    subprotocols=["hermes-gateway-v1", "hermes-gateway-ticket." + ticket],
                    proxy=None, open_timeout=10, close_timeout=1,
                    ping_interval=20, ping_timeout=5) as ws:
                if ws.subprotocol != "hermes-gateway-v1":
                    raise StreamClosed("Hermes gateway protocol mismatch")
                logger.info("bot-group relay stream up stream=%s", self.stream)
                while not self._closed:
                    # 出站优先：引擎 JSON-RPC 帧 → 本机 gateway（原样文本转发）
                    try:
                        text = self._outbound.get_nowait()
                    except queue.Empty:
                        text = None
                    if text is not None:
                        ws.send(text)
                        continue
                    # 入站：本机事件帧（id 未决的响应由 GatewayClient 认领，
                    # 其余事件帧无 id，靠流标识归属）→ 回调给 relay 打包回引擎；
                    # 阻塞至多 0.5s，不阻塞出站。
                    try:
                        raw = ws.recv(timeout=0.5)
                    except TimeoutError:
                        continue
                    frame = json.loads(raw)
                    self._on_event(self, frame)
        except Exception as exc:  # noqa: BLE001
            if not self._closed:
                logger.warning("bot-group relay stream down stream=%s cause=%s",
                               self.stream, type(exc).__name__)
        finally:
            self._finish()

    def _finish(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._outbound.put("")

    @property
    def closed(self) -> bool:
        return self._closed

    def send_text(self, text: str) -> None:
        """引擎帧转发给本机 gateway；流已关闭时丢弃（引擎侧按断线语义核对，
        插件绝不重投、绝不伪造回执）。"""
        if self._closed:
            raise StreamClosed("stream closed")
        try:
            self._outbound.put_nowait(text)
        except queue.Full:
            raise StreamClosed("stream outbound full")

    def close(self) -> None:
        self._finish()


class StreamRegistry:
    """按 stream 标识维护本机 gateway 连接（按 home 隔离，多 profile 各管各的）。"""

    def __init__(self, home: str, *, on_event):
        self._home = home
        self._on_event = on_event
        self._streams: dict[str, LocalGatewayStream] = {}
        self._lock = threading.Lock()

    def open(self, stream: str) -> LocalGatewayStream:
        with self._lock:
            existing = self._streams.get(stream)
            if existing is not None and not existing.closed:
                return existing
            created = LocalGatewayStream(stream, self._home, on_event=self._on_event)
            self._streams[stream] = created
            return created

    def close_all(self) -> None:
        with self._lock:
            streams = list(self._streams.values())
            self._streams.clear()
        for stream in streams:
            stream.close()
