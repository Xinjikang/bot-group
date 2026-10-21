"""Control authorization and task terminal-state races using isolated SQLite."""
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine import api, auth, db, stop, taskpool
from scripts.test_task_brief import BRIEF
from scripts.unified_test_support import isolated_database, confirmed_requirement_fields


class ControlAuthorizationTests(unittest.TestCase):
    def test_control_routes_enforce_admin_identity_before_effects(self):
        with isolated_database() as database:
            (Path(database).parent / 'alpha').mkdir()
            app = FastAPI()
            app.include_router(api.router_api)
            with TestClient(app, client=('127.0.0.1', 4321)) as client, patch.object(stop, 'stop_profile', return_value={'stopped': True}) as stopped:
                for password in ('', 'isolated-test-password'):
                    with self.subTest(password_enabled=bool(password)), patch.dict(os.environ, {'GROUP_ADMIN_PASSWORD': password}):
                        token = auth.login(password).get('token', '')
                        bot = {'X-Group-Profile': 'dev', 'X-Group-Key': auth.bot_token('dev')}
                        denied = [(bot, 403), ({**bot, 'Authorization': 'Bearer ' + token}, 403)]
                        if password:
                            denied += [({}, 401), ({'Authorization': 'Bearer invalid'}, 401)]
                        with closing(db.get_connection()) as conn:
                            before = list(conn.iterdump())
                        for headers, expected in denied:
                            for url, body in (('/stop', {'project': 'alpha', 'profile': 'dev'}),
                                              ('/projects/alpha/archive', {'archived': True})):
                                response = client.post(url, json=body, headers=headers)
                                self.assertEqual(response.status_code, expected, response.text)
                        stopped.assert_not_called()
                        with closing(db.get_connection()) as conn:
                            self.assertEqual(list(conn.iterdump()), before)
                        headers = {'Authorization': 'Bearer ' + token} if password else {}
                        response = client.post('/stop', json={'project': 'alpha', 'profile': 'dev'}, headers=headers)
                        self.assertEqual(response.status_code, 200, response.text)
                        stopped.assert_called_once_with('dev', 'alpha', None)
                        stopped.reset_mock()
                        for archived in (True, False):
                            response = client.post('/projects/alpha/archive', json={'archived': archived}, headers=headers)
                            self.assertEqual(response.status_code, 200, response.text)
                            with closing(db.get_connection()) as conn:
                                self.assertEqual(conn.execute("SELECT archived FROM projects WHERE name='alpha'").fetchone()[0], archived)


class TaskControlRaceTests(unittest.TestCase):
    def assert_terminal_wins(self, operation, terminal):
        with isolated_database():
            rid = taskpool.req_create('alpha', 'default', 'race', **confirmed_requirement_fields())['requirement_id']
            tid = taskpool.task_create('alpha', 'architect', rid, [{'title': 'work', 'next_bot': 'dev', **BRIEF}])['task_ids'][0]
            self.assertTrue(taskpool.task_claim('alpha', 'dev')['claimed'])
            read, release, writing, committed = (threading.Event() for _ in range(4))
            original = taskpool._conn

            class Cursor:
                def __init__(self, cursor):
                    self.cursor = cursor

                def fetchone(self):
                    row = self.cursor.fetchone()
                    read.set()
                    if not release.wait(5):
                        raise RuntimeError('task reader was not released')
                    return row

            class Connection:
                def __init__(self, connection):
                    self.connection = connection

                def __getattr__(self, name):
                    return getattr(self.connection, name)

                def execute(self, sql, *args):
                    cursor = self.connection.execute(sql, *args)
                    if sql.startswith('SELECT * FROM tasks WHERE id=? AND project=?'):
                        return Cursor(cursor)
                    return cursor

            def terminal_write():
                # Independent writer avoids init_schema accidentally serializing the test.
                with closing(db.get_connection()) as conn:
                    writing.set()
                    conn.execute("UPDATE tasks SET status=?,next_bot='',claimed_by='' WHERE id=?", (terminal, tid))
                    conn.execute('UPDATE requirements SET status=? WHERE id=?', (terminal, rid))
                    conn.commit()
                    committed.set()

            def invoke():
                if operation == 'task_block':
                    return taskpool.task_block('alpha', 'dev', tid, 'late failure')
                return getattr(taskpool, operation)('alpha', tid)

            with patch.object(taskpool, '_conn', side_effect=lambda: Connection(original())), ThreadPoolExecutor(max_workers=2) as pool:
                pending = pool.submit(invoke)
                try:
                    self.assertTrue(read.wait(5))
                    writer = pool.submit(terminal_write)
                    self.assertTrue(writing.wait(5))
                    self.assertFalse(committed.wait(0.2), 'terminal writer overtook ownership validation')
                finally:
                    release.set()
                self.assertNotIn('error', pending.result(timeout=5))
                writer.result(timeout=5)
            with closing(db.get_connection()) as conn:
                self.assertEqual(tuple(conn.execute('SELECT status,claimed_by,next_bot FROM tasks WHERE id=?', (tid,)).fetchone()),
                                 (terminal, '', ''))
                self.assertEqual(conn.execute('SELECT status FROM requirements WHERE id=?', (rid,)).fetchone()[0], terminal)
                before = list(conn.iterdump())
            self.assertIn('error', invoke())
            with closing(db.get_connection()) as conn:
                self.assertEqual(list(conn.iterdump()), before)

    def test_bot_block_serializes_with_cancellation(self):
        self.assert_terminal_wins('task_block', 'cancelled')

    def test_admin_block_serializes_with_cancellation(self):
        self.assert_terminal_wins('admin_task_block', 'cancelled')

    def test_admin_cancel_serializes_with_completion(self):
        self.assert_terminal_wins('admin_task_cancel', 'done')


if __name__ == '__main__':
    unittest.main()
