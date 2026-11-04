"""Real pooled WebSockets with serial dispatch, lost replies and bounded recovery."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from engine import hermes_connections as connections
from engine import hermes_gateway_client as transport
from engine import hermes_gateway_sessions as sessions


def snapshot(revision=0):
    return dict(session_id='synthetic-session', revision=revision, execution_generation=0,
                running=False, pending=False, subscription_id='synthetic-subscription',
                info=dict(source='acp', cwd='/synthetic', edit_approval_policy='workspace_session'))


@contextmanager
def wire(handler):
    from websockets.sync.server import serve
    from websockets.exceptions import ConnectionClosed

    def dispatch(socket):
        try:
            for raw in socket:
                call = json.loads(raw)
                result = handler(socket, call)
                if result is not None:
                    socket.send(json.dumps(dict(jsonrpc='2.0', id=call['id'], result=result)))
        except ConnectionClosed:
            pass

    transport.resume_connections()
    with tempfile.TemporaryDirectory() as directory, \
         serve(dispatch, '127.0.0.1', 0, subprotocols=['hermes-gateway-v1']) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        home = Path(directory)
        endpoint = dict(api_origin='http://127.0.0.1:' + str(server.socket.getsockname()[1]),
                        profile_id=str(home), instance_id='synthetic-instance', authority_epoch=1)
        clients = []
        options = []
        now = [0]

        def open_client(home, command, callback, **kwargs):
            options.append(kwargs)
            client = transport.GatewayClient(endpoint, on_event=callback)
            clients.append(client)
            return client

        pool = connections.Pool(open_client, lambda: ('synthetic-source', 'sha'),
                                lambda home: False, lambda: now[0])
        with patch.object(transport, 'ticket', return_value='synthetic-ticket'):
            try:
                with pool.acquire(home, ['synthetic-command']) as lease:
                    yield lease, pool, now, clients, options
            finally:
                pool.stop()
                for client in clients:
                    client.close()
                pool.join()
                server.shutdown()
                thread.join(3)


class HeartbeatRecoveryTests(unittest.TestCase):
    def test_sweep_skips_serial_rpc_then_heartbeats_same_connection(self):
        entered, release = threading.Event(), threading.Event()
        methods = []

        def handler(socket, call):
            methods.append(call['method'])
            if call['method'] == 'session.mutate':
                entered.set()
                if not release.wait(6):
                    raise TimeoutError('synthetic mutation release missing')
            return snapshot(1)

        with wire(handler) as (lease, pool, now, clients, _), ThreadPoolExecutor(2) as executor:
            mutation = executor.submit(sessions.mutate, lease, snapshot(), 'model', {'model': 'synthetic'})
            try:
                self.assertTrue(entered.wait(2))
                now[0] = 21
                sweep = executor.submit(pool.sweep)
                sweep.result(timeout=1)
                self.assertEqual(methods, ['session.mutate'])
                self.assertFalse(clients[0].closed)
            finally:
                release.set()
                mutation.result(4)
            pool.sweep()
            self.assertEqual(methods, ['session.mutate', 'ping'])
            self.assertEqual(len(clients), 1)

    def test_lost_mutation_response_reuses_receipt_and_refreshes_snapshot(self):
        mutations = []
        revision = [0]

        def handler(socket, call):
            if call['method'] == 'session.mutate':
                mutations.append(call['params'])
                if len(mutations) == 1:
                    revision[0] += 1
                    socket.close(code=1011, reason='synthetic private detail')
                    return None
                self.assertEqual(mutations[0], call['params'])
            return snapshot(revision[0])

        with wire(handler) as (lease, _, _, clients, options):
            result = sessions.mutate(lease, snapshot(), 'model', {'model': 'synthetic'})
            refreshed = sessions.attach(lease, result['session_id'])
            self.assertEqual(refreshed['revision'], 1)
            self.assertEqual(revision[0], 1)
            self.assertEqual(len(mutations), 2)
            self.assertEqual(len(clients), 2)
            self.assertFalse(options[1]['allow_start'])
            self.assertIs(lease.client, clients[1])
            self.assertEqual(lease.entry.subscriptions[(clients[1], result['session_id'])],
                             'synthetic-subscription')

    def test_uncommitted_mutation_retries_once_with_original_fences(self):
        calls, committed = [], []

        def handler(socket, call):
            calls.append(call)
            if len(calls) == 1:
                socket.close(code=1011)
                return None
            self.assertEqual(call['params'], calls[0]['params'])
            committed.append(call['params'])
            return snapshot(1)

        with wire(handler) as (lease, _, _, clients, _):
            self.assertEqual(sessions.mutate(lease, snapshot(), 'reasoning', {'effort': 'high'})['revision'], 1)
            self.assertEqual(len(committed), 1)
            self.assertEqual(len(clients), 2)
            self.assertEqual(committed[0]['expected_revision'], 0)
            self.assertEqual(committed[0]['expected_generation'], 0)

    def test_create_response_loss_preserves_creation_request(self):
        requests = []

        def handler(socket, call):
            self.assertEqual(call['method'], 'session.create')
            requests.append(call['params'])
            if len(requests) == 1:
                socket.close(code=1011)
                return None
            return snapshot()

        with wire(handler) as (lease, _, _, clients, _):
            self.assertEqual(sessions.new(lease, title='synthetic', workdir='/synthetic',
                             system_prompt='synthetic private prompt', model_args=[])['session_id'],
                             'synthetic-session')
            self.assertEqual(requests[0], requests[1])
            self.assertTrue(requests[0]['request_id'])
            self.assertEqual(len(clients), 2)

    def test_second_disconnect_stops_and_diagnostics_exclude_private_data(self):
        calls = []

        def handler(socket, call):
            calls.append(call['params'])
            socket.close(code=1011, reason='synthetic-private-close-secret')

        with self.assertLogs('engine', level='INFO') as captured:
            with wire(handler) as (lease, _, _, clients, _):
                with self.assertRaises(transport.GatewayTransportError):
                    sessions.mutate(lease, snapshot(), 'editor_mcp',
                                    {'env': {'TOKEN': 'synthetic-private-token'}})
                self.assertEqual(len(clients), 2)
                self.assertEqual(calls[0], calls[1])
        logs = '\n'.join(captured.output)
        self.assertIn('received_code=1011', logs)
        self.assertIn('method=session.mutate', logs)
        self.assertIn('retry=1', logs)
        self.assertNotIn('synthetic-private', logs)
        self.assertNotIn(calls[0]['request_id'], logs)

    def test_revision_refusal_is_not_retried_and_other_lease_remains_usable(self):
        methods = []

        def handler(socket, call):
            methods.append(call['method'])
            if call['method'] == 'session.mutate':
                socket.send(json.dumps(dict(jsonrpc='2.0', id=call['id'],
                            error=dict(code=-32000, data=dict(reason='revision_conflict')))))
                return None
            return snapshot()

        with wire(handler) as (lease, pool, _, clients, _):
            with pool.acquire(lease.entry.home, ['synthetic-command']) as other:
                with self.assertRaisesRegex(transport.GatewayRefusal, 'revision_conflict'):
                    sessions.mutate(lease, snapshot(), 'model', {'model': 'synthetic'})
                self.assertEqual(other.rpc('prompt.receipt')['session_id'], 'synthetic-session')
            self.assertEqual(methods, ['session.mutate', 'prompt.receipt'])
            self.assertEqual(len(clients), 1)

    def test_prompt_response_loss_never_replays_prompt(self):
        methods = []

        def handler(socket, call):
            methods.append(call['method'])
            socket.close(code=1011)

        with wire(handler) as (lease, _, _, clients, _):
            with self.assertRaises(transport.GatewayTransportError):
                lease.rpc('prompt.submit', session_id='synthetic-session', submission_id='original', text='synthetic')
            with self.assertRaises(ValueError):
                sessions.initialization_rpc(lease, 'prompt.submit', submission_id='original')
            self.assertEqual(methods, ['prompt.submit'])
            self.assertEqual(len(clients), 1)

    def test_idle_unresponsive_heartbeat_still_closes_and_reports_cause(self):
        methods = []

        def handler(socket, call):
            methods.append(call['method'])

        with self.assertLogs('engine', level='WARNING') as captured:
            with wire(handler) as (_, pool, now, clients, _):
                now[0] = 21
                pool.sweep()
                self.assertTrue(clients[0].closed)
                self.assertEqual(pool.status()['state'], 'error')
        self.assertEqual(methods, ['ping'])
        logs = '\n'.join(captured.output)
        self.assertIn('phase=heartbeat', logs)
        self.assertIn('cause=heartbeat_failed', logs)
        self.assertIn('method=ping', logs)
        self.assertIn('cause=timeout', logs)

    def test_reconnect_reuses_successor_without_rebinding_other_lease(self):
        def handler(socket, call):
            return snapshot()

        with wire(handler) as (lease, pool, now, clients, _):
            with pool.acquire(lease.entry.home, ['synthetic-command']) as other:
                pool.failed(lease.entry, lease.client)
                now[0] = 2
                pool.sweep()
                lease.reconnect()
                self.assertIs(lease.client, clients[1])
                self.assertIs(other.client, clients[0])
                self.assertEqual(len(clients), 2)
            pool.revision = lambda: ('synthetic-source', 'new-sha')
            with self.assertRaisesRegex(transport.GatewayRefusal, 'gateway_restart_required'):
                lease.reconnect()
            self.assertEqual(len(clients), 2)


class InitializationStateTests(unittest.TestCase):
    def test_session_becoming_busy_during_configuration_defers_before_submission(self):
        from engine import db, hermes_gateway as gateway, hermes_session_auth as auth
        from scripts.unified_test_support import isolated_database

        remote = Mock()
        remote.rpc.return_value = dict(capabilities=['durable-admission-v1', 'acp-session-mcp-v1'])
        with isolated_database(), \
             patch.object(gateway, 'connection', return_value=(remote, '/synthetic', [])), \
             patch.object(gateway, '_binding', return_value=snapshot()), \
             patch.object(auth, 'grant', return_value={}), \
             patch.object(auth, 'configure', return_value={**snapshot(), 'pending': True}):
            result = gateway.spawn('synthetic', 'work', 'synthetic', None, 'synthetic-project',
                                   bot={'id': 1}, workdir='/synthetic', model_args=[])
            self.assertTrue(result['busy'])
            self.assertEqual([c.args[0] for c in remote.rpc.call_args_list], ['runtime.describe'])
            conn = db.get_connection()
            try:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM hermes_executions').fetchone()[0], 0)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM wake_runs').fetchone()[0], 0)
            finally:
                conn.close()


if __name__ == '__main__':
    unittest.main()
