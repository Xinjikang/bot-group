"""Isolated reset ownership, archived gates and structured removed-project defaults."""
import json
import unittest
from unittest.mock import patch

from engine import api, project_initialization as init
from scripts import test_project_import as imports


class SetupHistoryTests(unittest.TestCase):
    setUp = imports.ImportTests.setUp

    def prepare(self, root=None, mode='import', **extra):
        return self.client.post('/projects/import/prepare', json={
            'root': str(root or self.root), 'creation_mode': mode, 'template': {'id': 'software'}, **extra})

    def partial(self):
        root = self.base / 'unfinished'
        root.mkdir()
        plan = self.client.post('/projects/initializations/preview', json={
            'root': str(root), 'display_name': 'Old unfinished'}).json()
        with patch.object(init, '_after_publish', side_effect=PermissionError('injected failure')):
            response = self.client.post(f'/projects/initializations/{plan["initialization_id"]}/submit', json={'revision': plan['revision']})
        self.assertEqual(response.json()['status'], 'failed')
        return root, response.json()

    def test_clean_partial_and_start_new_without_resume(self):
        root, plan = self.partial()
        self.assertTrue((root / 'AGENTS.md').exists())
        response = self.prepare(root, 'new')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('AGENTS.md', response.json()['cleaned_files'])
        self.assertEqual(list(root.iterdir()), [])
        self.assertEqual(response.json()['existing_projects'], [])
        old = self.conn.execute('SELECT removed,readiness_status FROM projects WHERE name=?', (plan['project'],)).fetchone()
        self.assertEqual(tuple(old), (1, 'failed'))
        retry = self.client.post(f'/projects/initializations/{plan["initialization_id"]}/retry', json={'revision': plan['revision']})
        self.assertEqual(retry.status_code, 409)
        self.assertEqual(self.prepare(root, 'new').json()['cleaned_files'], [])

    def test_import_partial_preserves_modified_and_unowned_files(self):
        root, _ = self.partial()
        (root / 'AGENTS.md').write_text('User edited rules', encoding='utf-8')
        (root / 'source.txt').write_text('User source', encoding='utf-8')
        before = imports.fingerprints(root)
        response = self.prepare(root)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()['historical_config'])
        self.assertEqual(before, imports.fingerprints(root))

    def test_reset_preserves_published_documents_and_test_scripts(self):
        root, _ = self.partial()
        for relative in ('docs/INDEX.md', 'doc/design.md', 'scripts/test_real.py', 'README.md'):
            path = root / relative
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b'Existing project deliverable\r\n')
        before = {p: (root / p).read_bytes() for p in ('docs/INDEX.md', 'doc/design.md', 'scripts/test_real.py', 'README.md')}
        response = self.prepare(root, 'new')
        self.assertEqual(response.status_code, 200, response.text)
        for relative, content in before.items():
            self.assertEqual((root / relative).read_bytes(), content)

    def test_new_preserves_real_project_files(self):
        before = imports.fingerprints(self.root)
        self.assertEqual(self.prepare(mode='new').status_code, 409)
        self.assertEqual(before, imports.fingerprints(self.root))

    def test_registered_and_archived_are_not_cleared_or_migrated(self):
        created = self.client.post('/projects/import', json=self.body).json()['project']
        before = imports.fingerprints(self.root)
        for archived in (0, 1):
            self.conn.execute('UPDATE projects SET archived=? WHERE name=?', (archived, created)); self.conn.commit()
            for mode in ('new', 'import'):
                data = self.prepare(mode=mode).json()
                self.assertEqual(data['existing_projects'][0]['archived'], archived)
                self.assertEqual(data['cleaned_files'], [])
                self.assertNotIn('historical_config', data)
        self.assertEqual(imports.fingerprints(self.root), before)
        self.assertEqual(self.client.post('/projects/import', json=self.body).status_code, 409)

    def test_only_completed_removed_project_supplies_structured_configuration(self):
        created = self.client.post('/projects/import', json=self.body).json()['project']
        self.conn.execute("INSERT INTO project_config_events(project,actor,action,detail,created_at) VALUES(?,'user','project.import',?,'now')",
                          (created, json.dumps({'template': {'id': 'writing', 'version': 'old'}})))
        self.conn.commit()
        self.assertEqual(self.client.post(f'/projects/{created}/remove').status_code, 200)
        response = self.prepare()
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json(); history = data['historical_config']
        self.assertEqual(data['template']['id'], 'writing')
        self.assertEqual(history['display_name'], self.body['display_name'])
        self.assertEqual(history['description'], self.body['description'])
        self.assertEqual(history['roles'], self.body['roles'])
        self.assertEqual(history['members'], self.body['members'])
        resource = {'services': {}, 'browser': None, 'test_data_directory': str(self.base / 'history-test-data')}
        from engine import project_workspaces
        self.conn.execute('INSERT INTO workspace_resources VALUES(?,?,?,?,1)',
                          (created, project_workspaces.key(str(self.root)), 'historical-workspace', json.dumps(resource)))
        self.conn.commit()
        self.assertEqual(self.prepare().json()['historical_config']['workspace_resources'], [{'key': 'shared', 'config': resource}])
        self.assertEqual(self.prepare(template_explicit=True).json()['template']['id'], 'software')
        self.conn.execute("UPDATE projects SET readiness_status='failed' WHERE name=?", (created,)); self.conn.commit()
        self.assertIsNone(self.prepare().json()['historical_config'])

    def test_fresh_rules_are_backed_up_and_replace_old_prose(self):
        before = (self.root / 'AGENTS.md').read_bytes()
        (self.root / 'scripts').mkdir()
        (self.root / 'scripts' / 'TEST_INDEX.md').write_bytes(b'Existing test index\r\n')
        (self.root / 'scripts' / 'test_user.py').write_bytes(b'# User test\r\n')
        assets = {path: (self.root / path).read_bytes() for path in ('doc/design.md', 'scripts/TEST_INDEX.md', 'scripts/test_user.py', 'code.bin')}
        discovered = self.prepare().json()
        body = {**self.body, 'roles': discovered['default_roles'], 'members': [], 'owner_bot_id': None,
                'template': discovered['template'], 'supplement': True, 'git_setup': True, 'creation_mode': 'import'}
        checked = self.client.post('/projects/import/preview', json=body).json()
        draft = checked['file_plan']['agents_draft']
        self.assertNotIn('Keep this workflow', draft['content'])
        body['agents_edit'] = draft
        body['plan_hash'] = self.client.post('/projects/import/preview', json=body).json()['file_plan']['plan_hash']
        result = self.client.post('/projects/import', json=body)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn('scripts/TEST_INDEX.md', result.json()['review_reminder'])
        self.assertEqual((self.root / 'AGENTS.md').read_text(encoding='utf-8'), draft['content'])
        self.assertEqual(next(self.root.glob('AGENTS.md.backup-*')).read_bytes(), before)
        self.assertEqual((self.root / 'code.bin').read_bytes(), bytes(range(256)))
        for path, content in assets.items():
            self.assertEqual((self.root / path).read_bytes(), content)

    def test_removed_prototype_project_restores_template_selection(self):
        created = self.client.post('/projects/import', json=self.body).json()['project']
        self.conn.execute(
            "INSERT INTO project_config_events(project,actor,action,detail,created_at) "
            "VALUES(?,'user','project.import',?,'now')",
            (created, json.dumps({'template': {'id': 'product-prototype', 'version': '1.0.0'}})))
        self.conn.commit()
        self.assertEqual(self.client.post(f'/projects/{created}/remove').status_code, 200)
        response = self.prepare()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['template']['id'], 'product-prototype')
        self.assertEqual(response.json()['owner_role'], '产品经理')
        self.assertEqual(self.prepare(template_explicit=True).json()['template']['id'], 'software')

    def test_prepare_requires_admin(self):
        self.app.dependency_overrides[api._resolve_identity] = lambda: ('bot', 'bot')
        self.assertEqual(self.prepare().status_code, 403)


if __name__ == '__main__':
    unittest.main()
