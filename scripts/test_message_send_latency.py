"""Atomic message/wake persistence and content-free send timing diagnostics."""
import sqlite3
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from engine import api, auth, db, events, router, wake
from scripts.unified_test_support import isolated_database


class MessageSendTests(unittest.TestCase):
    def setUp(self):
        fixture = isolated_database()
        fixture.__enter__()
        self.addCleanup(fixture.__exit__, None, None, None)
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        self.observer = db.get_connection()
        self.addCleanup(self.observer.close)
        self.initial = self.counts(self.observer)

    @staticmethod
    def counts(conn):
        return tuple(conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                     for table in ('messages', 'inbox', 'wake_jobs'))

    def test_message_inbox_and_multiple_wakes_commit_once(self):
        enqueue = wake.enqueue
        commits = []

        class Connection:
            def __getattr__(_, key):
                return getattr(self.conn, key)

            def commit(_):
                commits.append(True)
                self.conn.commit()

        def observe(conn, *args, **kwargs):
            self.assertTrue(conn.in_transaction)
            self.assertFalse(kwargs['commit'])
            result = enqueue(conn, *args, **kwargs)
            self.assertEqual(self.counts(self.observer), self.initial)
            return result

        with patch.object(wake, 'enqueue', side_effect=observe):
            result = router.route_message(Connection(), 'user', '@default @architect update', 'user', None, 'alpha')
        self.assertEqual(len(commits), 1)
        self.assertEqual(len(result['wake']), 2)
        self.assertEqual(self.counts(self.observer), tuple(a+b for a, b in zip(self.initial, (1, 2, 2))))

    def test_enqueue_error_rolls_back_whole_http_send(self):
        app = FastAPI()
        app.include_router(api.router_api)
        enqueue = wake.enqueue
        def fail(conn, *args, **kwargs):
            enqueue(conn, *args, **kwargs)
            raise sqlite3.OperationalError('isolated enqueue failure')
        with TestClient(app, client=('127.0.0.1', 0)) as client, \
             patch.object(auth, 'configured', return_value=False), \
             patch.object(wake, 'enqueue', side_effect=fail), patch.object(events.bus, 'publish') as publish:
            with self.assertRaises(sqlite3.OperationalError):
                client.post('/msg', json={'project': 'alpha', 'sender': 'user', 'text': '@default update'})
            publish.assert_not_called()
        self.assertEqual(self.counts(self.observer), self.initial)

    def test_dedup_retains_both_messages_and_one_wake(self):
        first = router.route_message(self.conn, 'user', '@default first', 'user', None, 'alpha')
        second = router.route_message(self.conn, 'user', '@default second', 'user', None, 'alpha')
        self.assertEqual(len(first['wake']), 1)
        self.assertEqual(second['wake'], [])
        self.assertEqual(self.counts(self.observer), tuple(a+b for a, b in zip(self.initial, (2, 2, 1))))

    def test_timing_logs_exclude_message_content(self):
        app = FastAPI()
        app.include_router(api.router_api)
        with TestClient(app, client=('127.0.0.1', 0)) as client, \
             patch.object(auth, 'configured', return_value=False), patch.object(events.bus, 'publish'), \
             self.assertLogs('engine', level='INFO') as captured:
            response = client.post('/msg', json={'project': 'alpha', 'sender': 'user',
                                                 'text': '@default PRIVATE-SEND-CONTENT'})
        self.assertEqual(response.status_code, 200, response.text)
        lines = '\n'.join(captured.output)
        self.assertIn('message.persist', lines)
        self.assertIn('message.send', lines)
        self.assertIn('commit_ms=', lines)
        self.assertIn('outcome=ok', lines)
        self.assertIn(f'message_id={response.json()["message_id"]}', lines)
        self.assertNotIn('PRIVATE-SEND-CONTENT', lines)


if __name__ == '__main__':
    unittest.main()
