"""MCP loopback 代理（req#173 验收 3）：方向 B 的 MCP 隧道。

ECS 本机 MCP 进程把 GROUP_ENGINE_URL 指向本代理（127.0.0.1:<port>）：
收到的 HTTP 请求 → 打包 http_request 帧沿已认证 listen 通道发给引擎 →
引擎转发到自身 API（回环）按成员权限执行 → http_response 帧回传 →
本代理应答 MCP 客户端。来源在引擎侧是配对连接（非回环），权限核对
走成员级来源绑定同一套逻辑。
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)

# 透传的请求头白名单（去 hop-by-hop 与宿主无关头）
_FORWARD_HEADERS = ("content-type", "accept", "user-agent", "authorization",
                    "x-group-key", "x-group-profile")


class _Handler(BaseHTTPRequestHandler):
    server: "MCPProxyServer"

    def log_message(self, fmt, *args):  # 日志脱敏：不记 URL 查询（可能含凭据）
        logger.info("mcpproxy %s %s", self.command, self.path.split("?")[0])

    def _do(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if parsed.query:
            path += "?" + parsed.query
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        headers = {key.lower(): value for key, value in self.headers.items()
                   if key.lower() in _FORWARD_HEADERS}
        request_id = uuid.uuid4().hex
        # manager node2092：响应等待队列先于发帧注册——引擎经配对通道的
        # 回帧若在注册前到达（同步快速应答），deliver_http_response 取不到
        # waiter 即丢响应、本线程空等超时；发帧失败（ConnectionError）时
        # 释放注册位，避免残留。
        waiter: queue.Queue = queue.Queue(maxsize=1)
        proxy = self.server.proxy
        with proxy.lock:
            proxy.pending[request_id] = waiter
        try:
            self.server.listener.forward_http_request(
                request_id, self.command, path, headers, body)
        except ConnectionError as exc:
            with proxy.lock:
                proxy.pending.pop(request_id, None)
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(
                {"error": f"relay channel not authenticated: {exc}"}).encode())
            return
        try:
            status, resp_headers, resp_body = waiter.get(timeout=300)
        except queue.Empty:
            with proxy.lock:
                proxy.pending.pop(request_id, None)
            self.send_response(504)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "engine response timeout"}')
            return
        self.send_response(int(status or 502))
        for key, value in resp_headers.items():
            self.send_header(key, value)
        self.end_headers()
        payload = (resp_body or "").encode("utf-8", errors="replace")
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _do


class MCPProxyServer:
    """loopback HTTP 代理：只绑 127.0.0.1（ECS 本地进程访问）。"""

    def __init__(self, listener, port: int):
        self.listener = listener
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        # manager node2092：HTTP handler 经 self.server.listener 取转发目标——
        # self.server 是 _httpd（ThreadingHTTPServer 实例），listener 必须挂在
        # 它上面而不是仅存 MCPProxyServer 外层，否则 handler 抛 AttributeError。
        self._httpd.listener = listener
        self._httpd.proxy = self
        self.pending = {}
        self.lock = threading.Lock()
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="bot-group-mcpproxy", daemon=True)

    def start(self) -> None:
        self._thread.start()
        logger.info("mcpproxy listening on 127.0.0.1:%s", self.port)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        with self.lock:
            for waiter in self.pending.values():
                waiter.put_nowait((502, {}, '{"error":"relay proxy stopped"}'))
            self.pending.clear()


    def deliver_http_response(self, request_id, status, headers, body):
        with self.lock:
            waiter = self.pending.pop(request_id, None)
        if waiter is not None:
            waiter.put((status, headers or {}, body or ""))


def start_proxy(listener, port: int = 0) -> MCPProxyServer:
    """启动 loopback 代理；port=0 自动分配。"""
    proxy = MCPProxyServer(listener, port)
    proxy.start()
    listener.proxy = proxy
    return proxy
