"""出站 WSS 客户端：插件 ↔ bot-group 引擎（req#168 验收 1/4/5/6）。

- hello 握手：member/credential/code_version/plugin_version/mcp_fingerprint/
  protocol_version + 随包 MCP 解释器/入口路径。
- generation fencing：引擎 hello_ok 下发连接代；此后收/发的帧都带当前代，
  旧代帧（晚到的旧连接数据）拒收；重连后只让当前连接接新消息。
- 心跳保活（90s TTL 内至少一次）；断开后按退避重连，不自行重投任何帧。
- 帧路由：kind=frame → 按 stream 维护本机 gateway 连接（独立线程取票转发，
  事件帧回传）；mcp_snapshot_request → 实时重算随包 MCP 摘要回复（换包
  缓存 bug 场景：连接不断、磁盘 MCP 原地替换后重核）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import replace

from . import config as relay_config
from . import mcp_digest
from . import streams as streams_mod

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 45.0      # 引擎 TTL 90s：45s 心跳留足余量
CLOSE_BAD_PROTOCOL = 4003
_GATEWAY_CLIENTS = {}
_GATEWAY_LOCK = threading.Lock()


def gateway_client(cfg, peer):
    key = (cfg.home, cfg.member, peer)
    with _GATEWAY_LOCK:
        client = _GATEWAY_CLIENTS.get(key)
        if client is None or not client._thread.is_alive():
            client = RelayClient(cfg, peer=peer)
            _GATEWAY_CLIENTS[key] = client
            client.start()
        return client


def stop_gateway_client(client):
    key = (client.cfg.home, client.cfg.member, client.peer)
    with _GATEWAY_LOCK:
        if _GATEWAY_CLIENTS.get(key) is client:
            del _GATEWAY_CLIENTS[key]
    client.stop()


class RelayClient:
    """一条出站 WSS（一个成员一条连接）。"""

    def __init__(self, cfg: relay_config.RelayConfig, *, on_status=None, follow_saved=True, peer=""):
        self.cfg = cfg
        self.peer = peer
        self._follow_saved = follow_saved
        self._on_status = on_status or (lambda state: None)
        self._generation = 0
        # 携带邀请时，重连只使用为本次邀请确认的凭据。
        self._credential = "" if cfg.invite else cfg.credential
        self._refusal = None
        self._engine_url = ""
        self._stop = threading.Event()
        self._retry = threading.Event()
        self._listener = None
        self._listener_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run_for_peer, name="bot-group-relay-outbound",
                                        daemon=True)

    # ---------- 生命周期 ----------

    def _outbound_state(self, status, **fields):
        from . import pairing
        from urllib.parse import urlsplit
        host = urlsplit(self.cfg.engine_url)
        pairing.set_outbound_state(status, member=self.cfg.member, engine_host=host.hostname,
                                   engine_port=host.port, **fields)

    def start(self) -> None:
        self._thread.start()

    def _run_for_peer(self):
        from . import pairing
        with pairing.using_peer(self.peer):
            self._run()

    def pair_invitation(self) -> None:
        """Persist administrator confirmation without claiming the gateway's daily link."""
        from websockets.sync.client import connect
        from . import pairing
        if not self.cfg.invite:
            raise ValueError("missing invitation")
        while not pairing.load_register_credential(self.cfg.member, engine_url=self.cfg.engine_url,
                                                   invite=self.cfg.invite):
            with connect(relay_config.ws_url(self.cfg.engine_url), proxy=None,
                         open_timeout=10, close_timeout=2) as ws:
                if self._handshake(ws, pairing_only=True) is False:
                    raise ValueError("invitation rejected")
            if self._credential:
                return
            time.sleep(1)

    def confirm_credential(self):
        """Save an explicitly supplied key only after an authenticated handshake."""
        from . import pairing
        from websockets.sync.client import connect
        if pairing.saved_register(self.cfg.member) == (self.cfg.engine_url, self.cfg.credential):
            return
        with connect(relay_config.ws_url(self.cfg.engine_url), proxy=None,
                     open_timeout=10, close_timeout=2) as ws:
            if not self._handshake(ws):
                raise ValueError("relay credential rejected")
        pairing.save_register_credential(self.cfg.member, self.cfg.credential, engine_url=self.cfg.engine_url)

    def stop(self) -> None:
        self._stop.set()
        self._retry.set()
        self._close_listener()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=12)

    def _open_listener(self):
        from . import listen
        with self._listener_lock:
            if self._stop.is_set():
                raise ValueError("网关正在停止")
            if self._listener is None:
                self._listener = listen.restore_listener(self.cfg)
                self._listener.wake_outbound = self._retry.set

    def _close_listener(self):
        with self._listener_lock:
            if self._listener is not None:
                self._listener.stop()
                self._listener = None

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def engine_url(self) -> str:
        return self._engine_url

    # ---------- 出站循环 ----------

    def _run(self) -> None:
        import websockets.sync.client as ws_sync
        from . import pairing
        window = relay_config.relay_retry.RetryRound()
        peer_name = f"{self.cfg.member}@{self.peer or 'default'}"
        paused = pairing.outbound_state().get("state") == "stopped"
        while not self._stop.is_set():
            if self._listener is None and pairing.listener_selected(self.cfg.member):
                try:
                    self._open_listener()
                except (OSError, ValueError, RuntimeError) as exc:
                    logger.warning("relay listener restore failed: %s", type(exc).__name__)
            if (pairing.listener_selected(self.cfg.member, confirmed=True)
                    and not pairing.outbound_requested(self.cfg.member)):
                if window.attempts or window.reasons:
                    window = relay_config.relay_retry.RetryRound()
                self._retry.wait(timeout=2)
                self._retry.clear()
                continue
            if not self.cfg.engine_url:
                saved_url, key = pairing.saved_register(self.cfg.member)
                if not saved_url or not key:
                    self._retry.wait(timeout=2)
                    self._retry.clear()
                    continue
                self.cfg = replace(self.cfg, engine_url=saved_url, credential=key)
                self._credential = key
            if self._follow_saved and (not self.cfg.invite or self._credential):
                saved = pairing.load_register_credential(self.cfg.member, engine_url=self.cfg.engine_url)
                if saved:
                    self._credential = saved
            if paused:
                if pairing.outbound_state().get("state") == "stopped":
                    self._retry.wait(timeout=2)
                    self._retry.clear()
                    continue
                paused = False
                window = relay_config.relay_retry.RetryRound()
            if not window.attempts:
                if not window.reasons:
                    window = relay_config.relay_retry.RetryRound()
                self._outbound_state("connecting")
            window.attempts += 1
            connected = False
            stage = "tcp"
            self._refusal = None
            try:
                with ws_sync.connect(relay_config.ws_url(self.cfg.engine_url),
                                     proxy=None, open_timeout=10, close_timeout=2,
                                     ping_interval=20, ping_timeout=5,
                                     max_size=8 * 1024 * 1024) as ws:
                    stage = "auth"
                    handshake = self._handshake(ws)
                    if self._stop.is_set():
                        return
                    if handshake is None:  # Pending or approved; reconnect without opening any streams.
                        window = relay_config.relay_retry.RetryRound()
                        if self._stop.wait(1.0):
                            return
                        continue
                    if not handshake:
                        raise ValueError("invalid handshake receipt")
                    connected = True
                    stage = "connected"
                    if window.reasons:
                        window.finish(logger, "bot-group relay link restored", peer_name, "restored")
                    else:
                        logger.info("bot-group relay link up member=%s generation=%d", peer_name, self._generation)
                    pairing.select_role(self.cfg.member, "dial")
                    self._outbound_state("online")
                    self._close_listener()
                    self._on_status("online")
                    streams = streams_mod.StreamRegistry(self.cfg.home, ws, self.protocol_version,
                                                          legacy=self.legacy_engine)
                    try:
                        self._recv_loop(ws, streams)
                    finally:
                        streams.close_all()
            except Exception as exc:  # noqa: BLE001 断线重连
                if self._stop.is_set():
                    return
                refused = self._refusal or relay_config.relay_retry.rejection(exc)
                if not refused and not connected and isinstance(exc, (ValueError, KeyError, TypeError, AttributeError)):
                    refused = ("protocol", "认证回执无效")
                reason = refused[1] if refused else relay_config.relay_retry.transient_reason(stage, exc)
                if connected:
                    window = relay_config.relay_retry.RetryRound()
                if not window.reasons:
                    logger.info("bot-group relay link down member=%s cause=%s", peer_name, reason)
                window.reasons[reason] += 1
                self._generation = 0
                self._on_status("offline")
                if (pairing.listener_selected(self.cfg.member, confirmed=True) and
                        not pairing.outbound_requested(self.cfg.member)):
                    self._outbound_state("listening")
                    continue
                if refused or window.attempts > len(relay_config.relay_retry.DELAYS):
                    summary = window.finish(logger, "bot-group relay dial stopped", peer_name,
                                            "rejected" if refused else "exhausted")
                    self._outbound_state("stopped", reason=reason, **summary)
                    paused = True
                    continue
                if not connected:
                    self._outbound_state("backoff", reason=reason, attempts=window.attempts)
                    self._retry.wait(timeout=relay_config.relay_retry.DELAYS[window.attempts - 1])
                    self._retry.clear()

    def _handshake(self, ws, *, pairing_only=False) -> bool | None:
        snapshot = mcp_digest.snapshot(self.cfg.mcp_python,
                                       code_version=relay_config.plugin_version())
        from . import pairing
        scope = {'engine_url': self.cfg.engine_url, 'invite': self.cfg.invite}
        credential = self._credential or pairing.load_register_credential(self.cfg.member, **scope)
        if pairing_only and credential:
            return True
        candidate = None
        hello = {"relay": {"kind": "hello"},
                 "member": self.cfg.member,
                 "credential": credential,
                 "code_version": snapshot["code_version"],
                 "plugin_version": snapshot["code_version"],
                 "mcp_fingerprint": snapshot["mcp_fingerprint"],
                 **relay_config.relay_protocol.offer(),
                 "mcp_python": snapshot["mcp_python"],
                 "mcp_entry": snapshot["mcp_entry"]}
        if not credential and self.cfg.invite:
            proof, candidate = pairing.register_offer(self.cfg.member, **scope)
            hello.update(invite=self.cfg.invite, join_proof=proof,
                         credential_commitment=hashlib.sha256(candidate.encode()).hexdigest())
        try:
            ws.send(json.dumps(hello, ensure_ascii=False))
            reply = json.loads(ws.recv(timeout=10))
        except Exception as exc:
            self._refusal = relay_config.relay_retry.rejection(exc)
            if self._refusal:
                if pairing_only:
                    if self._refusal[0] == "protocol":
                        raise relay_config.relay_protocol.ProtocolMismatch(self._refusal[1]) from None
                    raise ValueError(self._refusal[1]) from None
                return False
            raise
        receipt = reply.get('relay') or {}
        try:
            self.protocol_version = relay_config.relay_protocol.selected(receipt)
        except relay_config.relay_protocol.ProtocolMismatch as exc:
            self._refusal = relay_config.relay_retry.rejection(exc)
            ws.close(code=CLOSE_BAD_PROTOCOL, reason=relay_config.relay_protocol.REJECTION)
            return False
        self.legacy_engine = "protocol_version" not in receipt
        kind = receipt.get('kind')
        if candidate and kind == 'hello_pending':
            self._on_status('pending_confirmation')
            return None
        if candidate and kind == 'hello_confirmed':
            pairing.save_register_credential(self.cfg.member, candidate, **scope)
            self._credential = candidate
            return None
        if candidate or kind != 'hello_ok':
            return False
        self._credential = credential
        self._generation = int(reply["relay"].get("generation") or 0)
        self._engine_url = reply.get("engine_url") or ""
        return self._generation > 0

    def _recv_loop(self, ws, streams) -> None:
        last_beat = 0.0
        while not self._stop.is_set():
            try:
                raw = ws.recv(timeout=0.5)
                envelope = json.loads(raw)
                self._route_inbound(ws, envelope, streams)
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

    def _route_inbound(self, ws, envelope: dict, streams=None) -> None:
        meta = envelope.get("relay") or {}
        kind = meta.get("kind")
        frame_gen = meta.get("generation")
        # generation fencing：缺代/旧代帧一律拒收（旧连接晚到帧不得影响当前代）
        if kind not in ("hello_ok",) and frame_gen != self._generation:
            logger.warning("bot-group relay stale frame dropped member=%s kind=%s gen=%s",
                           self.cfg.member, kind, frame_gen)
            return
        if kind == "direction_cancel_listen":
            if self._listener is not None and self._listener._active_ws is None:
                self._close_listener()
            return
        if kind == "direction_listen":
            from . import pairing
            try:
                saved = pairing.saved_listener(self.cfg.member)
                requested = envelope.get("direction") or {}
                if any(requested.get(k) != saved[k] for k in ("fingerprint", "port")):
                    raise ValueError("引擎保存的端点与云端配对记录不一致")
                self._open_listener()
                result = {"ready": True}
            except Exception as exc:
                logger.warning("relay listener preparation failed: %s", type(exc).__name__)
                result = {"error": str(exc)[:220]}
            ws.send(json.dumps({"relay": {"kind": "direction_result",
                                         "generation": self._generation,
                                         "request_id": meta.get("request_id")},
                                "direction": result}))
            return
        if kind == 'workspace_request':
            from . import workspaces
            workspaces.respond(ws, meta, envelope.get('workspace'))
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
        if kind in ("frame", "stream_release"):
            streams.route(meta, envelope)
