"""Database persistence, identity boundaries and validation for the user's display name."""
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from engine import api, auth, db


class UserNameAPI:
    def __enter__(self):
        self.stack = ExitStack()
        directory = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='user_name_'))
        self.path = str(Path(directory) / 'group.db')
        self.stack.enter_context(patch.dict('os.environ', {'GROUP_DB': self.path}))
        self.stack.enter_context(patch.object(api.auth, '_password', return_value=''))
        conn = db.get_connection()
        try:
            db.init_schema(conn)
            conn.execute("INSERT INTO projects(name,root,created_at) VALUES('p',?,'now')", (directory,))
            conn.execute("INSERT INTO projects(name,root,created_at) VALUES('q',?,'now')", (directory,))
            conn.execute("INSERT INTO members(profile,display_name,role) VALUES('bot-a','用户','developer')")
            conn.execute("INSERT INTO messages(project,ts,sender,sender_type,content,mentions) "
                         "VALUES('p','2026-10-20T10:00:00','user','user','Sample message','[\"user\"]')")
            conn.commit()
            db.init_schema(conn)
        finally:
            conn.close()
        self.app = FastAPI()
        self.app.include_router(api.router_api)
        self.client = self.stack.enter_context(TestClient(self.app, client=('127.0.0.1', 4321)))
        return self

    def __exit__(self, *args):
        self.stack.__exit__(*args)


class UserDisplayNameTests(unittest.TestCase):
    def setUp(self):
        self.fixture = UserNameAPI()
        self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        self.client = self.fixture.client
        self.url = '/user/display-name'

    def test_default_and_persistence_across_connections_and_clients(self):
        initial = self.client.get(self.url)
        self.assertEqual(initial.json(), {'profile': 'user', 'display_name': '用户'})
        self.assertIn('no-store', initial.headers['cache-control'])
        response = self.client.patch(self.url, json={'display_name': '  Shared name 🌟  '})
        self.assertEqual(response.status_code, 200, response.text)
        conn = db.get_connection()
        try:
            db.init_schema(conn)
            self.assertEqual(conn.execute("SELECT display_name FROM bots WHERE profile='user'").fetchone()[0],
                             'Shared name 🌟')
        finally:
            conn.close()
        with TestClient(self.fixture.app, client=('127.0.0.1', 4321)) as fresh_client:
            self.assertEqual(fresh_client.get(self.url).json()['display_name'], 'Shared name 🌟')

    def test_legacy_names_survive_schema_initialization(self):
        conn = db.get_connection()
        try:
            for name in ('主人', 'Existing custom name'):
                conn.execute("UPDATE bots SET display_name=? WHERE profile='user'", (name,))
                conn.commit()
                db.init_schema(conn)
                self.assertEqual(self.client.get(self.url).json()['display_name'], name)
            conn.execute("DELETE FROM bots WHERE profile='user'")
            conn.execute("INSERT INTO members(profile,display_name,role) VALUES('user','Legacy name','')")
            conn.commit()
            db.init_schema(conn)
            self.assertEqual(self.client.get(self.url).json()['display_name'], 'Legacy name')
        finally:
            conn.close()

    def test_only_display_fields_change_and_routing_remains_user(self):
        conn = db.get_connection()
        try:
            tables = ('bots', 'members', 'messages', 'project_members', 'project_permissions', 'wake_jobs')
            before = {table: [dict(row) for row in conn.execute(f'SELECT * FROM {table}')] for table in tables}
            response = self.client.patch(self.url, json={'display_name': 'bot-a'})
            self.assertEqual(response.status_code, 200)
            after = {table: [dict(row) for row in conn.execute(f'SELECT * FROM {table}')] for table in tables}
            for row in before['bots']:
                if row['profile'] == 'user':
                    row['display_name'] = 'bot-a'
            self.assertEqual(before, after)
            self.assertTrue(db.is_builtin_user('user'))
            self.assertFalse(db.is_builtin_user('bot-a'))
            with patch.object(api.auth, 'verify_token', return_value=True):
                response = self.client.post('/msg', headers={'Authorization': 'Bearer test'},
                    json={'sender': 'bot-a', 'text': 'Routing check @user', 'project': 'p'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(conn.execute('SELECT sender FROM messages ORDER BY id DESC LIMIT 1').fetchone()[0], 'user')
        finally:
            conn.close()

    def test_invalid_values_and_fields_leave_database_unchanged(self):
        for value in ('', ' ', 'x\ny', 'x\ty', 'x\0y', 'x\u2028y', 'x' * 201, '🌟' * 101, 7, None):
            with self.subTest(value=repr(value)):
                self.assertEqual(self.client.patch(self.url, json={'display_name': value}).status_code, 400)
        self.assertEqual(self.client.patch(self.url, json={'display_name': 'name', 'profile': 'bot-a'}).status_code, 400)
        self.assertEqual(self.client.get(self.url).json()['display_name'], '用户')
        self.assertEqual(self.client.patch(self.url, json={'display_name': '🌟' * 100}).status_code, 200)

    def test_user_authentication_and_bot_denial(self):
        for profile in ('bot-a', 'user'):
            headers = {'X-Group-Profile': profile, 'X-Group-Key': auth.bot_token(profile)}
            expected = 401 if profile == 'user' else 403
            self.assertEqual(self.client.get(self.url, headers=headers).status_code, expected)
            self.assertEqual(self.client.patch(self.url, headers=headers, json={'display_name': 'No'}).status_code, expected)
        with patch.object(api.auth, '_password', return_value='configured'), \
                patch.object(api.auth, 'verify_token', side_effect=lambda token: token == 'valid'):
            self.assertEqual(self.client.get(self.url).status_code, 401)
            self.assertEqual(self.client.patch(self.url, json={'display_name': 'No'}).status_code, 401)
            self.assertEqual(self.client.get(self.url, headers={'Authorization': 'Bearer invalid'}).status_code, 401)
            self.assertEqual(self.client.patch(self.url, headers={'Authorization': 'Bearer valid'},
                                              json={'display_name': 'Allowed'}).status_code, 200)
            self.assertEqual(self.client.patch(self.url, headers={'Authorization': 'Bearer valid', 'X-Group-Profile': 'user'},
                                              json={'display_name': 'No'}).status_code, 401)
        self.assertEqual(self.client.get(self.url).json()['display_name'], 'Allowed')


if __name__ == '__main__':
    unittest.main()
