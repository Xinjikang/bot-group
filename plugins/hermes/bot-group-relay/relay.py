"""出站 WSS 客户端：插件 ↔ bot-group 引擎（req#168 验收 1/4/5/6）。

- hello 握手：member/credential/code_version/plugin_version/mcp_fingerprint/
  protocol_version + 随包 MCP 解释器/入口路径；引擎不匹配即拒（同 tag 配套）。
- generation fencing：引擎 hello_ok 下发连接代；此后收/发的帧都带当前代，
  旧代帧（晚到的旧连接数据）拒收；重连后只让当前连接接新消息。
- 心跳保活（90s TTL 内至少一次）；断开后按退避重连，不自行重投任何帧。
- 帧路由：kind=frame → 按 stream 维护本机 gateway 连接（独立线程取票转发，
  事件帧回传）；mcp_snapshot_request → 实时重算随包 MCP 摘要回复（换包
  缓存 bug 场景：连接不断、磁盘 MCP 原地替换后重核）。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time

from . import config as relay_config
from . import mcp_digest
from . import streams as streams_mod

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 45.0      # 引擎 TTL 90s：45s 心跳留足余量
RECONNECT_BASE = 1.0
RECONNECT_CAP = 30.0
CLOSE_BAD_PROTOCOL = 4003


class RelayClient:
    """一条出站 WSS（一个成员一条连接）。"""

    def __init__(self, cfg: relay_config.RelayConfig, *, on_status=None):
        self.cfg = cfg
        self._on_status = on_status or (lambda state: None)
        self._generation = 0
        self._credential = cfg.credential   # req#175：邀请兑换后更新为持久凭据
        self._engine_url = ""
        self._streams = streams_mod.StreamRegistry(cfg.home, on_event=self._on_local_event)
        self._send_queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="bot-group-relay-outbound",
                                        daemon=True)

    # ---------- 生命周期 ----------

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._streams.close_all()

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def engine_url(self) -> str:
        return self._engine_url

    # ---------- 出站循环 ----------

    def _run(self) -> None:
        import websockets.sync.client as ws_sync
        backoff = RECONNECT_BASE
        while not self._stop.is_set():
            try:
                with ws_sync.connect(relay_config.ws_url(self.cfg.engine_url),
                                     proxy=None, open_timeout=10, close_timeout=2,
                                     ping_interval=20, ping_timeout=5,
                                     max_size=8 * 1024 * 1024) as ws:
                    if not self._handshake(ws):
                        return  # 凭据/版本/摘要不匹配：引擎拒绝，重试无意义
                    backoff = RECONNECT_BASE
                    self._on_status("online")
                    self._recv_loop(ws)
            except Exception as exc:  # noqa: BLE001 断线重连
                if self._stop.is_set():
                    return
                logger.warning("bot-group relay link down member=%s cause=%s",
                               self.cfg.member, type(exc).__name__)
            self._on_status("offline")
            self._streams.close_all()
            self._generation = 0
            if self._stop.wait(timeout=backoff):
                return
            backoff = min(backoff * 2, RECONNECT_CAP)

    def _handshake(self, ws) -> bool:
        snapshot = mcp_digest.snapshot(self.cfg.mcp_python,
                                       code_version=relay_config.plugin_version())
        # 认证材料（req#175）：持久凭据优先（含邀请兑换后落盘的），
        # 无凭据才带一次性邀请——邀请只用于首次接入。
        from . import pairing
        credential = (self.cfg.credential
                      or pairing.load_register_credential(self.cfg.member))
        hello = {"relay": {"kind": "hello"},
                 "member": self.cfg.member,
                 "credential": credential,
                 "code_version": snapshot["code_version"],
                 "plugin_version": snapshot["code_version"],
                 "mcp_fingerprint": snapshot["mcp_fingerprint"],
                 "protocol_version": relay_config.RELAY_PROTOCOL_VERSION,
                 "mcp_python": snapshot["mcp_python"],
                 "mcp_entry": snapshot["mcp_entry"]}
        if not credential and self.cfg.invite:
            hello["invite"] = self.cfg.invite
        try:
            ws.send(json.dumps(hello, ensure_ascii=False))
            reply = json.loads(ws.recv())
        except Exception as exc:  # noqa: BLE001 关闭码经异常可见
            code = getattr(getattr(exc, "rcvd", None), "code", None)
            # 引擎按版本/凭据/摘要拒绝：重试无意义，结束出站循环
            if code in (4001, 4002, 4003):
                logger.warning("bot-group relay handshake rejected member=%s code=%s",
                               self.cfg.member, code)
                return False
            raise
        if reply.get("relay", {}).get("kind") != "hello_ok":
            return False
        # req#175 邀请兑换：hello_ok 下发节点持久凭据 → 落盘供重连
        issued_credential = reply.get("relay", {}).get("credential") or ""
        if issued_credential:
            from . import pairing
            pairing.save_register_credential(self.cfg.member, issued_credential)
            self._credential = issued_credential
        self._generation = int(reply["relay"].get("generation") or 0)
        self._engine_url = reply.get("engine_url") or ""
        logger.info("bot-group relay link up member=%s generation=%d",
                    self.cfg.member, self._generation)
        return self._generation > 0

    def _recv_loop(self, ws) -> None:
        last_beat = 0.0
        while not self._stop.is_set():
            # 本地事件回传（流线程塞入，出站线程统一发送）
            while True:
                try:
                    pending = self._send_queue.get_nowait()
                except queue.Empty:
                    break
                ws.send(pending)
            try:
                # recv 至多 0.5s：send_queue 的本地事件回传不能等心跳间隔
                raw = ws.recv(timeout=0.5)
                envelope = json.loads(raw)
                self._route_inbound(ws, envelope)
            except TimeoutError:
                pass
            except Exception as exc:  # noqa: BLE001
                if not self._stop.is_set():
                    raise
            if time.monotonic() - last_beat >= HEARTBEAT_INTERVAL:
                ws.send(json.dumps({"relay": {"kind": "heartbeat",
                                              "generation": self._generation}},
                                   ensure_ascii=False))
                last_beat = time.monotonic()

    def _route_inbound(self, ws, envelope: dict) -> None:
        meta = envelope.get("relay") or {}
        kind = meta.get("kind")
        frame_gen = meta.get("generation")
        # generation fencing：缺代/旧代帧一律拒收（旧连接晚到帧不得影响当前代）
        if kind not in ("hello_ok",) and frame_gen != self._generation:
            logger.warning("bot-group relay stale frame dropped member=%s kind=%s gen=%s",
                           self.cfg.member, kind, frame_gen)
            return
        if kind == "mcp_snapshot_request":
            # 同步问询：实时重算本次实际摘要（换包未异步上报也能取到本次值）
            snapshot = mcp_digest.snapshot(self.cfg.mcp_python,
                                           code_version=relay_config.plugin_version())
            ws.send(json.dumps({
                "relay": {"kind": "mcp_report", "request_id": meta.get("request_id"),
                          "generation": self._generation},
                "mcp_fingerprint": snapshot["mcp_fingerprint"],
                "code_version": snapshot["code_version"],
                "mcp_python": snapshot["mcp_python"],
                "mcp_entry": snapshot["mcp_entry"],
            }, ensure_ascii=False))
            return
        if kind != "frame":
            logger.warning("bot-group relay unknown frame member=%s kind=%s",
                           self.cfg.member, kind)
            return
        stream = meta.get("stream")
        frame = envelope.get("frame")
        if not isinstance(stream, str) or frame is None:
            return
        try:
            local = self._streams.open(stream)
            local.send_text(json.dumps(frame, ensure_ascii=False))
        except streams_mod.StreamClosed:
            logger.warning("bot-group relay frame dropped stream=%s (closed)", stream)

    # ---------- 本地事件回传 ----------

    def _on_local_event(self, local: streams_mod.LocalGatewayStream, frame: dict) -> None:
        """本机 gateway 事件帧 → 打包 relay 帧回引擎（流标识归属）。"""
        envelope = json.dumps({
            "relay": {"kind": "frame", "stream": local.stream,
                      "generation": self._generation},
            "frame": frame,
        }, ensure_ascii=False)
        self._send_queue.put(envelope)
