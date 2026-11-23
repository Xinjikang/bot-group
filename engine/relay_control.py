"""Correlate administrator requests with replies on the same authenticated relay link."""
from concurrent.futures import Future
import json
import threading
import time
import uuid

from fastapi import HTTPException
from . import relay_server

_lock = threading.Lock()
_requests = {}


def receive(link, request_id, result):
    if not isinstance(request_id, str) or not isinstance(result, dict):
        return
    with _lock:
        future = _requests.pop((link, request_id), None)
    if future is not None:
        future.set_result(result)


def request(link, *, kind, field, spec, timeout, timeout_message):
    if link is None or link.closed:
        raise HTTPException(409, '远程节点离线，请连接后重试')
    rid, future = uuid.uuid4().hex, Future()
    with _lock:
        _requests[link, rid] = future
    try:
        link.send_text(json.dumps({'relay': {'kind': kind, 'generation': link.generation,
                                             'request_id': rid}, field: spec}))
        deadline = time.monotonic() + timeout
        while not future.done():
            if link.closed or time.monotonic() >= deadline:
                raise HTTPException(504, timeout_message)
            try:
                future.result(timeout=0.5)
            except TimeoutError:
                continue
        result = future.result()
        if link is not relay_server.active_link(link.member):
            raise HTTPException(409, '远程节点已重新连接，请重试')
        if result.get('error'):
            raise HTTPException(409, str(result['error'])[:300])
        return result
    except ConnectionError:
        raise HTTPException(409, '远程节点连接中断，请重试') from None
    finally:
        with _lock:
            _requests.pop((link, rid), None)
