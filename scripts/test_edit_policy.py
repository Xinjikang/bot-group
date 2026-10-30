"""Global edit-policy persistence, administrator boundaries and per-turn application."""
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from engine import api, auth, db, edit_policy, hermes_gateway_sessions as sessions
from engine.hermes_gateway_client import GatewayRefusal
from scripts.unified_test_support import isolated_database
from scripts.test_hermes_gateway_sessions import _snapshot, _mutations


class EditPolicyTests(unittest.TestCase):
    def setUp(self):
        fixture = isolated_database()
        fixture.__enter__()
        self.addCleanup(fixture.__exit__, None, None, None)
        password = patch.object(api.auth, '_password', return_value='')
        password.start()
        self.addCleanup(password.stop)
        app = FastAPI()
        app.include_router(api.router_api)
        self.app = app
        self.client = TestClient(app, client=('127.0.0.1', 4321))
        self.addCleanup(self.client.close)
        self.url = '/system/edit-approval-policy'

    def test_default_persistence_and_global_scope(self):
        response = self.client.get(self.url)
        self.assertEqual(response.json(), {'policy': 'workspace_session'})
        self.assertIn('no-store', response.headers['cache-control'])
        for policy in edit_policy.POLICIES:
            self.assertEqual(self.client.patch(self.url, json={'policy': policy}).json(), {'policy': policy})
            with TestClient(self.app, client=('127.0.0.1', 4321)) as fresh:
                for project in ('alpha', 'beta'):
                    self.assertEqual(fresh.get(self.url, params={'project': project}).json(), {'policy': policy})
        conn = db.get_connection()
        try:
            rows = conn.execute('SELECT project,value FROM settings WHERE key=?', (edit_policy.KEY,)).fetchall()
            self.assertEqual([tuple(row) for row in rows], [('', 'session')])
            db.init_schema(conn)
            self.assertEqual(edit_policy.read(conn), 'session')
        finally:
            conn.close()

    def test_invalid_values_and_scope_rejected(self):
        self.client.patch(self.url, json={'policy': 'ask'})
        for body in ({}, {'policy': None}, {'policy': []}, {'policy': {}}, {'policy': True},
                     {'policy': 'workspace'}, {'policy': 'smart'}, {'policy': 'session', 'project': 'alpha'},
                     {'policy': 'session', 'bot_id': 1}):
            with self.subTest(body=body):
                self.assertEqual(self.client.patch(self.url, json=body).status_code, 400)
                self.assertEqual(self.client.get(self.url).json(), {'policy': 'ask'})

    def test_admin_authentication_and_bot_denial(self):
        headers = {'X-Group-Profile': 'dev', 'X-Group-Key': auth.bot_token('dev')}
        for method in ('get', 'patch'):
            kwargs = {'json': {'policy': 'session'}} if method == 'patch' else {}
            self.assertEqual(getattr(self.client, method)(self.url, headers=headers, **kwargs).status_code, 403)
            with patch.object(api.auth, '_password', return_value='configured'), \
                    patch.object(api.auth, 'verify_token', side_effect=lambda token: token == 'valid'):
                self.assertEqual(getattr(self.client, method)(self.url, **kwargs).status_code, 401)
                self.assertEqual(getattr(self.client, method)(self.url, headers={'Authorization': 'Bearer wrong'},
                                                              **kwargs).status_code, 401)
                self.assertEqual(getattr(self.client, method)(self.url, headers={'Authorization': 'Bearer valid'},
                                                              **kwargs).status_code, 200)

    def test_each_turn_reads_global_value_for_all_projects_and_profiles(self):
        conn = db.get_connection()
        self.addCleanup(conn.close)
        for policy in ('session', 'ask', 'workspace_session'):
            self.client.patch(self.url, json={'policy': policy})
            for project, profile in (('alpha', 'one'), ('beta', 'two')):
                snap = _snapshot(edit_approval_policy='ask' if policy != 'ask' else 'session')
                client = Mock(endpoint={'profile_id': profile})
                client.rpc.return_value = snap
                sessions.configure_tracked(conn, project, client, snap,
                    workdir='/w', model_args=[], server={'command': 'python', 'args': [], 'env': {}})
                calls = [call for call in _mutations(client) if call.kwargs['operation'] == 'edit_policy']
                self.assertEqual([call.kwargs['payload'] for call in calls], [{'policy': policy}])

    def test_corrupt_value_falls_back_and_project_override_is_ignored(self):
        conn = db.get_connection()
        try:
            conn.execute('INSERT INTO settings(project,key,value,updated_at) VALUES(?,?,?,?)',
                         ('alpha', edit_policy.KEY, 'session', 'now'))
            conn.execute('INSERT INTO settings(project,key,value,updated_at) VALUES(?,?,?,?)',
                         ('', edit_policy.KEY, 'invalid', 'now'))
            conn.commit()
            self.assertEqual(edit_policy.read(conn), 'workspace_session')
        finally:
            conn.close()

    def test_busy_round_is_unchanged_and_mutation_failure_propagates(self):
        conn = db.get_connection()
        self.addCleanup(conn.close)
        self.client.patch(self.url, json={'policy': 'session'})
        client = Mock(endpoint={'profile_id': 'one'})
        for snap in (_snapshot(running=True), _snapshot(pending=True)):
            with self.assertRaises(GatewayRefusal):
                sessions.configure_tracked(conn, 'alpha', client, snap,
                    workdir='/w', model_args=[], server={})
            client.rpc.assert_not_called()
        client.rpc.side_effect = GatewayRefusal('edit_policy_failed')
        with self.assertRaisesRegex(GatewayRefusal, 'edit_policy_failed'):
            sessions.configure_tracked(conn, 'alpha', client, _snapshot(),
                workdir='/w', model_args=[], server={})
        self.assertEqual(len(client.rpc.call_args_list), 1)


if __name__ == '__main__':
    unittest.main()
