"""Runtime HTTP traffic and workers use the schema initialized at startup."""
import os
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine import api, auth, db, queue, scanner, wake
from scripts.unified_test_support import isolated_database, REQUIREMENT_FIELDS


class RuntimeSchemaTests(unittest.TestCase):
    def setUp(self):
        fixture = isolated_database()
        fixture.__enter__()
        self.addCleanup(fixture.__exit__, None, None, None)
        env = patch.dict(os.environ, {'GROUP_ADMIN_PASSWORD': 'isolated-password'})
        env.start()
        self.addCleanup(env.stop)
        auth._LOGIN_ATTEMPTS.clear()
        self.admin = {'Authorization': 'Bearer ' + auth.login('isolated-password')['token']}
        self.bot = {'X-Group-Profile': 'default', 'X-Group-Key': auth.bot_token('default')}
        app = FastAPI()
        app.include_router(api.router_api)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        conn = db.get_connection()
        try:
            self.topic = conn.execute(
                "INSERT INTO threads(project,title,creator,members,status,round,turn_profile,created_at,last_active_at) "
                "VALUES('alpha','Busy topic','user','[\"user\",\"default\"]','open',1,'default',?,?)",
                (db.now_iso(), db.now_iso())).lastrowid
            conn.commit()
        finally:
            conn.close()
        self.connect = db.get_connection

    def runtime_connection(self):
        conn = self.connect()
        from engine.operation_transaction import Lease
        if isinstance(conn, Lease):
            return conn
        conn.execute('PRAGMA busy_timeout=250')
        # A deterministic guard against schema writes in actual request paths.
        ddl = {sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX,
               sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_DROP_TABLE,
               sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_DROP_TRIGGER,
               sqlite3.SQLITE_ALTER_TABLE}
        conn.set_authorizer(lambda action, *args: sqlite3.SQLITE_DENY if action in ddl else sqlite3.SQLITE_OK)
        return conn

    def test_reads_complete_while_another_connection_holds_write_lock(self):
        writer = self.connect()
        try:
            writer.execute('BEGIN IMMEDIATE')
            writer.execute("UPDATE threads SET title='Uncommitted title' WHERE id=?", (self.topic,))
            with patch.object(db, 'get_connection', side_effect=self.runtime_connection):
                for path in (f'/messages?thread_id={self.topic}', '/user/display-name', '/tasks'):
                    with self.subTest(path=path):
                        response = self.client.get(path, headers=self.admin)
                        self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(writer.in_transaction)
        finally:
            writer.rollback()
            writer.close()

    def test_busy_topic_accepts_messages_during_concurrent_polling(self):
        session = wake.session_name('default', 'chat', self.topic, 'alpha')
        conn = self.connect()
        version = conn.execute('PRAGMA schema_version').fetchone()[0]
        conn.close()

        def poll():
            for _ in range(12):
                response = self.client.get(f'/messages?thread_id={self.topic}', headers=self.admin)
                self.assertEqual(response.status_code, 200, response.text)

        with patch.object(wake, '_running_sessions', {session}), \
             patch.object(db, 'get_connection', side_effect=self.runtime_connection), \
             ThreadPoolExecutor(max_workers=3) as pool:
            readers = [pool.submit(poll) for _ in range(2)]
            ids = []
            for i in range(8):
                self.assertTrue(wake.session_busy('default', 'chat', self.topic, 'alpha'))
                response = self.client.post('/msg', headers=self.admin, json={
                    'sender': 'user', 'project': 'alpha', 'thread_id': self.topic, 'text': f'Update {i}'})
                self.assertEqual(response.status_code, 200, response.text)
                ids.append(response.json()['message_id'])
            for reader in readers:
                reader.result(timeout=5)
        self.assertEqual(len(set(ids)), 8)
        conn = self.connect()
        try:
            self.assertEqual(conn.execute('PRAGMA schema_version').fetchone()[0], version)
            self.assertEqual(conn.execute('SELECT count(*) FROM messages WHERE thread_id=?',
                                          (self.topic,)).fetchone()[0], 8)
            self.assertEqual(conn.execute('SELECT turn_profile FROM threads WHERE id=?',
                                          (self.topic,)).fetchone()[0], 'default')
        finally:
            conn.close()

    def test_mcp_write_and_replay_use_initialized_schema(self):
        headers = {**self.bot, 'X-Group-Operation-Id': 'a' * 32}
        body = {'arguments': {'project': 'alpha', 'title': 'Schema boundary', **REQUIREMENT_FIELDS}}
        with patch.object(db, 'get_connection', side_effect=self.runtime_connection):
            first = self.client.post('/task-tools/req_create', headers=headers, json=body)
            second = self.client.post('/task-tools/req_create', headers=headers, json=body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json(), second.json())

    def test_background_ticks_keep_schema_version(self):
        conn = self.connect()
        version = conn.execute('PRAGMA schema_version').fetchone()[0]
        conn.close()
        # A stop event ends each worker after its first actual idle iteration.
        for module in (queue, scanner):
            with self.subTest(worker=module.__name__):
                stopped = threading.Event()
                def stop_after_tick(*args):
                    stopped.set()
                    return True
                with patch.object(stopped, 'wait', side_effect=stop_after_tick), \
                     patch.object(db, 'get_connection', side_effect=self.runtime_connection), \
                     patch.object(module.logger, 'exception') as errors:
                    module.run_forever(stopped)
                    errors.assert_not_called()
        conn = self.connect()
        try:
            self.assertEqual(conn.execute('PRAGMA schema_version').fetchone()[0], version)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
