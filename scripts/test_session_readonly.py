"""Read-only session lookup and workspace GETs use isolated homes and control peers."""
import json
import os
import sqlite3
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from engine import hermes_gateway as gateway, hermes_gateway_client as transport
from engine import hermes_runtime, session_adapter as adapter
from scripts import test_project_roles


class ReadOnlySessionTests(unittest.TestCase):
    setUp = test_project_roles.ProjectRoleTests.setUp
    join = test_project_roles.ProjectRoleTests.join

    def lookup(self, profile='unregistered'):
        return adapter.existing_session_id(self.conn, 'p', profile, 'work', None, 'old-title')

    def control_reply(self, home):
        return {'protocol': 1, 'id': 1, 'ok': True, 'result': {
            'state': 'ready', 'served_profiles': [{'home': str(home), 'profile_id': str(home)}],
            'instance_id': 'fixture-instance', 'authority_epoch': 1, 'runtime_protocol': 1,
            'api_origin': 'http://127.0.0.1:12345', 'capabilities': ['session-authority-v1']}}

    def exchange_patch(self, **kwargs):
        name = '_pipe_exchange' if os.name == 'nt' else '_socket_exchange'
        return patch.object(transport, name, **kwargs)

    def test_absent_gateway_get_preserves_rows_and_profile_directory(self):
        member = self.join(host='hermes')
        before = self.conn.total_changes
        bots_before = self.conn.execute('SELECT COUNT(*) FROM bots').fetchone()[0]
        with patch.object(gateway, 'enabled', return_value=True), \
             patch.object(gateway, 'ensure', side_effect=AssertionError('read started gateway')) as ensure, \
             patch.object(transport.subprocess, 'run', side_effect=AssertionError('read spawned child')), \
             self.exchange_patch(side_effect=FileNotFoundError):
            self.assertIsNone(self.lookup())
            response = self.client.get('/projects/p/workspaces')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['members'][0]['profile'], member)
        self.assertIs(response.json()['members'][0]['work_session_exists'], False)
        ensure.assert_not_called()
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM session_bindings').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bots').fetchone()[0], bots_before)
        self.assertFalse((self.root / 'hermes').exists())

    def test_live_gateway_title_lookup_is_read_only_and_closes_client(self):
        home = self.root / 'hermes'
        member = self.join(host='hermes')
        remote = MagicMock()
        remote.__enter__.return_value = remote
        remote.rpc.return_value = {'sessions': [{'session_id': 'existing'}]}
        before = self.conn.total_changes
        with patch.object(gateway, 'enabled', return_value=True), \
             patch.object(gateway, 'ensure', side_effect=AssertionError('read started gateway')), \
             self.exchange_patch(return_value=json.dumps(self.control_reply(home)).encode()) as exchange, \
             patch.object(gateway, 'GatewayClient', return_value=remote):
            self.assertEqual(self.lookup(member), 'existing')
            response = self.client.get('/projects/p/workspaces')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIs(response.json()['members'][0]['work_session_exists'], True)
        self.assertEqual(exchange.call_count, 2)
        self.assertEqual(remote.__exit__.call_count, 2)
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM session_bindings').fetchone()[0], 0)
        self.assertEqual(remote.rpc.call_args.args, ('session.list',))

    def test_binding_is_returned_without_gateway_discovery(self):
        member = self.join(host='hermes')
        bot_id = self.conn.execute('SELECT bot_id FROM members WHERE profile=?', (member,)).fetchone()[0]
        self.conn.execute("INSERT INTO session_bindings(project,bot_id,profile,kind,thread_id,session_id,updated_at) "
                          "VALUES('p',?,?,'work',0,'bound','now')", (bot_id, member))
        self.conn.commit()
        with patch.object(gateway, 'enabled', side_effect=AssertionError('bound lookup probed gateway')):
            self.assertEqual(self.lookup(member), 'bound')

    def test_gateway_failures_degrade_only_for_read_only_queries(self):
        from websockets.exceptions import WebSocketException
        failures = [transport.GatewayTransportError('offline'), transport.GatewayRefusal('unavailable'),
                    hermes_runtime.HermesConfigurationError('invalid installation'),
                    subprocess.TimeoutExpired('fixture', 2), OSError('offline'), WebSocketException('closed')]
        for error in failures:
            for stage in ('enabled', 'connection', 'rpc'):
                with self.subTest(error=type(error).__name__, stage=stage):
                    remote = MagicMock()
                    remote.__enter__.return_value = remote
                    with patch.object(gateway, 'enabled', return_value=True) as enabled, \
                         patch.object(gateway, 'connection', return_value=(remote, None, None)) as connect:
                        {'enabled': enabled, 'connection': connect, 'rpc': remote.rpc}[stage].side_effect = error
                        self.assertIsNone(self.lookup())
                        with self.assertRaises(type(error)):
                            adapter.resolve_session_by_title('default', 'old-title')

    def test_workspace_get_handles_lookup_failure_as_unknown(self):
        self.join(host='hermes')
        failures = [transport.GatewayTransportError('offline'), transport.GatewayRefusal('unavailable'),
                    hermes_runtime.HermesConfigurationError('invalid installation'),
                    subprocess.TimeoutExpired('fixture', 2), sqlite3.OperationalError('locked'), OSError('offline')]
        for error in failures:
            with self.subTest(error=type(error).__name__), \
                 patch.object(adapter, 'existing_session_id', side_effect=error):
                response = self.client.get('/projects/p/workspaces')
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIsNone(response.json()['members'][0]['work_session_exists'])

    def test_legacy_lookup_preserves_database_and_missing_profile(self):
        path = self.root / 'legacy.db'
        with sqlite3.connect(path) as legacy:
            legacy.execute('CREATE TABLE sessions(id TEXT,title TEXT,started_at REAL)')
            legacy.executemany('INSERT INTO sessions VALUES(?,?,?)',
                               [('old', 'old-title', 1), ('latest', 'old-title', 2)])
        legacy.close()
        before = path.read_bytes()
        with patch.object(gateway, 'enabled', return_value=False), \
             patch.object(adapter, '_ensure_src_on_path', side_effect=AssertionError('imported Hermes')), \
             patch.object(adapter, 'profile_state_db', return_value=path):
            self.assertEqual(self.lookup(), 'latest')
        self.assertEqual(path.read_bytes(), before)
        missing = self.root / 'missing' / 'state.db'
        with patch.object(gateway, 'enabled', return_value=False), \
             patch.object(adapter, 'profile_state_db', return_value=missing):
            self.assertIsNone(self.lookup())
        self.assertFalse(missing.parent.exists())

    def test_create_session_keeps_gateway_startup(self):
        remote = MagicMock()
        remote.__enter__.return_value = remote
        with patch.object(gateway, 'enabled', return_value=True), \
             patch.object(hermes_runtime, 'executable', return_value='fixture-hermes'), \
             patch.object(gateway, 'ensure', return_value={'fixture': True}) as ensure, \
             patch.object(gateway, 'discover', side_effect=AssertionError('creation used discovery')), \
             patch.object(gateway, 'GatewayClient', return_value=remote), \
             patch.object(gateway.sessions, 'new', return_value={'session_id': 'new'}):
            self.assertEqual(adapter.create_session('default', 'title'), 'new')
        ensure.assert_called_once_with(['fixture-hermes'], self.root / 'hermes')

    def test_discovery_resolves_multiplexer_without_starting_profile(self):
        home = self.root / 'profiles' / 'worker'
        with self.exchange_patch(side_effect=[FileNotFoundError(),
                                 json.dumps(self.control_reply(home)).encode()]) as exchange:
            endpoint = transport.discover(home)
        self.assertEqual(endpoint['profile_id'], str(home))
        self.assertEqual(endpoint['control_home'], str(self.root))
        self.assertEqual([c.args[0] for c in exchange.call_args_list], [home, self.root])
        self.assertFalse(home.exists())

    def test_discovery_rejects_invalid_identity_and_origin(self):
        home = self.root / 'hermes'
        invalid = [('state', 'starting'), ('runtime_protocol', 2), ('instance_id', ''),
                   ('authority_epoch', 0), ('capabilities', []), ('served_profiles', []),
                   ('api_origin', 'https://example.test'), ('api_origin', 'http://127.0.0.1/path')]
        for field, value in invalid:
            reply = self.control_reply(home)
            reply['result'][field] = value
            with self.subTest(field=field, value=value), \
                 self.exchange_patch(return_value=json.dumps(reply).encode()):
                with self.assertRaises(transport.GatewayTransportError):
                    transport.discover(home)
        for raw in (b'invalid', b'null', b'{}', b'{"result":[]}'):
            with self.subTest(raw=raw), self.exchange_patch(return_value=raw):
                with self.assertRaises(transport.GatewayTransportError):
                    transport.discover(home)


if __name__ == '__main__':
    unittest.main()
