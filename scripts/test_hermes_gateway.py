"""Gateway ownership across engine recovery, HTTP credentials and stop boundaries."""
import os
import time
import unittest
from unittest.mock import Mock, patch

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


if __name__ == '__main__':
    unittest.main()
