"""Thinking heartbeats identify a recorded execution, never the latest profile wake."""
import json
import time
import unittest
from unittest.mock import Mock, patch
from engine import api, db, events, heartbeat, hermes_gateway, wake
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

    def test_http_snapshot_with_200_bindings_keeps_gateway_out_of_request(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        self.conn.executemany(
            "INSERT INTO session_bindings(project,bot_id,profile,kind,thread_id,session_id,"
            "runtime,generation,title,updated_at) VALUES('p',0,'dev-2','chat',?,?,'hermes',0,'',?)",
            [(index, f'native-{index}', db.now_iso()) for index in range(1, 201)])
        self.conn.commit()
        run = dict(profile='dev-1', session='dev-1-p-work', project='p',
                   thread_id=None, wake_started_at=100, spawned_at='2026-09-22T10:00:00')
        app = FastAPI()
        app.include_router(api.router_api)
        app.dependency_overrides[api.authorize] = lambda: None
        latencies = []
        with patch.object(heartbeat, 'active_entries', return_value=[]), \
                patch.object(wake, '_running_wakes', {run['session']: run}), \
                patch.object(events.bus, 'publish') as publish, TestClient(app) as client:
            for snapshot in ({'running': True, 'pending': []},
                             {'running': False, 'pending': [{'status': 'queued'}]}):
                with self.subTest(snapshot=snapshot):
                    lease = Mock()
                    lease.rpc.return_value = snapshot
                    with patch.object(hermes_gateway, '_native_cache', {}), \
                            patch.object(hermes_gateway, 'connection',
                                         return_value=(lease, None, [])) as connection:
                        for _ in range(10):
                            started = time.perf_counter()
                            response = client.get('/thinking')
                            latencies.append((time.perf_counter() - started) * 1000)
                            self.assertEqual(response.status_code, 200)
                            self.assertEqual(response.json(), {'thinking': [], 'runs': [run]})
                        connection.assert_not_called()
            publish.assert_not_called()
        print(json.dumps({'thinking_snapshot': {'bindings': 200, 'requests': len(latencies),
            'gateway_calls': 0, 'p95_ms': round(sorted(latencies)[18], 3),
            'max_ms': round(max(latencies), 3)}}), flush=True)


if __name__ == '__main__':
    unittest.main()
