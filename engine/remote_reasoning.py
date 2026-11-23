"""Bounded, shared transcript reads for the remote Hermes reasoning panel."""
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import math
import threading
import time

from . import hermes_gateway, reasoning

logger = logging.getLogger(__name__)
_MAX_ACTIVE = 2
_workers = ThreadPoolExecutor(max_workers=_MAX_ACTIVE, thread_name_prefix="relay-history")
_lock = threading.Lock()
_reads = {}
_SUCCESS_TTL = 5
_ERROR_TTL = 20


def _fetch(profile, session_id, title):
    started = time.time()
    try:
        client, _, _ = hermes_gateway.connection(profile, read_only=True, history=True)
        with client:
            if session_id:
                target = client.rpc("session.resume", session_id=session_id, timeout=5)["session_id"]
            else:
                matches = client.rpc("session.list", title=title, timeout=5)["sessions"]
                target = matches[0]["session_id"] if matches else None
            rows = []
            if target:
                exported = client.rpc("session.export", session_id=target, format="json", timeout=10)
                rows = json.loads(exported["content"])["messages"]
        return time.monotonic() + _SUCCESS_TTL, started, rows
    except Exception as exc:
        logger.warning("Remote history read failed profile=%s error=%s", profile, type(exc).__name__)
        return time.monotonic() + _ERROR_TTL, started, None


def read(profile, generation, session_id, title, after_id, started_at, ended_at, completed_at):
    key = (profile, generation, session_id, None if session_id else title)
    with _lock:
        now = time.monotonic()
        for other, future in list(_reads.items()):
            if future.done() and future.result()[0] <= now:
                del _reads[other]
        future = _reads.get(key)
        if future is not None:
            if not future.done():
                return {"retry_after_ms": 1000}
            expires, captured, rows = future.result()
            if rows is None:
                return {"retry_after_ms": max(1000, math.ceil((expires - now) * 1000))}
            # A final read must include events flushed when the execution ended.
            if completed_at is not None and captured < completed_at <= time.time():
                del _reads[key]
                future = None
        if future is None:
            if len(_reads) < 8 and sum(not f.done() for f in _reads.values()) < _MAX_ACTIVE:
                _reads[key] = _workers.submit(_fetch, profile, session_id, title)
            return {"retry_after_ms": 1000}
    return reasoning._gateway_entries(rows, after_id, started_at, ended_at)
