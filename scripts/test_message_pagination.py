"""Read-only API pagination against an isolated in-memory database."""
import sqlite3
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from engine import api, taskpool


class Connection(sqlite3.Connection):
    def close(self):
        pass


class PaginationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:', factory=Connection, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('CREATE TABLE attachments(message_id INTEGER, position INTEGER)')
        self.conn.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY, thread_id INTEGER)')
        self.conn.executemany('INSERT INTO messages VALUES (?, ?)', [(i, i % 2) for i in range(1, 251)])
        app = FastAPI()
        app.dependency_overrides[api._resolve_identity] = lambda: ('user', 'user')
        app.include_router(api.router_api)
        self.client = TestClient(app, client=('127.0.0.1', 4321))
        self.patcher = patch.object(api, '_conn', return_value=self.conn)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.client.close()
        sqlite3.Connection.close(self.conn)

    def test_latest_and_older_pages_with_concurrent_insert(self):
        first = self.client.get('/messages?limit=100').json()
        self.assertEqual([m['id'] for m in first['messages']], list(range(151, 251)))
        self.assertTrue(first['has_more'])
        self.conn.execute('INSERT INTO messages VALUES(251, 1)')
        older = self.client.get('/messages?limit=100&before_id=151').json()
        self.assertEqual([m['id'] for m in older['messages']], list(range(51, 151)))
        last = self.client.get('/messages?limit=100&before_id=51').json()
        self.assertEqual([m['id'] for m in last['messages']], list(range(1, 51)))
        self.assertFalse(last['has_more'])
        self.assertEqual(self.client.get('/messages?since_id=250').json()['messages'], [{'id': 251, 'thread_id': 1}])

    def test_legacy_thread_and_validation(self):
        self.assertEqual(self.client.get('/messages?thread_id=1').json()['count'], 125)
        self.assertEqual(self.client.get('/messages').json()['count'], 250)
        for limit in (0, -1, 501):
            self.assertEqual(self.client.get(f'/messages?limit={limit}').status_code, 422)
        self.assertFalse(self.client.get('/messages?limit=100&before_id=1').json()['has_more'])

    def test_topic_anchor_is_outside_latest_page(self):
        self.conn.execute('CREATE TABLE threads(id INTEGER PRIMARY KEY, status TEXT, hidden INTEGER)')
        self.conn.execute("INSERT INTO threads VALUES(1, 'closed', 0)")
        topic = self.client.get('/threads').json()['threads'][0]
        self.assertEqual(topic['first_message_id'], 1)
        page = self.client.get('/messages?limit=100').json()['messages']
        self.assertGreater(page[0]['id'], topic['first_message_id'])

    def test_project_filter_applies_before_pagination_and_to_updates(self):
        self.conn.execute("ALTER TABLE messages ADD COLUMN project TEXT DEFAULT 'xjk'")
        self.conn.executemany('INSERT INTO messages VALUES (?, 1, ?)', [(i, 'cicp') for i in range(251, 356)])
        first = self.client.get('/messages?project=cicp&limit=100').json()
        self.assertEqual([m['id'] for m in first['messages']], list(range(256, 356)))
        older = self.client.get('/messages?project=cicp&before_id=256&limit=100').json()
        self.assertEqual([m['id'] for m in older['messages']], list(range(251, 256)))
        self.assertFalse(older['has_more'])
        self.assertEqual(self.client.get('/messages?project=xjk&since_id=250').json()['messages'], [])
        self.assertEqual(self.client.get('/messages?project=empty&limit=100').json()['messages'], [])
        self.assertEqual(self.client.get('/messages?project=cicp&thread_id=0').json()['messages'], [])
        self.assertEqual(self.client.get('/messages?project=cicp&since_id=354').json()['count'], 1)

    def test_project_description_persists_on_create(self):
        self.conn.execute("CREATE TABLE projects(name TEXT PRIMARY KEY, root TEXT, description TEXT DEFAULT '', archived INTEGER, created_at TEXT, display_name TEXT, directory_mode TEXT, worktree_root TEXT)")
        self.conn.execute('CREATE TABLE project_config_events(id INTEGER PRIMARY KEY,project TEXT,actor TEXT,action TEXT,detail TEXT,created_at TEXT)')
        self.conn.commit()
        with patch.object(taskpool, '_conn', return_value=self.conn):
            response = self.client.post('/projects', json={'name': 'cicp', 'root': str(Path.cwd()), 'description': '  项目说明  '})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.conn.execute('SELECT description FROM projects').fetchone()[0], '项目说明')
            taskpool.project_add('cicp', 'E:/Code/cicp-new')
            self.assertEqual(self.conn.execute('SELECT description FROM projects').fetchone()[0], '项目说明')


if __name__ == '__main__':
    unittest.main()
