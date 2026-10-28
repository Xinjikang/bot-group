"""Gateway ownership across engine recovery, HTTP credentials and stop boundaries."""
import os
import threading
import time
import unittest
from unittest.mock import Mock, call, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine import api, auth, db, hermes_gateway as gateway, hermes_gateway_ledger as ledger, wake
from scripts.unified_test_support import isolated_database


class GatewayTests(unittest.TestCase):
    def setUp(self):
        fixture = isolated_database()
        fixture.__enter__()
        self.addCleanup(fixture.__exit__, None, None, None)
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        self.session = wake.session_name('default', 'work', None, 'alpha')
        self.start = time.time() - 10
        self.conn.execute('INSERT INTO wake_runs(session,started_at,project,profile,runtime_session_id) '
                          "VALUES(?,?,'alpha','default','sid')", (self.session, self.start))
        self.row = ledger.prepare(self.conn, session=self.session, project='alpha', profile='default', kind='work',
            thread_id=None, task_id=None, job_id=None, event_floor=0, source='', python='["hermes"]',
            home='/isolated', workdir='/isolated/work', session_id='sid', profile_id='home', started_at=self.start)
        self.eid = self.row['execution_id']
        app = FastAPI()
        app.include_router(api.router_api)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        context = dict(profile='default', project='alpha', thread_id=None,
                       session=self.session, wake_started_at=self.start)
        self.headers = {'X-Group-Profile': 'default', 'X-Group-Key': auth.execution_token('default', context)}

    def terminal(self):
        return ledger.apply_receipt(self.conn, self.eid, dict(admission_id='a',
            ref={'session_id': 'sid', 'profile_id': 'home'}, authority_epoch=1,
            execution_generation=1, status='terminal', outcome='completed'))

    def test_recovery_preserves_remote_execution_and_local_credential(self):
        job = self.conn.execute("INSERT INTO wake_jobs(profile,project,kind,trigger,status,enqueued_at) "
                                "VALUES('default','alpha','work','test','running',?)", (db.now_iso(),)).lastrowid
        self.conn.execute('UPDATE hermes_executions SET job_id=? WHERE execution_id=?', (job, self.eid))
        self.conn.commit()
        db.recover_on_startup(self.conn)
        self.assertEqual(self.conn.execute('SELECT status FROM wake_jobs WHERE id=?', (job,)).fetchone()[0], 'done')
        self.assertIsNone(self.conn.execute('SELECT ended_at FROM wake_runs WHERE session=?', (self.session,)).fetchone()[0])
        with patch.object(gateway, '_start_worker') as worker, patch.object(gateway, '_publish'):
            gateway.recover()
        worker.assert_called_once_with(self.eid)
        self.assertIsNone(gateway.process_state(self.session))

    def test_terminal_revokes_token_before_transport_cleanup(self):
        self.assertEqual(self.client.get('/commands', headers=self.headers).status_code, 200)
        self.terminal()
        self.assertTrue(gateway.active(self.session))
        self.assertEqual(self.client.get('/commands', headers=self.headers).status_code, 401)
        ledger.finish_cleanup(self.conn, self.eid)
        self.assertEqual(gateway.process_state(self.session), [])

    def test_inbox_scope_rejects_other_project_and_wrong_kind(self):
        for params in ({'project': 'beta', 'kind': 'work'}, {'project': 'alpha', 'kind': 'chat'},
                       {'project': 'alpha', 'kind': 'work', 'thread_id': '123'}):
            self.assertEqual(self.client.get('/inbox', params=params, headers=self.headers).status_code, 403)
        self.assertEqual(self.client.get('/inbox', params={'profile': 'default', 'project': 'alpha', 'kind': 'work'},
                                         headers=self.headers).status_code, 200)

    def test_heartbeat_tracks_mcp_identity_and_rejects_foreign_topic(self):
        self.assertEqual(self.client.post('/heartbeat', json={'pid': 77, 'thread_id': 2}, headers=self.headers).status_code, 403)
        self.assertEqual(self.client.post('/heartbeat', json={'pid': 77}, headers=self.headers).status_code, 200)
        self.assertEqual(self.conn.execute('SELECT pid FROM wake_runs WHERE session=?', (self.session,)).fetchone()[0], 77)

    def test_unknown_cancel_retains_occupancy_and_never_kills_gateway(self):
        from engine import stop
        with patch.object(gateway, '_start_worker'), patch.object(gateway.time, 'monotonic', side_effect=[0, 31]), \
                patch.object(stop, '_kill_process_tree') as kill, patch.object(stop, '_release_tasks') as release:
            result = stop._stop_session('default', 'alpha', None)
        self.assertFalse(result['stopped'])
        self.assertTrue(gateway.active(self.session))
        self.assertEqual(ledger.read(self.conn, self.eid)['cancel_reason'], 'user')
        kill.assert_not_called()
        release.assert_not_called()

    def test_submit_failure_after_send_remains_unknown_without_resubmission(self):
        ledger.uncertain(self.conn, self.eid, 'transport')
        remote = Mock()
        ledger.reconcile(remote, self.conn, self.eid, now=self.start + 151)
        self.assertEqual(ledger.read(self.conn, self.eid)['state'], 'manual')
        remote.rpc.assert_not_called()

    def test_shutdown_waits_for_remote_terminal_and_mcp_cleanup(self):
        from engine import browser_resources, process_tree
        from engine.lifecycle import Shutdown
        self.addCleanup(wake._shutting_down.clear)
        exited = Mock()
        controller = Shutdown(threading.Event(), exited, timeout=5)
        with patch.object(wake, 'active_runs', return_value=[]), \
             patch.object(browser_resources, 'release', return_value={'ok': True}) as release, \
             patch.object(gateway, 'cancel') as cancel, \
             patch.object(process_tree, 'terminate') as kill:
            controller.begin()
            try:
                self.assertTrue(wake._shutting_down.wait(2))
                self.assertTrue(controller._busy())
                self.terminal()
                self.assertTrue(controller._busy())
                release.assert_not_called()
                exited.assert_not_called()
                ledger.finish_cleanup(self.conn, self.eid)
            finally:
                controller.worker.join(7)
            self.assertFalse(controller.worker.is_alive())
            self.assertEqual(controller.snapshot()['state'], 'stopping')
            release.assert_called_once_with()
            exited.assert_called_once_with()
            cancel.assert_not_called()
            kill.assert_not_called()

    def test_shutdown_timeout_settles_gateway_before_releasing_resources(self):
        from engine import browser_resources, process_tree
        from engine.lifecycle import Shutdown
        self.addCleanup(wake._shutting_down.clear)
        exited = Mock()
        controller = Shutdown(threading.Event(), exited, timeout=0)

        def settle(execution_id):
            self.assertEqual(ledger.read(self.conn, execution_id)['cancel_reason'], 'shutdown')
            exited.assert_not_called()
            gateway._finish(self.conn, self.terminal(), Mock())

        with patch.object(wake, 'active_runs', return_value=[]), \
             patch.object(gateway, '_start_worker', side_effect=settle), \
             patch.object(gateway.sessions, 'clear_mcp') as clear, \
             patch.object(gateway.sessions, 'mutate') as rewind, \
             patch.object(browser_resources, 'release', return_value={'ok': True}) as release, \
             patch.object(process_tree, 'terminate') as kill:
            controller._run()
        self.assertEqual(controller.snapshot()['state'], 'stopping')
        self.assertFalse(gateway.active(self.session))
        clear.assert_called_once()
        rewind.assert_not_called()
        kill.assert_not_called()
        self.assertEqual(release.call_args_list, [
            call(session=self.session, started_at=self.start, pending_only=True), call()])
        exited.assert_called_once_with()

    def test_unconfirmed_gateway_cancel_keeps_engine_available(self):
        from engine import browser_resources, process_tree
        from engine.lifecycle import Shutdown
        self.addCleanup(wake._shutting_down.clear)
        exited = Mock()
        controller = Shutdown(threading.Event(), exited, timeout=0)
        native_cancel = gateway.cancel
        with patch.object(wake, 'active_runs', return_value=[]), \
             patch.object(gateway, '_start_worker'), \
             patch.object(gateway, 'cancel', side_effect=lambda session, reason:
                          native_cancel(session, reason=reason, timeout=0)), \
             patch.object(browser_resources, 'release') as release, \
             patch.object(process_tree, 'terminate') as kill:
            controller._run()
        self.assertEqual(controller.snapshot()['state'], 'failed')
        self.assertTrue(gateway.active(self.session))
        self.assertEqual(ledger.read(self.conn, self.eid)['cancel_reason'], 'shutdown')
        self.assertFalse(wake._shutting_down.is_set())
        self.assertFalse(controller.stop_event.is_set())
        release.assert_not_called()
        exited.assert_not_called()
        kill.assert_not_called()

    def test_newer_direct_execution_uses_its_own_process_lifecycle(self):
        self.terminal()
        ledger.finish_cleanup(self.conn, self.eid)
        self.conn.execute('INSERT INTO wake_runs(session,started_at,project,profile) VALUES(?,?,?,?)',
                          (self.session, self.start + 1, 'alpha', 'default'))
        self.conn.commit()
        self.assertIs(gateway.process_state(self.session), False)

    def test_queued_cancellation_preserves_earlier_completed_turn(self):
        ledger.cancel(self.conn, self.eid)
        row = self.terminal()
        remote = Mock()
        with patch.object(gateway.sessions, 'attach', return_value={'messages': [
                {'role': 'user', 'row_id': 5, 'timestamp': self.start - 5}]}), \
                patch.object(gateway.sessions, 'mutate') as mutate, \
                patch.object(gateway.sessions, 'clear_mcp') as clear:
            gateway._finish(self.conn, row, remote)
        mutate.assert_not_called()
        clear.assert_called_once_with(remote, 'sid')
        self.assertFalse(gateway.active(self.session))

    def test_pre_submit_failure_still_clears_native_mcp(self):
        ledger.not_submitted(self.conn, self.eid, 'configuration_failed')
        remote = Mock()
        with patch.object(gateway, 'connection', return_value=(remote, '/isolated', ['hermes'])), \
                patch.object(gateway.sessions, 'attach'), patch.object(gateway.sessions, 'clear_mcp') as clear, \
                patch.object(wake, '_report_execution_error'), patch.object(wake, '_handle_task_exit'):
            gateway._watch(self.eid)
        clear.assert_called_once_with(remote, 'sid')
        self.assertFalse(gateway.active(self.session))

    def test_ambiguous_title_does_not_attach_create_or_bind(self):
        remote = Mock()
        remote.rpc.return_value = {'sessions': [{'session_id': 'one'}, {'session_id': 'two'}]}
        with patch.object(gateway.sessions, 'attach') as attach, \
             patch.object(gateway.sessions, 'new') as new:
            with self.assertRaisesRegex(gateway.GatewayRefusal, 'ambiguous_session_title'):
                gateway._binding(self.conn, remote, {'id': 123}, 'alpha', 'default',
                                 'work', None, self.session, '/isolated/work', [])
        attach.assert_not_called()
        new.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM session_bindings').fetchone()[0], 0)

    def test_unique_title_becomes_persisted_id_binding(self):
        remote = Mock()
        remote.rpc.return_value = {'sessions': [{'session_id': 'one'}]}
        snapshot = {'session_id': 'one', 'info': {'source': 'acp'}}
        with patch.object(gateway.sessions, 'attach', return_value=snapshot) as attach, \
             patch.object(gateway.sessions, 'new') as new:
            for _ in range(2):
                self.assertEqual(gateway._binding(self.conn, remote, {'id': 123}, 'alpha', 'default',
                                 'work', None, self.session, '/isolated/work', []), snapshot)
        remote.rpc.assert_called_once_with('session.list', title=self.session)
        self.assertEqual(attach.call_args_list, [call(remote, 'one'), call(remote, 'one')])
        new.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT session_id FROM session_bindings').fetchone()[0], 'one')

    def test_missing_title_creates_and_binds_native_session(self):
        remote = Mock()
        remote.rpc.return_value = {'sessions': []}
        snapshot = {'session_id': 'new', 'info': {'source': 'acp'}}
        with patch.object(gateway.sessions, 'attach') as attach, \
             patch.object(gateway.sessions, 'new', return_value=snapshot) as new, \
             patch.object(wake, 'wake_system_prompt', return_value='test prompt'):
            self.assertEqual(gateway._binding(self.conn, remote, {'id': 123}, 'alpha', 'default',
                             'work', None, self.session, '/isolated/work', []), snapshot)
        attach.assert_not_called()
        new.assert_called_once()
        self.assertEqual(self.conn.execute('SELECT session_id FROM session_bindings').fetchone()[0], 'new')

    def test_terminal_cleanup_pauses_and_recovers_without_resubmission(self):
        self.terminal()
        remote = Mock()
        with patch.object(gateway, 'connection', return_value=(remote, '/isolated', ['hermes'])), \
             patch.object(gateway.sessions, 'attach'), \
             patch.object(gateway.sessions, 'clear_mcp', side_effect=ConnectionError('offline')) as clear, \
             patch.object(gateway.time, 'sleep') as sleep, \
             patch.object(wake, '_handle_task_exit'):
            gateway._watch(self.eid)
            row = ledger.read(self.conn, self.eid)
            self.assertEqual(clear.call_count, 3)
            self.assertEqual(sleep.call_args_list, [call(3), call(3)])
            self.assertEqual(row['state'], 'terminal')
            self.assertEqual(row['cleanup_pending'], 1)
            self.assertIn('cleanup_paused after 3 attempts', row['detail'])
            self.assertIsNone(gateway.process_state(self.session))
            self.assertNotIn(self.eid, gateway._workers)
            self.assertEqual(self.client.get('/commands', headers=self.headers).status_code, 401)
            clear.side_effect = None
            with patch.object(gateway, '_publish'), \
                 patch.object(gateway, '_start_worker', side_effect=gateway._watch) as worker:
                gateway.recover()
            worker.assert_called_once_with(self.eid)
        self.assertFalse(gateway.active(self.session))
        self.assertEqual(ledger.read(self.conn, self.eid)['detail'], '')
        remote.rpc.assert_not_called()

    def test_gateway_unavailable_during_terminal_cleanup_has_bounded_attempts(self):
        self.terminal()
        with patch.object(gateway, 'connection', side_effect=ConnectionError('offline')) as connect, \
             patch.object(gateway.time, 'sleep') as sleep:
            gateway._watch(self.eid)
        self.assertEqual(connect.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(gateway.active(self.session))
        self.assertIn('cleanup_paused', ledger.read(self.conn, self.eid)['detail'])

    def test_transient_cleanup_failure_finishes_within_retry_limit(self):
        self.terminal()
        remote = Mock()
        with patch.object(gateway, 'connection', return_value=(remote, '/isolated', ['hermes'])), \
             patch.object(gateway.sessions, 'attach'), \
             patch.object(gateway.sessions, 'clear_mcp', side_effect=[ConnectionError('offline'), None]) as clear, \
             patch.object(gateway.time, 'sleep'), patch.object(wake, '_handle_task_exit'):
            gateway._watch(self.eid)
        self.assertEqual(clear.call_count, 2)
        self.assertFalse(gateway.active(self.session))
        self.assertEqual(ledger.read(self.conn, self.eid)['detail'], '')

    def test_stop_retries_paused_cleanup_and_preserves_original_cancel_reason(self):
        ledger.cancel(self.conn, self.eid, 'shutdown')
        self.terminal()
        self.conn.execute("UPDATE hermes_executions SET detail='cleanup_paused' WHERE execution_id=?", (self.eid,))
        self.conn.commit()
        remote = Mock()
        with patch.object(gateway, 'connection', return_value=(remote, '/isolated', ['hermes'])), \
             patch.object(gateway.sessions, 'attach'), patch.object(gateway.sessions, 'clear_mcp') as clear, \
             patch.object(gateway.sessions, 'mutate') as rewind, \
             patch.object(gateway, '_start_worker', side_effect=gateway._watch):
            self.assertTrue(gateway.cancel(self.session))
        clear.assert_called_once_with(remote, 'sid')
        rewind.assert_not_called()
        self.assertFalse(gateway.active(self.session))
        self.assertEqual(ledger.read(self.conn, self.eid)['cancel_reason'], 'shutdown')


if __name__ == '__main__':
    unittest.main()
