"""Hermes public CLI discovery and hermes-gateway-v1 RPC transport.

No Hermes Python modules are loaded. The native control protocol issues a
single-use ticket, which authenticates the official /api/ws endpoint.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
from urllib.parse import urlsplit


class GatewayTransportError(RuntimeError):
    """An RPC outcome may be unknown; the caller must reconcile its receipt."""


class GatewayRefusal(RuntimeError):
    def __init__(self, reason, code=None):
        super().__init__(reason)
        self.reason, self.code = reason, code


def _pipe_exchange(home, data, timeout):
    import _winapi as win
    import ctypes
    import psutil
    from ctypes import wintypes

    digest = hashlib.sha256(os.path.normcase(str(Path(home).resolve())).encode()).hexdigest()[:16]
    name = rf'\\.\pipe\hermes-gateway-{digest}'
    deadline = time.monotonic() + timeout

    def budget():
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError('Hermes control deadline exceeded')
        return max(1, int(left * 1000))

    def complete(operation):
        try:
            if win.WaitForSingleObject(operation.event, budget()) != win.WAIT_OBJECT_0:
                raise TimeoutError('Hermes control deadline exceeded')
            count, error = operation.GetOverlappedResult(False)
            if error:
                raise OSError(error, 'Hermes control I/O failed')
            return count
        except BaseException:
            operation.cancel()
            operation.GetOverlappedResult(True)
            raise

    while True:
        try:
            # Identification-only impersonation lets the server authenticate the
            # caller without using its token to perform unrelated operations.
            handle = win.CreateFile(name, 0xC0000000, 0, 0, win.OPEN_EXISTING,
                                    win.FILE_FLAG_OVERLAPPED | 0x00110000, 0)
            break
        except OSError as exc:
            if exc.winerror != 231:
                raise
            win.WaitNamedPipe(name, budget())
    try:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        peer_pid = wintypes.ULONG()
        get_peer = kernel.GetNamedPipeServerProcessId
        get_peer.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
        get_peer.restype = wintypes.BOOL
        if not get_peer(handle, ctypes.byref(peer_pid)):
            raise PermissionError('Cannot verify Hermes control peer')
        if psutil.Process(peer_pid.value).username() != psutil.Process().username():
            raise PermissionError('Hermes control peer belongs to another user')
        remaining = data
        while remaining:
            operation, _ = win.WriteFile(handle, remaining, overlapped=True)
            sent = complete(operation)
            if not sent:
                raise ConnectionError('Hermes control closed during request')
            remaining = remaining[sent:]
        response = bytearray()
        while b'\n' not in response:
            operation, _ = win.ReadFile(handle, 4096, overlapped=True)
            count = complete(operation)
            if not count:
                raise ConnectionError('Hermes control closed before response')
            response.extend(operation.getbuffer()[:count])
            if len(response) > 65536:
                raise ValueError('Hermes control response too large')
        return bytes(response).split(b'\n', 1)[0]
    finally:
        win.CloseHandle(handle)


def _socket_exchange(home, data, timeout):
    path = Path(home) / 'gateway.sock'
    if not path.exists():
        pointer = Path(home) / 'gateway.sock.path'
        stat = pointer.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise PermissionError('Unsafe Hermes control pointer')
        path = Path(pointer.read_text(encoding='utf-8').strip())
    stat = path.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise PermissionError('Unsafe Hermes control socket')
    with socket.socket(socket.AF_UNIX) as peer:
        peer.settimeout(timeout)
        peer.connect(str(path))
        peer.sendall(data)
        response = bytearray()
        while b'\n' not in response:
            chunk = peer.recv(4096)
            if not chunk or len(response) + len(chunk) > 65536:
                raise ConnectionError('Invalid Hermes control response')
            response.extend(chunk)
    return bytes(response).split(b'\n', 1)[0]


def ticket(endpoint, *, purpose='interactive'):
    request = {'protocol': 1, 'id': 1, 'verb': 'session-ticket', 'params': {
        'profile_id': endpoint['profile_id'], 'instance_id': endpoint['instance_id'], 'purpose': purpose}}
    exchange = _pipe_exchange if os.name == 'nt' else _socket_exchange
    response = json.loads(exchange(endpoint.get('control_home') or endpoint['profile_id'],
                                   json.dumps(request).encode() + b'\n', 5))
    result = response.get('result') or {}
    if (response.get('ok') is not True or response.get('protocol') != 1 or response.get('id') != 1
            or any(result.get(key) != endpoint[key] for key in ('profile_id', 'instance_id', 'runtime_protocol'))
            or not isinstance(result.get('ticket'), str) or not result['ticket']):
        raise GatewayTransportError('Hermes control identity mismatch')
    return result['ticket']


def discover(home: Path, *, timeout=2):
    """Read an existing owner's control identity, including a serving multiplexer."""
    exchange = _pipe_exchange if os.name == 'nt' else _socket_exchange
    homes = [home]
    if home.parent.name == 'profiles':
        homes.append(home.parent.parent)
    for control_home in homes:
        try:
            raw = exchange(control_home, b'{"protocol":1,"verb":"identify","id":1}\n', timeout)
        except (FileNotFoundError, ConnectionRefusedError):
            continue
        try:
            response = json.loads(raw)
            payload = response['result']
            if (response.get('ok') is not True or response.get('protocol') != 1
                    or response.get('id') != 1 or payload.get('state') != 'ready'):
                raise GatewayTransportError('Hermes gateway is unavailable for reading')
            matches = [p for p in payload['served_profiles']
                       if Path(p['home']).resolve() == home.resolve()]
            if len(matches) != 1 or Path(matches[0]['profile_id']).resolve() != home.resolve():
                raise GatewayTransportError('Hermes gateway profile mismatch')
            endpoint = {key: payload[key] for key in
                        ('instance_id', 'authority_epoch', 'runtime_protocol', 'api_origin')}
            endpoint.update(profile_id=matches[0]['profile_id'], control_home=str(control_home))
            if (not isinstance(endpoint['instance_id'], str) or not endpoint['instance_id']
                    or type(endpoint['authority_epoch']) is not int or endpoint['authority_epoch'] <= 0
                    or 'session-authority-v1' not in payload['capabilities']):
                raise GatewayTransportError('Hermes gateway identity is invalid')
            return _validate_endpoint(endpoint, home)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise GatewayTransportError('Hermes gateway discovery returned invalid data') from exc
    raise GatewayTransportError('Hermes gateway is not running')


def _validate_endpoint(endpoint, home):
    if Path(endpoint['profile_id']).resolve() != Path(home).resolve() or endpoint['runtime_protocol'] != 1:
        raise GatewayTransportError('Hermes gateway profile or protocol mismatch')
    origin = urlsplit(endpoint['api_origin'])
    if (origin.scheme not in ('http', 'https') or origin.username or origin.password
            or origin.query or origin.fragment or origin.path not in ('', '/')
            or not ipaddress.ip_address(origin.hostname).is_loopback):
        raise GatewayTransportError('Hermes gateway must publish a loopback origin')
    return endpoint


def ensure(command: list[str], home: Path, *, env=None, timeout=60):
    child_env = dict(os.environ if env is None else env)
    child_env['HERMES_HOME'] = str(home)
    result = subprocess.run(command + ['gateway', 'ensure', '--json', '--timeout', str(timeout)],
                            env=child_env, capture_output=True, text=True, encoding='utf-8',
                            timeout=timeout + 10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        status = json.loads(result.stdout)
    except ValueError as exc:
        raise GatewayTransportError('Hermes gateway ensure returned invalid JSON') from exc
    if result.returncode or status.get('state') != 'ready' or not isinstance(status.get('endpoint'), dict):
        reason = status.get('reason_code')
        raise GatewayTransportError('Hermes gateway unavailable: ' + (reason if isinstance(reason, str) else 'unknown'))
    endpoint = status['endpoint']
    if status.get('client_code_sha') and endpoint.get('code_sha') != status['client_code_sha']:
        raise GatewayTransportError('Hermes gateway needs restart after installation update')
    return _validate_endpoint(endpoint, home)


# 活跃 viewer 连接 registry：引擎重启本地收尾需要集中、有界地关闭所有连接（设计 6.1 本地连接关闭硬超时 5s）。
_ACTIVE: set['GatewayClient'] = set()
_ACTIVE_LOCK = threading.Lock()
_CLOSING = threading.Event()


def _register(client: 'GatewayClient') -> bool:
    with _ACTIVE_LOCK:
        if _CLOSING.is_set():
            return False
        _ACTIVE.add(client)
        return True


def resume_connections() -> None:
    with _ACTIVE_LOCK:
        _CLOSING.clear()


def _unregister(client: 'GatewayClient') -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.discard(client)


def _close_one(client: 'GatewayClient') -> None:
    try:
        client.close()
    except Exception:
        pass


def close_all(timeout: float = 5.0) -> None:
    """Close viewers within one total budget and reject late connections."""
    with _ACTIVE_LOCK:
        _CLOSING.set()
        clients = list(_ACTIVE)
    if not clients:
        return
    deadline = time.monotonic() + timeout
    workers = []
    for client in clients:
        worker = threading.Thread(target=_close_one, args=(client,), daemon=True)
        worker.start()
        workers.append(worker)
    for worker in workers:
        worker.join(max(0.0, deadline - time.monotonic()))


class GatewayClient:
    """One viewer connection; the receiver drains events independently of RPCs."""
    def __init__(self, endpoint, *, on_event=None):
        from websockets.sync.client import connect
        if _CLOSING.is_set():
            raise GatewayTransportError('Hermes viewers are closing')
        origin = endpoint['api_origin'].rstrip('/')
        url = origin.replace('https:', 'wss:', 1).replace('http:', 'ws:', 1) + '/api/ws'
        self.endpoint = endpoint
        self._socket = connect(url, subprotocols=['hermes-gateway-v1', 'hermes-gateway-ticket.' + ticket(endpoint)],
                               proxy=None, open_timeout=10, close_timeout=3, max_size=8 * 1024 * 1024)
        if self._socket.subprotocol != 'hermes-gateway-v1':
            self._socket.close()
            raise GatewayTransportError('Hermes gateway protocol mismatch')
        self._condition = threading.Condition()
        self._pending = {}
        self._sequence = 0
        self._closed = False
        self._on_event = on_event
        if not _register(self):
            self.close()
            raise GatewayTransportError('Hermes viewers are closing')
        threading.Thread(target=self._read, name='hermes-gateway-reader', daemon=True).start()

    def _read(self):
        try:
            for raw in self._socket:
                frame = json.loads(raw)
                with self._condition:
                    rid = frame.get('id')
                    if rid in self._pending:
                        self._pending[rid] = frame
                        self._condition.notify_all()
                    elif self._on_event is not None:
                        self._on_event(frame)
        except Exception:
            pass
        finally:
            _unregister(self)
            with self._condition:
                self._closed = True
                self._condition.notify_all()

    def rpc(self, method, *, timeout=30, **params):
        with self._condition:
            if self._closed or _CLOSING.is_set():
                raise GatewayTransportError('Hermes viewer disconnected')
            self._sequence += 1
            rid = self._sequence
            self._pending[rid] = None
            try:
                self._socket.send(json.dumps({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params}))
                if not self._condition.wait_for(lambda: self._closed or self._pending[rid] is not None, timeout):
                    raise GatewayTransportError('Hermes RPC timed out; reconcile its receipt')
                frame = self._pending[rid]
                if frame is None:
                    raise GatewayTransportError('Hermes RPC outcome unknown; reconcile its receipt')
                if 'error' in frame:
                    error = frame['error']
                    reason = (error.get('data') or {}).get('reason') or error.get('message')
                    safe = reason if isinstance(reason, str) and re.fullmatch(r'[a-zA-Z0-9_]{1,80}', reason) else 'request_failed'
                    raise GatewayRefusal(safe, error.get('code'))
                return frame['result']
            finally:
                self._pending.pop(rid, None)

    def close(self):
        _unregister(self)
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._socket.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
