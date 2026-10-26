"""Track connected UI pages and arbitrate browser launches per client address."""
from __future__ import annotations

import asyncio
import ipaddress
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from .api import _admin_only, _resolve_identity

router = APIRouter()


def peer_key(host: str) -> str:
    try:
        address = ipaddress.ip_address(host)
        if address.is_loopback or (getattr(address, 'ipv4_mapped', None) and address.ipv4_mapped.is_loopback):
            return 'loopback'
    except ValueError:
        pass
    return host


class Presence:
    """All methods run on the ASGI event loop; open reservations expire in 10s."""
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.clients: dict[str, tuple[str, asyncio.Queue]] = {}
        self.pending: dict[str, tuple[str, float]] = {}

    def connect(self, peer: str):
        key, queue = uuid.uuid4().hex, asyncio.Queue(maxsize=1)
        self.clients[key] = (peer, queue)
        self.pending.pop(peer, None)
        return key, queue

    def disconnect(self, key: str):
        self.clients.pop(key, None)

    def activate(self, peer: str) -> dict:
        for client_peer, queue in reversed(list(self.clients.values())):
            if client_peer == peer:
                if queue.empty():
                    queue.put_nowait('activate')
                return {'action': 'reuse'}
        now = self.clock()
        self.pending = {key: value for key, value in self.pending.items() if value[1] > now}
        if peer in self.pending:
            return {'action': 'pending'}
        token = uuid.uuid4().hex
        self.pending[peer] = (token, now + 10)
        return {'action': 'open', 'reservation': token}

    def release(self, peer: str, token: str):
        if self.pending.get(peer, (None,))[0] == token:
            self.pending.pop(peer, None)


def _state(request: Request) -> Presence:
    state = getattr(request.app.state, 'ui_presence', None)
    if state is None:
        state = request.app.state.ui_presence = Presence()
    return state


def _peer(request: Request) -> str:
    if request.client is None:
        raise HTTPException(400, 'Client address required')
    return peer_key(request.client.host)


@router.get('/desktop/events')
async def desktop_events(request: Request):
    # The login page also registers. This stream carries only activation signals;
    # browser fetch metadata excludes cross-origin registration before login.
    if request.headers.get('sec-fetch-site') != 'same-origin':
        raise HTTPException(403, 'Same-origin UI connection required')
    state, peer = _state(request), _peer(request)

    async def stream():
        key, queue = state.connect(peer)
        try:
            yield 'retry: 1000\n: connected\n\n'
            while True:
                stop = getattr(request.app.state, 'stop_event', None)
                if stop is not None and stop.is_set():
                    break
                try:
                    await asyncio.wait_for(queue.get(), timeout=1)
                    yield 'event: activate\ndata: {}\n\n'
                except asyncio.TimeoutError:
                    yield ': keepalive\n\n'
        finally:
            state.disconnect(key)

    return StreamingResponse(stream(), media_type='text/event-stream', headers={
        'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no',
    })


@router.post('/admin/ui/activate')
async def activate_ui(request: Request, identity=Depends(_resolve_identity)):
    _admin_only(identity, '打开界面仅限管理员')
    return _state(request).activate(_peer(request))


@router.delete('/admin/ui/open/{reservation}')
async def release_open(reservation: str, request: Request, identity=Depends(_resolve_identity)):
    _admin_only(identity, '打开界面仅限管理员')
    _state(request).release(_peer(request), reservation)
    return {'ok': True}
