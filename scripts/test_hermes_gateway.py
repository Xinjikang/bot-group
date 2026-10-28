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
        self.assertFalse(gateway.active(self.session))
        self.assertEqual(len(ledger.cleanup_pending(self.conn, self.session)), 1)
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

    def test_shutdown_skips_gateway_execution_and_releases_locally(self):
        from engine import browser_resources, process_tree
        from engine.lifecycle import Shutdown
        self.addCleanup(wake._shutting_down.clear)
        exited = Mock()
        controller = Shutdown(threading.Event(), exited, timeout=5)
        with patch.object(wake, 'active_runs', return_value=[]), \
             patch.object(browser_resources, 'release', return_value={'ok': True}) as release, \
             patch.object(gateway, 'close_connections') as close_conns, \
             patch.object(gateway, 'cancel') as cancel, \
             patch.object(process_tree, 'terminate') as kill:
            controller.begin()
            try:
                self.assertTrue(wake._shutting_down.wait(2))
                # gateway 托管未终态执行不阻塞引擎退出（设计 4.4：不等 bot 终态）。
                self.assertFalse(controller._busy())
            finally:
                controller.worker.join(7)
            self.assertFalse(controller.worker.is_alive())
            self.assertEqual(controller.snapshot()['state'], 'stopping')
            close_conns.assert_called_once_with(timeout=5)
            release.assert_called_once_with(exclude_managed=True)
            exited.assert_called_once_with()
            cancel.assert_not_called()
            kill.assert_not_called()
            # bot 继续运行：执行仍 active，未被取消。
            self.assertTrue(gateway.active(self.session))

    def test_shutdown_preserves_gateway_execution_through_local_release(self):
        from engine import browser_resources, process_tree
        from engine.lifecycle import Shutdown
        self.addCleanup(wake._shutting_down.clear)
        exited = Mock()
        controller = Shutdown(threading.Event(), exited, timeout=0)
        with patch.object(wake, 'active_runs', return_value=[]), \
             patch.object(gateway, 'close_connections') as close_conns, \
             patch.object(browser_resources, 'release', return_value={'ok': True}) as release, \
             patch.object(process_tree, 'terminate') as kill:
            controller._run()
        self.assertEqual(controller.snapshot()['state'], 'stopping')
        # bot 继续运行：执行仍 active，未被取消、未被本地收尾清除。
        self.assertTrue(gateway.active(self.session))
        close_conns.assert_called_once_with(timeout=5)
        kill.assert_not_called()
        release.assert_called_once_with(exclude_managed=True)
        exited.assert_called_once_with()

    def test_shutdown_does_not_cancel_gateway_execution(self):
        from engine import browser_resources, process_tree
        from engine.lifecycle import Shutdown
        self.addCleanup(wake._shutting_down.clear)
        exited = Mock()
        controller = Shutdown(threading.Event(), exited, timeout=0)
        with patch.object(wake, 'active_runs', return_value=[]), \
             patch.object(gateway, 'close_connections'), \
             patch.object(gateway, 'cancel') as cancel, \
             patch.object(browser_resources, 'release', return_value={'ok': True}) as release, \
             patch.object(process_tree, 'terminate') as kill:
            controller._run()
        self.assertEqual(controller.snapshot()['state'], 'stopping')
        # bot 继续运行：执行仍 active，未取消。
        self.assertTrue(gateway.active(self.session))
        self.assertIsNone(ledger.read(self.conn, self.eid)['cancel_reason'])
        cancel.assert_not_called()
        release.assert_called_once_with(exclude_managed=True)
        exited.assert_called_once_with()
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

    def test_terminal_cleanup_fails_boundedly_and_marks_failed(self):
        self.terminal()
        remote = Mock()
        with patch.object(gateway, 'connection', return_value=(remote, '/isolated', ['hermes'])), \
             patch.object(gateway.sessions, 'attach'), \
             patch.object(gateway.sessions, 'clear_mcp', side_effect=ConnectionError('offline')) as clear, \
             patch.object(gateway.time, 'sleep') as sleep, \
             patch.object(wake, '_handle_task_exit'):
            gateway._watch(self.eid)
        row = ledger.read(self.conn, self.eid)
        self.assertEqual(clear.call_count, 5)
        self.assertEqual(len(sleep.call_args_list), 4)
        for expected, actual in zip((1.0, 2.0, 4.0, 8.0), [c.args[0] for c in sleep.call_args_list]):
            self.assertAlmostEqual(actual, expected, delta=0.5)
        self.assertEqual(row['state'], 'terminal')
        self.assertEqual(row['cleanup_pending'], 1)
        self.assertEqual(row['retry_count'], 5)
        self.assertIn('cleanup_failed after 5 attempts', row['detail'])
        self.assertIsNone(gateway.process_state(self.session))
        self.assertNotIn(self.eid, gateway._workers)
        self.assertEqual(self.client.get('/commands', headers=self.headers).status_code, 401)
        # 耗尽后 recover() 不自动重试（D2：不刷新重试预算、不自动续办）
        with patch.object(gateway, '_publish') as publish, \
             patch.object(gateway, '_start_worker') as worker:
            gateway.recover()
        worker.assert_not_called()
        publish.assert_not_called()
        remote.rpc.assert_not_called()

    def test_cleanup_deadline_expired_marks_failed_without_retry(self):
        self.terminal()
        # 手动把 cleanup_deadline 改为已过期，模拟清理最终期限已到（reviewer #255 次要②）。
        self.conn.execute("UPDATE hermes_executions SET cleanup_deadline=? WHERE execution_id=?",
                          (time.time() - 1, self.eid))
        self.conn.commit()
        remote = Mock()
        with patch.object(gateway, 'connection', return_value=(remote, '/isolated', ['hermes'])), \
             patch.object(gateway.sessions, 'attach'), \
             patch.object(gateway.sessions, 'clear_mcp', side_effect=ConnectionError('offline')) as clear, \
             patch.object(gateway.time, 'sleep'), patch.object(wake, '_handle_task_exit'):
            gateway._watch(self.eid)
        row = ledger.read(self.conn, self.eid)
        self.assertEqual(clear.call_count, 1)  # 期限已过，不再登记重试
        self.assertEqual(row['retry_count'], 0)
        self.assertIn('cleanup deadline exceeded', row['detail'])
        self.assertEqual(row['cleanup_pending'], 1)

    def test_late_cleanup_does_not_touch_successor_execution(self):
        # D1：A terminal 后清理 worker 迟到，后继 B（同 session 不同 started_at）已登记，
        # A 的收尾按 started_at 精确匹配，不误删 B 的 wake_runs（reviewer #255 次要④）。
        self.terminal()
        b_start = self.start + 5
        self.conn.execute('INSERT INTO wake_runs(session,started_at,project,profile,runtime_session_id) '
                          "VALUES(?,?,'alpha','default','sid-b')", (self.session, b_start))
        self.conn.commit()
        remote = Mock()
        with patch.object(gateway, 'connection', return_value=(remote, '/isolated', ['hermes'])), \
             patch.object(gateway.sessions, 'attach'), \
             patch.object(gateway.sessions, 'clear_mcp'), \
             patch.object(wake, '_handle_task_exit'):
            gateway._watch(self.eid)
        a_ended = self.conn.execute('SELECT ended_at FROM wake_runs WHERE started_at=?',
                                    (self.start,)).fetchone()[0]
        b_ended = self.conn.execute('SELECT ended_at FROM wake_runs WHERE started_at=?',
                                    (b_start,)).fetchone()[0]
        self.assertIsNotNone(a_ended)  # A 收尾
        self.assertIsNone(b_ended)     # B 未被 A 的迟到清理误删

    def test_recover_resumes_pending_cleanup_within_budget(self):
        self.terminal()
        ledger.register_cleanup_retry(self.conn, self.eid, 'offline', now=self.start + 200)
        ledger.register_cleanup_retry(self.conn, self.eid, 'offline', now=self.start + 210)
        remote = Mock()
        with patch.object(gateway, 'connection', return_value=(remote, '/isolated', ['hermes'])), \
             patch.object(gateway.sessions, 'attach'), \
             patch.object(gateway.sessions, 'clear_mcp') as clear, \
             patch.object(wake, '_handle_task_exit'), \
             patch.object(gateway, '_start_worker', side_effect=gateway._watch):
            gateway.recover()
        clear.assert_called_once_with(remote, 'sid')
        self.assertEqual(ledger.read(self.conn, self.eid)['cleanup_pending'], 0)
        self.assertEqual(ledger.read(self.conn, self.eid)['retry_count'], 0)

    def test_gateway_unavailable_during_terminal_cleanup_has_bounded_attempts(self):
        self.terminal()
        with patch.object(gateway, 'connection', side_effect=ConnectionError('offline')) as connect, \
             patch.object(gateway.time, 'sleep') as sleep:
            gateway._watch(self.eid)
        self.assertEqual(connect.call_count, 5)
        self.assertEqual(sleep.call_count, 4)
        self.assertFalse(gateway.active(self.session))
        self.assertEqual(len(ledger.cleanup_pending(self.conn, self.session)), 1)
        self.assertIn('cleanup_failed', ledger.read(self.conn, self.eid)['detail'])

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

    def test_manual_state_stops_automatic_reconciliation(self):
        ledger.uncertain(self.conn, self.eid, 'lost', now=self.start + 151)
        self.assertEqual(ledger.read(self.conn, self.eid)['state'], 'manual')
        remote = Mock()
        with patch.object(gateway, 'connection', return_value=(remote, '/isolated', ['hermes'])), \
             patch.object(gateway.sessions, 'attach'):
            gateway._watch(self.eid)
        remote.rpc.assert_not_called()
        self.assertNotIn(self.eid, gateway._workers)
        # manual 保留安全占用（state != terminal）。
        self.assertTrue(gateway.active(self.session))

    def test_cleanup_pending_blocks_same_session_spawn(self):
        self.terminal()
        with patch.object(gateway, 'connection') as connect:
            result = gateway.spawn('default', 'work', 'sig', None, 'alpha',
                                   bot={'id': 1}, workdir='/w', model_args=[])
        self.assertTrue(result['busy'])
        self.assertEqual(result['detail'], 'gateway cleanup pending')
        connect.assert_not_called()

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

    def test_close_all_bounds_and_closes_every_viewer(self):
        from engine.hermes_gateway_client import close_all, _register, _unregister

        class FakeClient:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        a, b = FakeClient(), FakeClient()
        _register(a)
        _register(b)
        try:
            close_all(timeout=5)
        finally:
            _unregister(a)
            _unregister(b)
        self.assertTrue(a.closed)
        self.assertTrue(b.closed)

    def test_close_all_respects_total_budget_when_close_hangs(self):
        from engine.hermes_gateway_client import close_all, _register, _unregister

        class HangingClient:
            def close(self):
                time.sleep(30)

        client = HangingClient()
        _register(client)
        start = time.monotonic()
        try:
            close_all(timeout=0.5)
        finally:
            _unregister(client)
        self.assertLess(time.monotonic() - start, 2)


if __name__ == '__main__':
    unittest.main()
