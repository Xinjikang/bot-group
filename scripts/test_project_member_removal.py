"""Scoped member removal, handoff guards, rollback and retained resources."""
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from engine import api, auth, db, project_membership as membership, project_permissions, wake
from scripts import test_abnormal_exit as fixtures


class MemberRemovalTests(unittest.TestCase):
    def setUp(self):
        fixtures.AbnormalExitTests.setUp(self)
        self.conn.execute("UPDATE tasks SET status='done' WHERE project='p'")
        self.conn.execute("INSERT INTO project_permissions VALUES('p','dev-1','task.create')")
        self.conn.execute("INSERT INTO project_permissions VALUES('q','dev-1','task.create')")
        for project, tid in (('p', 11), ('q', 12)):
            self.conn.execute(
                "INSERT INTO threads(id,project,title,creator,members,created_at,last_active_at,turn_profile) "
                "VALUES(?,?, 'topic','user',?, 'now','now','dev-1')",
                (tid, project, json.dumps(['user', 'dev-1', 'dev-2'])))
            self.conn.execute("INSERT INTO messages(id,project,ts,sender,sender_type,content,thread_id) "
                              "VALUES(?,?,'now','user','user','keep history',?)", (tid, project, tid))
            self.conn.execute("INSERT INTO inbox(profile,message_id,status) VALUES('dev-1',?,'pending')", (tid,))
            self.conn.execute("INSERT INTO wake_jobs(profile,project,kind,trigger,status,enqueued_at) "
                              "VALUES('dev-1',?,'work','test','queued','now')", (project,))
        self.workspace = Path(self.tmp.name) / 'retained-worktree'
        self.workspace.mkdir()
        (self.workspace / 'work.txt').write_text('local changes')
        self.conn.execute('INSERT INTO project_workspaces VALUES(?,?,?)', ('p', 'dev-1', str(self.workspace)))
        self.conn.commit()
        active = patch.object(wake, 'active_runs', return_value=[])
        active.start(); self.addCleanup(active.stop)

    def remove(self):
        return api.remove_project_member('p', 'dev-1', ('user', 'user'))

    def blockers(self):
        return membership.preview('p', 'dev-1')['blockers']

    def test_removal_preserves_global_identity_other_project_and_history(self):
        messages = [tuple(row) for row in self.conn.execute('SELECT * FROM messages')]
        tasks = [tuple(row) for row in self.conn.execute('SELECT * FROM tasks')]
        self.assertTrue(membership.preview('p', 'dev-1')['can_remove'])
        result = self.remove()
        self.assertTrue(result['removed'])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM project_members WHERE project='p' AND profile='dev-1'").fetchone())
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM project_members WHERE project='q' AND profile='dev-1'").fetchone())
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM members WHERE profile='dev-1'").fetchone())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM project_permissions WHERE project='p' AND profile='dev-1'").fetchone())
        self.assertIsNone(project_permissions.check(self.conn, 'q', 'dev-1', 'task.create'))
        self.assertEqual(messages, [tuple(row) for row in self.conn.execute('SELECT * FROM messages')])
        self.assertEqual(tasks, [tuple(row) for row in self.conn.execute('SELECT * FROM tasks')])
        self.assertNotIn('dev-1', json.loads(self.conn.execute('SELECT members FROM threads WHERE id=11').fetchone()[0]))
        self.assertIn('dev-1', json.loads(self.conn.execute('SELECT members FROM threads WHERE id=12').fetchone()[0]))
        self.assertNotEqual(self.conn.execute('SELECT turn_profile FROM threads WHERE id=11').fetchone()[0], 'dev-1')
        self.assertEqual(self.conn.execute('SELECT turn_profile FROM threads WHERE id=12').fetchone()[0], 'dev-1')
        self.assertEqual(self.conn.execute("SELECT status FROM inbox WHERE profile='dev-1' AND message_id=11").fetchone()[0], 'expired')
        self.assertEqual(self.conn.execute("SELECT status FROM inbox WHERE profile='dev-1' AND message_id=12").fetchone()[0], 'pending')
        self.assertEqual(self.conn.execute("SELECT status FROM wake_jobs WHERE profile='dev-1' AND project='p'").fetchone()[0], 'killed')
        self.assertEqual(self.conn.execute("SELECT status FROM wake_jobs WHERE profile='dev-1' AND project='q'").fetchone()[0], 'queued')
        self.assertEqual((self.workspace / 'work.txt').read_text(), 'local changes')
        self.assertEqual(self.conn.execute("SELECT path FROM project_workspaces WHERE project='p' AND profile='dev-1'").fetchone()[0], str(self.workspace))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM project_config_events WHERE action='member.remove'").fetchone()[0], 1)

    def test_owner_task_decomposition_and_topic_blockers(self):
        self.conn.execute("UPDATE settings SET value='dev-1' WHERE project='p' AND key='owner_profile'")
        self.conn.execute("UPDATE tasks SET status='blocked',next_bot='dev-1' WHERE id=1")
        self.conn.execute("UPDATE requirements SET decompose_owner='dev-1',decompose_state='decomposing' WHERE id=1")
        self.conn.execute("UPDATE threads SET creator='dev-1' WHERE id=11")
        self.conn.commit()
        self.assertEqual({item['kind'] for item in self.blockers()}, {'owner', 'task', 'requirement', 'thread'})
        with self.assertRaises(HTTPException) as raised:
            self.remove()
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(len(raised.exception.detail['blockers']), 4)

    def test_final_check_catches_new_task_after_preview(self):
        self.assertTrue(membership.preview('p', 'dev-1')['can_remove'])
        self.conn.execute("UPDATE tasks SET status='pending',next_bot='dev-1' WHERE id=1")
        self.conn.commit()
        with self.assertRaises(HTTPException) as raised:
            self.remove()
        self.assertEqual(raised.exception.status_code, 409)

    def test_running_jobs_and_processes_block_removal(self):
        self.conn.execute("UPDATE wake_jobs SET status='running' WHERE project='p' AND profile='dev-1'")
        self.conn.commit()
        self.assertEqual(self.blockers()[0]['kind'], 'runtime')
        self.conn.execute("UPDATE wake_jobs SET status='done' WHERE project='p'")
        self.conn.commit()
        with patch.object(wake, 'active_runs', return_value=[{'project': 'p', 'profile': 'dev-1'}]):
            self.assertEqual(self.blockers()[0]['kind'], 'runtime')
        with patch.object(wake, 'active_runs', return_value=[{'project': 'q', 'profile': 'dev-1'}]):
            self.assertEqual(self.blockers(), [])

    def test_failed_audit_rolls_back_all_changes_including_turn(self):
        before = {table: [tuple(row) for row in self.conn.execute(f'SELECT * FROM {table}')]
                  for table in ('project_members', 'project_permissions', 'threads', 'inbox', 'wake_jobs')}
        with patch.object(membership.ws, 'audit', side_effect=RuntimeError('audit failure')):
            with self.assertRaises(RuntimeError):
                self.remove()
        for table, rows in before.items():
            self.assertEqual(rows, [tuple(row) for row in self.conn.execute(f'SELECT * FROM {table}')], table)

    def test_disabled_member_can_leave_and_rejoin_without_old_grants(self):
        self.conn.execute("UPDATE project_members SET active=0 WHERE project='p' AND profile='dev-1'")
        self.conn.commit()
        self.remove()
        api.set_member_active('dev-1', {'project': 'p', 'active': True}, ('user', 'user'))
        member = self.conn.execute("SELECT role,role_id FROM project_members WHERE project='p' AND profile='dev-1'").fetchone()
        self.assertEqual(member['role'], '')
        self.assertIsNone(member['role_id'])
        self.assertIsNotNone(project_permissions.check(self.conn, 'p', 'dev-1', 'task.create'))

    def test_bot_cannot_inspect_or_remove_and_builtin_user_is_protected(self):
        for endpoint in (api.preview_project_member_removal, api.remove_project_member):
            with self.assertRaises(HTTPException) as raised:
                endpoint('p', 'dev-1', ('dev-1', 'bot'))
            self.assertEqual(raised.exception.status_code, 403)
        with self.assertRaises(HTTPException) as raised:
            membership.remove('p', 'user', 'user')
        self.assertEqual(raised.exception.status_code, 400)
        with self.assertRaises(HTTPException) as raised:
            membership.preview('missing', 'dev-1')
        self.assertEqual(raised.exception.status_code, 404)

    def test_authenticated_http_removal_and_member_scope_revocation(self):
        app = FastAPI()
        app.include_router(api.router_api)
        with patch.dict(os.environ, {'GROUP_ADMIN_PASSWORD': 'isolated-test-password'}), TestClient(app) as client:
            admin = {'Authorization': 'Bearer ' + auth.login('isolated-test-password')['token']}
            bot = {'X-Group-Profile': 'dev-1', 'X-Group-Key': auth.bot_token('dev-1')}
            endpoint = '/projects/p/members/dev-1'
            self.assertEqual(client.get(endpoint + '/removal', headers=bot).status_code, 403)
            self.assertEqual(client.delete(endpoint, headers=bot).status_code, 403)
            self.assertTrue(client.get(endpoint + '/removal', headers=admin).json()['can_remove'])
            self.assertEqual(client.delete(endpoint, headers=admin).status_code, 200)
            self.assertEqual(client.get('/members?project=p', headers=bot).status_code, 403)
            self.assertEqual(client.get('/members?project=q', headers=bot).status_code, 200)
            self.assertEqual(client.delete(endpoint, headers=admin).status_code, 404)


if __name__ == '__main__':
    unittest.main()
