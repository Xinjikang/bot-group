"""Queued state-query failures retain ownership and emit one durable notification."""
import json
import unittest
from unittest.mock import patch

from engine import hermes_gateway, queue, scheduling_diagnostics
from engine.hermes_gateway_client import GatewayRefusal
from scripts import test_native_busy_scheduling as fixtures


class SchedulingDiagnosticsTests(fixtures.NativeBusyFixtures, unittest.TestCase):
    setUp = fixtures.NativeBusySchedulingTests.setUp

    def test_failure_is_visible_alerts_once_and_recovers_same_job(self):
        self._bind('hermes-a', session_id='native-a')
        self.native_states['native-a'] = GatewayRefusal('storage_unavailable')
        job = self._job('hermes-a', 'work')
        self._job('hermes-b', 'work')
        clock = [100.0]
        with patch.object(hermes_gateway.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch('engine.execution_errors._secrets', return_value=[]):
            self.assertTrue(queue.process_one(self.conn))
            for now in (104, 111, 124, 149, 180, 181):
                clock[0] = now
                queue.process_one(self.conn)
            row = self.conn.execute('SELECT * FROM wake_jobs WHERE id=?', (job['id'],)).fetchone()
            self.assertEqual(row['status'], 'queued')
            detail = json.loads(row['detail'].removeprefix(scheduling_diagnostics.PREFIX))
            self.assertEqual(detail['state'], 'error')
            self.assertEqual(detail['reason'], 'storage_unavailable')
            errors = self.conn.execute('SELECT * FROM execution_errors').fetchall()
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]['category'], 'session_state')
            msg = self.conn.execute('SELECT content FROM messages WHERE id=?', (errors[0]['message_id'],)).fetchone()
            self.assertIn('storage_unavailable', msg['content'])
            self.assertEqual(self.delivered, [('hermes-b', 'work', None)])
            # A new process/cache still uses the persistent notification key.
            self._clear_native_cache()
            for now in (200, 204, 211, 224, 249, 280):
                clock[0] = now
                queue.process_one(self.conn)
            self.assertEqual(self.conn.execute('SELECT count(*) FROM execution_errors').fetchone()[0], 1)
            self.native_states['native-a'] = {'running': False, 'pending': False}
            clock[0] = 311
            self.assertTrue(queue.process_one(self.conn))
            row = self.conn.execute('SELECT * FROM wake_jobs WHERE id=?', (job['id'],)).fetchone()
            self.assertEqual(row['status'], 'done')
            self.assertFalse((row['detail'] or '').startswith(scheduling_diagnostics.PREFIX))

    def test_busy_has_no_query_error_and_generation_discards_old_failure(self):
        self._bind('hermes-a', session_id='native-a')
        self.native_states['native-a'] = GatewayRefusal('storage_unavailable')
        job = self._job('hermes-a', 'work')
        queue.process_one(self.conn)
        self.conn.execute("UPDATE session_bindings SET generation=1 WHERE profile='hermes-a'")
        self.conn.commit()
        self.native_states['native-a'] = {'running': True, 'pending': False}
        queue.process_one(self.conn)
        row = self.conn.execute('SELECT * FROM wake_jobs WHERE id=?', (job['id'],)).fetchone()
        self.assertEqual(row['status'], 'queued')
        self.assertIsNone(row['detail'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM execution_errors').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
