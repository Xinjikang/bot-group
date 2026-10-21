"""Project identity and worktree lifecycle against isolated SQLite and real local Git."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from scripts.unified_test_support import REQUIREMENT_FIELDS

from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from engine import api, db, taskpool, project_workspaces as ws, wake


class WorkspaceTests(unittest.TestCase):
    def set_mode(self, profile, mode):
        return self.client.post('/projects/p/workspaces/' + profile, json={'action': 'set_mode', 'mode': mode})

    def test_default_worktree_and_retained_mapping_after_mode_switch(self):
        self.assertEqual(wake._workdir_for('default', 'p'), str(self.root))
        created = self.create('default')
        path = created['path']
        self.assertEqual(wake._workdir_for('default', 'p'), path)
        # Main repository state must not come from default's detached worktree.
        self.assertEqual(ws.view('p')['repository']['path'], str(self.root))
        self.assertIn('"目录模式": "独立 worktree"', wake.wake_system_prompt('work', 'p', path))
        self.assertEqual(self.set_mode('default', 'shared').status_code, 200)
        self.assertTrue(Path(path).is_dir())
        member = ws.view('p', 'default')['members'][0]
        self.assertEqual(member['directory_mode'], 'shared')
        self.assertEqual(member['assigned_workspace_path'], path)
        self.assertEqual(member['workspace']['path'], str(self.root))
        self.assertIn('"目录模式": "共用项目主目录"', wake.wake_system_prompt('work', 'p', str(self.root)))
        self.assertEqual(self.set_mode('default', 'worktree').status_code, 200)
        self.assertEqual(wake._workdir_for('default', 'p'), path)
        self.set_mode('default', 'shared')
        ws.manage('p', 'default', {'action': 'remove'}, 'user')
        self.assertFalse(Path(path).exists())
        self.assertEqual(wake._workdir_for('default', 'p'), str(self.root))

    def test_modes_scoped_to_project_and_idle_validation(self):
        taskpool.project_add('q', str(self.root), directory_mode='worktree', worktree_root=str(self.base / 'other'))
        self.assertEqual(self.set_mode('dev', 'shared').status_code, 200)
        self.assertEqual(wake._workdir_for('dev', 'p'), str(self.root))
        self.assertEqual(ws.member_mode(self.conn, ws.project(self.conn, 'q'), 'dev'), 'worktree')
        self.assertEqual(self.set_mode('reviewer', 'shared').status_code, 200)
        self.assertEqual(wake._workdir_for('reviewer', 'p'), str(self.root))
        self.assertEqual(self.set_mode('dev', 'invalid').status_code, 400)
        self.assertEqual(self.set_mode('dev', 'worktree').status_code, 409)
        self.assertEqual(wake._workdir_for('dev', 'p'), str(self.root))
        with patch.object(wake, 'active_runs', return_value=[{'project': 'p', 'profile': 'dev'}]):
            self.assertEqual(self.set_mode('dev', 'shared').status_code, 409)
        self.assertEqual(self.set_mode('user', 'shared').status_code, 400)

    def test_default_adopts_existing_worktree_and_migration_preserves_choice(self):
        path = self.base / 'trees' / 'default'
        self.run_git('worktree', 'add', '--detach', str(path))
        ws.manage('p', 'default', {'action': 'adopt'}, 'user')
        db.init_schema(self.conn)
        db.init_schema(self.conn)
        self.assertEqual(wake._workdir_for('default', 'p'), str(path))
        self.assertEqual(self.set_mode('reviewer', 'shared').status_code, 200)
        with self.assertRaises(HTTPException):
            ws.manage('p', 'reviewer', {'action': 'adopt', 'path': str(path)}, 'user')

    def test_project_removal_preserves_member_directory_preferences(self):
        self.set_mode('dev', 'shared')
        taskpool.project_remove('p')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM project_workspace_modes').fetchone()[0], 1)

    def test_workspace_view_batches_repository_reads_and_scopes_member(self):
        self.create()
        with patch.object(ws, 'git', wraps=ws.git) as commands:
            result = ws.view('p')
        self.assertEqual(sum(call.args[1:] == ('worktree', 'list', '--porcelain') for call in commands.call_args_list), 1)
        dev = next(m for m in result['members'] if m['profile'] == 'dev')
        self.assertTrue(dev['workspace_assigned'])
        self.assertTrue(dev['workspace']['valid'])
        partial = self.client.get('/projects/p/workspaces?profile=dev').json()
        self.assertEqual([m['profile'] for m in partial['members']], ['dev'])

    def test_create_generates_stable_id_from_display_name(self):
        body = {'display_name': '中文项目', 'root': str(self.root)}
        first = self.client.post('/projects', json=body)
        second = self.client.post('/projects', json=body)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        project_id = first.json()['project']
        self.assertRegex(project_id, r'^p_[0-9a-f]{32}$')
        self.assertNotEqual(project_id, second.json()['project'])
        self.assertEqual(self.client.patch('/projects/' + project_id, json={'display_name': '改名'}).status_code, 200)
        row = self.conn.execute('SELECT name,display_name FROM projects WHERE name=?', (project_id,)).fetchone()
        self.assertEqual(tuple(row), (project_id, '改名'))
        self.assertEqual(self.client.post('/projects', json={**body, 'display_name': ' '}).status_code, 400)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / 'repo'
        self.root.mkdir()
        self.patch = patch.dict(os.environ, {'GROUP_DB': str(self.base / 'group.db')})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.run_git('init')
        self.run_git('config', 'user.name', 'Test')
        self.run_git('config', 'user.email', 'test@example.test')
        (self.root / 'tracked.txt').write_text('initial', encoding='utf-8')
        self.run_git('add', 'tracked.txt')
        self.run_git('commit', '-m', 'initial')
        self.conn = db.get_connection()
        db.init_schema(self.conn)
        self.addCleanup(self.conn.close)
        self.conn.executemany('INSERT INTO members(profile,display_name) VALUES(?,?)',
                             [('default', 'D'), ('dev', 'Dev'), ('reviewer', 'Reviewer')])
        self.conn.commit()
        taskpool.project_add('p', str(self.root), directory_mode='worktree', worktree_root=str(self.base / 'trees'))
        from scripts.project_test_support import enable_members
        enable_members(self.conn, ('p',))
        # 预插一条用户消息（id=1），供 REQUIREMENT_FIELDS 的 source_ref 引用。
        self.conn.execute("INSERT INTO messages(project,ts,sender,sender_type,content,mentions) VALUES('p',?,'user','user','fixture source','[]')", (db.now_iso(),))
        self.conn.commit()
        self.app = FastAPI()
        self.app.include_router(api.router_api)
        self.app.dependency_overrides[api._resolve_identity] = lambda: ('user', 'user')
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def run_git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.STDOUT, text=True).strip()

    def create(self, profile='dev', **body):
        return ws.manage('p', profile, {'action': 'create', **body}, 'user')

    def test_detached_creation_and_launch_preserve_main_branch(self):
        branch = self.run_git('branch', '--show-current')
        result = self.create()
        state = ws.inspect(self.root, result['path'])
        self.assertTrue(state['detached'])
        self.assertEqual(state['head'], self.run_git('rev-parse', 'HEAD'))
        self.assertEqual(self.run_git('branch', '--show-current'), branch)
        self.assertEqual(wake._workdir_for('dev', 'p'), result['path'])
        self.assertEqual(wake._workdir_for('default', 'p'), str(self.root))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM project_config_events WHERE action='workspace.create'").fetchone()[0], 1)

    def test_adopt_preserves_dirty_files_and_branch(self):
        path = self.base / 'trees' / 'dev'
        self.run_git('worktree', 'add', '-b', 'existing', str(path))
        (path / 'tracked.txt').write_text('unfinished', encoding='utf-8')
        ws.manage('p', 'dev', {'action': 'adopt'}, 'user')
        state = ws.inspect(self.root, path)
        self.assertEqual(state['branch'], 'existing')
        self.assertTrue(state['dirty'])
        with self.assertRaises(HTTPException):
            ws.manage('p', 'dev', {'action': 'remove'}, 'user')
        self.assertTrue(path.is_dir())

    def test_explicit_start_and_no_implicit_creation(self):
        original = self.run_git('rev-parse', 'HEAD')
        (self.root / 'tracked.txt').write_text('second', encoding='utf-8')
        self.run_git('commit', '-am', 'second')
        with self.assertRaises(HTTPException):
            wake._workdir_for('dev', 'p')
        result = self.create(start_ref=original)
        self.assertEqual(ws.inspect(self.root, result['path'])['head'], original)

    def test_invalid_ref_and_main_directory_rejected(self):
        for body in ({'start_ref': 'missing-ref'}, {'path': str(self.root)}, {'start_ref': '--help'}):
            with self.assertRaises(HTTPException):
                self.create(**body)
        self.assertFalse((self.base / 'trees' / 'dev').exists())

    def test_foreign_repo_and_duplicate_mapping_rejected(self):
        foreign = self.base / 'foreign'
        foreign.mkdir()
        subprocess.check_call(['git', '-C', str(foreign), 'init'], stdout=subprocess.DEVNULL)
        with self.assertRaises(HTTPException):
            ws.manage('p', 'dev', {'action': 'adopt', 'path': str(foreign)}, 'user')
        result = self.create()
        with self.assertRaises(HTTPException):
            ws.manage('p', 'reviewer', {'action': 'adopt', 'path': result['path']}, 'user')

    def test_remove_keeps_main_and_registration_cleans_up(self):
        result = self.create()
        ws.manage('p', 'dev', {'action': 'remove'}, 'user')
        self.assertFalse(Path(result['path']).exists())
        self.assertTrue(self.root.is_dir())
        self.assertEqual(len(ws.registrations(self.root)), 1)

    def test_running_bot_blocks_operations(self):
        with patch.object(wake, 'active_runs', return_value=[{'project': 'p', 'profile': 'dev'}]):
            with self.assertRaises(HTTPException):
                self.create()

    def test_display_rename_preserves_all_project_references(self):
        self.create()
        self.conn.execute("INSERT INTO project_permissions VALUES('p','default','req.create')")
        self.conn.commit()
        rid = taskpool.req_create('p', 'default', 'requirement', **REQUIREMENT_FIELDS)['requirement_id']
        response = self.client.patch('/projects/p', json={'display_name': 'New label'})
        self.assertEqual(response.status_code, 200, response.text)
        row = self.conn.execute("SELECT name,display_name FROM projects WHERE name='p'").fetchone()
        self.assertEqual(tuple(row), ('p', 'New label'))
        self.assertEqual(self.conn.execute('SELECT project FROM requirements WHERE id=?', (rid,)).fetchone()[0], 'p')
        self.assertEqual(self.conn.execute('SELECT project FROM project_workspaces').fetchone()[0], 'p')
        self.assertEqual(wake.session_name('dev', 'work', project='p'), 'dev-p-work')

    def test_modes_and_api_permissions(self):
        response = self.client.patch('/projects/p', json={'directory_mode': 'shared'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(wake._workdir_for('dev', 'p'), str(self.root))
        self.app.dependency_overrides[api._resolve_identity] = lambda: ('dev', 'bot')
        self.assertEqual(self.client.post('/projects/p/workspaces/dev', json={'action': 'create'}).status_code, 403)

    def test_cross_project_worktree_root_and_id_change_rejected(self):
        taskpool.project_add('q', str(self.root))
        response = self.client.patch('/projects/q', json={'directory_mode': 'worktree', 'worktree_root': str(self.base / 'trees')})
        self.assertEqual(response.status_code, 409, response.text)
        response = self.client.patch('/projects/p', json={'name': 'changed-id'})
        self.assertEqual(response.status_code, 400)

    def test_legacy_id_migration_idempotent(self):
        self.conn.execute("UPDATE projects SET display_name='',directory_mode='' WHERE name='p'")
        self.conn.commit()
        db.init_schema(self.conn)
        db.init_schema(self.conn)
        row = ws.project(self.conn, 'p')
        self.assertEqual(row['display_name'], 'p')
        self.assertEqual(row['directory_mode'], 'worktree')


if __name__ == '__main__':
    unittest.main()
