"""Session policy refresh, persisted override ownership and native reset boundaries."""
from __future__ import annotations

import json
from pathlib import Path
import unittest
import uuid
from unittest.mock import Mock

from engine import hermes_gateway_sessions as sessions
from engine.hermes_connections import Pool
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


def _success(snapshot, method, **params):
    if method == 'session.mutate' and params['operation'] == 'model':
        return {'session_id': snapshot['session_id'], 'operation': 'model', 'revision': 2,
                'model': params['payload']['model']}
    return snapshot


class ConfigureRefreshTests(unittest.TestCase):
    def _configure(self, *, snapshot=None, workdir='/w', model_args=(), server=None):
        snapshot = _snapshot() if snapshot is None else snapshot
        server = server or {'command': 'python', 'args': ['-m', 'mcp'], 'env': {}}
        client = Mock()
        # attach 返回同一 snapshot（真实 resume 会重读 config 派生字段）。
        client.rpc.side_effect = lambda method, **kw: _success(snapshot, method, **kw)
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

    def test_editor_mcp_installed_without_empty_manifest(self):
        _, client = self._configure()
        mcp = [c for c in _mutations(client) if c.kwargs['operation'] == 'editor_mcp']
        self.assertEqual(len(mcp), 1)
        self.assertEqual(mcp[0].kwargs['payload']['mcp_servers'][0]['name'], 'task_queue')

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
        self.client.closed = False
        self.client.rpc.return_value = self.snapshot
        self.pool = Pool(opener=lambda *args, **kwargs: self.client,
                         revision=lambda: ('fixture-source', 'fixture-sha'), pinned=lambda _: False)
        self.addCleanup(self.pool.stop)
        self.lease = self.pool.acquire(Path(db.db_path()).parent, ['fixture'])
        self.addCleanup(self.lease.close)
        self.server = {'command': 'python', 'args': [], 'env': {}}

    def configure(self, args=(), project='alpha', snapshot=None):
        conn = db.get_connection()
        try:
            return sessions.configure_tracked(conn, project, self.lease, snapshot or self.snapshot,
                workdir='/w', model_args=list(args), server=self.server)
        finally:
            conn.close()

    def test_identical_turns_keep_policy_and_resume_private_transport(self):
        self.client.rpc.side_effect = lambda method, **kw: _success(self.snapshot, method, **kw)
        self.configure(['--model', 'fixture', '--provider', 'local', '--reasoning', 'high'])
        self.client.reset_mock()
        self.configure(['--model', 'fixture', '--provider', 'local', '--reasoning', 'high'])
        self.assertEqual(_mutation_ops(self.client), [])
        self.assertIn('editor', self.client.rpc.call_args.kwargs)

    def test_failed_switch_retries_and_notifies_user_once_per_target(self):
        conn = db.get_connection()
        self.addCleanup(conn.close)
        warning = 'context_cache'
        active = {'model': 'A', 'provider': 'original'}
        self.snapshot['info'].update(active)

        def rpc(method, **params):
            if method == 'session.mutate' and params['operation'] == 'model':
                if warning:
                    return {'status': 'confirmation_required', 'warnings': [{'kind': warning}]}
                self.snapshot['info'].update(params['payload'])
            return _success(self.snapshot, method, **params)

        self.client.rpc.side_effect = rpc
        def turn(provider, **kwargs):
            return sessions.configure_tracked(conn, 'alpha', self.lease, self.snapshot, workdir='/w',
                model_args=['--model', 'B', '--provider', provider], server=self.server, member='fixture', **kwargs)

        self.assertEqual(turn('one')['info']['model'], 'A')
        turn('one', refresh_model=False)
        turn('one')
        warning = 'data_policy'
        turn('two')
        notices = conn.execute("SELECT content,mentions,need_reply FROM messages WHERE sender='engine' ORDER BY id").fetchall()
        self.assertEqual(len(notices), 2)
        self.assertTrue(all(json.loads(row['mentions']) == ['user'] and not row['need_reply'] for row in notices))
        self.assertIn('model.switch_context_confirm_tokens=0', notices[0]['content'])
        self.assertIn('A（original）', notices[0]['content'])
        self.assertIn('/new @fixture', notices[0]['content'])
        self.assertIn('数据使用政策', notices[1]['content'])
        self.assertNotIn('switch_context', notices[1]['content'])
        self.assertEqual([r[0] for r in conn.execute('SELECT profile FROM inbox')], ['user', 'user'])
        warning = None
        self.assertEqual(turn('two')['info']['model'], 'B')
        turn('two')
        self.assertEqual(_mutation_ops(self.client).count('model'), 4)

    def test_legacy_target_is_rechecked_and_resolution_error_keeps_the_turn(self):
        conn = db.get_connection()
        self.addCleanup(conn.close)
        self.configure()
        config_key = conn.execute("SELECT key FROM settings WHERE key LIKE 'hermes_config:%'").fetchone()[0]
        legacy = {'model': {'--model': 'B'}}
        conn.execute("UPDATE settings SET value=? WHERE project='alpha' AND key=?",
                     (json.dumps(legacy), config_key))
        conn.commit()

        def unresolved(method, **params):
            if method == 'session.mutate' and params['operation'] == 'model':
                raise GatewayRefusal('model_resolution_failed')
            return self.snapshot

        self.client.rpc.side_effect = unresolved
        def turn():
            return sessions.configure_tracked(conn, 'alpha', self.lease, self.snapshot, workdir='/w',
                                              model_args=['--model', 'B'], server=self.server, member='fixture')
        self.assertEqual(turn()['session_id'], 'sid')
        notice = conn.execute("SELECT content FROM messages WHERE sender='engine'").fetchone()[0]
        self.assertIn('无法解析', notice)
        self.assertNotIn('switch_context', notice)
        self.client.rpc.side_effect = lambda method, **kw: _success(self.snapshot, method, **kw)
        turn()
        updated = json.loads(conn.execute('SELECT value FROM settings WHERE key=?', (config_key,)).fetchone()[0])
        self.assertEqual(updated['applied_model'], legacy['model'])
        self.assertNotIn('failed_target', updated)

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

    def test_legacy_migration_uses_lease_profile_and_reuses_import(self):
        target = 'bg-' + uuid.uuid5(uuid.NAMESPACE_URL, 'isolated-profile:legacy:acp').hex
        imported = []

        def rpc(method, **params):
            if method == 'session.resume':
                self.assertEqual(params['session_id'], target)
                if not imported:
                    raise GatewayRefusal('not_found')
                return {**self.snapshot, 'session_id': target}
            if method == 'session.export':
                self.assertEqual(params['session_id'], 'legacy')
                return {'content': json.dumps({'id': 'legacy', 'title': 'fixture'})}
            if method == 'session.mutate':
                self.assertEqual(params['operation'], 'import')
                imported.append(params['payload']['sessions'][0])
                return {}
            raise AssertionError(method)

        self.client.rpc.side_effect = rpc
        legacy = {**_snapshot(source='cli'), 'session_id': 'legacy'}
        for _ in range(2):
            result = sessions.migrate_legacy(self.lease, legacy, workdir='/w')
            self.assertEqual(result['session_id'], target)
        self.assertEqual(len(imported), 1)
        self.assertEqual(imported[0]['id'], target)
        self.assertEqual(imported[0]['source'], 'acp')


if __name__ == '__main__':
    unittest.main()
