"""Soft removal preserves data and releases directories; isolated HTTP/SQLite."""
import unittest
from unittest.mock import patch

from engine import db, project_workspaces, taskpool
from scripts import test_project_import as imports


class RemovalTests(unittest.TestCase):
    setUp = imports.ImportTests.setUp

    def register(self):
        response = self.client.post('/projects/import', json=self.body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['project']

    def test_remove_preserves_data_files_and_reimport_blocks_restore(self):
        old = self.register()
        self.conn.execute('INSERT INTO requirements(project,title,created_at,updated_at) VALUES(?,?,?,?)', (old, 'History', 'now', 'now'))
        self.conn.commit()
        before = imports.fingerprints(self.root)
        preserved = {table: [tuple(r) for r in self.conn.execute(f'SELECT * FROM {table} WHERE project=?', (old,))]
                     for table in ('requirements', 'project_members', 'project_roles', 'project_permissions', 'settings')}
        self.assertEqual(self.client.post(f'/projects/{old}/remove').status_code, 200)
        from engine import wake
        profile = self.conn.execute('SELECT profile FROM project_members WHERE project=? LIMIT 1', (old,)).fetchone()[0]
        self.assertFalse(wake.enqueue(self.conn, profile, 'work', 'test', project=old))
        for table, rows in preserved.items():
            self.assertEqual(rows, [tuple(r) for r in self.conn.execute(f'SELECT * FROM {table} WHERE project=?', (old,))])
        self.assertEqual(before, imports.fingerprints(self.root))
        self.assertEqual(self.client.get('/projects?include_archived=true').json()['projects'], [])
        self.assertEqual(self.client.get('/projects/removed').status_code, 405)
        new = self.register()
        self.assertNotEqual(old, new)
        self.assertEqual(self.client.post(f'/projects/{old}/restore').status_code, 404)
        self.assertEqual(self.client.post(f'/projects/{old}/archive', json={'archived': False}).status_code, 409)
        self.assertEqual(self.client.post(f'/projects/{new}/remove').status_code, 200)
        self.assertEqual(self.client.post(f'/projects/{old}/restore').status_code, 404)
        self.assertEqual(self.client.get('/projects').json()['projects'], [])
        db.init_schema(self.conn)
        self.assertEqual(self.conn.execute('SELECT removed FROM projects WHERE name=?', (new,)).fetchone()[0], 1)

    def test_tasks_jobs_processes_and_workspaces_block_removal(self):
        name = self.register()
        req = self.conn.execute("INSERT INTO requirements(project,title,created_at,updated_at) VALUES(?,'req','now','now')", (name,)).lastrowid
        self.conn.execute("INSERT INTO tasks(project,requirement_id,title,status,created_at) VALUES(?,?,'busy','pending','now')", (name, req))
        self.conn.commit()
        self.assertEqual(self.client.post(f'/projects/{name}/remove').status_code, 409)
        self.conn.execute('DELETE FROM tasks WHERE project=?', (name,))
        self.conn.execute("INSERT INTO wake_jobs(profile,project,kind,trigger,enqueued_at) VALUES('bot',?,'work','test','now')", (name,))
        self.conn.commit()
        self.assertEqual(self.client.post(f'/projects/{name}/remove').status_code, 409)
        self.conn.execute('DELETE FROM wake_jobs WHERE project=?', (name,))
        self.conn.commit()
        with patch('engine.wake.active_runs', return_value=[{'project': name}]):
            self.assertEqual(self.client.post(f'/projects/{name}/remove').status_code, 409)
        self.conn.execute('INSERT INTO project_workspaces VALUES(?,?,?)', (name, 'bot', str(self.base / 'workspace')))
        self.conn.commit()
        self.assertEqual(self.client.post(f'/projects/{name}/remove').status_code, 409)
        self.assertEqual(self.conn.execute('SELECT removed FROM projects WHERE name=?', (name,)).fetchone()[0], 0)

    def test_removed_projects_hidden_and_removal_authorization(self):
        for i in range(25):
            self.conn.execute("INSERT INTO projects(name,root,display_name,archived,removed,created_at) VALUES(?,?,?,1,1,'now')",
                              (f'old-{i:02}', str(self.root), f'Project {i:02}'))
        self.conn.commit()
        self.assertEqual(self.client.get('/projects?include_archived=true').json()['projects'], [])
        self.assertEqual(self.client.get('/projects/removed?search=Project%2024').status_code, 405)
        from engine import api
        self.app.dependency_overrides[api._resolve_identity] = lambda: ('bot', 'bot')
        self.assertEqual(self.client.post('/projects/old-00/remove').status_code, 403)

    def test_archive_restore_also_checks_directory_conflicts(self):
        old = self.register()
        self.assertNotIn('error', taskpool.project_archive(old))
        self.assertEqual(self.client.post('/projects/import', json=self.body).status_code, 409)
        # Simulate a historical duplicate to keep the restore collision guard covered.
        self.assertNotIn('error', taskpool.project_add('legacy-duplicate', str(self.root)))
        self.assertIn('error', taskpool.project_archive(old, False))

    def assert_restore_blocked(self, name, message):
        response = self.client.post(f'/projects/{name}/archive', json={'archived': False})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn(message, response.text)
        self.assertIn('项目保持归档', response.text)
        self.assertEqual(self.conn.execute('SELECT archived FROM projects WHERE name=?', (name,)).fetchone()[0], 1)

    def test_restore_missing_root_preserves_archive_and_can_retry(self):
        name = self.register()
        self.assertNotIn('error', taskpool.project_archive(name))
        backup = self.base / 'saved-root'
        self.root.rename(backup)
        self.assert_restore_blocked(name, '项目主目录不存在')
        self.assertFalse(self.root.exists())
        backup.rename(self.root)
        self.assertEqual(self.client.post(f'/projects/{name}/archive', json={'archived': False}).status_code, 200)

    def test_restore_checks_git_and_registered_worktrees(self):
        import subprocess
        def git(root, *args):
            subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)
        git(self.root, 'init')
        git(self.root, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit', '--allow-empty', '-m', 'Initial')
        self.body.update(directory_mode='worktree', worktree_root=str(self.base / 'worktrees'))
        name = self.register()
        tree = self.base / 'worktrees' / 'member'
        git(self.root, 'worktree', 'add', '--detach', str(tree))
        self.conn.execute('INSERT INTO project_workspaces VALUES(?,?,?)', (name, 'member', str(tree)))
        self.conn.commit()
        self.assertNotIn('error', taskpool.project_archive(name))
        saved_git = self.base / 'saved-git'
        (self.root / '.git').rename(saved_git)
        self.assert_restore_blocked(name, '项目主仓库无效')
        saved_git.rename(self.root / '.git')
        saved_tree = self.base / 'saved-tree'
        tree.rename(saved_tree)
        self.assert_restore_blocked(name, '成员 member 的 worktree 无效')
        self.assertFalse(tree.exists())
        tree.mkdir()
        git(tree, 'init')
        git(tree, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
            'commit', '--allow-empty', '-m', 'Different repository')
        self.assert_restore_blocked(name, '工作目录属于其他仓库')
        tree.rename(self.base / 'unrelated-repository')
        saved_tree.rename(tree)
        self.assertEqual(self.client.post(f'/projects/{name}/archive', json={'archived': False}).status_code, 200)

    def test_legacy_remove_is_logical_and_failure_rolls_back(self):
        name = self.register()
        with patch.object(project_workspaces, 'audit', side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):
                self.client.post(f'/projects/{name}/remove')
        self.assertEqual(self.conn.execute('SELECT removed FROM projects WHERE name=?', (name,)).fetchone()[0], 0)
        self.assertEqual(taskpool.project_remove(name), {'removed': name})
        self.assertIsNotNone(self.conn.execute('SELECT * FROM projects WHERE name=?', (name,)).fetchone())

    def test_concurrent_removal_is_idempotent(self):
        from concurrent.futures import ThreadPoolExecutor
        first = self.register()
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda name: self.client.post(f'/projects/{name}/remove').status_code, (first, first)))
        self.assertEqual(statuses, [200, 200])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM projects WHERE removed=1').fetchone()[0], 1)

    def test_removed_worktree_configuration_releases_root(self):
        import subprocess
        def git(*args):
            subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True)
        git('init')
        git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'Initial')
        self.body.update(directory_mode='worktree', worktree_root=str(self.base / 'worktrees'))
        old = self.register()
        self.assertEqual(self.client.post(f'/projects/{old}/remove').status_code, 200)
        new = self.register()
        self.assertNotEqual(old, new)
        self.assertEqual(self.client.post(f'/projects/{old}/restore').status_code, 404)


if __name__ == '__main__':
    unittest.main()
