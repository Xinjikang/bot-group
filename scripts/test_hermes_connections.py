"""Isolated connection lifecycle, wire multiplexing and passive status checks."""
import concurrent.futures
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from engine import hermes_connections as connections, hermes_gateway as gateway
from engine import hermes_gateway_client as transport


class Remote:
    def __init__(self, callback=None):
        self.closed = False
        self.callback = callback
        self.calls = []

    def rpc(self, method, **params):
        if self.closed:
            raise transport.GatewayTransportError('closed')
        self.calls.append((method, params))
        if method in ('session.resume', 'session.create'):
            return {'session_id': params.get('session_id', 'created'), 'subscription_id': str(len(self.calls))}
        return {'pong': True}

    def close(self):
        self.closed = True


class PoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.now = 0
        self.revision = ('source', 'sha')
        self.remotes = []
        self.opener = Mock(side_effect=self.open)
        self.pinned = Mock(return_value=False)
        self.pool = connections.Pool(self.opener, lambda: self.revision, self.pinned, lambda: self.now)
        self.addCleanup(self.pool.stop)

    def open(self, home, command, callback, **options):
        remote = Remote(callback)
        self.remotes.append(remote)
        return remote

    def acquire(self, **kw):
        return self.pool.acquire(self.home, ['fixture'], **kw)

    def test_concurrent_leases_share_connection_and_profile_isolation(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            leases = list(executor.map(lambda _: self.acquire(), range(8)))
        self.assertEqual(self.opener.call_count, 1)
        self.assertEqual(len({id(lease.client) for lease in leases}), 1)
        other = self.pool.acquire(self.home/'profiles'/'other', ['fixture'])
        self.assertIsNot(other.client, leases[0].client)
        for lease in [*leases, other]:
            lease.close()
        self.assertFalse(self.remotes[0].closed)

    def test_subscription_survives_last_lease_until_idle_sweep(self):
        first, second = self.acquire(), self.acquire()
        first.rpc('session.resume', session_id='a')
        snapshot = second.rpc('session.resume', session_id='a')
        first.close()
        self.assertFalse(any(m == 'session.detach' for m, _ in second.client.calls))
        second.close()
        self.assertFalse(any(m == 'session.detach' for m, _ in second.client.calls))
        self.now = 600
        self.pool.sweep()
        self.assertEqual(second.client.calls[-1], ('session.detach', {
            'session_id': 'a', 'subscription_id': snapshot['subscription_id'],
            'timeout': second.client.calls[-1][1]['timeout']}))
        self.assertTrue(second.client.closed)

    def test_events_routed_by_session_and_consumer_failure_isolated(self):
        self.pool.start()
        seen = threading.Event()
        wrong = Mock()
        first = self.acquire(callback=lambda frame: seen.set())
        second = self.acquire(callback=wrong)
        first.rpc('session.resume', session_id='a')
        second.rpc('session.resume', session_id='b')
        first.client.callback({'method': 'event', 'params': {'session_id': 'a'}})
        self.assertTrue(seen.wait(2))
        wrong.assert_not_called()
        first.close(); second.close()

    def test_idle_expiry_ignores_heartbeat_status_and_read_only_queries(self):
        lease = self.acquire(); remote = lease.client; lease.close()
        for stamp in (20, 100, 599):
            self.now = stamp; self.pool.sweep(); self.pool.status()
            with self.acquire(business=False) as reader:
                reader.rpc('session.list')
            self.assertFalse(remote.closed)
        self.now = 600; self.pool.sweep()
        self.assertTrue(remote.closed)
        self.assertEqual(self.pool.status()['state'], 'waiting')
        self.acquire().close()
        self.assertEqual(self.opener.call_count, 2)

    def test_active_lease_and_durable_unknown_protect_expiry(self):
        lease = self.acquire(); self.now = 800; self.pool.sweep()
        self.assertFalse(lease.client.closed)
        lease.close(); self.now = 1500; self.pinned.return_value = True; self.pool.sweep()
        self.assertFalse(lease.client.closed)
        self.pinned.return_value = False; self.pool.sweep()
        self.assertTrue(lease.client.closed)

    def test_new_revision_blocks_acquisition_and_heartbeat_reports_restart(self):
        lease = self.acquire(); lease.close()
        self.revision = ('source', 'new')
        with self.assertRaises(transport.GatewayRefusal): self.acquire()
        self.now = 30; self.pool.sweep()
        self.assertEqual(self.pool.status()['state'], 'restart_required')
        self.assertFalse(lease.client.closed)

    def test_disconnect_reconnects_without_replaying_business(self):
        lease = self.acquire()
        lease.rpc('prompt.submit', session_id='a', submission_id='original')
        self.pool.failed(lease.entry, lease.client)
        self.now = 1; self.pool.sweep()
        with self.acquire() as recovered:
            recovered.rpc('prompt.receipt', session_id='a', admission_id='original-admission')
        self.assertEqual(sum(m == 'prompt.submit' for r in self.remotes for m, _ in r.calls), 1)
        self.assertIs(self.opener.call_args.kwargs['allow_start'], False)
        lease.close()

    def test_endpoint_stays_bound_to_lease_after_reconnect(self):
        first = self.acquire()
        first.client.endpoint = {'profile_id': 'fixture', 'instance_id': 'original'}
        original = first.client.endpoint
        self.pool.failed(first.entry, first.client)
        self.now = 1
        self.pool.sweep()
        second = self.acquire()
        second.client.endpoint = {'profile_id': 'fixture', 'instance_id': 'replacement'}
        self.assertIs(first.endpoint, original)
        self.assertEqual(second.endpoint['instance_id'], 'replacement')
        with self.assertRaises(AttributeError):
            first.endpoint = second.endpoint
        first.close()
        second.close()

    def test_reconnect_attempts_are_bounded(self):
        lease = self.acquire(); lease.close(); self.remotes[0].close()
        self.opener.side_effect = transport.GatewayUnavailable('offline')
        for stamp in (0, 1, 3, 7, 15, 31, 100):
            self.now = stamp; self.pool.sweep()
        self.assertEqual(self.opener.call_count, 4)
        self.assertEqual(self.pool.status()['state'], 'offline')

    def test_shutdown_rejects_connection_finishing_late(self):
        def opening(*args, **kw):
            self.pool.stop()
            return self.open(*args, **kw)
        self.opener.side_effect = opening
        with self.assertRaises(transport.GatewayTransportError): self.acquire()
        self.assertTrue(self.remotes[0].closed)
        with self.assertRaises(transport.GatewayTransportError): self.acquire()

    def test_failed_open_is_shared_by_concurrent_waiters(self):
        self.opener.side_effect = transport.GatewayUnavailable('offline')
        def attempt(_):
            try: self.acquire()
            except transport.GatewayUnavailable: return 'offline'
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            self.assertEqual(list(executor.map(attempt, range(8))), ['offline']*8)
        self.assertEqual(self.opener.call_count, 1)

    def test_business_can_start_after_read_only_discovery_found_no_gateway(self):
        self.opener.side_effect = transport.GatewayUnavailable('offline')
        with self.assertRaises(transport.GatewayUnavailable): self.acquire(business=False)
        self.opener.side_effect = self.open
        self.acquire().close()
        self.assertTrue(self.opener.call_args.kwargs['allow_start'])


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        connections._START_FAILURES.clear()
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.endpoint = {'code_sha': 'sha', 'profile_id': str(self.home), 'instance_id': 'instance',
                         'authority_epoch': 2, 'runtime_protocol': 1, 'api_origin': 'http://127.0.0.1:1'}
        self.remote = Remote()
        self.remote.rpc = Mock(return_value={**self.endpoint,
                          'capabilities': ['durable-admission-v1', 'acp-session-mcp-v1']})

    def call(self, start=True):
        return connections.open_connection(self.home, ['fixture'], None, allow_start=start, revision=('source', 'sha'))

    def test_healthy_gateway_uses_no_cli(self):
        with patch.object(gateway, 'discover', return_value=self.endpoint), \
             patch.object(gateway, 'ensure') as ensure, patch.object(gateway, 'GatewayClient', return_value=self.remote):
            self.assertIs(self.call(), self.remote)
        ensure.assert_not_called()

    def test_absence_alone_allows_start(self):
        with patch.object(gateway, 'discover', side_effect=transport.GatewayUnavailable('absent')), \
             patch.object(gateway, 'ensure', return_value=self.endpoint) as ensure, \
             patch.object(gateway, 'GatewayClient', return_value=self.remote):
            self.call(); ensure.assert_called_once()
            with self.assertRaises(transport.GatewayUnavailable): self.call(False)
            self.assertEqual(ensure.call_count, 1)

    def test_ambiguous_failures_never_start(self):
        for error in (PermissionError(), TimeoutError(), transport.GatewayTransportError('invalid')):
            with self.subTest(error=type(error)), patch.object(gateway, 'discover', side_effect=error), \
                 patch.object(gateway, 'ensure') as ensure:
                with self.assertRaises(type(error)): self.call()
                ensure.assert_not_called()

    def test_named_profiles_share_failed_start_result(self):
        with patch.object(gateway, 'discover', side_effect=transport.GatewayUnavailable('absent')), \
             patch.object(gateway, 'ensure', side_effect=TimeoutError('startup')) as ensure:
            for profile in ('a', 'b'):
                with self.assertRaises(TimeoutError):
                    connections.open_connection(self.home/'profiles'/profile, ['fixture'], None,
                                                allow_start=True, revision=('source', 'sha'))
        self.assertEqual(ensure.call_count, 1)

    def test_version_and_runtime_identity_must_match(self):
        with patch.object(gateway, 'discover', return_value={**self.endpoint, 'code_sha': 'old'}), \
             patch.object(gateway, 'GatewayClient') as client:
            with self.assertRaises(transport.GatewayRefusal): self.call()
            client.assert_not_called()
        self.remote.rpc.return_value['instance_id'] = 'other'
        with patch.object(gateway, 'discover', return_value=self.endpoint), \
             patch.object(gateway, 'GatewayClient', return_value=self.remote):
            with self.assertRaises(transport.GatewayRefusal): self.call()
        self.assertTrue(self.remote.closed)

    def test_multiplexer_profile_has_its_own_positive_epoch(self):
        self.remote.rpc.return_value['authority_epoch'] = 17
        with patch.object(gateway, 'discover', return_value=self.endpoint), \
             patch.object(gateway, 'GatewayClient', return_value=self.remote):
            self.assertIs(self.call(), self.remote)
            for invalid in (None, True, 0, -1):
                self.remote.rpc.return_value['authority_epoch'] = invalid
                with self.assertRaises(transport.GatewayRefusal): self.call()


class WireTests(unittest.TestCase):
    def test_concurrent_rpc_responses_and_events_use_one_ws(self):
        from websockets.sync.server import serve
        events = []
        def handler(socket):
            calls = [json.loads(socket.recv()), json.loads(socket.recv())]
            socket.send(json.dumps({'method': 'event', 'params': {'session_id': 'event-session'}}))
            for call in reversed(calls):
                socket.send(json.dumps({'jsonrpc': '2.0', 'id': call['id'], 'result': call['params']['value']}))
            try: socket.recv()
            except Exception: pass
        transport.resume_connections()
        with serve(handler, '127.0.0.1', 0, subprotocols=['hermes-gateway-v1']) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            endpoint = {'api_origin': 'http://127.0.0.1:' + str(server.socket.getsockname()[1])}
            try:
                with patch.object(transport, 'ticket', return_value='synthetic'):
                    client = transport.GatewayClient(endpoint, on_event=events.append)
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(client.rpc, 'echo', value=value, timeout=2) for value in (11, 22)]
                    self.assertEqual([f.result(3) for f in futures], [11, 22])
                self.assertEqual(events[0]['params']['session_id'], 'event-session')
                client.close()
            finally:
                server.shutdown(); thread.join(2)


class StatusApiTests(unittest.TestCase):
    from scripts.test_project_roles import ProjectRoleTests
    setUp = ProjectRoleTests.setUp

    def test_status_endpoint_is_passive_and_mode_is_global(self):
        with patch.object(gateway, 'enabled', return_value=False), \
             patch.object(gateway, 'connection', side_effect=AssertionError('status opened WS')):
            response = self.client.get('/system/hermes-gateway')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['mode'], 'subprocess')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        with patch.object(gateway, 'enabled', return_value=True):
            self.assertEqual(self.client.get('/system/hermes-gateway').json()['mode'], 'gateway')

    def test_durable_and_hud_admission_protect_idle_connection(self):
        from engine import project_hud, hermes_gateway_ledger as ledger
        home = self.root/'hermes'
        self.assertFalse(connections.unresolved(home))
        row = ledger.prepare(self.conn, session='fixture', project='p', profile='default', kind='work',
            thread_id=None, task_id=None, job_id=None, event_floor=0, source='', python='[]', home=home,
            workdir=self.root, session_id='fixture', profile_id=str(home))
        for state, pending, expected in (('running', 0, True), ('manual', 0, True),
                                         ('terminal', 1, True), ('terminal', 0, False)):
            self.conn.execute('UPDATE hermes_executions SET state=?,cleanup_pending=? WHERE execution_id=?',
                              (state, pending, row['execution_id']))
            self.conn.commit()
            self.assertEqual(connections.unresolved(home), expected)
        project_hud.schema(self.conn)
        for terminal in (False, True):
            self.conn.execute('INSERT OR REPLACE INTO hud_sessions(id,activity,updated,state) VALUES(?,?,?,?)',
                ('fixture', 0, 0, json.dumps({'gateway': {'home': str(home), 'attempted': True, 'terminal': terminal}})))
            self.conn.commit()
            self.assertEqual(connections.unresolved(home), not terminal)
        with patch('engine.db.get_connection', side_effect=OSError('database unavailable')):
            self.assertTrue(connections.unresolved(home))

    def test_status_endpoint_rejects_bot_identity(self):
        from engine import api
        self.client.app.dependency_overrides[api._resolve_identity] = lambda: ('default', 'bot')
        self.assertEqual(self.client.get('/system/hermes-gateway').status_code, 403)


if __name__ == '__main__':
    unittest.main()
