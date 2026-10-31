"""Session reuse, scoped writes, revocation and restart handoff on isolated SQLite."""
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from engine import api, auth, db, hermes_gateway as gateway, hermes_gateway_ledger as ledger
from engine import hermes_session_auth as grants, signing_keys, task_access, wake
from scripts.unified_test_support import isolated_database


class SessionReuseTests(unittest.TestCase):
    def setUp(self):
        fixture = isolated_database()
        self.path = fixture.__enter__()
        self.addCleanup(fixture.__exit__, None, None, None)
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        keys = patch.object(auth, 'signing_keys', return_value=(1, {1: b'isolated-session-key'}))
        keys.start()
        self.addCleanup(keys.stop)
        self.bot = db._ensure_hermes_bot(self.conn, 'default')
        self.session = wake.session_name('default', 'work', None, 'alpha')
        self.conn.execute("INSERT INTO session_bindings(project,bot_id,profile,kind,thread_id,session_id,runtime,generation,title,updated_at) "
                          "VALUES('alpha',?,'default','work',0,'sid','hermes',0,?,?)",
                          (self.bot, self.session, db.now_iso()))
        self.conn.commit()
        self.grant = self.make_grant()
        self.context = grants.context(self.grant)
        self.headers = self.headers_for()
        app = FastAPI(); app.include_router(api.router_api)
        self.client = TestClient(app); self.addCleanup(self.client.close)
        self.snapshot = {'session_id': 'sid', 'revision': 1, 'execution_generation': 0,
                         'running': False, 'pending': [], 'info': {'source': 'acp',
                         'cwd': str(Path(self.path).parent), 'edit_approval_policy': 'workspace_session'}}
        self.remote = Mock(endpoint={'profile_id': 'fixture'}, closed=False)
        self.remote.rpc.return_value = self.snapshot

    def make_grant(self):
        return grants.grant(self.conn, project='alpha', profile='default', bot_id=self.bot, kind='work',
                            thread_id=None, session=self.session, home=Path(self.path).parent,
                            command=['fixture'], workdir=str(Path(self.path).parent), model_args=[])

    def headers_for(self, generation=None):
        return {'X-Group-Profile': 'default', 'X-Group-Key': auth.session_token(
            'default', grants.context(self.grant), signing_generation=generation)}

    def run_turn(self):
        start = time.time()
        self.conn.execute("INSERT INTO wake_runs(session,started_at,project,profile,bot_id,runtime_session_id) "
                          "VALUES(?,?,'alpha','default',?,'sid')", (self.session, start, self.bot))
        return ledger.prepare(self.conn, session=self.session, project='alpha', profile='default', kind='work',
                              thread_id=None, task_id=None, job_id=None, event_floor=0, source='', python='[]',
                              home=Path(self.path).parent, workdir=Path(self.path).parent, session_id='sid',
                              profile_id='fixture', started_at=start)

    def finish(self, row):
        ledger.apply_receipt(self.conn, row['execution_id'], dict(admission_id=row['submission_id'],
            ref={'session_id': 'sid', 'profile_id': 'fixture'}, authority_epoch=1,
            execution_generation=1, status='terminal', outcome='completed'))
        self.conn.execute('UPDATE wake_runs SET ended_at=? WHERE session=? AND started_at=?',
                          (time.time(), self.session, row['started_at']))
        self.conn.commit(); ledger.finish_cleanup(self.conn, row['execution_id'])

    def send(self, **extra):
        return self.client.post('/msg', headers=self.headers, json={
            'project': 'alpha', 'thread_id': None, 'text': '@architect fixture', 'need_reply': False,
            'execution': {**self.context, 'credential_type': 'session'}, **extra})

    def test_one_credential_two_turns_and_independent_message_provenance(self):
        for _ in range(2):
            row = self.run_turn()
            response = self.send()
            self.assertEqual(response.status_code, 200, response.text)
            saved = self.conn.execute('SELECT wake_started_at FROM message_executions WHERE message_id=?',
                                      (response.json()['message_id'],)).fetchone()
            self.assertEqual(saved[0], row['started_at'])
            self.finish(row)
        self.assertEqual(self.client.get('/commands', headers=self.headers).status_code, 200)
        self.assertEqual(self.send().status_code, 409)

    def test_session_tasks_and_transaction_recheck(self):
        row = self.run_turn()
        response = self.client.post('/task-tools/req_list', headers=self.headers,
            json={'arguments': {'project': 'alpha'}, 'execution': {**self.context, 'credential_type': 'session'}})
        self.assertEqual(response.status_code, 200, response.text)
        scope = auth.session_scope('default', self.headers['X-Group-Key'], auth.signing_keys()[1])
        resolved = grants.resolve(self.conn, scope)
        self.finish(row)
        with self.assertRaises(HTTPException):
            with task_access.scope('default', 'alpha', 'task_claim', resolved):
                task_access.begin_write(self.conn)
        self.conn.rollback()

    def test_cancel_keeps_grant_but_blocks_writes_until_next_turn(self):
        row = self.run_turn(); ledger.cancel(self.conn, row['execution_id'])
        self.assertEqual(self.send().status_code, 409)
        self.assertEqual(self.client.get('/commands', headers=self.headers).status_code, 200)
        self.finish(row)
        self.run_turn(); self.assertEqual(self.send().status_code, 200)

    def test_token_type_tampering_and_foreign_scope_are_rejected(self):
        token = self.headers['X-Group-Key']
        self.assertFalse(auth.verify_bot('default', token.replace('session.', 'run.', 1)))
        self.run_turn()
        self.assertEqual(self.send(project='beta').status_code, 403)
        self.assertEqual(self.send(execution={**self.context, 'session_id': 'other'}).status_code, 403)
        self.assertEqual(self.client.get('/members?project=beta', headers=self.headers).status_code, 403)

    def test_membership_revoke_rejoin_never_revives_old_token(self):
        self.conn.execute("UPDATE project_members SET active=0 WHERE project='alpha' AND profile='default'")
        self.conn.commit()
        self.assertEqual(self.client.get('/commands', headers=self.headers).status_code, 401)
        self.conn.execute("UPDATE project_members SET active=1 WHERE project='alpha' AND profile='default'")
        self.conn.commit()
        self.assertEqual(self.client.get('/commands', headers=self.headers).status_code, 401)
        with patch.object(grants, 'detach'):
            new = self.make_grant()
        self.assertEqual(new['generation'], 1)
        self.assertEqual(self.client.get('/commands', headers=self.headers).status_code, 401)

    def test_binding_switch_revokes_before_remote_detach(self):
        self.conn.execute("UPDATE session_bindings SET session_id='next',generation=generation+1")
        self.conn.commit()
        self.assertEqual(self.client.get('/commands', headers=self.headers).status_code, 401)
        self.assertEqual(self.conn.execute('SELECT revoked,detached FROM hermes_session_grants').fetchone()[:], (1, 0))

    def test_stable_mcp_config_and_changed_policy(self):
        grants.configure(self.conn, self.grant, self.remote, self.snapshot)
        first = [c for c in self.remote.rpc.call_args_list if c.args[0] == 'session.mutate']
        self.assertEqual([c.kwargs['operation'] for c in first], ['editor_mcp'])
        spec = first[0].kwargs['payload']['mcp_servers'][0]
        env = {v['name']: v['value'] for v in spec['env']}
        self.assertNotIn('wake_started_at', json.loads(env['XDG_GROUP_ENGINE_EXECUTION']))
        self.assertTrue(env['GROUP_ENGINE_TOKEN'].startswith('session.'))
        self.remote.reset_mock()
        grants.configure(self.conn, self.grant, self.remote, self.snapshot)
        self.assertFalse(any(c.args[0] == 'session.mutate' for c in self.remote.rpc.call_args_list))
        self.assertEqual(self.remote.rpc.call_args.kwargs['editor']['mcp_servers'][0], spec)

    def test_idle_heartbeat_does_not_report_thinking(self):
        with patch('engine.heartbeat.beat') as beat:
            response = self.client.post('/heartbeat', headers=self.headers, json={'pid': 12})
            self.assertEqual(response.status_code, 200, response.text)
            beat.assert_not_called()

    def test_permission_revocation_marks_grant_even_after_permission_restored(self):
        from engine import project_permissions
        before = project_permissions._snapshot(self.conn, 'alpha')
        after = {k: [target for target in v if target != 'default'] for k, v in before['grants'].items()}
        project_permissions.save_in_transaction(self.conn, 'alpha', {'revision': before['revision'], 'grants': after}, 'user')
        self.conn.commit()
        self.assertEqual(self.client.get('/commands', headers=self.headers).status_code, 401)

    def test_topic_close_and_reopen_does_not_revive_grant(self):
        tid = self.conn.execute("INSERT INTO threads(project,title,creator,members,created_at,last_active_at) "
                                "VALUES('alpha','fixture','default','[\"default\"]',?,?)",
                                (db.now_iso(), db.now_iso())).lastrowid
        self.conn.execute("UPDATE session_bindings SET kind='chat',thread_id=?", (tid,))
        self.conn.execute("UPDATE hermes_session_grants SET kind='chat',thread_id=?", (tid,))
        self.conn.commit()
        self.grant = self.conn.execute('SELECT * FROM hermes_session_grants').fetchone()
        self.headers = self.headers_for()
        self.assertEqual(self.client.get('/commands', headers=self.headers).status_code, 200)
        self.conn.execute("UPDATE threads SET status='closed' WHERE id=?", (tid,)); self.conn.commit()
        self.conn.execute("UPDATE threads SET status='open' WHERE id=?", (tid,)); self.conn.commit()
        self.assertEqual(self.client.get('/commands', headers=self.headers).status_code, 401)

    def test_detach_failure_retries_durably(self):
        self.conn.execute('UPDATE hermes_session_grants SET revoked=1'); self.conn.commit()
        self.remote.rpc.side_effect = ConnectionError('offline')
        with patch.object(gateway, 'connection', return_value=(self.remote, '', [])):
            grants.maintain()
            self.assertEqual(self.conn.execute('SELECT detached FROM hermes_session_grants').fetchone()[0], 0)
            self.remote.rpc.side_effect = None
            self.remote.rpc.return_value = {**self.snapshot, 'subscription_id': 'subscription'}
            grants.maintain()
        self.assertEqual(self.conn.execute('SELECT detached FROM hermes_session_grants').fetchone()[0], 1)
        self.assertIn('session.detach', [c.args[0] for c in self.remote.rpc.call_args_list])

    def test_recovery_waits_for_active_execution_before_handoff(self):
        self.run_turn()
        with patch.object(auth, 'signing_keys', return_value=(2, {1: b'old', 2: b'new'})), \
             patch.object(gateway, 'connection') as connect:
            grants.maintain()
        connect.assert_not_called()

    def test_mcp_stable_context_drives_message_and_task_calls(self):
        import importlib.util
        source = Path(__file__).resolve().parents[1] / 'mcp/server.py'
        spec = importlib.util.spec_from_file_location('session_reuse_mcp', source)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        async def send(method, url, *, body=None, headers, params=None):
            return self.client.request(method, url.replace('http://test-engine', ''), json=body, headers=headers, params=params)
        with patch.dict(os.environ, {'TASK_QUEUE_ROLE': 'default', 'HERMES_PROFILE': 'default',
                'GROUP_ENGINE_URL': 'http://test-engine', 'GROUP_ENGINE_TOKEN': self.headers['X-Group-Key'],
                'XDG_GROUP_ENGINE_EXECUTION': json.dumps({**self.context, 'credential_type': 'session'})}), \
                patch.object(module.mcp_http, '_send', side_effect=send):
            for _ in range(2):
                row = self.run_turn()
                self.assertIn('message_id', module.group_send('@architect fixture', False))
                self.assertNotIn('error', module._task_call('req_list', {'project': 'alpha'}))
                self.finish(row)

    def test_old_key_retained_through_failed_handoff_and_existing_execution(self):
        ring = (1, {1: b'old'})
        with patch.object(auth, 'signing_keys', side_effect=lambda: ring):
            self.conn.execute('UPDATE hermes_session_grants SET signing_generation=1'); self.conn.commit()
            self.grant = self.conn.execute('SELECT * FROM hermes_session_grants').fetchone()
            old_token = self.headers_for(1)['X-Group-Key']
            row = self.run_turn()
            ring = (2, {1: b'old', 2: b'new'})
            refs = signing_keys._state(self.conn)[1]
            self.assertIn(1, refs)
            self.finish(row)
            self.remote.rpc.side_effect = ConnectionError('offline')
            with self.assertRaises(ConnectionError):
                grants.configure(self.conn, self.grant, self.remote, self.snapshot)
            saved = self.conn.execute('SELECT * FROM hermes_session_grants').fetchone()
            self.assertEqual((saved['signing_generation'], saved['pending_generation']), (1, 2))
            self.assertIn(1, signing_keys._state(self.conn)[1])
            grants.validate(self.conn, auth.session_scope('default', old_token, ring[1]))
            self.remote.rpc.side_effect = None
            grants.configure(self.conn, saved, self.remote, self.snapshot)
            self.assertNotIn(1, signing_keys._state(self.conn)[1])
            with self.assertRaises(HTTPException):
                grants.validate(self.conn, auth.session_scope('default', old_token, ring[1]))


if __name__ == '__main__':
    unittest.main()
