"""Session policy refresh, persisted override ownership and native reset boundaries."""
from __future__ import annotations

import unittest
from unittest.mock import Mock

from engine import hermes_gateway_sessions as sessions
from engine.hermes_gateway_client import GatewayRefusal
from engine import db
from scripts.unified_test_support import isolated_database


def _snapshot(*, source='acp', cwd='/w', running=False, pending=False,
              edit_approval_policy='workspace_session'):
    return {
        'session_id': 'sid', 'revision': 1, 'execution_generation': 1,
        'running': running, 'pending': pending,
        'info': {'source': source, 'cwd': cwd, 'edit_approval_policy': edit_approval_policy},
    }


def _mutations(client):
    return [c for c in client.rpc.call_args_list if c.args[0] == 'session.mutate']


def _mutation_ops(client):
    return [c.kwargs['operation'] for c in _mutations(client)]


class ConfigureRefreshTests(unittest.TestCase):
    def _configure(self, *, snapshot=None, workdir='/w', model_args=(), server=None):
        snapshot = _snapshot() if snapshot is None else snapshot
        server = server or {'command': 'python', 'args': ['-m', 'mcp'], 'env': {}}
        client = Mock()
        # attach 返回同一 snapshot（真实 resume 会重读 config 派生字段）。
        client.rpc.side_effect = lambda method, **kw: snapshot if method == 'session.resume' else snapshot
        return sessions.configure(client, snapshot, workdir=workdir, model_args=list(model_args), server=server), client

    def test_explicit_model_and_reasoning_are_mutated(self):
        snapshot, client = self._configure(model_args=['--model', 'gpt-x', '--provider', 'p', '--reasoning', 'high'])
        ops = _mutation_ops(client)
        self.assertIn('model', ops)
        self.assertIn('reasoning', ops)
        model = next(c for c in _mutations(client) if c.kwargs['operation'] == 'model')
        self.assertEqual(model.kwargs['payload'], {'model': 'gpt-x', 'provider': 'p'})
        reasoning = next(c for c in _mutations(client) if c.kwargs['operation'] == 'reasoning')
        self.assertEqual(reasoning.kwargs['payload'], {'effort': 'high'})

    def test_deleted_model_and_reasoning_are_not_overridden(self):
        snapshot, client = self._configure(model_args=[])
        self.assertNotIn('model', _mutation_ops(client))
        self.assertNotIn('reasoning', _mutation_ops(client))
        self.assertEqual(snapshot['session_id'], 'sid')

    def test_workspace_change_is_migrated(self):
        snapshot, client = self._configure(snapshot=_snapshot(cwd='/old'), workdir='/new')
        self.assertIn('workspace', _mutation_ops(client))

    def test_workspace_unchanged_is_not_mutated(self):
        _, client = self._configure(snapshot=_snapshot(cwd='/w'), workdir='/w')
        self.assertNotIn('workspace', _mutation_ops(client))

    def test_editor_mcp_refreshed_every_admission(self):
        _, client = self._configure()
        mcp = [c for c in _mutations(client) if c.kwargs['operation'] == 'editor_mcp']
        # 空 manifest 退役旧连接，再注入新 spec 重新发现工具描述。
        self.assertEqual(mcp[0].kwargs['payload'], {'mcp_servers': []})
        self.assertEqual(mcp[1].kwargs['payload']['mcp_servers'][0]['name'], 'task_queue')

    def test_non_acp_session_refused(self):
        with self.assertRaises(GatewayRefusal):
            self._configure(snapshot=_snapshot(source='other'))

    def test_busy_session_refused(self):
        with self.assertRaises(GatewayRefusal):
            self._configure(snapshot=_snapshot(running=True))

    def test_edit_policy_enforced_when_missing(self):
        _, client = self._configure(snapshot=_snapshot(edit_approval_policy=None))
        self.assertIn('edit_policy', _mutation_ops(client))


class TrackedReasoningTests(unittest.TestCase):
    def setUp(self):
        fixture = isolated_database()
        fixture.__enter__()
        self.addCleanup(fixture.__exit__, None, None, None)
        self.snapshot = _snapshot()
        self.client = Mock(endpoint={'profile_id': 'isolated-profile'})
        self.client.rpc.return_value = self.snapshot
        self.server = {'command': 'python', 'args': [], 'env': {}}

    def configure(self, args=(), project='alpha', snapshot=None):
        conn = db.get_connection()
        try:
            return sessions.configure_tracked(conn, project, self.client, snapshot or self.snapshot,
                workdir='/w', model_args=list(args), server=self.server)
        finally:
            conn.close()

    def test_clear_after_reopen_resets_once(self):
        self.configure(['--reasoning', 'high'])
        self.client.reset_mock()
        self.configure()
        calls = [c for c in _mutations(self.client) if c.kwargs['operation'] == 'reasoning']
        self.assertEqual([c.kwargs['payload'] for c in calls], [{'effort': None}])
        self.client.reset_mock()
        self.configure()
        self.assertNotIn('reasoning', _mutation_ops(self.client))

    def test_native_choice_and_other_session_are_preserved(self):
        self.configure()
        self.assertNotIn('reasoning', _mutation_ops(self.client))
        self.configure(['--reasoning', 'high'])
        self.client.reset_mock()
        self.configure(snapshot={**self.snapshot, 'session_id': 'other'})
        self.configure(project='beta')
        self.assertNotIn('reasoning', _mutation_ops(self.client))

    def test_failed_reset_keeps_record_for_retry(self):
        self.configure(['--reasoning', 'high'])
        self.client.rpc.side_effect = GatewayRefusal('session_busy')
        with self.assertRaises(GatewayRefusal):
            self.configure()
        self.client.rpc.side_effect = None
        self.client.reset_mock()
        self.configure()
        self.assertIn('reasoning', _mutation_ops(self.client))

    def test_failed_pin_does_not_mark_native_session_as_owned(self):
        self.client.rpc.side_effect = GatewayRefusal('session_busy')
        with self.assertRaises(GatewayRefusal):
            self.configure(['--reasoning', 'high'])
        self.client.rpc.side_effect = None
        self.client.reset_mock()
        self.configure()
        self.assertNotIn('reasoning', _mutation_ops(self.client))


if __name__ == '__main__':
    unittest.main()
