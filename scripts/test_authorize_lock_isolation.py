"""SQLite contention, authorization worker ownership and heartbeat races."""
import asyncio
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine import access, api, auth, db, hermes_gateway_ledger as ledger, hermes_session_auth as grants, wake
from scripts.unified_test_support import isolated_database


class AuthorizeIsolationTests(unittest.TestCase):
    def setUp(self):
        fixture = isolated_database()
        self.path = fixture.__enter__()
        self.addCleanup(fixture.__exit__, None, None, None)
        keys = patch.object(auth, 'signing_keys', return_value=(1, {1: b'lock-isolation-test'}))
        keys.start()
        self.addCleanup(keys.stop)
        self.connect = db.get_connection
        self.session = wake.session_name('default', 'work', None, 'alpha')
        self.started = time.time() - 10
        conn = self.connect()
        try:
            self.row = self.prepare(conn, self.started)
        finally:
            conn.close()
        context = dict(profile='default', project='alpha', thread_id=None,
                       session=self.session, wake_started_at=self.started)
        self.headers = {'X-Group-Profile': 'default', 'X-Group-Key': auth.execution_token('default', context)}
        self.app = FastAPI()
        self.app.include_router(api.router_api)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def prepare(self, conn, started):
        conn.execute("INSERT INTO wake_runs(session,started_at,project,profile,runtime_session_id) "
                     "VALUES(?,?,'alpha','default','sid')", (self.session, started))
        return ledger.prepare(conn, session=self.session, project='alpha', profile='default', kind='work',
            thread_id=None, task_id=None, job_id=None, event_floor=0, source='', python='[]',
            home=Path(self.path).parent, workdir=Path(self.path).parent, session_id='sid',
            profile_id='fixture', started_at=started)

    def use_session_credential(self):
        conn = self.connect()
        try:
            bot = db._ensure_hermes_bot(conn, 'default')
            conn.execute("INSERT INTO session_bindings(project,bot_id,profile,kind,thread_id,session_id,"
                         "runtime,generation,title,updated_at) VALUES('alpha',?,'default','work',0,"
                         "'sid','hermes',0,?,?)", (bot, self.session, db.now_iso()))
            conn.commit()
            grant = grants.grant(conn, project='alpha', profile='default', bot_id=bot, kind='work',
                thread_id=None, session=self.session, home=Path(self.path).parent,
                command=['fixture'], workdir=str(Path(self.path).parent), model_args=[])
            self.headers['X-Group-Key'] = auth.session_token('default', grants.context(grant))
        finally:
            conn.close()

    def post(self, **body):
        return self.client.post('/heartbeat', json={'pid': 77, **body}, headers=self.headers)

    def between_auth_and_endpoint(self, mutation):
        original = access._authorize_member
        def authorize(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get('connection') is None:
                conn = self.connect()
                try:
                    mutation(conn)
                    conn.commit()
                finally:
                    conn.close()
            return result
        return patch.object(access, '_authorize_member', side_effect=authorize)

    def test_auth_connection_open_queries_and_close_stay_in_one_worker(self):
        loop_thread = None
        records = []
        class ObservedConnection(sqlite3.Connection):
            def close(conn):
                records.append(('close', id(conn), threading.get_ident()))
                super().close()
        def connect():
            conn = sqlite3.connect(self.path, factory=ObservedConnection)
            conn.row_factory = sqlite3.Row
            records.append(('open', id(conn), threading.get_ident()))
            conn.set_trace_callback(lambda sql: records.append(('query', id(conn), threading.get_ident())))
            return conn
        async def check():
            nonlocal loop_thread
            loop_thread = threading.get_ident()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                         base_url='http://isolated.test') as client:
                response = await client.get('/commands', headers=self.headers)
                self.assertEqual(response.status_code, 200, response.text)
        with patch.object(db, 'get_connection', side_effect=connect):
            asyncio.run(check())
        self.assertTrue(records)
        for identity in {r[1] for r in records}:
            entries = [r for r in records if r[1] == identity]
            self.assertEqual(entries[0][0], 'open')
            self.assertEqual(entries[-1][0], 'close')
            self.assertEqual(len({r[2] for r in entries}), 1)
            self.assertNotEqual(entries[0][2], loop_thread)

    def test_denied_heartbeats_leave_pid_unchanged(self):
        for body, status in (({'profile': 'architect'}, 403), ({'thread_id': 999}, 403),
                             ({'pid': 0}, 422), ({'pid': True}, 422), ({'pid': '77'}, 422)):
            with self.subTest(body=body):
                self.assertEqual(self.post(**body).status_code, status)
        conn = self.connect()
        try:
            self.assertIsNone(conn.execute('SELECT pid FROM wake_runs WHERE session=?', (self.session,)).fetchone()[0])
        finally:
            conn.close()

    def test_authorize_performs_no_heartbeat_writes(self):
        statements = []
        original = access._authorize_member
        def checked(*args, **kwargs):
            conn = self.connect()
            conn.set_trace_callback(statements.append)
            try:
                result = original(*args, **kwargs, connection=conn)
                self.assertFalse(conn.in_transaction)
                self.assertFalse(any(sql.lstrip().upper().startswith(('UPDATE ', 'INSERT ', 'BEGIN '))
                                     for sql in statements))
                return result
            finally:
                conn.close()
        # Read only the dependency; the real endpoint is replaced after authorization.
        from fastapi import Depends
        app = FastAPI()
        @app.post('/heartbeat', dependencies=[Depends(access.authorize)])
        def endpoint():
            return {'ok': True}
        with patch.object(access, '_authorize_member', side_effect=checked), TestClient(app) as client:
            self.assertEqual(client.post('/heartbeat', headers=self.headers, json={'pid': 77}).status_code, 200)
        self.assertTrue(statements)

    def test_terminal_between_auth_and_write_rejects_and_releases_transaction(self):
        def terminal(conn):
            conn.execute("UPDATE hermes_executions SET state='terminal' WHERE execution_id=?", (self.row['execution_id'],))
            conn.execute('UPDATE wake_runs SET ended_at=1 WHERE session=?', (self.session,))
        with self.between_auth_and_endpoint(terminal), patch('engine.heartbeat.beat') as beat:
            self.assertEqual(self.post().status_code, 401)
            beat.assert_not_called()
        conn = self.connect()
        try:
            conn.execute('PRAGMA busy_timeout=0')
            conn.execute('BEGIN IMMEDIATE')
            self.assertIsNone(conn.execute('SELECT pid FROM wake_runs WHERE session=?', (self.session,)).fetchone()[0])
        finally:
            conn.close()

    def test_membership_revoked_between_auth_and_write(self):
        def revoke(conn):
            conn.execute("UPDATE project_members SET active=0 WHERE profile='default' AND project='alpha'")
        with self.between_auth_and_endpoint(revoke), patch('engine.heartbeat.beat') as beat:
            self.assertEqual(self.post().status_code, 403)
            beat.assert_not_called()

    def test_ended_wake_row_returns_idle_without_renewing_heartbeat(self):
        def end(conn):
            conn.execute('UPDATE wake_runs SET ended_at=1 WHERE session=?', (self.session,))
        with self.between_auth_and_endpoint(end), patch('engine.heartbeat.beat') as beat:
            response = self.post()
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()['idle'])
            beat.assert_not_called()

    def test_session_turnover_does_not_assign_previous_pid_to_new_execution(self):
        self.use_session_credential()
        def replace(conn):
            conn.execute("UPDATE hermes_executions SET state='terminal' WHERE execution_id=?", (self.row['execution_id'],))
            conn.execute('UPDATE wake_runs SET ended_at=1 WHERE session=?', (self.session,))
            self.prepare(conn, self.started + 1)
        with self.between_auth_and_endpoint(replace), patch('engine.heartbeat.beat') as beat:
            response = self.post()
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()['idle'])
            beat.assert_not_called()
        conn = self.connect()
        try:
            self.assertTrue(all(row[0] is None for row in conn.execute('SELECT pid FROM wake_runs')))
        finally:
            conn.close()

    def test_idle_session_heartbeat_stays_read_only_under_write_lock(self):
        self.use_session_credential()
        writer = self.connect()
        try:
            writer.execute("UPDATE hermes_executions SET state='terminal' WHERE execution_id=?", (self.row['execution_id'],))
            writer.commit()
            writer.execute('BEGIN IMMEDIATE')
            def limited():
                conn = self.connect()
                conn.execute('PRAGMA busy_timeout=50')
                return conn
            with patch.object(db, 'get_connection', side_effect=limited), patch('engine.heartbeat.beat') as beat:
                response = self.post()
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()['idle'])
                beat.assert_not_called()
            self.assertTrue(writer.in_transaction)
        finally:
            writer.close()

    def test_lock_timeout_is_503_and_subsequent_heartbeat_succeeds(self):
        writer = self.connect()
        def limited():
            conn = self.connect()
            conn.execute('PRAGMA busy_timeout=100')
            return conn
        try:
            writer.execute('BEGIN IMMEDIATE')
            with patch.object(db, 'get_connection', side_effect=limited), patch('engine.heartbeat.beat') as beat:
                response = self.post()
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.headers['retry-after'], '1')
                beat.assert_not_called()
            writer.rollback()
            self.assertEqual(self.post().status_code, 200)
        finally:
            writer.close()


class WalInitializationTests(unittest.TestCase):
    def test_file_in_memory_journal_mode_switches_to_wal_and_memory_database_stays_usable(self):
        with tempfile.TemporaryDirectory() as directory:
            for path, expected in ((str(Path(directory) / 'memory-journal.db'), 'wal'), (':memory:', 'memory')):
                with self.subTest(path=path):
                    conn = sqlite3.connect(path)
                    conn.row_factory = sqlite3.Row
                    try:
                        conn.execute('PRAGMA journal_mode=MEMORY')
                        db.init_schema(conn)
                        self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], expected)
                    finally:
                        conn.close()

    def test_each_database_and_replacement_initializes_once_and_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ('first.db', 'second.db', 'first.db'):
                path = Path(directory) / name
                if path.exists():
                    path.unlink()
                conn = sqlite3.connect(path)
                conn.row_factory = sqlite3.Row
                statements = []
                conn.set_trace_callback(statements.append)
                try:
                    db.init_schema(conn)
                    db.init_schema(conn)
                    self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
                    self.assertEqual(sum(sql.upper() == 'PRAGMA JOURNAL_MODE=WAL' for sql in statements), 1)
                    self.assertEqual(conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0], 0)
                finally:
                    conn.close()

    def test_failed_initialization_retries_same_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'retry.db'
            reader = sqlite3.connect(path)
            conn = sqlite3.connect(path, timeout=0)
            conn.row_factory = sqlite3.Row
            try:
                reader.execute('CREATE TABLE hold(value)')
                reader.execute('BEGIN')
                reader.execute('SELECT * FROM hold').fetchall()
                with self.assertRaises(sqlite3.OperationalError):
                    db.init_schema(conn)
                self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
                reader.rollback()
                db.init_schema(conn)
                self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            finally:
                reader.close()
                conn.close()

    def test_runtime_connection_keeps_per_connection_settings_without_wal_pragma(self):
        with isolated_database():
            with db.keep_wal_open() as keeper:
                statements = []
                connect = sqlite3.connect
                def traced(*args, **kwargs):
                    connection = connect(*args, **kwargs)
                    connection.set_trace_callback(statements.append)
                    return connection
                with patch.object(sqlite3, 'connect', side_effect=traced):
                    conn = db.get_connection()
                try:
                    self.assertFalse(any('JOURNAL_MODE' in sql.upper() for sql in statements))
                    self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
                    self.assertEqual(conn.execute('PRAGMA busy_timeout').fetchone()[0], 30000)
                    self.assertEqual(conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
                    conn.execute('BEGIN IMMEDIATE')
                    conn.execute("UPDATE members SET display_name='checkpoint' WHERE profile='default'")
                    conn.commit()
                    self.assertEqual(conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0], 0)
                    self.assertFalse(keeper.in_transaction)
                finally:
                    conn.close()


if __name__ == '__main__':
    unittest.main()
