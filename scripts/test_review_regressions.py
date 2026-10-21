"""Review regressions: temporary databases/homes and real isolated Git worktrees."""
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch

import tomlkit

from engine import auth, db, taskpool, wake, codex_project_access as access
from scripts import test_project_roles, test_project_workspaces
from scripts.test_task_brief import BRIEF
from scripts.unified_test_support import isolated_database, confirmed_requirement_fields, role_id_of


class EnrollmentAuthorizationTests(unittest.TestCase):
    setUp = test_project_roles.ProjectRoleTests.setUp

    def test_bot_and_anonymous_requests_cannot_mutate_members_or_config(self):
        config = self.root / 'codex' / 'config.toml'
        before = config.read_bytes()
        database = list(self.conn.iterdump())
        body = dict(project='p', host_type='codex', profile='default', config_root=str(config.parent))
        for password in ('', 'isolated-test-password'):
            with self.subTest(password_enabled=bool(password)), patch.dict(os.environ, {'GROUP_ADMIN_PASSWORD': password}):
                token = auth.login(password).get('token', '')
                bot = {'X-Group-Profile': 'bot', 'X-Group-Key': auth.bot_token('bot')}
                for headers in (bot, {**bot, 'Authorization': 'Bearer ' + token}):
                    self.assertEqual(self.client.post('/bots/register', json=body, headers=headers).status_code, 403)
                if password:
                    self.assertEqual(self.client.post('/bots/register', json=body).status_code, 401)
                self.assertEqual(list(self.conn.iterdump()), database)
                self.assertEqual(config.read_bytes(), before)
                self.assertFalse((config.parent / '.group-engine-backups').exists())
        with patch.dict(os.environ, {'GROUP_ADMIN_PASSWORD': 'isolated-test-password'}):
            token = auth.login('isolated-test-password')['token']
            response = self.client.post('/bots/register', json=body, headers={'Authorization': 'Bearer ' + token})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotEqual(config.read_bytes(), before)


class TaskHandoffRaceTests(unittest.TestCase):
    def test_cancel_during_handoff_leaves_task_cancelled(self):
        with isolated_database():
            rid = taskpool.req_create('alpha', 'default', 'race', **confirmed_requirement_fields())['requirement_id']
            tid = taskpool.task_create('alpha', 'architect', rid, [{'title': 'work', 'next_bot': 'dev', **BRIEF}])['task_ids'][0]
            self.assertTrue(taskpool.task_claim('alpha', 'dev')['claimed'])
            role = role_id_of('alpha', 'reviewer')
            read, release, cancelling, cancelled = (threading.Event() for _ in range(4))
            original = taskpool._conn

            class Cursor:
                def __init__(self, cursor):
                    self.cursor = cursor

                def fetchone(self):
                    row = self.cursor.fetchone()
                    read.set()
                    if not release.wait(5):
                        raise RuntimeError('handoff was not released')
                    return row

            class Connection:
                def __init__(self, connection):
                    self.connection = connection

                def __getattr__(self, name):
                    return getattr(self.connection, name)

                def execute(self, sql, *args):
                    cursor = self.connection.execute(sql, *args)
                    if sql.startswith('SELECT * FROM tasks WHERE id=? AND project=?'):
                        return Cursor(cursor)
                    return cursor

            local = threading.local()

            def connect():
                connection = original()
                return Connection(connection) if getattr(local, 'handoff', False) else connection

            def handoff():
                local.handoff = True
                return taskpool.task_log('alpha', 'dev', tid, 'dev', result='verified',
                                         next_bot='reviewer', expected_role_id=role)

            # Use an independent writer without schema initialization, which
            # would otherwise serialize requests before reaching the handoff.
            def cancel():
                connection = db.get_connection()
                try:
                    cancelling.set()
                    connection.execute("UPDATE tasks SET status='cancelled',next_bot='',claimed_by='' WHERE id=?", (tid,))
                    connection.commit()
                    cancelled.set()
                finally:
                    connection.close()

            with patch.object(taskpool, '_conn', side_effect=connect), ThreadPoolExecutor(max_workers=2) as pool:
                pending = pool.submit(handoff)
                try:
                    self.assertTrue(read.wait(5))
                    cancellation = pool.submit(cancel)
                    self.assertTrue(cancelling.wait(5))
                    cancelled.wait(0.2)
                finally:
                    release.set()
                self.assertTrue(pending.result(timeout=5)['logged'])
                cancellation.result(timeout=5)
            with closing(db.get_connection()) as conn:
                self.assertEqual(tuple(conn.execute('SELECT status,next_bot,claimed_by FROM tasks WHERE id=?', (tid,)).fetchone()),
                                 ('cancelled', '', ''))
                self.assertIn('error', taskpool.task_log('alpha', 'dev', tid, 'dev', result='late result',
                                                        next_bot='reviewer', expected_role_id=role))


class WorkspaceGrantTests(unittest.TestCase):
    setUp = test_project_workspaces.WorkspaceTests.setUp
    run_git = test_project_workspaces.WorkspaceTests.run_git

    def member(self):
        home = self.base / 'codex'
        home.mkdir()
        self.config = home / 'config.toml'
        self.config.write_text('default_permissions=":read-only"\napproval_policy="on-request"\napprovals_reviewer="auto_review"\n', encoding='utf-8')
        self.conn.execute("UPDATE projects SET directory_mode='shared' WHERE name='p'")
        self.conn.commit()
        response = self.client.post('/bots/register', json=dict(project='p', host_type='codex', profile='default', config_root=str(home)))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['member_profile']

    def grants(self):
        config = tomlkit.parse(self.config.read_text(encoding='utf-8'))
        self.assertEqual(config['approval_policy'], 'on-request')
        self.assertEqual(config['approvals_reviewer'], 'auto_review')
        return config['permissions'][config['default_permissions']]['filesystem']

    def test_create_adopt_and_member_mode_changes_grant_selected_directory(self):
        member = self.member()
        with patch.object(wake, 'find_bot_processes', return_value=[]):
            response = self.client.patch('/projects/p', json={'directory_mode': 'worktree'})
            self.assertEqual(response.status_code, 200, response.text)
            custom = self.base / 'custom-tree'
            response = self.client.post(f'/projects/p/workspaces/{member}', json={'action': 'create', 'path': str(custom)})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.grants()[custom.as_posix()], 'write')
            for mode, target in (('shared', self.root), ('worktree', custom)):
                config = tomlkit.parse(self.config.read_text())
                del config['permissions'][config['default_permissions']]['filesystem'][target.as_posix()]
                self.config.write_text(tomlkit.dumps(config), encoding='utf-8')
                response = self.client.post(f'/projects/p/workspaces/{member}', json={'action': 'set_mode', 'mode': mode})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.grants()[target.as_posix()], 'write')
            # Removing only the grant simulates a pre-fix, already assigned tree.
            config = tomlkit.parse(self.config.read_text())
            del config['permissions'][config['default_permissions']]['filesystem'][custom.as_posix()]
            self.config.write_text(tomlkit.dumps(config), encoding='utf-8')
            response = self.client.post(f'/projects/p/workspaces/{member}', json={'action': 'adopt', 'path': str(custom)})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.grants()[custom.as_posix()], 'write')

    def test_project_root_change_and_failed_grant_are_atomic(self):
        self.member()
        target = self.base / 'relocated'
        target.mkdir()
        with patch.object(wake, 'find_bot_processes', return_value=[]):
            before = self.config.read_bytes()
            with patch.object(access.files, '_stage', side_effect=PermissionError('synthetic failure')):
                with self.assertRaises(PermissionError):
                    self.client.patch('/projects/p', json={'root': str(target)})
            self.assertEqual(self.conn.execute("SELECT root FROM projects WHERE name='p'").fetchone()[0], str(self.root))
            self.assertEqual(self.config.read_bytes(), before)
            response = self.client.patch('/projects/p', json={'root': str(target)})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.grants()[target.as_posix()], 'write')

    def test_workspace_grant_failure_preserves_mapping_and_recoverable_git_tree(self):
        member = self.member()
        with patch.object(wake, 'find_bot_processes', return_value=[]):
            self.assertEqual(self.client.patch('/projects/p', json={'directory_mode': 'worktree'}).status_code, 200)
            target = self.base / 'failed-tree'
            before = self.config.read_bytes()
            with patch.object(access.files, '_stage', side_effect=PermissionError('synthetic failure')):
                with self.assertRaises(PermissionError):
                    self.client.post(f'/projects/p/workspaces/{member}', json={'action': 'create', 'path': str(target)})
            self.assertIsNone(self.conn.execute('SELECT path FROM project_workspaces WHERE profile=?', (member,)).fetchone())
            self.assertEqual(self.config.read_bytes(), before)
            self.assertTrue(target.is_dir())
            response = self.client.post(f'/projects/p/workspaces/{member}', json={'action': 'adopt', 'path': str(target)})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.grants()[target.as_posix()], 'write')


if __name__ == '__main__':
    unittest.main()
