"""Legacy Hermes bindings use CLI process checks and retain queued topic work."""
from pathlib import Path
import unittest
from unittest.mock import patch

from engine import db, hermes_gateway as gateway, hermes_runtime, process_query, queue, wake
from engine import hermes_gateway_ledger as ledger
from engine.hermes_gateway_client import GatewayTransportError
from scripts.test_native_busy_scheduling import NativeBusyFixtures


class LegacyHermesSchedulingTests(NativeBusyFixtures, unittest.TestCase):
    def setUp(self):
        self._setup_db()
        self.delivered = []
        self._setup_queue_and_process_fixtures()
        self.source = Path(self.tmp.name) / 'hermes-source'
        (self.source / 'hermes_cli').mkdir(parents=True)
        patcher = patch.object(hermes_runtime, 'source_directory', return_value=self.source)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(gateway, 'connection',
                              side_effect=GatewayTransportError('Legacy identify has no ready state'))
        self.connection = patcher.start()
        self.addCleanup(patcher.stop)

    def _binding(self, kind='work', thread_id=0, switching=0):
        session = wake.session_name('hermes-a', kind, thread_id or None, 'p')
        self._bind('hermes-a', kind, thread_id, session_id='legacy-a', switching=switching)
        self.conn.execute('UPDATE session_bindings SET title=? WHERE session_id=?',
                          (session, 'legacy-a'))
        self.conn.commit()
        return session

    def _gateway_layout(self):
        for name in ('gateway_runtime_cli.py', 'gateway_chat_startup.py'):
            (self.source / 'hermes_cli' / name).touch()

    def _execution(self, session):
        return ledger.prepare(self.conn, session=session, project='p', profile='hermes-a',
                              kind='work', thread_id=None, task_id=None, job_id=None,
                              event_floor=0, source=str(self.source), python='["hermes"]',
                              home=self.tmp.name, workdir=self.tmp.name, session_id='legacy-a',
                              profile_id='fixture', started_at=100.0)

    def test_legacy_bound_idle_session_uses_process_scan(self):
        session = self._binding()
        self.assertFalse(gateway.enabled())
        with patch.object(process_query, 'list_processes', return_value=[]) as scan:
            self.assertEqual(wake.find_bot_processes('hermes-a', session), [])
        scan.assert_called_once()
        self.connection.assert_not_called()
        self.assertEqual(gateway._native_cache, {})

    def test_legacy_bound_cli_pid_keeps_original_job_queued_until_exit(self):
        session = self._binding()
        job = self._job('hermes-a')
        records = [(123, ['python.exe', 'hermes', '-p', 'hermes-a', 'chat', '-c', 'legacy-a']),
                   (124, ['python.exe', 'hermes', '-p', 'hermes-b', 'chat', '-c', 'legacy-a'])]
        with patch.object(process_query, 'list_processes', return_value=records):
            self.assertEqual(wake.find_bot_processes('hermes-a', session), [123])
            self.assertFalse(queue.process_one(self.conn))
        saved = self.conn.execute('SELECT * FROM wake_jobs WHERE id=?', (job['id'],)).fetchone()
        self.assertEqual(saved['status'], 'queued')
        self.assertIsNone(saved['started_at'])
        self.assertEqual(self.delivered, [])
        self.assertTrue(queue.process_one(self.conn))
        saved = self.conn.execute('SELECT * FROM wake_jobs WHERE id=?', (job['id'],)).fetchone()
        self.assertEqual(saved['status'], 'done')
        self.assertEqual(saved['enqueued_at'], job['enqueued_at'])
        self.assertEqual(self.delivered, [('hermes-a', 'work', None)])
        self.connection.assert_not_called()

    def test_legacy_unknown_process_snapshot_defers_without_gateway_query(self):
        self._binding()
        job = self._job('hermes-a')
        with patch.object(process_query, 'list_processes', return_value=None):
            self.assertFalse(queue.process_one(self.conn))
        saved = self.conn.execute('SELECT status FROM wake_jobs WHERE id=?', (job['id'],)).fetchone()
        self.assertEqual(saved['status'], 'queued')
        self.connection.assert_not_called()

    def test_legacy_mode_ignores_native_error_cache(self):
        session = self._binding()
        gateway._native_cache_failure(('legacy-a', 0), 'transport_error')
        self.assertEqual(wake.find_bot_processes('hermes-a', session), [])
        self.connection.assert_not_called()

    def test_switching_binding_keeps_legacy_session_deferred(self):
        session = self._binding(switching=1)
        with patch.object(process_query, 'list_processes') as scan:
            self.assertIsNone(wake.find_bot_processes('hermes-a', session))
        scan.assert_not_called()
        self.connection.assert_not_called()

    def test_unresolved_gateway_execution_keeps_ownership_in_legacy_mode(self):
        session = self._binding()
        row = self._execution(session)
        with patch.object(process_query, 'list_processes') as scan:
            self.assertIsNone(wake.find_bot_processes('hermes-a', session))
            self.conn.execute("UPDATE hermes_executions SET state='terminal',cleanup_pending=1 "
                              'WHERE execution_id=?', (row['execution_id'],))
            self.conn.commit()
            self.assertIsNone(wake.find_bot_processes('hermes-a', session))
        scan.assert_not_called()
        self.connection.assert_not_called()

    def test_completed_gateway_record_with_newer_cli_run_scans_processes(self):
        session = self._binding()
        row = self._execution(session)
        self.conn.execute("UPDATE hermes_executions SET state='terminal',cleanup_pending=0 "
                          'WHERE execution_id=?', (row['execution_id'],))
        self.conn.execute('INSERT INTO wake_runs(session,started_at,runtime,profile,project) '
                          "VALUES(?,101,'hermes','hermes-a','p')", (session,))
        self.conn.commit()
        records = [(123, ['python.exe', 'hermes', '-p', 'hermes-a', 'chat', '-c', 'legacy-a'])]
        with patch.object(process_query, 'list_processes', return_value=records):
            self.assertEqual(wake.find_bot_processes('hermes-a', session), [123])
        self.connection.assert_not_called()

    def test_gateway_layout_transport_failure_stays_deferred(self):
        session = self._binding()
        self._gateway_layout()
        self.assertTrue(gateway.enabled())
        with patch.object(process_query, 'list_processes') as scan:
            self.assertIsNone(wake.find_bot_processes('hermes-a', session))
        scan.assert_not_called()
        self.connection.assert_called_once_with('hermes-a', read_only=True)
        self.assertEqual(gateway._native_cache[('legacy-a', 0)][0], 'error')

    def test_failed_topic_job_recovers_after_legacy_restart_with_same_binding_and_order(self):
        self._binding('chat', 7)
        self.conn.execute("INSERT INTO threads(id,project,title,creator,members,status,created_at,"
                          "last_active_at,turn_profile) VALUES(7,'p','t','user','[\"hermes-a\"]',"
                          "'open','now','now','hermes-a')")
        self.conn.commit()
        job = self._job('hermes-a', 'chat', 7)
        second = self._job('hermes-b')
        original_binding = dict(self.conn.execute('SELECT * FROM session_bindings').fetchone())
        original_topic = dict(self.conn.execute('SELECT * FROM threads WHERE id=7').fetchone())
        # Reproduce a persisted query failure before restarting against legacy CLI.
        self._gateway_layout()
        self.assertTrue(queue.process_one(self.conn))
        saved = self.conn.execute('SELECT * FROM wake_jobs WHERE id=?', (job['id'],)).fetchone()
        self.assertEqual(saved['status'], 'queued')
        self.assertIn('transport_error', saved['detail'])
        self.assertEqual(self.delivered, [('hermes-b', 'work', None)])
        (self.source / 'hermes_cli' / 'gateway_chat_startup.py').unlink()
        gateway._native_cache.clear()
        db.recover_on_startup(self.conn)
        self.connection.reset_mock()
        self.assertTrue(queue.process_one(self.conn))
        self.assertFalse(queue.process_one(self.conn))
        saved = self.conn.execute('SELECT * FROM wake_jobs WHERE id=?', (job['id'],)).fetchone()
        self.assertEqual(saved['status'], 'done')
        self.assertEqual(saved['enqueued_at'], job['enqueued_at'])
        self.assertLess(saved['id'], second['id'])
        self.assertEqual(saved['detail'], 'probe')
        self.assertEqual(self.delivered, [('hermes-b', 'work', None), ('hermes-a', 'chat', 7)])
        self.assertEqual(dict(self.conn.execute('SELECT * FROM session_bindings').fetchone()),
                         original_binding)
        current_topic = dict(self.conn.execute('SELECT * FROM threads WHERE id=7').fetchone())
        for key in ('turn_profile', 'turn_parent_id', 'round', 'members', 'status'):
            self.assertEqual(current_topic[key], original_topic[key])
        self.connection.assert_not_called()


if __name__ == '__main__':
    unittest.main()
