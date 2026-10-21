"""Project roster roles, exact Bot handoff and existing-data migration."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from engine import api, auth, bot_identity, db, scanner, session_adapter, taskpool, wake
from scripts.unified_test_support import confirmed_requirement_fields


class ProjectRoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'codex').mkdir()
        (self.root / 'codex' / 'config.toml').write_text('default_permissions=":workspace"\n', encoding='utf-8')
        env = patch.dict(os.environ, {'GROUP_DB': str(self.root / 'group.db'),
                                     'HERMES_HOME': str(self.root / 'hermes')})
        env.start()
        self.addCleanup(env.stop)
        adapter = patch.object(session_adapter, 'available', return_value=False)
        adapter.start()
        self.addCleanup(adapter.stop)
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        db.init_schema(self.conn)
        for project in ('p', 'q'):
            self.conn.execute("INSERT INTO projects(name,root,created_at,directory_mode) VALUES(?,?,?,'shared')",
                              (project, str(self.root), db.now_iso()))
        self.conn.commit()
        app = FastAPI()
        app.include_router(api.router_api)
        self.client = TestClient(app, client=('127.0.0.1', 4321))
        self.addCleanup(self.client.close)

    def join(self, project='p', profile='default', host='codex'):
        response = self.client.post('/bots/register', json={'project': project,
            'host_type': host, 'profile': profile, 'config_root': str(self.root / host), 'display_name': '知夏'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['member_profile']

    def set_role(self, member, role, project='p'):
        response = self.client.patch(f'/projects/{project}/members/{member}/role', json={'role': role})
        self.assertEqual(response.status_code, 200, response.text)

    def roster(self, project='p', **params):
        response = self.client.get('/members', params={'project': project, 'active_only': True, **params})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['members']

    def test_fresh_database_has_no_member_role_or_permission_defaults(self):
        for table in ('members', 'project_members', 'project_permissions'):
            self.assertEqual(self.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)
        member = self.join()
        self.assertEqual(self.roster()[0]['role'], '')
        self.assertFalse(self.conn.execute('SELECT 1 FROM project_permissions WHERE profile=?', (member,)).fetchone())

    def test_arbitrary_roles_are_project_scoped_and_rejoin_preserves_them(self):
        member = self.join()
        self.assertEqual(self.join('q'), member)
        self.set_role(member, '质量把关人')
        self.set_role(member, '资料整理', 'q')
        self.assertEqual(self.join(), member)
        self.assertEqual(self.roster(role='质量把关人')[0]['profile'], member)
        self.assertEqual(self.roster('q', role='资料整理')[0]['profile'], member)
        self.assertEqual(self.roster('q', role='质量把关人'), [])
        self.assertEqual(self.client.get('/members').json()['members'][0]['role'], '')
        self.assertEqual(self.conn.execute('SELECT role FROM bots WHERE host_type=?', ('codex',)).fetchone()[0], '')
        self.assertEqual(self.conn.execute('SELECT display_name FROM members WHERE profile=?', (member,)).fetchone()[0], '知夏')

    def test_join_another_project_starts_with_empty_role_and_no_grants(self):
        member = self.join()
        self.set_role(member, '协调者')
        self.conn.execute("INSERT INTO project_permissions VALUES('p',?,'task.create')", (member,))
        self.conn.commit()
        self.join('q')
        self.assertEqual(self.roster('q')[0]['role'], '')
        self.assertFalse(self.conn.execute("SELECT 1 FROM project_permissions WHERE project='q'").fetchone())

    def test_disabled_and_foreign_members_are_excluded_from_enabled_roster(self):
        member = self.join()
        foreign = self.join('q', 'other')
        self.set_role(member, '协调者')
        self.set_role(foreign, '协调者', 'q')
        self.conn.execute("UPDATE project_members SET active=0 WHERE project='p' AND profile=?", (member,))
        self.conn.commit()
        self.assertEqual(self.roster(role='协调者'), [])
        self.assertEqual(self.roster('q', role='协调者')[0]['profile'], foreign)

    def test_role_edit_requires_admin_and_valid_project_membership(self):
        member = self.join()
        path = f'/projects/p/members/{member}/role'
        self.assertEqual(self.client.patch(path, headers={'X-Group-Profile': member, 'X-Group-Key': auth.bot_token(member)}, json={'role': '管理员'}).status_code, 403)
        for value in (None, 42, 'x\ny', 'x' * 201):
            self.assertEqual(self.client.patch(path, json={'role': value}).status_code, 400)
        self.assertEqual(self.client.patch(f'/projects/q/members/{member}/role', json={'role': '协调者'}).status_code, 404)
        self.assertEqual(self.client.get('/members', params={'role': '协调者'}).status_code, 400)
        self.set_role(member, '  自定义  ')
        self.assertEqual(self.roster(role='自定义')[0]['profile'], member)
        self.set_role(member, '')
        self.assertEqual(self.roster(role='')[0]['profile'], member)

    def test_workspace_and_roster_share_project_role_ids(self):
        member = self.join()
        self.join('q')
        ids = {}
        for project in ('p', 'q'):
            response = self.client.post(f'/projects/{project}/roles', json={'name': '协调者'})
            self.assertEqual(response.status_code, 200, response.text)
            ids[project] = response.json()['id']
            self.set_role(member, '协调者', project)
        self.assertNotEqual(ids['p'], ids['q'])
        for project in ('p', 'q'):
            response = self.client.get(f'/projects/{project}/workspaces')
            self.assertEqual(response.status_code, 200, response.text)
            workspace_member = next(m for m in response.json()['members'] if m['profile'] == member)
            self.assertEqual(workspace_member['role_id'], ids[project])
            self.assertEqual(self.roster(project)[0]['role_id'], ids[project])

    def test_registration_rejects_project_role_and_grants(self):
        for field, value in (('role', '协调者'), ('permissions', []), ('grants', {})):
            payload = {'host_type': 'codex', 'profile': 'default', 'project': 'p',
                       'config_root': str(self.root), field: value}
            self.assertEqual(self.client.post('/bots/register', json=payload).status_code, 400)
        self.assertFalse(self.conn.execute('SELECT 1 FROM members').fetchone())

    def task(self, member):
        fields = confirmed_requirement_fields('p')
        self.conn.execute("INSERT INTO project_permissions VALUES('p',?,'task.create')", (member,))
        self.conn.execute(
            "INSERT INTO requirements(id,project,title,created_at,updated_at,"
            "background_goal,scope,approach_basis,acceptance,undecided,source_ref) "
            "VALUES(1,'p','work','now','now',?,?,?,?,?,?)",
            (fields['background'], fields['scope'], fields['approach'],
             fields['acceptance'], fields['undecided'], fields['source_ref']))
        self.conn.commit()
        from scripts.test_task_brief import BRIEF
        result = taskpool.task_create('p', member, 1, [{'title': 'work', 'next_bot': member, **BRIEF}])
        self.assertNotIn('error', result, result)
        self.assertIn('task_ids', result, result)
        self.assertEqual(len(result['task_ids']), 1, result)
        return result['task_ids'][0]

    def test_scanner_dispatches_exact_bot_even_when_another_role_matches_its_handle(self):
        member = self.join()
        other = self.join(profile='second')
        self.set_role(other, member)
        self.conn.execute('UPDATE members SET role=? WHERE profile=?', (member, other))
        self.conn.commit()
        self.task(member)
        scanner.scan_tasks(self.conn)
        self.assertEqual([r[0] for r in self.conn.execute("SELECT profile FROM wake_jobs WHERE trigger='scan:task'")], [member])

    def test_approval_and_stale_wake_use_exact_holder(self):
        member = self.join()
        other = self.join(profile='second')
        self.conn.execute('UPDATE members SET role=? WHERE profile=?', (member, other))
        self.conn.commit()
        tid = self.task(member)
        self.conn.execute("UPDATE tasks SET status='in_progress',claimed_by=?,claimed_at='2000-01-01T00:00:00' WHERE id=?", (member, tid))
        self.conn.commit()
        scanner.scan_tasks(self.conn)
        self.assertEqual([r[0] for r in self.conn.execute('SELECT profile FROM wake_jobs')], [member])
        self.conn.execute("UPDATE tasks SET status='blocked',block_kind='decision' WHERE id=?", (tid,))
        self.conn.commit()
        with patch.object(wake, 'deliver_approved', return_value={'ok': True}) as deliver:
            response = self.client.post(f'/approval/{tid}/resolve', json={'action': 'approve', 'project': 'p'})
        self.assertEqual(response.status_code, 200, response.text)
        deliver.assert_called_once_with(member, tid, 'p')

    def test_old_handoff_key_rejection_rolls_back_entire_batch(self):
        member = self.join()
        self.task(member)
        before = tuple(self.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in ('tasks', 'nodes', 'events'))
        from scripts.test_task_brief import BRIEF
        result = taskpool.task_create('p', member, 1, [{'title': 'good', 'next_bot': member, **BRIEF},
                                                    {'title': 'old', 'next_role': member, **BRIEF}])
        self.assertIn('next_bot', result['error'])
        self.assertEqual(before, tuple(self.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in ('tasks', 'nodes', 'events')))

    def test_old_columns_migrate_once_without_reviving_cleared_roles(self):
        member = self.join()
        tid = self.task(member)
        self.conn.execute('ALTER TABLE tasks RENAME COLUMN next_bot TO next_role')
        self.conn.execute('ALTER TABLE project_members DROP COLUMN role')
        self.conn.execute('UPDATE members SET role=? WHERE profile=?', ('旧协调者', member))
        self.conn.commit()
        db.init_schema(self.conn)
        self.assertEqual(self.conn.execute('SELECT next_bot FROM tasks WHERE id=?', (tid,)).fetchone()[0], member)
        self.assertEqual(self.roster()[0]['role'], '旧协调者')
        self.set_role(member, '')
        db.init_schema(self.conn)
        self.assertEqual(self.roster()[0]['role'], '')
        self.assertEqual(self.conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_old_project_members_primary_key_rebuild_preserves_project_role(self):
        member = self.join(host='hermes')
        self.set_role(member, '本项目专属')
        self.conn.execute('ALTER TABLE project_members RENAME TO old_pm')
        self.conn.execute("CREATE TABLE project_members(project TEXT,profile TEXT,active INTEGER,role TEXT,PRIMARY KEY(project,profile))")
        self.conn.execute('INSERT INTO project_members SELECT project,profile,active,role FROM old_pm')
        self.conn.execute('DROP TABLE old_pm')
        self.conn.commit()
        db.init_schema(self.conn)
        self.assertEqual(self.roster()[0]['role'], '本项目专属')
        self.assertIsNotNone(self.conn.execute('SELECT bot_id FROM project_members').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
