"""Explicit per-project owner configuration and notification selection."""
import unittest
from unittest.mock import patch
from engine import db, taskpool, task_retry, api, wake
from scripts import test_abnormal_exit as fixtures


class ProjectOwnerTests(unittest.TestCase):
    setUp = fixtures.AbnormalExitTests.setUp

    def test_wake_owner_profile_is_current_scoped_and_available(self):
        def check(expected, project='p'):
            for kind in ('work', 'chat'):
                prompt = wake.wake_system_prompt(kind, project, self.tmp.name)
                self.assertIn('"项目负责人": "' + expected + '"', prompt)
        taskpool._save_project_owner(self.conn, 'p', 'dev-1')
        self.conn.commit()
        check('dev-1')  # Owner still sees its own identity; self-escalation stays in the rules.
        check('default', 'q')
        self.conn.execute("UPDATE project_members SET active=0 WHERE project='p' AND profile='dev-1'")
        self.conn.commit()
        check('user')
        for owner in ('missing', '', 'user'):
            taskpool._save_project_owner(self.conn, 'p', owner)
            self.conn.commit()
            check('user')

    def test_save_read_clear_and_project_isolation(self):
        with patch.object(taskpool, '_conn', side_effect=db.get_connection):
            self.assertNotIn('error', taskpool.project_update('p', owner_profile='dev-2'))
            projects = {p['name']: p for p in taskpool.project_list()['projects']}
            self.assertEqual(projects['p']['owner_profile'], 'dev-2')
            self.assertEqual(projects['q']['owner_profile'], 'default')
            self.assertEqual(task_retry.notification_targets(self.conn, 'p', 'dev-1'), ['dev-2'])
            taskpool.project_update('p', owner_profile='')
            self.assertEqual(task_retry.notification_targets(self.conn, 'p', 'dev-1'), ['user'])

    def test_invalid_or_disabled_owner_rejected_without_other_edits(self):
        self.conn.execute("UPDATE project_members SET active=0 WHERE project='p' AND profile='dev-2'")
        self.conn.commit()
        with patch.object(taskpool, '_conn', side_effect=db.get_connection):
            for owner in ('missing', 'dev-2', 123):
                self.assertIn('error', taskpool.project_update('p', root='should-not-write', owner_profile=owner))
        self.assertEqual(self.conn.execute("SELECT root FROM projects WHERE name='p'").fetchone()[0], self.tmp.name)

    def test_disabled_or_self_owner_falls_back_to_user(self):
        taskpool._save_project_owner(self.conn, 'p', 'dev-1')
        self.assertEqual(task_retry.notification_targets(self.conn, 'p', 'dev-1'), ['user'])
        taskpool._save_project_owner(self.conn, 'p', 'dev-2')
        self.conn.execute("UPDATE project_members SET active=0 WHERE project='p' AND profile='dev-2'")
        self.assertEqual(task_retry.notification_targets(self.conn, 'p', 'dev-1'), ['user'])

    def test_creation_and_rename_preserve_owner(self):
        with patch.object(taskpool, '_conn', side_effect=db.get_connection), patch.object(taskpool, '_sync_session_titles', return_value=0):
            self.assertNotIn('error', taskpool.project_add('new', '.'))
            api.set_member_active('dev-2', {'project': 'new', 'active': True}, ('user', 'user'))
            self.assertNotIn('error', taskpool.project_update('new', owner_profile='dev-2'))
            taskpool.project_update('new', new_name='renamed')
        self.assertEqual(task_retry.notification_targets(self.conn, 'new', 'dev-1'), ['dev-2'])
        self.assertEqual(self.conn.execute("SELECT display_name FROM projects WHERE name='new'").fetchone()[0], 'renamed')

    def test_api_owner_only_update_and_invalid_owner(self):
        with patch.object(taskpool, '_conn', side_effect=db.get_connection):
            api.patch_project('p', {'owner_profile': 'dev-2'}, ('user', 'user'))
            self.assertEqual(task_retry.notification_targets(self.conn, 'p', 'dev-1'), ['dev-2'])
            with self.assertRaises(api.HTTPException) as error:
                api.post_project({'name': 'bad', 'root': '.', 'owner_profile': 'missing'}, ('user', 'user'))
            self.assertEqual(error.exception.status_code, 400)

    def test_removed_project_preserves_owner_and_requires_new_id(self):
        with patch.object(taskpool, '_conn', side_effect=db.get_connection):
            taskpool.project_add('temporary', '.', owner_profile='dev-2')
            taskpool.project_remove('temporary')
            self.assertIn('error', taskpool.project_add('temporary', '.'))
        self.assertEqual(self.conn.execute("SELECT value FROM settings WHERE project='temporary' AND key='owner_profile'").fetchone()[0], 'dev-2')


if __name__ == '__main__':
    unittest.main()
