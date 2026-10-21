"""Project grants, all mutation boundaries and admin API against temporary SQLite."""
import json
import os
import tempfile
import unittest
from scripts.unified_test_support import REQUIREMENT_FIELDS

from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from engine import api, db, project_permissions as permissions, taskpool, taskqueue_proxy


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'GROUP_DB': str(Path(self.tmp.name) / 'group.db')})
        env.start()
        self.addCleanup(env.stop)
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        db.init_schema(self.conn)
        for profile in ('custom-lead', 'custom-dev', 'default', 'architect', 'user'):
            self.conn.execute('INSERT INTO members(profile,display_name) VALUES(?,?)', (profile, profile))
        for project in ('p', 'q'):
            self.conn.execute('INSERT INTO projects(name,root,created_at) VALUES(?,?,?)', (project, self.tmp.name, db.now_iso()))
            self.conn.execute('INSERT INTO project_members(project,profile,active) SELECT ?,profile,1 FROM members', (project,))
        # req#112：给 custom-lead/custom-dev 设角色实体 + role_id（task_log 交接 expected_role_id 核对）
        for name in ('lead', 'dev'):
            self.conn.execute("INSERT INTO project_roles(project, name, created_at) VALUES('p', ?, ?)",
                              (name, db.now_iso()))
        self.conn.execute("UPDATE project_members SET role_id=(SELECT id FROM project_roles WHERE project='p' AND name='lead'), role='lead' "
                          "WHERE project='p' AND profile='custom-lead'")
        self.conn.execute("UPDATE project_members SET role_id=(SELECT id FROM project_roles WHERE project='p' AND name='dev'), role='dev' "
                          "WHERE project='p' AND profile='custom-dev'")
        self.conn.execute("INSERT INTO settings(key,project,value,updated_at) VALUES('owner_profile','p','custom-lead',?)", (db.now_iso(),))
        self.conn.execute("INSERT INTO requirements(id,project,title,description,created_at,updated_at) VALUES(1,'p','req','Legacy body',?,?)", (db.now_iso(), db.now_iso()))
        self.conn.execute("INSERT INTO tasks(id,project,requirement_id,title,next_bot,created_at) VALUES(1,'p',1,'task','custom-lead',?)", (db.now_iso(),))
        # 预插一条用户消息（id=1），供 REQUIREMENT_FIELDS 的 source_ref 引用。
        self.conn.execute("INSERT INTO messages(project,ts,sender,sender_type,content,mentions) VALUES('p',?,'user','user','fixture source','[]')", (db.now_iso(),))
        self.conn.commit()
        db.init_schema(self.conn)
        app = FastAPI()
        app.include_router(api.router_api)
        self.identity = ('user', 'user')
        app.dependency_overrides[api._resolve_identity] = lambda: self.identity
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def grant(self, permission, profile='custom-lead', project='p'):
        self.conn.execute('INSERT OR IGNORE INTO project_permissions VALUES(?,?,?)', (project, profile, permission))
        self.conn.commit()

    def calls(self, profile='custom-lead', project='p'):
        return {
            'req.create': lambda: taskpool.req_create(project, profile, 'new', **REQUIREMENT_FIELDS),
            'req.update': lambda: taskpool.req_update(project, profile, 1, title='updated'),
            'req.resume': lambda: taskpool.req_resume(project, profile, 1),
            'task.resume': lambda: taskpool.task_resume(project, profile, 1),
            'task.create': lambda: taskpool.task_create(project, profile, 1, [{'title': 'new', 'next_bot': 'custom-dev', 'draft': True}]),
            'task.update': lambda: taskpool.task_update(project, profile, 1, title='changed'),
            'task.complete': lambda: taskpool.task_complete(project, profile, 1),
            'req.complete': lambda: taskpool.req_complete(project, profile, 1),
            'req.cancel': lambda: taskpool.req_cancel(project, profile, 1),
            'task.cancel': lambda: taskpool.task_cancel(project, profile, 1),
        }

    def test_default_deny_including_owner_and_named_profiles_no_side_effects(self):
        before = '\n'.join(self.conn.iterdump())
        for profile in ('custom-lead', 'default', 'architect', 'user', 'missing'):
            for key, call in self.calls(profile).items():
                with self.subTest(profile=profile, permission=key):
                    self.assertEqual(call()['code'], 'permission_denied')
        self.assertEqual(before, '\n'.join(self.conn.iterdump()))

    def test_each_grant_enables_only_that_operation(self):
        for key in permissions.PERMISSIONS:
            with self.subTest(permission=key):
                self.conn.execute('DELETE FROM project_permissions')
                self.conn.execute("UPDATE tasks SET status='pending',next_bot='custom-lead',claimed_by='' WHERE id=1")
                self.conn.execute("UPDATE requirements SET status='open', decompose_state='pending', decompose_credential='', decompose_owner='', decompose_expires_at='' WHERE id=1")
                if key == 'req.complete':
                    self.conn.execute("UPDATE tasks SET status='done' WHERE requirement_id=1")
                if key == 'req.resume':
                    self.conn.execute("UPDATE requirements SET status='blocked' WHERE id=1")
                if key == 'task.resume':
                    self.conn.execute("UPDATE tasks SET status='blocked' WHERE id=1")
                if key == 'task.complete':
                    self.conn.execute("UPDATE tasks SET briefing_status='ready',description='Implement API',"
                                      "design_basis='Approved design',acceptance_criteria='Tests pass',prerequisites='[]' WHERE id=1")
                self.conn.commit()
                self.grant(key)
                for other, call in self.calls().items():
                    if other != key:
                        self.assertEqual(call()['code'], 'permission_denied')
                result = self.calls()[key]()
                self.assertNotIn('error', result)

    def test_isolation_disabled_deleted_and_revoked(self):
        self.grant('task.update')
        self.assertNotIn('error', self.calls()['task.update']())
        self.assertEqual(self.calls(project='q')['task.update']()['code'], 'permission_denied')
        self.conn.execute("UPDATE project_members SET active=0 WHERE project='p' AND profile='custom-lead'")
        self.conn.commit()
        self.assertEqual(self.calls()['task.update']()['code'], 'permission_denied')
        self.conn.execute("UPDATE project_members SET active=1")
        self.conn.commit()
        self.assertEqual(self.calls()['task.update']()['code'], 'permission_denied')
        self.grant('task.update')
        self.assertNotIn('error', self.calls()['task.update']())
        self.conn.execute("DELETE FROM members WHERE profile='custom-lead'")
        self.conn.commit()
        self.assertEqual(self.calls()['task.update']()['code'], 'permission_denied')
        self.conn.execute("INSERT INTO members(profile,display_name) VALUES('custom-lead','Lead')")
        self.conn.execute('DELETE FROM project_permissions')
        self.conn.commit()
        self.assertEqual(self.calls()['task.update']()['code'], 'permission_denied')
        self.assertIsNotNone(permissions.check(self.conn, 'p', 'custom-lead', 'unknown'))

    def test_only_active_project_members_can_receive_grants(self):
        old = permissions.view('p')
        self.conn.execute("UPDATE project_members SET active=0 WHERE project='p' AND profile='custom-dev'")
        self.conn.execute("DELETE FROM project_members WHERE project='p' AND profile='architect'")
        self.conn.commit()
        current = permissions.view('p')
        self.assertNotIn('custom-dev', [m['profile'] for m in current['members']])
        self.assertNotIn('architect', [m['profile'] for m in current['members']])
        self.assertNotEqual(old['revision'], current['revision'])
        self.assertEqual(self.client.put('/projects/p/permissions', json=old).status_code, 409)
        for profile in ('custom-dev', 'architect'):
            current['grants']['task.update'] = [profile]
            self.assertEqual(self.client.put('/projects/p/permissions', json=current).status_code, 400)

    def test_leave_revokes_only_this_project_and_rejoin_is_empty(self):
        self.grant('task.update')
        self.grant('task.update', project='q')
        self.conn.execute("DELETE FROM project_members WHERE project='p' AND profile='custom-lead'")
        self.conn.execute("INSERT INTO project_members(project,profile,active) VALUES('p','custom-lead',1)")
        self.conn.commit()
        self.assertIsNotNone(permissions.check(self.conn, 'p', 'custom-lead', 'task.update'))
        self.assertIsNone(permissions.check(self.conn, 'q', 'custom-lead', 'task.update'))

    def test_migration_removes_legacy_inactive_grants_and_preserves_active(self):
        self.conn.execute("UPDATE project_members SET active=0 WHERE project='p' AND profile='custom-dev'")
        self.grant('task.update', profile='custom-dev')
        self.grant('task.update')
        self.conn.execute("DELETE FROM engine_migrations WHERE name='active_member_permissions'")
        self.conn.commit()
        db.init_schema(self.conn)
        db.init_schema(self.conn)
        rows = [r[0] for r in self.conn.execute("SELECT profile FROM project_permissions WHERE project='p'")]
        self.assertEqual(rows, ['custom-lead'])

    def test_existing_state_and_assignee_rules_remain(self):
        for key in permissions.PERMISSIONS:
            self.grant(key)
        self.assertIn('unfinished', taskpool.req_complete('p', 'custom-lead', 1)['error'])
        self.assertIn('error', taskpool.task_update('p', 'custom-lead', 1, next_bot='missing'))
        self.conn.execute("UPDATE tasks SET status='in_progress',claimed_by='custom-dev' WHERE id=1")
        self.conn.commit()
        self.assertIn('cannot reassign', taskpool.task_update('p', 'custom-lead', 1, next_bot='custom-dev')['error'])
        self.assertIn('not pending', taskpool.task_complete('p', 'custom-lead', 1)['error'])
        result = taskpool.task_cancel('p', 'custom-lead', 1)
        self.assertEqual(result['previous_assignee'], 'custom-dev')
        self.assertEqual(tuple(self.conn.execute('SELECT status,next_bot,claimed_by FROM tasks WHERE id=1').fetchone()), ('cancelled', '', ''))
        lead_role_id = self.conn.execute("SELECT id FROM project_roles WHERE project='p' AND name='lead'").fetchone()['id']
        self.assertIn('error', taskpool.task_log('p', 'custom-dev', 1, 'dev', result='Verified fixture delivery', next_bot='custom-lead', expected_role_id=lead_role_id))
        self.assertIn('error', taskpool.task_cancel('p', 'custom-lead', 1))

    def test_admin_api_save_revision_validation_audit_and_cleanup(self):
        data = self.client.get('/projects/p/permissions').json()
        self.assertEqual(len(data['permissions']), len(permissions.PERMISSIONS))
        self.assertTrue(all(not profiles for profiles in data['grants'].values()))
        data['grants']['task.update'] = ['custom-lead', 'custom-dev']
        body = {'revision': data['revision'], 'grants': data['grants']}
        response = self.client.put('/projects/p/permissions', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.put('/projects/p/permissions', json=body).status_code, 409)
        detail = json.loads(self.conn.execute("SELECT detail FROM project_config_events WHERE action='permissions.update'").fetchone()[0])
        self.assertEqual(detail['before']['task.update'], [])
        body['revision'] = response.json()['revision']
        body['grants']['req.create'] = ['unknown']
        self.assertEqual(self.client.put('/projects/p/permissions', json=body).status_code, 400)
        self.assertEqual(self.client.get('/projects/missing/permissions').status_code, 404)
        self.identity = ('custom-lead', 'bot')
        self.assertEqual(self.client.get('/projects/p/permissions').status_code, 403)
        self.assertEqual(self.client.put('/projects/p/permissions', json=body).status_code, 403)
        count = self.conn.execute('SELECT count(*) FROM project_permissions').fetchone()[0]
        taskpool.project_remove('p')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM project_permissions').fetchone()[0], count)

    def test_schema_upgrade_is_idempotent_and_grants_are_preserved(self):
        self.conn.execute('DROP TABLE project_permissions')
        self.conn.commit()
        db.init_schema(self.conn)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM project_permissions').fetchone()[0], 0)
        self.grant('task.update')
        db.init_schema(self.conn)
        self.assertIsNone(permissions.check(self.conn, 'p', 'custom-lead', 'task.update'))

    def test_user_management_remains_admin_only_and_needs_no_bot_grant(self):
        self.identity = ('custom-lead', 'bot')
        self.assertEqual(self.client.post('/admin/task/1/cancel', json={'project': 'p'}).status_code, 403)
        self.identity = ('user', 'user')
        response = self.client.post('/admin/task/1/cancel', json={'project': 'p'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['cancelled'])

    def test_resume_role_grants_actor_and_independent_states(self):
        rid = self.conn.execute(
            "INSERT INTO project_roles(project,name,created_at) VALUES('p','coordinator',?)",
            (db.now_iso(),)).lastrowid
        self.conn.execute("UPDATE project_members SET role_id=? WHERE project='p' AND profile='custom-lead'", (rid,))
        self.conn.execute("UPDATE requirements SET status='blocked' WHERE id=1")
        self.conn.execute("UPDATE tasks SET status='blocked',block_kind='technical',claimed_by='custom-dev',claimed_at='old' WHERE id=1")
        self.conn.commit()
        self.grant('req.resume', f'role:{rid}')
        self.grant('task.resume', f'role:{rid}')
        self.assertTrue(taskpool.req_resume('p', 'custom-lead', 1)['resumed'])
        self.assertEqual(self.conn.execute('SELECT status FROM tasks WHERE id=1').fetchone()[0], 'blocked')
        result = taskpool.task_resume('p', 'custom-lead', 1, next_bot='custom-dev')
        self.assertTrue(result['resumed'], result)
        self.assertEqual(tuple(self.conn.execute('SELECT status,block_kind,claimed_by,claimed_at,next_bot FROM tasks WHERE id=1').fetchone()),
                         ('pending', '', '', None, 'custom-dev'))
        rows = [tuple(r) for r in self.conn.execute("SELECT actor,event_type FROM events WHERE event_type IN ('resume','req_resume') ORDER BY id")]
        self.assertEqual(rows, [('custom-lead', 'req_resume'), ('custom-lead', 'resume')])

    def test_resume_rejects_inactive_foreign_and_wrong_state_without_writes(self):
        for key in ('req.resume', 'task.resume'):
            self.grant(key)
            self.grant(key, project='q')
        self.conn.execute("UPDATE requirements SET status='blocked' WHERE id=1")
        self.conn.execute("UPDATE tasks SET status='blocked' WHERE id=1")
        self.conn.commit()
        for project in ('q', 'missing'):
            before = list(self.conn.iterdump())
            for call in (taskpool.req_resume, taskpool.task_resume):
                self.assertIn('error', call(project, 'custom-lead', 1))
            self.assertEqual(list(self.conn.iterdump()), before)
        self.conn.execute("UPDATE project_members SET active=0 WHERE project='p' AND profile='custom-lead'")
        self.conn.commit()
        before = list(self.conn.iterdump())
        for call in (taskpool.req_resume, taskpool.task_resume):
            self.assertEqual(call('p', 'custom-lead', 1)['code'], 'permission_denied')
        self.assertEqual(list(self.conn.iterdump()), before)
        self.conn.execute("UPDATE project_members SET active=1 WHERE project='p' AND profile='custom-lead'")
        self.conn.commit()
        for key in ('req.resume', 'task.resume'):
            self.grant(key)
        for req_state, task_state in (('open', 'pending'), ('done', 'done'), ('cancelled', 'cancelled')):
            self.conn.execute('UPDATE requirements SET status=? WHERE id=1', (req_state,))
            self.conn.execute('UPDATE tasks SET status=? WHERE id=1', (task_state,))
            self.conn.commit()
            before = list(self.conn.iterdump())
            for call in (taskpool.req_resume, taskpool.task_resume):
                self.assertIn('error', call('p', 'custom-lead', 1))
            self.assertEqual(list(self.conn.iterdump()), before)

    def test_task_resume_validates_target_and_preserves_requirement(self):
        self.grant('task.resume')
        self.conn.execute("UPDATE requirements SET status='blocked' WHERE id=1")
        self.conn.execute("UPDATE tasks SET status='blocked' WHERE id=1")
        self.conn.commit()
        before = list(self.conn.iterdump())
        self.assertIn('error', taskpool.task_resume('p', 'custom-lead', 1, next_bot='missing'))
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertTrue(taskpool.task_resume('p', 'custom-lead', 1)['resumed'])
        self.assertEqual(self.conn.execute('SELECT status FROM requirements WHERE id=1').fetchone()[0], 'blocked')

    def test_human_resume_endpoints_preserved_and_bot_denied(self):
        self.conn.execute("UPDATE requirements SET status='blocked' WHERE id=1")
        self.conn.execute("UPDATE tasks SET status='blocked' WHERE id=1")
        self.conn.commit()
        for path, key in (('/admin/task/1/resume', 'task.resume'),
                          ('/admin/requirement/1/resume', 'req.resume')):
            self.grant(key)
            self.identity = ('custom-lead', 'bot')
            self.assertEqual(self.client.post(path, json={'project': 'p'}).status_code, 403)
            self.conn.execute('DELETE FROM project_permissions')
            self.conn.commit()
            self.identity = ('user', 'user')
            response = self.client.post(path, json={'project': 'p'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()['resumed'])

    def test_permission_order_groups_requirements_before_tasks(self):
        data = self.client.get('/projects/p/permissions').json()
        self.assertEqual([p['key'] for p in data['permissions']], [
            'req.create', 'req.update', 'req.resume', 'req.complete', 'req.cancel',
            'task.create', 'task.update', 'task.resume', 'task.complete', 'task.cancel'])

    def test_human_requirement_close_checks_children_identity_and_project(self):
        path = '/admin/requirement/1/complete'
        self.identity = ('custom-lead', 'bot')
        self.grant('req.complete')
        self.assertEqual(self.client.post(path, json={'project': 'p'}).status_code, 403)
        self.identity = ('user', 'user')
        self.conn.execute('DELETE FROM project_permissions')
        self.conn.commit()
        before = list(self.conn.iterdump())
        for body in ({}, {'project': 'q'}, {'project': 'p'}):
            self.assertEqual(self.client.post(path, json=body).status_code, 400)
        self.assertEqual(list(self.conn.iterdump()), before)
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=1")
        self.conn.commit()
        response = self.client.post(path, json={'project': 'p'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.conn.execute('SELECT status FROM requirements WHERE id=1').fetchone()[0], 'done')
        self.assertEqual(self.conn.execute("SELECT actor FROM events WHERE event_type='req_complete'").fetchone()[0], 'user')
        self.assertEqual(self.client.post('/admin/requirement/1/archive', json={'project': 'p'}).status_code, 200)

    def test_topic_proxy_cannot_bypass_grants(self):
        self.conn.execute("INSERT INTO threads(id,project,title,creator,members,task_id,created_at,last_active_at) VALUES(1,'p','topic','custom-lead','[]',1,?,?)", (db.now_iso(), db.now_iso()))
        self.conn.commit()
        with self.assertRaises(taskqueue_proxy.ProxyError) as error:
            taskqueue_proxy.task_update(self.conn, 'custom-lead', 1, 1, {'title': 'changed'})
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM messages').fetchone()[0], 1)
        self.grant('task.update')
        self.assertTrue(taskqueue_proxy.task_update(self.conn, 'custom-lead', 1, 1, {'title': 'changed'})['updated'])


if __name__ == '__main__':
    unittest.main()
