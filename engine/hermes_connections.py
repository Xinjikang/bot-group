"""Profile-scoped WS leases, idle expiry and passive engine-wide status."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from .hermes_gateway_client import GatewayTransportError, GatewayRefusal, GatewayUnavailable, remaining

IDLE_SECONDS = 600
HEARTBEAT_SECONDS = 20
_LOG = logging.getLogger(__name__)
_START_LOCK = threading.Lock()
_START_FAILURES = {}


def installed_revision():
    from . import hermes_runtime
    from .session_adapter import _hermes_home
    source = hermes_runtime.source_directory(_hermes_home())
    stamp = source / 'install-stamp.json'
    if stamp.is_file():
        value = json.loads(stamp.read_text(encoding='utf-8-sig')).get('commit')
    else:
        result = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'],
                                capture_output=True, text=True, timeout=1,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        value = result.stdout.strip() if result.returncode == 0 else None
    if not isinstance(value, str) or not value:
        raise GatewayRefusal('installation_identity_unknown')
    return (str(source.resolve()), value)


def open_connection(home, command, callback, *, allow_start, revision):
    from . import hermes_gateway as gateway
    deadline = time.monotonic() + 5
    try:
        endpoint = gateway.discover(home, timeout=remaining(deadline))
    except GatewayUnavailable:
        if not allow_start:
            raise
        # Named profiles may share a single native multiplexer.
        with _START_LOCK:
            try:
                endpoint = gateway.discover(home, timeout=2)
            except GatewayUnavailable:
                root = home.parent.parent if home.parent.name == 'profiles' else home
                key = (os.path.normcase(str(root.resolve())), tuple(command))
                failed = _START_FAILURES.get(key)
                if failed and time.monotonic()-failed[0] < 5:
                    raise failed[1]
                try:
                    endpoint = gateway.ensure(command, home)
                    _START_FAILURES.pop(key, None)
                except Exception as exc:
                    _START_FAILURES[key] = (time.monotonic(), exc)
                    raise
        deadline = time.monotonic() + 5
    if endpoint.get('code_sha') != revision[1]:
        raise GatewayRefusal('gateway_restart_required')
    client = gateway.GatewayClient(endpoint, on_event=callback, deadline=deadline)
    try:
        info = client.rpc('runtime.describe', timeout=remaining(deadline))
        if (info.get('profile_id') != endpoint['profile_id']
                or info.get('instance_id') != endpoint['instance_id']
                or type(info.get('authority_epoch')) is not int or info['authority_epoch'] <= 0):
            raise GatewayRefusal('gateway_identity_mismatch')
        # Control identify carries the launch profile epoch; routed profiles own separate epochs.
        if not {'durable-admission-v1', 'acp-session-mcp-v1'} <= set(info.get('capabilities', [])):
            raise GatewayRefusal('gateway_capabilities_unavailable')
        return client
    except BaseException:
        client.close()
        raise


def unresolved(home):
    """Keep uncertain durable executions occupied even after a watcher exits."""
    from . import db
    conn = None
    try:
        conn = db.get_connection()
        if conn.execute("SELECT 1 FROM hermes_executions WHERE home=? AND "
                        "(state<>'terminal' OR cleanup_pending<>0) LIMIT 1", (str(home),)).fetchone():
            return True
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hud_sessions'").fetchone():
            return False
        for row in conn.execute('SELECT state FROM hud_sessions'):
            record = json.loads(row['state']).get('gateway', {})
            if record.get('home') == str(home) and record.get('attempted') and not record.get('terminal'):
                return True
        return False
    except Exception:
        # A failed ledger read cannot prove the profile is idle.
        return True
    finally:
        if conn is not None:
            conn.close()


class Entry:
    def __init__(self, home, command, now):
        self.home, self.command = home, command
        self.gate = threading.RLock()
        self.client = None
        self.revision = None
        self.leases = set()
        self.subscriptions = {}
        self.activity = now
        self.checked = 0
        self.state = 'waiting'
        self.retries = 0
        self.retry_at = 0
        self.last_error = None
        self.last_allow_start = False


class Lease:
    def __init__(self, pool, entry, client, callback, business):
        self.pool, self.entry, self.client = pool, entry, client
        self.callback, self.business = callback, business
        self.sessions = set()
        self.closed = False

    @property
    def endpoint(self):
        """Identity of this lease's bound connection, including after pool reconnects."""
        return self.client.endpoint

    def rpc(self, method, **params):
        if self.closed:
            raise GatewayTransportError('Hermes connection lease closed')
        sid = params.get('session_id')
        with self.pool.lock:
            if sid:
                self.sessions.add(sid)
            if self.business:
                self.entry.activity = self.pool.clock()
        try:
            if method in ('session.resume', 'session.create'):
                with self.entry.gate:
                    result = self.client.rpc(method, **params)
                    if isinstance(result, dict) and result.get('subscription_id'):
                        sid = result['session_id']
                        with self.pool.lock:
                            self.sessions.add(sid)
                            self.entry.subscriptions[(self.client, sid)] = result['subscription_id']
                    return result
            result = self.client.rpc(method, **params)
            if method == 'session.detach':
                with self.pool.lock:
                    self.entry.subscriptions.pop((self.client, sid), None)
            return result
        except GatewayTransportError:
            self.pool.failed(self.entry, self.client)
            raise

    def close(self):
        with self.pool.lock:
            if self.closed:
                return
            self.closed = True
            self.entry.leases.discard(self)
            if self.business:
                self.entry.activity = self.pool.clock()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class Pool:
    def __init__(self, opener=open_connection, revision=installed_revision, pinned=unresolved,
                 clock=time.monotonic, maintenance=None):
        self.opener, self.revision, self.pinned, self.clock = opener, revision, pinned, clock
        self.maintenance = maintenance
        self.lock = threading.RLock()
        self.entries = {}
        self.stopped = False
        self.stop_event = threading.Event()
        self.worker = None
        self.threads = []
        self.events = queue.Queue(maxsize=512)

    def start(self):
        with self.lock:
            if not self.stopped and self.worker is None:
                self.worker = threading.Thread(target=self._run, daemon=True, name='hermes-connections')
                self.threads = [self.worker, threading.Thread(
                    target=self._events, daemon=True, name='hermes-connection-events')]
                if self.maintenance:
                    self.threads.append(threading.Thread(
                        target=self._maintain, daemon=True, name='hermes-session-resources'))
                for thread in self.threads:
                    thread.start()

    def _event(self, entry, generation, frame):
        try:
            self.events.put_nowait((entry, generation, frame))
        except queue.Full:
            pass  # Reasoning is advisory; RPC receipts use the transport's separate response map.

    def _events(self):
        while not self.stop_event.is_set():
            try:
                entry, generation, frame = self.events.get(timeout=.5)
            except queue.Empty:
                continue
            sid = (frame.get('params') or {}).get('session_id')
            with self.lock:
                callbacks = [lease.callback for lease in entry.leases if lease.callback
                             and not lease.closed and lease.client is generation.get('client')
                             and sid in lease.sessions]
            for callback in callbacks:
                try:
                    callback(frame)
                except Exception:
                    _LOG.exception('Hermes event consumer failed')

    def _connect(self, entry, allow_start):
        with self.lock:
            if self.stopped:
                raise GatewayTransportError('Hermes connection pool stopped')
            entry.state = 'connecting'
            entry.last_allow_start = allow_start
        client = None
        try:
            revision = self.revision()
            generation = {}
            client = self.opener(entry.home, entry.command,
                                 lambda frame: self._event(entry, generation, frame),
                                 allow_start=allow_start, revision=revision)
            generation['client'] = client
            with self.lock:
                if self.stopped:
                    raise GatewayTransportError('Hermes connection pool stopped')
                entry.client, entry.revision = client, revision
                entry.subscriptions.clear()
                entry.state, entry.checked = 'connected', self.clock()
                entry.retries = 0
                entry.last_error = None
            return client
        except Exception as exc:
            if client:
                client.close()
            with self.lock:
                entry.state = ('offline' if isinstance(exc, GatewayUnavailable) else
                               'restart_required' if getattr(exc, 'reason', '') == 'gateway_restart_required' else 'error')
                entry.last_error, entry.retry_at = exc, self.clock()+5
            raise

    def acquire(self, home, command, *, callback=None, business=True):
        key = os.path.normcase(str(Path(home).resolve()))
        with self.lock:
            if self.stopped:
                raise GatewayTransportError('Hermes connection pool stopped')
            entry = self.entries.setdefault(key, Entry(Path(home), command, self.clock()))
        with entry.gate:
            if self.stopped:
                raise GatewayTransportError('Hermes connection pool stopped')
            if business:
                entry.command = command
            revision = self.revision()
            if entry.client and not entry.client.closed and entry.revision != revision:
                with self.lock:
                    entry.state = 'restart_required'
                raise GatewayRefusal('gateway_restart_required')
            client = entry.client
            if client is None or client.closed:
                start_after_read = (business and not entry.last_allow_start
                                    and isinstance(entry.last_error, GatewayUnavailable))
                if entry.last_error and self.clock() < entry.retry_at and not start_after_read:
                    raise entry.last_error
                client = self._connect(entry, business)
            with self.lock:
                if self.stopped:
                    raise GatewayTransportError('Hermes connection pool stopped')
                lease = Lease(self, entry, client, callback, business)
                entry.leases.add(lease)
                if business:
                    entry.activity = self.clock()
            return lease

    def failed(self, entry, client):
        with self.lock:
            if entry.client is client:
                entry.state, entry.retry_at = 'error', self.clock()+1
                entry.last_error = GatewayTransportError('Hermes connection interrupted; retry after reconnect')
            entry.subscriptions = {k: v for k, v in entry.subscriptions.items() if k[0] is not client}
        client.close()

    def sweep(self):
        with self.lock:
            entries = list(self.entries.values())
        for entry in entries:
            if self.stopped or not entry.gate.acquire(blocking=False):
                continue
            try:
                now = self.clock()
                with self.lock:
                    idle = not entry.leases and now-entry.activity >= IDLE_SECONDS
                if idle and not self.pinned(entry.home):
                    if entry.client:
                        deadline = time.monotonic() + 2
                        try:
                            for (owner, sid), sub in list(entry.subscriptions.items()):
                                if owner is entry.client and not owner.closed:
                                    owner.rpc('session.detach', session_id=sid, subscription_id=sub,
                                              timeout=remaining(deadline))
                        finally:
                            entry.client.close()
                            entry.client = None
                            entry.subscriptions.clear()
                            entry.state = 'waiting'
                            entry.last_error = None
                    entry.state = 'waiting'
                    entry.last_error = None
                    continue
                client = entry.client
                if client is None:
                    continue
                if client.closed:
                    if entry.state == 'connected':
                        entry.state, entry.retry_at = 'error', now+1
                    if entry.state == 'restart_required' or entry.retries >= 3 or now < entry.retry_at:
                        continue
                    retries = entry.retries + 1
                    try:
                        self._connect(entry, False)
                    except Exception:
                        entry.retries, entry.retry_at = retries, now+2**retries
                    continue
                if now-entry.checked >= HEARTBEAT_SECONDS:
                    if self.revision() != entry.revision:
                        entry.state = 'restart_required'
                        entry.checked = now
                        continue
                    client.rpc('ping', timeout=3)
                    entry.checked, entry.state = now, 'connected'
            except Exception:
                if entry.client:
                    self.failed(entry, entry.client)
            finally:
                entry.gate.release()

    def _run(self):
        while not self.stop_event.wait(1):
            self.sweep()

    def _maintain(self):
        while not self.stop_event.wait(3):
            try:
                self.maintenance()
            except Exception:
                _LOG.exception('Session resource maintenance failed')

    def status(self):
        with self.lock:
            states = []
            for entry in self.entries.values():
                state = entry.state
                if state == 'connected':
                    if entry.client.closed:
                        state = 'error'
                    elif self.clock()-entry.checked > HEARTBEAT_SECONDS+10:
                        state = 'unknown'
                states.append(state)
            state = next((s for s in ('restart_required', 'error', 'connecting', 'offline', 'unknown', 'connected')
                          if s in states), 'waiting')
            return {'state': state, 'updated_at': time.time()}

    def stop(self):
        with self.lock:
            self.stopped = True
            self.stop_event.set()

    def join(self, timeout=10):
        """Wait for all pool workers before their owning runtime is released."""
        deadline = time.monotonic() + timeout
        with self.lock:
            threads = list(self.threads)
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            raise TimeoutError('Hermes connection pool workers are still running')


def _maintain_sessions():
    from . import hermes_session_auth
    hermes_session_auth.maintain()


_POOL = Pool(maintenance=_maintain_sessions)


def acquire(home, command, *, callback=None, business=True):
    _POOL.start()
    return _POOL.acquire(home, command, callback=callback, business=business)


def resume():
    global _POOL
    if _POOL.stopped:
        _POOL = Pool(maintenance=_maintain_sessions)


def stop():
    _POOL.stop()


def status():
    from . import hermes_gateway
    try:
        mode = 'gateway' if hermes_gateway.enabled() else 'subprocess'
        return {'mode': mode, **_POOL.status()}
    except Exception:
        return {'mode': 'unknown', 'state': 'unknown', 'updated_at': time.time()}
