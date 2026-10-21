"""Task history uses a read-only, project-scoped projection of nodes and events."""
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine import api


class Connection(sqlite3.Connection):
    def close(self):
        pass


class TaskHistoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:', factory=Connection, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(Path('engine/schema.sql').read_text(encoding='utf-8'))
        self.conn.execute("INSERT INTO requirements(id,project,title,created_at,updated_at) "
                          "VALUES(1,'alpha','Requirement','2026-09-23T10:00:00','2026-09-23T10:00:00')")
        self.conn.execute("INSERT INTO tasks(id,project,requirement_id,title,status,created_at) "
                          "VALUES(1,'alpha',1,'Task','done','2026-09-23T10:00:00')")
        self.conn.execute("INSERT INTO nodes(id,project,task_id,actor,type,comment,result,meta,ts) "
                          "VALUES(1,'alpha',1,'architect','decompose','','','{}','2026-09-23T10:00:00')")
        self.conn.execute("INSERT INTO nodes(id,project,task_id,actor,type,comment,result,meta,ts) "
                          "VALUES(2,'alpha',1,'dev','dev','Delivered','pass','{\"commit\":\"abc123\"}','2026-09-23T10:02:00')")
        self.conn.execute("INSERT INTO nodes(id,project,task_id,actor,type,comment,result,meta,ts) "
                          "VALUES(3,'alpha',1,'default','accept','Closed','pass','{}','2026-09-23T10:02:00')")
        events = [
            (1, 'alpha', 1, None, 'dev', 'claim', '', '2026-09-23T10:00:00'),
            (2, 'alpha', 1, None, 'dev', 'execution.failed',
             json.dumps({'failure': 1, 'error': 'process exit=1'}), '2026-09-23T10:01:00'),
            (3, 'alpha', 1, 2, 'dev', 'node', 'dev', '2026-09-23T10:02:00'),
            (4, 'alpha', 1, 3, 'default', 'complete', 'accept', '2026-09-23T10:02:00'),
            (5, 'beta', 1, None, 'dev', 'block', 'other project secret', '2026-09-23T10:03:00'),
            (6, 'alpha', 999, None, 'dev', 'block', 'other task secret', '2026-09-23T10:03:00'),
        ]
        self.conn.executemany("INSERT INTO events(id,project,task_id,node_id,actor,event_type,detail,ts) "
                              "VALUES(?,?,?,?,?,?,?,?)", events)
        # Even malformed legacy cross-project node links must not enter this task's history.
        self.conn.execute("INSERT INTO nodes(project,task_id,actor,type,comment,ts) "
                          "VALUES('beta',1,'dev','review','other project secret','2026-09-23T10:03:00')")
        self.conn.commit()
        app = FastAPI()
        app.include_router(api.router_api)
        self.client = TestClient(app, client=('127.0.0.1', 4321))
        self.patcher = patch.object(api, '_conn', return_value=self.conn)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.client.close()
        sqlite3.Connection.close(self.conn)

    def test_complete_task_retains_failure_and_deduplicates_workflow(self):
        before = self.conn.total_changes
        response = self.client.get('/tasks/1/history?project=alpha')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['task']['status'], 'done')
        self.assertEqual([e['type'] for e in data['history']],
                         ['decompose', 'claim', 'execution.failed', 'dev', 'complete'])
        self.assertEqual(data['history'][3]['detail'], 'Delivered')
        self.assertEqual(json.loads(data['history'][3]['meta']), {'commit': 'abc123'})
        self.assertEqual(json.loads(data['history'][2]['detail'])['error'], 'process exit=1')
        self.assertNotIn('secret', response.text)
        self.assertEqual(self.conn.total_changes, before)

    def test_required_project_and_missing_task(self):
        for url, status in [('/tasks/1/history', 422), ('/tasks/1/history?project=', 422),
                            ('/tasks/1/history?project=beta', 404),
                            ('/tasks/999/history?project=alpha', 404)]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, status)

    def test_block_and_resume_reasons_remain_in_order(self):
        self.conn.execute("UPDATE tasks SET status='blocked' WHERE id=1")
        self.conn.executemany("INSERT INTO events(project,task_id,actor,event_type,detail,ts) "
                              "VALUES('alpha',1,'user',?,?,?)", [
                                  ('block', 'manual: missing credential', '2026-09-23T10:04:00'),
                                  ('resume', 'resume -> dev', '2026-09-23T10:05:00'),
                              ])
        data = self.client.get('/tasks/1/history?project=alpha').json()
        self.assertEqual(data['history'][-2]['detail'], 'manual: missing credential')
        self.assertEqual(data['history'][-1]['type'], 'resume')

    def test_legacy_task_without_logs(self):
        self.conn.execute('DELETE FROM nodes')
        self.conn.execute('DELETE FROM events')
        response = self.client.get('/tasks/1/history?project=alpha')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['history'], [])


if __name__ == '__main__':
    unittest.main()
