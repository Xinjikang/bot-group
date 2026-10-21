"""Explicit project membership migration and safe project archiving, isolated SQLite."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from engine import api, db, router, queue, taskpool, wake, project_permissions, project_workspaces


class ProjectLifecycleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dbpatch = patch.object(db, 'db_path', return_value=str(Path(tmp.name) / 'test.db'))
        dbpatch.start()
        self.addCleanup(dbpatch.stop)
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        db.init_schema(self.conn)
        for project in ('p', 'q'):
            root = Path(tmp.name) / project
            root.mkdir()
            self.conn.execute('INSERT INTO projects(name,root,created_at) VALUES(?,?,?)', (project, str(root), db.now_iso()))
        self.conn.execute("INSERT INTO members(profile,display_name) VALUES('bot','Bot')")
        self.conn.execute("INSERT INTO requirements(id,project,title,created_at,updated_at) VALUES(1,'p','req','now','now')")
        self.conn.commit()
        app = FastAPI()
        app.include_router(api.router_api)
        app.dependency_overrides[api._resolve_identity] = lambda: ('user', 'user')
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_new_members_default_out_and_explicit_enable_is_scoped(self):
        db.init_schema(self.conn)
        self.assertFalse(router._profile_active(self.conn, 'p', 'bot'))
        self.assertTrue(queue._profile_disabled(self.conn, 'p', 'bot'))
        self.assertFalse(wake.enqueue(self.conn, 'bot', 'work', 'test', project='p'))
        self.conn.execute("INSERT INTO project_permissions VALUES('p','bot','task.create')")
        self.conn.commit()
        self.assertIsNotNone(project_permissions.check(self.conn, 'p', 'bot', 'task.create'))
        self.assertEqual(self.client.get('/members?project=p&active_only=true').json()['members'], [])
        self.assertEqual(project_workspaces.view('p')['members'], [])
        self.assertEqual(project_permissions.view('p')['members'], [])
        api.set_member_active('bot', {'project': 'p', 'active': True}, ('user', 'user'))
        self.assertTrue(router._profile_active(self.conn, 'p', 'bot'))
        self.assertFalse(router._profile_active(self.conn, 'q', 'bot'))
        self.assertIsNotNone(project_permissions.check(self.conn, 'p', 'bot', 'task.create'))
        self.assertTrue(wake.enqueue(self.conn, 'bot', 'work', 'test', project='p'))

    def test_unjoined_queued_bot_is_not_delivered(self):
        self.conn.execute("INSERT INTO wake_jobs(profile,project,kind,trigger,enqueued_at) VALUES('bot','p','work','test','now')")
        self.conn.commit()
        with patch.object(wake, 'deliver') as deliver:
            self.assertFalse(queue.process_one(self.conn))
            deliver.assert_not_called()

    def test_new_project_owner_selection_does_not_enable_members(self):
        self.assertNotIn('error', taskpool.project_add('new-project', '.', owner_profile='bot'))
        self.assertFalse(router._profile_active(self.conn, 'new-project', 'bot'))
        from engine.task_retry import project_owner
        self.assertEqual(project_owner(self.conn, 'new-project'), 'user')

    def test_registration_does_not_join_existing_projects(self):
        self.client.post('/members', json={'profile': 'new-bot', 'display_name': 'New'})
        db.init_schema(self.conn)
        self.assertFalse(router._profile_active(self.conn, 'p', 'new-bot'))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM project_members WHERE profile='new-bot'").fetchone()[0], 0)

    def test_legacy_migration_preserves_states_once_including_archived(self):
        self.conn.execute("DELETE FROM engine_migrations WHERE name='explicit_project_membership'")
        self.conn.execute("INSERT INTO project_members(project,profile,active) VALUES('p','bot',0)")
        self.conn.execute("UPDATE projects SET archived=1 WHERE name='q'")
        self.conn.commit()
        db.init_schema(self.conn)
        self.assertFalse(router._profile_active(self.conn, 'p', 'bot'))
        self.assertTrue(router._profile_active(self.conn, 'q', 'bot'))
        self.conn.execute("INSERT INTO members(profile,display_name) VALUES('new','New')")
        self.conn.execute("INSERT INTO projects(name,root,created_at) VALUES('new','.','now')")
        self.conn.commit()
        db.init_schema(self.conn)
        self.assertFalse(router._profile_active(self.conn, 'p', 'new'))
        self.assertFalse(router._profile_active(self.conn, 'new', 'bot'))

    def test_archive_blocks_pending_running_and_blocked_tasks(self):
        for status in ('pending', 'in_progress', 'blocked'):
            with self.subTest(status=status):
                self.conn.execute('DELETE FROM tasks')
                self.conn.execute("INSERT INTO tasks(project,requirement_id,title,status,created_at) VALUES('p',1,'task',?,'now')", (status,))
                self.conn.commit()
                response = self.client.post('/projects/p/archive', json={'archived': True})
                self.assertEqual(response.status_code, 409)
                self.assertIn('请先处理', response.json()['detail'])
                self.assertEqual(self.conn.execute("SELECT archived FROM projects WHERE name='p'").fetchone()[0], 0)

    def test_archive_blocks_queued_and_running_wakes(self):
        for status in ('queued', 'running'):
            with self.subTest(status=status):
                self.conn.execute('DELETE FROM wake_jobs')
                self.conn.execute("INSERT INTO wake_jobs(profile,project,kind,trigger,status,enqueued_at) VALUES('bot','p','chat','test',?,'now')", (status,))
                self.conn.commit()
                self.assertIn('error', taskpool.project_archive('p'))

    def test_active_process_blocks_even_when_job_done(self):
        with patch.object(wake, 'active_runs', return_value=[{'project': 'p', 'profile': 'bot'}]):
            self.assertIn('error', taskpool.project_archive('p'))

    def test_recovered_process_unknown_or_alive_blocks(self):
        self.conn.execute("INSERT INTO wake_runs(project,session,started_at,ended_at,exit_code) VALUES('p','bot-p-work',1,2,-1)")
        self.conn.commit()
        for pids in (None, [123]):
            with patch.object(wake, 'find_bot_processes', return_value=pids):
                self.assertIn('error', taskpool.project_archive('p'))
        with patch.object(wake, 'find_bot_processes', return_value=[]):
            self.assertNotIn('error', taskpool.project_archive('p'))

    def test_terminal_history_other_projects_and_restore(self):
        for project, status in [('p', 'done'), ('p', 'cancelled'), ('q', 'in_progress')]:
            self.conn.execute('INSERT INTO tasks(project,requirement_id,title,status,created_at) VALUES(?,1,?,?,?)', (project, 'task', status, 'now'))
        self.conn.execute("INSERT INTO project_members(project,profile,active) VALUES('p','bot',1)")
        self.conn.commit()
        self.assertNotIn('error', taskpool.project_archive('p'))
        self.assertFalse(wake.enqueue(self.conn, 'bot', 'work', 'test', project='p'))
        with patch.object(wake.subprocess, 'Popen') as spawn:
            self.assertFalse(wake.deliver('bot', 'work', project='p')['ok'])
            spawn.assert_not_called()
        self.assertNotIn('error', taskpool.project_archive('p', False))
        self.assertTrue(wake.enqueue(self.conn, 'bot', 'work', 'test', project='p'))


if __name__ == '__main__':
    unittest.main()
