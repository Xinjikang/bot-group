"""DSH creation through the real API with isolated homes and synthetic credentials."""
import json
import os
from pathlib import Path
from unittest.mock import patch
import unittest

import yaml

from engine import discovery
from scripts import test_project_roles


class DshProfileCreateTests(unittest.TestCase):
    setUp = test_project_roles.ProjectRoleTests.setUp

    def fixture(self):
        root = self.root / 'dsh'
        source = root / 'profiles' / 'headless'
        source.mkdir(parents=True)
        (source / 'cordis.patch.yml').write_text(yaml.safe_dump([
            {'id': 'agent-default-model', 'config': {'model': 'source-model', 'provider': 'deepseek-account', 'reasoningEffort': 'high'}},
            {'id': 'deepseek-account', 'disabled': False},
            {'id': 'web-ui', 'config': {'port': 9999}},
            {'id': 'system-prompt', 'config': {'personaPrefix': 'source identity'}},
        ]), encoding='utf-8')
        (root / 'AGENTS.md').write_text('Shared instructions', encoding='utf-8')
        (root / '.credentials.yaml').write_text('synthetic credential', encoding='utf-8')
        entry = self.root / 'dsh.cmd'
        entry.write_text('synthetic entry', encoding='utf-8')
        env = patch.dict(os.environ, {'DSH_HOME': str(root), 'DSH_BIN': str(entry)})
        env.start()
        self.addCleanup(env.stop)
        return root, source, {'host_type': 'dsh', 'profile': 'new-agent', 'display_name': '新成员',
                              'source': {'profile': 'headless', 'config_root': str(root)},
                              'model': 'chosen-model', 'instructions': '独立个人指令', 'request_id': 'create-dsh'}

    def test_independent_profile_routing_retry_and_enrollment(self):
        root, source, body = self.fixture()
        before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
        response = self.client.post('/profiles/create', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        home = Path(result['home'])
        self.assertEqual(home, root / 'profiles' / body['profile'])
        self.assertNotEqual(home, source)
        entries = {item['id']: item for item in yaml.safe_load((home / 'cordis.patch.yml').read_text(encoding='utf-8'))}
        self.assertEqual(entries['agent-default-model']['config'],
                         {'model': 'chosen-model', 'provider': 'deepseek-account', 'reasoningEffort': 'high'})
        self.assertEqual(entries['system-prompt']['config']['personaPrefix'], body['instructions'])
        self.assertFalse(entries['deepseek-account']['disabled'])
        self.assertNotIn('web-ui', entries)
        package = json.loads((home / 'package.json').read_text())
        self.assertIn('@deepseek-ai/dsh-headless', package['dsh']['profile']['bundles'])
        self.assertTrue(discovery.validate_dsh_home(str(root), body['profile'])['ok'])
        self.assertTrue(any(c['profile'] == body['profile'] for c in discovery._scan_dsh()))
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertEqual(result['config_root'], str(root))
        self.assertIsNone(result['member_profile'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM project_members').fetchone()[0], 0)
        retry = self.client.post('/profiles/create', json=body)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()['bot_id'], result['bot_id'])
        duplicate = self.client.post('/profiles/create', json={**body, 'request_id': 'another'})
        self.assertEqual(duplicate.status_code, 409, duplicate.text)
        joined = self.client.post('/bots/register', json={'host_type': 'dsh', 'profile': body['profile'],
                                                         'config_root': str(root), 'project': 'p'})
        self.assertEqual(joined.status_code, 200, joined.text)
        self.assertEqual(joined.json()['bot_id'], result['bot_id'])

    def test_defaults_and_existing_source_conflict(self):
        root, source, body = self.fixture()
        response = self.client.post('/profiles/create', json={**body, 'profile': 'headless'})
        self.assertEqual(response.status_code, 409, response.text)
        response = self.client.post('/profiles/create', json={**body, 'source': None, 'model': '', 'instructions': '', 'project': 'p'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNotNone(response.json()['member_profile'])
        self.assertTrue((root / 'profiles' / body['profile'] / 'cordis.patch.yml').is_file())

    def test_assistant_creation_verifies_profile_persona(self):
        from engine import hud_configuration
        root, source, body = self.fixture()
        body.pop('request_id')
        result = hud_configuration._create_profile({'id': 'isolated-hud', 'project': 'p'}, body)
        self.assertTrue(result['instructions_verified'])
        self.assertEqual(Path(result['instructions_file']), root / 'profiles' / body['profile'] / 'cordis.patch.yml')

    def test_registration_failure_rolls_back_new_files_and_database(self):
        root, source, body = self.fixture()
        before = self.conn.execute('SELECT COUNT(*) FROM bots').fetchone()[0]
        with patch.object(discovery, 'register_bot', side_effect=ValueError('synthetic failure')):
            response = self.client.post('/profiles/create', json=body)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertFalse((root / 'profiles' / body['profile']).exists())
        self.assertEqual(list((root / 'profiles').glob('.create-*')), [])
        self.assertTrue(source.exists())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bots').fetchone()[0], before)

    def test_source_change_and_missing_entry_leave_no_profile(self):
        root, source, body = self.fixture()
        changed = self.client.post('/profiles/create', json={**body, 'source': {**body['source'], 'profile': 'missing'}})
        self.assertEqual(changed.status_code, 409, changed.text)
        with patch.object(discovery, '_dsh_entry', return_value=''):
            response = self.client.post('/profiles/create', json=body)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertFalse((root / 'profiles' / body['profile']).exists())


if __name__ == '__main__':
    unittest.main()
