"""Isolated approval identity, authority, freshness and transport regressions."""
import copy
import time
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine import api, db, hermes_approvals as approvals, hermes_gateway_ledger as ledger
from engine.hermes_gateway_client import GatewayTransportError
from scripts.unified_test_support import isolated_database


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        fixture = isolated_database()
        fixture.__enter__()
        self.addCleanup(fixture.__exit__, None, None, None)
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        self.row = ledger.prepare(self.conn, session='approval-fixture', project='alpha', profile='default',
            kind='chat', thread_id=7, task_id=None, job_id=None, event_floor=0, source='', python='["fixture"]',
            home='/isolated', workdir='/isolated/work', session_id='sid', profile_id='home', started_at=time.time())
        self.eid = self.row['execution_id']
        self.conn.execute("UPDATE hermes_executions SET state='running', generation=3 WHERE execution_id=?", (self.eid,))
        self.conn.commit()
        self.row = ledger.read(self.conn, self.eid)
        self.snapshot = {'session_id': 'sid', 'execution_generation': 3, 'prompts': [
            {'kind': 'approval', 'prompt_id': 'p1', 'execution_generation': 3,
             'command': 'echo fixture', 'description': '合成授权请求', 'choices': ['once', 'deny']},
            {'kind': 'sudo', 'prompt_id': 'secret', 'execution_generation': 3}]}
        self.gateway = Mock()
        self.gateway.rpc.side_effect = self.rpc
        approvals.sync(self.conn, self.row, self.gateway)
        self.gateway.reset_mock()
        self.connection = patch('engine.hermes_gateway.connection', return_value=(self.gateway, None, None)).start()
        self.addCleanup(patch.stopall)
        app = FastAPI(); app.include_router(api.router_api)
        app.dependency_overrides[api._resolve_identity] = lambda: ('user', 'user')
        self.app = app
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def rpc(self, method, **params):
        if method == 'session.resume':
            return copy.deepcopy(self.snapshot)
        if method == 'approval.respond':
            self.snapshot['prompts'] = []
            return {'status': 'resolved', 'prompt_id': params['prompt_id']}
        raise AssertionError(method)

    def post(self, **changes):
        body = dict(prompt_id='p1', generation=3, choice='once')
        body.update(changes)
        return self.client.post('/hermes-approvals/' + self.eid, json=body)

    def test_lists_only_owned_scope_and_regular_approvals(self):
        result = self.client.get('/hermes-approvals?project=alpha&thread_id=7').json()['approvals']
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['prompt_id'], 'p1')
        self.assertNotIn('home', result[0])
        self.assertEqual(self.client.get('/hermes-approvals?project=other').json()['approvals'], [])
        self.assertEqual(self.client.get('/hermes-approvals?thread_id=8').json()['approvals'], [])

    def test_bot_cannot_read_or_approve(self):
        self.app.dependency_overrides[api._resolve_identity] = lambda: ('default', 'bot')
        self.assertEqual(self.client.get('/hermes-approvals').status_code, 403)
        self.assertEqual(self.post().status_code, 403)
        self.gateway.rpc.assert_not_called()

    def test_response_uses_ledger_and_duplicate_is_not_replayed(self):
        self.assertEqual(self.post().json(), {'status': 'resolved'})
        self.gateway.rpc.assert_any_call('approval.respond', session_id='sid', execution_generation=3,
                                         prompt_id='p1', choice='once', timeout=10)
        self.connection.assert_called_with('default', home='/isolated', read_only=True)
        self.assertEqual(self.post().json(), {'status': 'already_resolved'})
        self.assertEqual(sum(c.args[0] == 'approval.respond' for c in self.gateway.rpc.call_args_list), 1)
        self.assertEqual(approvals.listing()['approvals'], [])

    def test_native_resolution_is_observed_without_responding(self):
        self.snapshot['prompts'] = []
        self.assertEqual(self.post().json()['status'], 'already_resolved')
        self.assertEqual(len(self.gateway.rpc.call_args_list), 1)

    def test_generation_and_permissions_are_rechecked(self):
        self.assertEqual(self.post(generation=4).status_code, 409)
        self.assertEqual(self.post(choice='always').status_code, 400)
        self.assertEqual(self.post(home='/other').status_code, 422)
        self.snapshot['execution_generation'] = 4
        self.assertEqual(self.post().status_code, 409)
        self.assertFalse(any(c.args[0] == 'approval.respond' for c in self.gateway.rpc.call_args_list))

    def test_stale_snapshot_disables_actions_and_terminal_is_removed(self):
        self.conn.execute('UPDATE hermes_approval_snapshots SET checked_at=0'); self.conn.commit()
        self.assertTrue(approvals.listing()['approvals'][0]['error'])
        self.conn.execute("UPDATE hermes_executions SET state='terminal'"); self.conn.commit()
        self.assertEqual(approvals.listing()['approvals'], [])
        self.assertEqual(self.post().status_code, 409)

    def test_lost_result_is_not_automatically_retried(self):
        def lost(method, **params):
            if method == 'approval.respond':
                raise GatewayTransportError('lost')
            return copy.deepcopy(self.snapshot)
        self.gateway.rpc.side_effect = lost
        self.assertEqual(self.post().status_code, 503)
        self.assertTrue(approvals.listing()['approvals'][0]['error'])
        self.assertEqual(sum(c.args[0] == 'approval.respond' for c in self.gateway.rpc.call_args_list), 1)
        self.gateway.rpc.side_effect = self.rpc
        approvals.sync(self.conn, self.row, self.gateway)
        self.assertFalse(approvals.listing()['approvals'][0]['error'])

    def test_sync_failure_preserves_pending_and_execution(self):
        self.gateway.rpc.side_effect = GatewayTransportError('lost')
        approvals.sync(self.conn, self.row, self.gateway)
        self.assertTrue(approvals.listing()['approvals'][0]['error'])
        self.assertEqual(ledger.read(self.conn, self.eid)['state'], 'running')


if __name__ == '__main__':
    unittest.main()
