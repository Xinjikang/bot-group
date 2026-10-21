"""Thinking heartbeats identify a recorded execution, never the latest profile wake."""
import unittest
from unittest.mock import patch
from engine import api, db, heartbeat, wake
from scripts import test_abnormal_exit as fixtures


class ThinkingRunIdentityTests(unittest.TestCase):
    setUp = fixtures.AbnormalExitTests.setUp

    def test_spawn_snapshot_precedes_first_heartbeat(self):
        run = dict(profile='dev-1', session='dev-1-p-work', project='p',
                   thread_id=None, wake_started_at=100, spawned_at='2026-09-22T10:00:00')
        with patch.object(api, '_conn', side_effect=db.get_connection), \
                patch.object(heartbeat, 'active_entries', return_value=[]), \
                patch.object(wake, '_running_wakes', {run['session']: run}):
            result = api.get_thinking()
            self.assertEqual(result['thinking'], [])
            self.assertEqual(result['runs'], [run])

    def thinking(self, entries):
        with patch.object(api, '_conn', side_effect=db.get_connection), \
                patch.object(heartbeat, 'active_entries', return_value=entries):
            return api.get_thinking()['thinking']

    def test_two_projects_and_closed_previous_run(self):
        self.conn.executemany('INSERT INTO wake_runs(session,started_at,pid,ended_at) VALUES(?,?,?,?)', [
            ('dev-1-p-work', 100, 10, 101),
            ('dev-1-p-work', 102, 11, None),
            ('dev-1-q-work', 103, 12, None),
        ])
        self.conn.commit()
        rows = self.thinking([{'profile': 'dev-1', 'thread_id': None, 'pid': pid} for pid in (10, 11, 12)])
        self.assertNotIn('session', rows[0])
        self.assertEqual((rows[1]['session'], rows[1]['wake_started_at']), ('dev-1-p-work', 102))
        self.assertEqual((rows[2]['session'], rows[2]['wake_started_at']), ('dev-1-q-work', 103))

    def test_ambiguous_or_wrong_profile_or_wrong_slot_remains_unassociated(self):
        self.conn.executemany('INSERT INTO wake_runs(session,started_at,pid) VALUES(?,?,?)', [
            ('dev-1-p-work', 100, 10), ('dev-1-q-work', 101, 10),
            ('dev-2-p-work', 100, 11), ('dev-1-p-chat-42', 100, 12),
        ])
        self.conn.commit()
        rows = self.thinking([{'profile': 'dev-1', 'thread_id': None, 'pid': pid} for pid in (10, 11, 12, 999)])
        self.assertTrue(all('session' not in row for row in rows))
        chat = self.thinking([{'profile': 'dev-1', 'thread_id': 42, 'pid': 12}])[0]
        self.assertEqual(chat['session'], 'dev-1-p-chat-42')

    def test_real_heartbeat_slot_exposes_pid_to_run_lookup(self):
        self.conn.execute('INSERT INTO wake_runs(session,started_at,pid) VALUES(?,?,?)',
                          ('dev-1-p-work', 100, 10))
        self.conn.commit()
        with patch.object(heartbeat, '_beats', {}), patch.object(api, '_conn', side_effect=db.get_connection):
            heartbeat.beat('dev-1', pid=10)
            self.assertEqual(heartbeat.active_entries(), [{'profile': 'dev-1', 'thread_id': None}])
            self.assertEqual(api.get_thinking()['thinking'][0]['wake_started_at'], 100)

    def test_project_with_dash_reads_verbatim(self):
        """req#72 中危：project 带横杠（bot-group）时直读 wake_runs.project，不切段。"""
        self.conn.execute('INSERT INTO wake_runs(session,started_at,pid,project) VALUES(?,?,?,?)',
                          ('dev-1-bot-group-work', 100, 10, 'bot-group'))
        self.conn.commit()
        rows = self.thinking([{'profile': 'dev-1', 'thread_id': None, 'pid': 10}])
        self.assertEqual(rows[0]['project'], 'bot-group')


if __name__ == '__main__':
    unittest.main()
