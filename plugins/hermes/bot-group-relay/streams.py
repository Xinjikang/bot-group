"""One authenticated relay connection owns its local Hermes gateway streams."""

from __future__ import annotations

import json
import logging
import queue
import threading
import hashlib

logger = logging.getLogger(__name__)

# Match the engine's transcript socket. Each relay message fits either dialer's limit.
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
CHUNK_CHARS = 32 * 1024


class LocalGatewayStream:
    def __init__(self, home, relay_ws, meta):
        self.home = home
        self.relay_ws = relay_ws
        self.stream = meta["stream"]
        self.generation = meta["generation"]
        self.chunked = meta.get("accept_chunks") is True
        self.protocol_version = meta.get("protocol_version", 1)
        self._outbound = queue.Queue(maxsize=256)
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"bot-group-stream-{self.stream}",
                                        daemon=True)
        self._thread.start()

    def _send(self, kind, **fields):
        meta = {"kind": kind, "stream": self.stream, "generation": self.generation}
        self.relay_ws.send(json.dumps({"relay": meta, **fields}, ensure_ascii=False))

    def _read_message(self, ws):
        if not self.chunked:
            self._send("frame", frame=json.loads(ws.recv()))
            return
        seq = 0
        for fragment in ws.recv_streaming(decode=True):
            for start in range(0, len(fragment), CHUNK_CHARS):
                if self._closed.is_set():
                    return
                self._send("frame_chunk", seq=seq, data=fragment[start:start + CHUNK_CHARS])
                seq += 1
        self._send("frame_chunk", seq=seq, end=True)

    def _write(self, ws):
        try:
            while not self._closed.is_set():
                text = self._outbound.get()
                if text is None:
                    break
                ws.send(text)
        except Exception as exc:
            self.fail(exc)
        finally:
            ws.close()

    def _run(self):
        from . import tickets
        try:
            from websockets.sync.client import connect
            endpoint, ticket = tickets.fetch_ticket(self.home, purpose="interactive")
            with connect(tickets.gateway_ws_url(endpoint),
                         subprotocols=["hermes-gateway-v1", "hermes-gateway-ticket." + ticket],
                         proxy=None, open_timeout=10, close_timeout=1, max_size=MAX_MESSAGE_BYTES,
                         ping_interval=20, ping_timeout=5) as ws:
                if ws.subprotocol != "hermes-gateway-v1":
                    raise ConnectionError("Hermes gateway protocol mismatch")
                if self._closed.is_set():
                    return
                writer = threading.Thread(target=self._write, args=(ws,), daemon=True,
                                          name=f"bot-group-stream-writer-{self.stream}")
                writer.start()
                while not self._closed.is_set():
                    self._read_message(ws)
        except Exception as exc:
            self.fail(exc)
        finally:
            self.close()

    def fail(self, exc):
        if self._closed.is_set():
            return
        self.close()
        code = getattr(getattr(exc, "sent", None), "code", None)
        if code is None:
            code = getattr(getattr(exc, "rcvd", None), "code", None)
        logger.warning("relay gateway stream closed stream=%s cause=%s code=%s",
                       self.stream, type(exc).__name__, code)
        if self.protocol_version < 2:
            return
        try:
            self._send("stream_closed", code=code)
        except OSError:
            pass
        except Exception:
            logger.debug("relay disconnected before stream close delivery", exc_info=True)

    def send_text(self, text):
        if not self._closed.is_set():
            try:
                self._outbound.put_nowait(text)
            except queue.Full as exc:
                self.fail(exc)

    def close(self):
        self._closed.set()
        try:
            self._outbound.put_nowait(None)
        except queue.Full:
            pass  # The writer observes _closed before taking its next item.


class StreamRegistry:
    """Only the receive loop for the owning authenticated socket mutates this registry."""

    def __init__(self, home, ws, protocol_version=2, *, legacy=False):
        self.home = home
        self.ws = ws
        self.protocol_version = protocol_version
        self.legacy = legacy
        self.streams = {}
        from . import pairing
        self.peer = pairing.current_peer()

    def route(self, meta, envelope):
        name = meta.get("stream")
        if not isinstance(name, str):
            return
        if meta["kind"] == "stream_release":
            if self.protocol_version < 2 and not self.legacy:
                return
            local = self.streams.pop(name, None)
            if local:
                local.close()
            return
        frame = envelope.get("frame")
        if frame is None:
            return
        if self.peer and isinstance(frame, dict) and frame.get("method") in ("session.create", "session.list"):
            # Host title lookups and idempotent creation share one Hermes database.
            params = dict(frame.get("params") or {})
            for key in ("title", "request_id"):
                value = params.get(key)
                if isinstance(value, str) and value:
                    digest = hashlib.sha256(f"{self.peer}:{key}:{value}".encode()).hexdigest()
                    params[key] = "bg-" + digest if key == "title" else digest[:32]
            frame = {**frame, "params": params}
        if name not in self.streams:
            # v0.3.4 signalled chunk support per stream under the legacy v1 hello.
            chunks = meta.get("accept_chunks") is True and (self.protocol_version >= 2 or self.legacy)
            stream_meta = dict(meta, accept_chunks=chunks, protocol_version=2 if chunks else self.protocol_version)
            self.streams[name] = LocalGatewayStream(self.home, self.ws, stream_meta)
        self.streams[name].send_text(json.dumps(frame, ensure_ascii=False))

    def close_all(self):
        for local in self.streams.values():
            local.close()
        self.streams.clear()
