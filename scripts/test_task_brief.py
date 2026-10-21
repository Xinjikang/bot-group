"""Publication gates and full handoff context, isolated SQLite only."""
import json
import unittest
from scripts.unified_test_support import REQUIREMENT_FIELDS, confirmed_requirement_fields

from unittest.mock import patch
from engine import db, taskpool, scanner, task_retry, queue, wake
from scripts.unified_test_support import isolated_database


BRIEF = dict(description='Implement the agreed API and preserve project isolation.',
             design_basis='doc/design.md section 2, approved revision abc123',
             acceptance_criteria='Cross-project reads fail; complete round trip passes.', prerequisites=[])


class TaskBriefTests(unittest.TestCase):
    def setUp(self):
        self.fixture = isolated_database()
        self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        self.rid = taskpool.req_create('alpha', 'default', 'Context', version='v2', description='R' * 12000, **confirmed_requirement_fields('alpha'))['requirement_id']

    def create(self, **values):
        return taskpool.task_create('alpha', 'architect', self.rid,
                                    [dict(title='Implement', next_bot='dev', **values)],
                                    credential='ctx')

    def test_completion_cannot_bypass_dependencies_and_has_no_rejected_side_effects(self):
        first = self.create(**BRIEF)['task_ids'][0]
        second = self.create(**{**BRIEF, 'prerequisites': [first]})['task_ids'][0]
        third = self.create(**{**BRIEF, 'prerequisites': [second]})['task_ids'][0]
        taskpool.task_update('alpha', 'architect', second, next_bot='architect', credential='ctx')
        conn = db.get_connection()
        try:
            def snapshot():
                return {table: [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY id')]
                        for table in ('tasks', 'requirements', 'nodes', 'events')}
            for status in ('pending', 'in_progress', 'blocked', 'cancelled'):
                with self.subTest(status=status):
                    conn.execute('UPDATE tasks SET status=? WHERE id=?', (status, first))
                    conn.commit()
                    before = snapshot()
                    result = taskpool.task_complete('alpha', 'architect', second)
                    self.assertEqual(result['code'], 'task_dependencies_incomplete')
                    self.assertEqual(result['dependency_blockers'][0]['id'], first)
                    self.assertEqual(snapshot(), before)
                    self.assertIsNone(task_retry.select_task(conn, 'dev', 'alpha', third))
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (first,))
            conn.commit()
            self.assertTrue(taskpool.task_complete('alpha', 'architect', second)['completed'])
            self.assertEqual(task_retry.select_task(conn, 'dev', 'alpha'), third)
        finally:
            conn.close()

    def test_completion_requires_published_complete_brief(self):
        tid = self.create(draft=True)['task_ids'][0]
        taskpool.task_update('alpha', 'architect', tid, next_bot='architect', credential='ctx')
        conn = db.get_connection()
        try:
            for briefing, description in (('draft', 'complete'), ('needs_details', 'complete'), ('ready', '')):
                with self.subTest(briefing=briefing):
                    conn.execute('UPDATE tasks SET briefing_status=?,description=? WHERE id=?',
                                 (briefing, description, tid))
                    conn.commit()
                    self.assertEqual(taskpool.task_complete('alpha', 'architect', tid)['code'], 'task_brief_incomplete')
                    self.assertEqual(taskpool.task_get('alpha', tid)['task']['status'], 'pending')
            taskpool.task_update('alpha', 'architect', tid, **BRIEF, publish=True, credential='ctx')
            self.assertTrue(taskpool.task_complete('alpha', 'architect', tid)['completed'])
        finally:
            conn.close()

    def test_dependency_chain_blocks_dispatch_claim_and_queued_work(self):
        first = self.create(**BRIEF)['task_ids'][0]
        second = self.create(**{**BRIEF, 'prerequisites': [first]})['task_ids'][0]
        third = self.create(**{**BRIEF, 'prerequisites': [second]})['task_ids'][0]
        taskpool.admin_task_block('alpha', first, 'pause prerequisite')
        conn = db.get_connection()
        try:
            self.assertEqual(scanner.scan_tasks(conn)['claimable'], 0)
            conn.commit()
            self.assertIsNone(task_retry.select_task(conn, 'dev', 'alpha', second))
            self.assertFalse(taskpool.task_claim('alpha', 'dev')['claimed'])
            packet = taskpool.task_get('alpha', second)
            self.assertEqual(packet['dependency_blockers'][0]['status'], 'blocked')
            wake.enqueue(conn, 'dev', 'work', 'scan:task', project='alpha')
            conn.commit()
            with patch.object(wake, 'session_busy', return_value=False), patch.object(wake, 'deliver') as deliver:
                queue.process_one(conn)
                deliver.assert_not_called()
            self.assertEqual(dict(conn.execute('SELECT id,status FROM tasks')),
                             {first:'blocked', second:'pending', third:'pending'})
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (first,))
            conn.commit()
            self.assertEqual(task_retry.select_task(conn, 'dev', 'alpha'), second)
            self.assertEqual(taskpool.task_claim('alpha', 'dev')['task']['id'], second)
            self.assertIn('error', taskpool.task_update('alpha', 'architect', second, prerequisites=[], credential='ctx'))
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (second,))
            conn.commit()
            self.assertEqual(taskpool.task_claim('alpha', 'dev')['task']['id'], third)
        finally:
            conn.close()

    def test_cancelled_missing_foreign_and_invalid_dependencies_fail_closed(self):
        from engine.task_brief import dependency_blockers
        first = self.create(**BRIEF)['task_ids'][0]
        second = self.create(**{**BRIEF, 'prerequisites': [first]})['task_ids'][0]
        conn = db.get_connection()
        try:
            conn.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (first,))
            conn.commit()
            self.assertFalse(taskpool.task_claim('alpha', 'dev')['claimed'])
            row = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (second,)).fetchone())
            for deps in ('[999999]', '[true]', '{}', 'broken'):
                self.assertTrue(dependency_blockers(conn, {**row, 'prerequisites': deps}))
            self.assertEqual(dependency_blockers(conn, {**row, 'project': 'beta'})[0]['status'], 'missing')
        finally:
            conn.close()

    def test_independent_work_continues_and_priority_does_not_override_dependencies(self):
        first = self.create(**BRIEF, priority=100)['task_ids'][0]
        second = self.create(**{**BRIEF, 'prerequisites': [first]}, priority=-100)['task_ids'][0]
        third = self.create(**BRIEF, priority=-200)['task_ids'][0]
        conn = db.get_connection()
        try:
            self.assertEqual(task_retry.select_task(conn, 'dev', 'alpha'), first)
            taskpool.admin_task_block('alpha', first)
            self.assertEqual(taskpool.task_claim('alpha', 'dev')['task']['id'], third)
            self.assertEqual(taskpool.task_get('alpha', second)['task']['status'], 'pending')
        finally:
            conn.close()

    def test_requirement_details_full_description_and_project_isolation(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from engine.api import router_api
        first = self.create(**BRIEF)['task_ids'][0]
        self.create(**{**BRIEF, 'prerequisites': [first]})
        app = FastAPI()
        app.include_router(router_api)
        with TestClient(app, client=('127.0.0.1', 4321)) as client:
            path = f'/requirements/{self.rid}'
            response = client.get(path, params={'project':'alpha'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['requirement']['description'], 'R' * 12000)
            self.assertEqual(response.json()['tasks'][1]['dependency_blockers'][0]['id'], first)
            self.assertEqual(client.get(path, params={'project':'beta'}).status_code, 404)
            self.assertEqual(client.get(path).status_code, 422)

    def test_incomplete_publish_is_atomic_and_reports_fields(self):
        result = taskpool.task_create('alpha', 'architect', self.rid, [
            dict(title='valid', next_bot='dev', **BRIEF), dict(title='missing', next_bot='dev')])
        self.assertEqual(result['task_index'], 1)
        self.assertEqual(set(result['fields']), set(BRIEF))
        self.assertEqual(taskpool.task_list('alpha', 'dev')['count'], 0)

    def test_draft_fill_publish_claim_context(self):
        tid = self.create(draft=True)['task_ids'][0]
        self.assertFalse(taskpool.task_claim('alpha', 'dev')['claimed'])
        self.assertIn('fields', taskpool.task_update('alpha', 'architect', tid, publish=True, credential='ctx'))
        filled = taskpool.task_update('alpha', 'architect', tid, **BRIEF, credential='ctx')
        self.assertEqual(filled['task']['briefing_status'], 'draft')
        taskpool.task_update('alpha', 'architect', tid, publish=True, credential='ctx')
        result = taskpool.task_claim('alpha', 'dev')
        self.assertTrue(result['claimed'])
        self.assertEqual(result['requirement']['description'], 'R' * 12000)
        self.assertEqual(result['requirement']['version'], 'v2')
        self.assertEqual(result['task']['design_basis'], BRIEF['design_basis'])
        self.assertEqual(result['missing_fields'], [])
        self.assertEqual(taskpool.task_get('alpha', tid)['task']['id'], tid)
        self.assertIn('error', taskpool.task_get('beta', tid))

    def test_legacy_migration_preserves_work_and_gates_pending(self):
        conn = db.get_connection()
        try:
            conn.execute("INSERT INTO tasks(project,requirement_id,title,status,next_bot,created_at) VALUES('alpha',?,'legacy','pending','dev','now')", (self.rid,))
            conn.commit()
            db.init_schema(conn)
            db.init_schema(conn)
            task = dict(conn.execute('SELECT * FROM tasks').fetchone())
            self.assertEqual(task['briefing_status'], 'needs_details')
            self.assertIsNone(task_retry.select_task(conn, 'dev', 'alpha'))
            self.assertEqual(scanner.scan_tasks(conn)['claimable'], 0)
            self.assertFalse(taskpool.task_claim('alpha', 'dev')['claimed'])
            conn.execute("UPDATE tasks SET status='in_progress',claimed_by='dev'")
            conn.commit()
            self.assertEqual(task_retry.select_task(conn, 'dev', 'alpha'), task['id'])
            self.assertEqual(taskpool.task_get('alpha', task['id'])['task']['status'], 'in_progress')
        finally:
            conn.close()

    def test_prerequisite_scope_cycle_and_delivery_context(self):
        first = self.create(**BRIEF)['task_ids'][0]
        second = self.create(**{**BRIEF, 'prerequisites': [first]})['task_ids'][0]
        self.assertIn('error', taskpool.task_update('alpha', 'architect', first, prerequisites=[second], credential='ctx'))
        self.assertIn('error', self.create(**{**BRIEF, 'prerequisites': [99999]}))
        conn = db.get_connection()
        try:
            conn.execute("INSERT INTO nodes(project,task_id,actor,type,result,ts) VALUES('alpha',?,'dev','dev','commit abc; tests passed','now')", (first,))
            conn.commit()
        finally:
            conn.close()
        packet = taskpool.task_get('alpha', second)
        self.assertEqual(packet['prerequisite_results'][0]['nodes'][-1]['result'], 'commit abc; tests passed')
        other = taskpool.req_create('beta', 'default', 'Other', **confirmed_requirement_fields('beta'))['requirement_id']
        foreign = taskpool.task_create('beta', 'architect', other, [dict(title='other', **BRIEF)])['task_ids'][0]
        self.assertIn('error', self.create(**{**BRIEF, 'prerequisites': [foreign]}))

    def test_queued_task_cannot_bypass_withdrawn_publication(self):
        tid = self.create(**BRIEF)['task_ids'][0]
        conn = db.get_connection()
        try:
            wake.enqueue(conn, 'dev', 'work', 'scan:task', project='alpha')
            conn.commit()
            taskpool.task_update('alpha', 'architect', tid, publish=False, credential='ctx')
            self.assertIsNone(task_retry.select_task(conn, 'dev', 'alpha'))
            with patch.object(wake, 'session_busy', return_value=False), patch.object(wake, 'deliver') as deliver:
                queue.process_one(conn)
                deliver.assert_not_called()
        finally:
            conn.close()

    def test_published_brief_cannot_be_blank_and_requires_permission(self):
        tid = self.create(**BRIEF)['task_ids'][0]
        self.assertIn('fields', taskpool.task_update('alpha', 'architect', tid, description='  ', credential='ctx'))
        self.assertEqual(taskpool.task_update('alpha', 'dev', tid, publish=False)['code'], 'permission_denied')
        self.assertEqual(taskpool.task_get('alpha', tid)['task']['description'], BRIEF['description'])

    def test_http_context_and_repeated_claim_keep_full_packet(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from engine.api import router_api
        tid = self.create(**BRIEF)['task_ids'][0]
        taskpool.task_claim('alpha', 'dev')
        repeated = taskpool.task_claim('alpha', 'dev')
        self.assertEqual(repeated['contexts'][0]['requirement']['description'], 'R' * 12000)
        app = FastAPI()
        app.include_router(router_api)
        with TestClient(app, client=('127.0.0.1', 4321)) as client:
            packet = client.get(f'/tasks/{tid}', params={'project': 'alpha'})
            self.assertEqual(packet.status_code, 200)
            self.assertEqual(packet.json()['task']['acceptance_criteria'], BRIEF['acceptance_criteria'])
            self.assertEqual(client.get(f'/tasks/{tid}', params={'project': 'beta'}).status_code, 404)


if __name__ == '__main__':
    unittest.main()
