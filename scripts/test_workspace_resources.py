"""Workspace resource persistence, isolation and wake injection in disposable projects."""
import copy
import json
import socket
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from engine import project_import, workspace_resources as resources, wake
from scripts import test_project_setup as setup_tests


class ResourceTests(unittest.TestCase):
    setUp = setup_tests.SetupTests.setUp
    configured = setup_tests.SetupTests.configured
    configured_setup = setup_tests.SetupTests.configured_setup
    reviewed = setup_tests.SetupTests.reviewed
    submit = setup_tests.SetupTests.submit

    def setup_project(self, worktree=False):
        body = self.configured_setup(new=True) if worktree else self.configured()
        body['members'].append({'bot_id': self.ids[1], 'role': '', 'permissions': []})
        body['workspace_resources'] = []
        preview = project_import.preview(body)
        body['workspace_resources'] = preview['workspace_resources']
        body, _ = self.reviewed(body)
        response = self.submit(body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json(), body

    def test_readonly_plan_and_saved_wake_json(self):
        body = self.configured_setup(new=True)
        body['workspace_resources'] = []
        preview = project_import.preview(body)
        self.assertEqual(len(preview['workspace_resources']), len(body['members']))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM workspace_resources').fetchone()[0], 0)
        self.assertFalse((self.base / 'fresh').exists())
        result, body = self.setup_project(True)
        path = self.conn.execute('SELECT path FROM project_workspaces').fetchone()[0]
        first = wake.wake_system_prompt('work', result['project'], path)
        second = wake.wake_system_prompt('work', result['project'], path)
        self.assertEqual(first, second)
        self.assertIn('工作区配置', first)
        self.assertIn('浏览器', first)
        self.assertIn(resources.RULE, first)
        self.assertNotIn('数据库名', first)
        stored = json.loads(self.conn.execute('SELECT config FROM workspace_resources').fetchone()[0])
        self.assertEqual(stored, resources.normalize(body['workspace_resources'][0]['config']))

    def test_shared_members_use_same_config_and_stale_revision_rejected(self):
        result, body = self.setup_project()
        project = result['project']
        profiles = [r[0] for r in self.conn.execute('SELECT profile FROM project_members WHERE project=?', (project,))]
        first = resources.member(project, profiles[0])
        self.assertEqual(first['config'], resources.member(project, profiles[-1])['config'])
        self.assertEqual(set(first['members']), set(profiles))
        config = copy.deepcopy(first['config']); config['services'] = {}
        url = f'/projects/{project}/workspaces/{profiles[0]}/resources'
        with patch('engine.project_workspaces.ensure_idle'):
            saved = self.client.put(url, json={'config': config, 'revision': first['revision']})
            self.assertEqual(saved.status_code, 200, saved.text)
            stale = self.client.put(url, json={'config': config, 'revision': first['revision']})
            self.assertEqual(stale.status_code, 409)
        self.assertEqual(resources.member(project, profiles[-1])['config'], config)

    def test_busy_and_nonadmin_changes_blocked(self):
        result, _ = self.setup_project()
        profile = result['members'][0]; project = result['project']
        current = resources.member(project, profile)
        with patch('engine.project_workspaces.ensure_idle', side_effect=HTTPException(409, 'running')):
            response = self.client.put(f'/projects/{project}/workspaces/{profile}/resources', json=current)
        self.assertEqual(response.status_code, 409)
        from engine import api
        self.app.dependency_overrides[api._resolve_identity] = lambda: (profile, 'bot')
        response = self.client.get(f'/projects/{project}/workspaces/{profile}/resources')
        self.assertEqual(response.status_code, 403)

    def test_port_and_directory_conflicts(self):
        _, body = self.setup_project()
        config = body['workspace_resources'][0]['config']
        with self.assertRaises(HTTPException):
            resources.validate(config, [config])
        overlap = copy.deepcopy(config)
        overlap['test_data_directory'] = overlap['browser']['data_directory']
        with self.assertRaises(HTTPException):
            resources.validate(overlap, [])
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0)); listener.listen()
            occupied = copy.deepcopy(config)
            occupied['services']['backend']['port'] = listener.getsockname()[1]
            with self.assertRaises(HTTPException):
                resources.validate(occupied, [])

    def test_invalid_types_and_duplicate_ports(self):
        config = resources.defaults(str(self.root), 'test', set())
        bad = copy.deepcopy(config); bad['services']['frontend']['port'] = True
        with self.assertRaises(HTTPException):
            resources.normalize(bad)
        bad = copy.deepcopy(config); bad['browser']['host'] = 'host.invalid'
        with self.assertRaises(HTTPException):
            resources.normalize(bad)
        bad = copy.deepcopy(config); bad['browser']['port'] = bad['services']['frontend']['port']
        with self.assertRaises(HTTPException):
            resources.validate(bad, [])

    def test_legacy_get_is_readonly_and_first_wake_persists(self):
        body, _ = self.reviewed(self.configured())
        result = self.submit(body).json()
        first = resources.member(result['project'], result['members'][0])
        self.assertEqual(first['revision'], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM workspace_resources').fetchone()[0], 0)
        text = wake.wake_system_prompt('chat', result['project'], str(self.root))
        self.assertEqual(text, wake.wake_system_prompt('chat', result['project'], str(self.root)))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM workspace_resources').fetchone()[0], 1)

    def test_conflicting_submit_leaves_no_project_or_files(self):
        _, existing = self.setup_project()
        body = self.configured_setup(new=True)
        body['directory_mode'] = 'shared'
        body['workspace_resources'] = copy.deepcopy(existing['workspace_resources'])
        response = self.submit(body)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse((self.base / 'fresh').exists())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0], 1)

    def test_dedicated_data_directory_and_atomic_rollback(self):
        body = self.configured_setup(new=True)
        body['workspace_resources'] = project_import.preview({**body, 'workspace_resources': []})['workspace_resources']
        body['workspace_resources'][0]['config']['test_data_directory'] = body['root']
        body, _ = self.reviewed(body)
        response = self.submit(body)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM workspace_resources').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM project_workspaces').fetchone()[0], 0)

    def test_independent_configs_have_distinct_ports_and_directories(self):
        result, body = self.setup_project(True)
        rows = self.conn.execute('SELECT * FROM workspace_resources WHERE project=?', (result['project'],)).fetchall()
        self.assertEqual(len(rows), 2)
        configs = [json.loads(row['config']) for row in rows]
        self.assertFalse(set(resources.ports(configs[0])) & set(resources.ports(configs[1])))
        self.assertFalse(set(resources.directories(configs[0])) & set(resources.directories(configs[1])))


if __name__ == '__main__':
    unittest.main()
