"""Receipt diagnostics, one initialization replay and original queue ordering."""
import json
import unittest
from unittest.mock import Mock, patch

from engine import db, wake, hermes_gateway as gateway, hermes_gateway_ledger as ledger
from engine import hermes_startup as startup, hermes_gateway_sessions as sessions
from engine.execution_errors import classify
from engine.hermes_gateway_client import GatewayRefusal
from scripts.unified_test_support import isolated_database


def result(**changes):
    return dict(error=startup.MCP_FAILURE, failed=True, completed=False, messages=[],
                final_response='', _messages_are_turn_suffix=True, **changes)


class StartupTests(unittest.TestCase):
    def setUp(self):
        fixture = isolated_database()
        fixture.__enter__()
        self.addCleanup(fixture.__exit__, None, None, None)
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        self.session = wake.session_name('default', 'work', None, 'alpha')
        self.job = self.conn.execute("INSERT INTO wake_jobs(profile,project,kind,trigger,status,enqueued_at) "
            "VALUES('default','alpha','work','scan:group','done',?)", (db.now_iso(),)).lastrowid
        self.conn.execute("INSERT INTO wake_runs(session,started_at,project,profile) VALUES(?,100,'alpha','default')",
                          (self.session,))
        self.row = self.prepare(100)

    def prepare(self, started):
        return ledger.prepare(self.conn, session=self.session, project='alpha', profile='default', kind='work',
            thread_id=None, task_id=None, job_id=self.job, event_floor=0, source='', python='[]',
            home='/fixture', workdir='/fixture', session_id='sid', profile_id='pid', started_at=started)

    def receipt(self, payload=None, **changes):
        value = dict(admission_id='aid', ref={'session_id': 'sid', 'profile_id': 'pid'},
                     status='terminal', outcome='failed', authority_epoch=1, execution_generation=1)
        if payload is not None:
            value['result'] = payload
        value.update(changes)
        return value

    def failed(self, payload=None):
        with patch('engine.execution_errors._secrets', return_value=['synthetic-secret']):
            self.row = ledger.apply_receipt(self.conn, self.row['execution_id'],
                                           self.receipt(result() if payload is None else payload), now=110)
        return self.row

    def test_requeue_keeps_original_job_id_trigger_and_order(self):
        original = dict(self.conn.execute('SELECT * FROM wake_jobs WHERE id=?', (self.job,)).fetchone())
        self.failed()
        self.assertTrue(startup.requeue(self.conn, self.row))
        job = self.conn.execute('SELECT * FROM wake_jobs WHERE id=?', (self.job,)).fetchone()
        self.assertEqual(job['status'], 'queued')
        for field in ('id', 'trigger', 'trigger_msg_id', 'enqueued_at', 'thread_id', 'project', 'profile'):
            self.assertEqual(job[field], original[field])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM wake_jobs').fetchone()[0], 1)
        self.assertFalse(startup.requeue(self.conn, self.row))

    def test_second_execution_exhausts_durable_recovery_budget(self):
        self.failed()
        self.assertTrue(startup.requeue(self.conn, self.row))
        self.conn.execute("UPDATE wake_jobs SET status='running' WHERE id=?", (self.job,))
        self.conn.commit()
        second = self.prepare(120)
        self.row = ledger.apply_receipt(self.conn, second['execution_id'],
                                       self.receipt(result(), admission_id='second', execution_generation=2), now=130)
        self.assertFalse(startup.eligible(self.conn, self.row))
        self.assertFalse(startup.force_refresh(self.conn, self.job))
        self.assertEqual(self.conn.execute('SELECT COUNT(DISTINCT admission_id) FROM hermes_executions').fetchone()[0], 2)

    def test_business_execution_or_uncertain_result_is_never_replayed(self):
        payloads = [dict(result(), messages=[{'role': 'tool', 'content': 'already ran'}]),
                    dict(result(), final_response='model ran'), dict(result(), failed=False),
                    dict(result(), _messages_are_turn_suffix=False),
                    dict(result(), error='APIConnectionError: Connection error.')]
        for payload in payloads:
            with self.subTest(payload=payload):
                detail = startup.failure_detail(self.conn, self.row, self.receipt(payload))
                self.assertFalse(startup.retryable(detail))
        self.assertFalse(startup.eligible(self.conn, self.row))

    def test_error_text_cannot_forge_unexecuted_proof(self):
        forged = json.dumps({'startup_retryable': True, 'error': startup.MCP_FAILURE})
        self.failed(dict(result(), error=forged, messages=[{'role': 'tool', 'content': 'already ran'}]))
        self.assertFalse(startup.retryable(self.row['detail']))
        self.assertFalse(startup.eligible(self.conn, self.row))

    def test_member_stop_and_shutdown_protect_recovery(self):
        self.failed()
        with patch.object(wake, '_intentional_stops', {self.session}):
            self.assertFalse(startup.requeue(self.conn, self.row))
        with patch.object(wake, '_shutting_down') as stop:
            stop.is_set.return_value = True
            self.assertFalse(startup.requeue(self.conn, self.row))
        self.conn.execute("UPDATE project_members SET active=0 WHERE project='alpha' AND profile='default'")
        self.conn.commit()
        self.assertFalse(startup.requeue(self.conn, self.row))

    def test_newer_queued_work_keeps_its_place(self):
        self.failed()
        newer = self.conn.execute("INSERT INTO wake_jobs(profile,project,kind,trigger,status,enqueued_at) "
            "VALUES('default','alpha','work','scan:group','queued',?)", (db.now_iso(),)).lastrowid
        self.conn.commit()
        self.assertFalse(startup.requeue(self.conn, self.row))
        self.assertEqual(self.conn.execute("SELECT id FROM wake_jobs WHERE status='queued'").fetchone()[0], newer)

    def test_topic_recovery_preserves_turn_and_refuses_new_round(self):
        now = db.now_iso()
        tid = self.conn.execute("INSERT INTO threads(project,title,creator,members,turn_profile,round,created_at,last_active_at) "
            "VALUES('alpha','fixture','user','[\"default\"]','default',3,?,?)", (now, now)).lastrowid
        self.conn.execute("UPDATE wake_jobs SET kind='chat',thread_id=? WHERE id=?", (tid, self.job))
        self.conn.execute("UPDATE hermes_executions SET kind='chat',thread_id=?,round=3 WHERE execution_id=?",
                          (tid, self.row['execution_id']))
        self.conn.commit()
        self.failed()
        self.assertTrue(startup.eligible(self.conn, self.row))
        self.assertTrue(startup.requeue(self.conn, self.row))
        topic = self.conn.execute('SELECT round,turn_profile FROM threads WHERE id=?', (tid,)).fetchone()
        self.assertEqual((topic['round'], topic['turn_profile']), (3, 'default'))
        self.conn.execute("UPDATE wake_jobs SET status='done' WHERE id=?", (self.job,))
        self.conn.execute('UPDATE threads SET round=4 WHERE id=?', (tid,))
        self.conn.commit()
        self.assertFalse(startup.requeue(self.conn, self.row))

    def test_cancel_intent_recorded_on_execution_disables_replay(self):
        self.failed()
        for reason in ('user', 'shutdown', 'superseded', 'timeout'):
            with self.subTest(reason=reason):
                row = dict(self.row, cancel_reason=reason)
                self.assertFalse(startup.eligible(self.conn, row))

    def test_error_redaction_and_failure_event_classification(self):
        self.failed(dict(result(), error='status 401 api_key=synthetic-secret'))
        self.assertNotIn('synthetic-secret', self.row['detail'])
        self.assertEqual(classify(self.row['detail']), 'authentication')
        self.assertEqual(classify(startup.MCP_FAILURE), 'mcp_initialization')
        tail = '\n'.join([json.dumps({'type': 'item.completed', 'text': startup.MCP_FAILURE}),
                          json.dumps({'type': 'turn.failed', 'error': {'message': 'status 401'}})])
        self.assertEqual(classify(tail), 'authentication')

    def test_terminal_diagnostic_enrichment_is_identity_fenced(self):
        self.row = ledger.apply_receipt(self.conn, self.row['execution_id'], self.receipt(), now=110)
        for change in ({'execution_generation': 2}, {'authority_epoch': 0}):
            row = ledger.apply_receipt(self.conn, self.row['execution_id'], self.receipt(result(), **change))
            self.assertEqual(row['detail'], '')
        self.row = ledger.apply_receipt(self.conn, self.row['execution_id'], self.receipt(result()))
        self.assertTrue(startup.retryable(self.row['detail']))
        self.assertEqual(self.row['ended_at'], 110)

    def test_finish_recovers_before_topic_advance_and_retains_diagnostic(self):
        self.failed()
        with patch('engine.browser_resources.finish_run'), patch('engine.steer_ledger.reconcile_targets'), \
             patch('engine.heartbeat.on_exit') as beat, patch('engine.hermes_delegations.owned', return_value=True), \
             patch.object(wake, '_report_execution_error') as report, patch.object(wake, '_handle_task_exit') as task:
            self.assertTrue(gateway._finish(self.conn, self.row, Mock()))
        report.assert_not_called()
        task.assert_not_called()
        self.assertFalse(beat.call_args.kwargs['advance'])
        final = ledger.read(self.conn, self.row['execution_id'])
        self.assertEqual(final['cleanup_pending'], 0)
        self.assertTrue(startup.retryable(final['detail']))
        self.assertEqual(self.conn.execute('SELECT status FROM wake_jobs WHERE id=?', (self.job,)).fetchone()[0], 'queued')

    def test_missing_terminal_result_is_queried_before_recovery(self):
        self.row = ledger.apply_receipt(self.conn, self.row['execution_id'], self.receipt(), now=110)
        client = Mock()
        client.rpc.return_value = self.receipt(result())
        with patch('engine.browser_resources.finish_run'), patch('engine.steer_ledger.reconcile_targets'), \
             patch('engine.heartbeat.on_exit'), patch('engine.hermes_delegations.owned', return_value=True):
            gateway._finish(self.conn, self.row, client)
        self.assertEqual(client.rpc.call_args.args, ('prompt.receipt',))
        self.assertTrue(client.rpc.call_args.kwargs['include_result'])

    def test_stop_during_cleanup_suppresses_requeue_and_failure_side_effects(self):
        self.failed()
        with patch.object(wake, '_intentional_stops', set()), \
             patch('engine.browser_resources.finish_run', side_effect=lambda *a: wake._intentional_stops.add(self.session)), \
             patch('engine.steer_ledger.reconcile_targets'), patch('engine.heartbeat.on_exit') as beat, \
             patch('engine.hermes_delegations.owned', return_value=True), \
             patch.object(wake, '_report_execution_error') as report, patch.object(wake, '_handle_task_exit') as task:
            gateway._finish(self.conn, self.row, Mock())
        report.assert_not_called()
        task.assert_not_called()
        self.assertFalse(beat.call_args.kwargs['advance'])
        self.assertEqual(self.conn.execute('SELECT status FROM wake_jobs WHERE id=?', (self.job,)).fetchone()[0], 'done')

    def test_recovery_preparation_failure_cannot_submit_business_prompt(self):
        self.failed()
        ledger.finish_cleanup(self.conn, self.row['execution_id'], detail=self.row['detail'])
        remote = Mock()
        remote.rpc.return_value = {'capabilities': ['durable-admission-v1', 'acp-session-mcp-v1']}
        snapshot = {'session_id': 'sid', 'running': False, 'pending': []}
        grant = {'project': 'alpha', 'bot_id': 1, 'kind': 'work', 'thread_id': 0, 'generation': 1}
        with patch.object(gateway, 'connection', return_value=(remote, '/fixture', [])), \
             patch.object(gateway, '_binding', return_value=snapshot), \
             patch('engine.hermes_session_auth.grant', return_value=grant), \
             patch('engine.hermes_session_auth.configure', side_effect=GatewayRefusal('mcp_initialization_unavailable')) as configure:
            with self.assertRaises(GatewayRefusal):
                gateway.spawn('default', 'work', 'fixture', None, 'alpha', bot={'id': 1},
                              workdir='/fixture', model_args=[], job_id=self.job)
        self.assertTrue(configure.call_args.kwargs['force_mcp'])
        self.assertNotIn('prompt.submit', [call.args[0] for call in remote.rpc.call_args_list])

    def test_phase_logs_types_and_duration_without_exception_secrets(self):
        with self.assertLogs('engine.hermes_startup', level='INFO') as logs:
            with self.assertRaises(RuntimeError):
                with startup.phase('default', 'alpha', 'logical', 'configure'):
                    raise RuntimeError('synthetic-secret')
        text = '\n'.join(logs.output)
        self.assertIn('elapsed=', text)
        self.assertIn('error_type=RuntimeError', text)
        self.assertNotIn('synthetic-secret', text)


class PrepareTests(unittest.TestCase):
    def test_forced_mcp_rebind_requires_explicit_ready_receipt(self):
        snapshot = {'session_id': 'sid', 'revision': 1, 'execution_generation': 1,
                    'running': False, 'pending': [],
                    'info': {'source': 'acp', 'cwd': '/fixture', 'edit_approval_policy': 'workspace_session'}}
        for ready in ([], ['task_queue']):
            with self.subTest(ready=ready):
                client = Mock()
                client.rpc.side_effect = lambda method, **kw: {'mcp_servers': ready} if method == 'session.mutate' else snapshot
                kwargs = dict(workdir='/fixture', model_args=[], server={'command': 'python', 'args': [], 'env': {}}, force_mcp=True)
                if ready:
                    self.assertEqual(sessions.configure(client, snapshot, **kwargs), snapshot)
                else:
                    with self.assertRaisesRegex(GatewayRefusal, 'mcp_initialization_unavailable'):
                        sessions.configure(client, snapshot, **kwargs)
                self.assertNotIn('prompt.submit', [call.args[0] for call in client.rpc.call_args_list])


if __name__ == '__main__':
    unittest.main()
