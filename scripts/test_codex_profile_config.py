import os
from pathlib import Path
import unittest
from unittest.mock import patch

import tomlkit
from scripts import test_project_roles
from engine import auth, codex_profile_config as config


class CodexConfigTests(unittest.TestCase):
    setUp = test_project_roles.ProjectRoleTests.setUp
    join = test_project_roles.ProjectRoleTests.join

    def fixture(self):
        root = self.root / 'codex'
        home = root / 'profiles' / 'dev-01'
        home.mkdir(parents=True)
        (root / 'config.toml').write_text('# Parent\nmodel = "parent-model"\n', encoding='utf-8')
        (home / 'config.toml').write_text('# Child\nmodel = "old-model"\nmodel_reasoning_effort = "high"\n[custom]\nkeep = 1\n', encoding='utf-8')
        (home / 'AGENTS.md').write_text('Original instructions', encoding='utf-8')
        env = patch.dict(os.environ, {'CODEX_HOME': str(root)})
        env.start()
        self.addCleanup(env.stop)
        response = self.client.post('/bots/register', json={'project': 'p', 'host_type': 'codex', 'profile': 'dev-01', 'config_root': str(home)})
        self.assertEqual(response.status_code, 200, response.text)
        return root, home, '/members/' + response.json()['member_profile'] + '/config'

    def payload(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.text)
        view = response.json()
        return {k: view[k] for k in ('description', 'soul', 'env', 'model', 'provider', 'reasoning_effort', 'revision')}

    def test_read_save_and_role_declaration(self):
        root, home, url = self.fixture()
        values = self.payload(url)
        self.assertEqual(values['model'], 'old-model')
        values.update(description='Select for development', soul='New instructions', model='new-model', provider='openai', reasoning_effort='medium')
        response = self.client.put(url, json=values)
        self.assertEqual(response.status_code, 200, response.text)
        parent = tomlkit.parse((root / 'config.toml').read_text())
        self.assertEqual(parent['agents']['dev-01']['description'], values['description'])
        self.assertEqual(Path(parent['agents']['dev-01']['config_file']), home / 'config.toml')
        self.assertEqual(parent['model'], 'parent-model')
        child = (home / 'config.toml').read_text()
        self.assertIn('# Child', child)
        self.assertEqual(tomlkit.parse(child)['custom']['keep'], 1)
        self.assertEqual((home / 'AGENTS.md').read_text(), 'New instructions')
        self.assertFalse((home / '.env').exists())
        self.assertEqual(len(response.json()['backup']), 2)
        self.assertEqual(self.payload(url)['description'], values['description'])
        self.assertEqual(self.client.put(url, json=values).status_code, 409)

    def test_parent_change_conflict_and_invalid_toml(self):
        root, home, url = self.fixture()
        values = self.payload(url)
        (root / 'config.toml').write_text('model = "changed"\n')
        self.assertEqual(self.client.put(url, json=values).status_code, 409)
        (home / 'config.toml').write_text('model = [')
        self.assertEqual(self.client.get(url).status_code, 400)

    def test_model_and_effort_only_save_stays_in_selected_home(self):
        root, home, url = self.fixture()
        response = self.client.post('/bots/register', json={'project': 'p', 'host_type': 'codex',
            'profile': 'default', 'config_root': str(root)})
        parent_url = '/members/' + response.json()['member_profile'] + '/config'
        parent_before = (root / 'config.toml').read_bytes()
        values = self.payload(url)
        values.update(model='gpt-6.1-sol', reasoning_effort='ultra')
        response = self.client.put(url, json=values)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.payload(url)['model'], 'gpt-6.1-sol')
        self.assertEqual(self.payload(url)['reasoning_effort'], 'ultra')
        self.assertEqual((root / 'config.toml').read_bytes(), parent_before)
        self.assertEqual(len(response.json()['backup']), 1)
        child_before = (home / 'config.toml').read_bytes()
        values = self.payload(parent_url)
        values.update(model='gpt-6-astra', reasoning_effort='low')
        response = self.client.put(parent_url, json=values)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.payload(parent_url)['model'], 'gpt-6-astra')
        self.assertEqual(self.payload(parent_url)['reasoning_effort'], 'low')
        self.assertEqual((home / 'config.toml').read_bytes(), child_before)

    def test_codex_effort_choices_and_legacy_value_preservation(self):
        root, home, url = self.fixture()
        originals = {p: p.read_bytes() for p in (root / 'config.toml', home / 'config.toml')}
        for effort in ('none', 'minimal', 'max'):
            with self.subTest(effort=effort):
                values = self.payload(url)
                values.update(model='new-model', reasoning_effort=effort)
                self.assertEqual(self.client.put(url, json=values).status_code, 400)
                for path, original in originals.items():
                    self.assertEqual(path.read_bytes(), original)
        for effort in ('low', 'medium', 'high', 'xhigh', 'ultra', ''):
            with self.subTest(effort=effort):
                values = self.payload(url)
                values['reasoning_effort'] = effort
                response = self.client.put(url, json=values)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.payload(url)['reasoning_effort'], effort)
        (home / 'config.toml').write_text('model = "old"\nmodel_reasoning_effort = "max"\n')
        values = self.payload(url)
        values['description'] = 'Changed role only'
        self.assertEqual(self.client.put(url, json=values).status_code, 200)
        self.assertEqual(self.payload(url)['reasoning_effort'], 'max')

    def test_save_rejects_a_different_member_or_config_path(self):
        root, home, url = self.fixture()
        view = self.client.get(url).json()
        self.assertEqual(view['member_profile'], url.split('/')[2])
        originals = {p: p.read_bytes() for p in (root / 'config.toml', home / 'config.toml')}
        for target in ({'member_profile': 'another-member'}, {'config_path': str(root / 'config.toml')}):
            values = self.payload(url)
            values.update(model='wrong-target', **target)
            self.assertEqual(self.client.put(url, json=values).status_code, 409)
            for path, original in originals.items():
                self.assertEqual(path.read_bytes(), original)
        values = self.payload(url)
        values.update(model='right-target', member_profile=view['member_profile'], config_path=view['config_path'])
        self.assertEqual(self.client.put(url, json=values).status_code, 200)

    def test_noop_and_bot_access_and_env_rejection(self):
        root, home, url = self.fixture()
        values = self.payload(url)
        backup_dir = home / '.group-engine-backups'
        before = {p.relative_to(backup_dir): p.read_bytes() for p in backup_dir.rglob('*') if p.is_file()}
        self.assertEqual(self.client.put(url, json=values).json()['backup'], [])
        self.assertEqual({p.relative_to(backup_dir): p.read_bytes() for p in backup_dir.rglob('*') if p.is_file()}, before)
        self.assertEqual(self.client.put(url, json=values, headers={'X-Group-Profile': 'bot', 'X-Group-Key': auth.bot_token('bot')}).status_code, 403)
        values['env'] = 'TOKEN=value'
        self.assertEqual(self.client.put(url, json=values).status_code, 400)

    def test_partial_write_failure_restores_both_homes(self):
        root, home, url = self.fixture()
        originals = {p: p.read_bytes() for p in (root / 'config.toml', home / 'config.toml', home / 'AGENTS.md')}
        values = self.payload(url)
        values.update(description='New role', model='new', soul='new')
        replace = os.replace
        def fail_once(src, dest):
            if str(dest) == str(root / 'config.toml'):
                raise OSError('simulated write failure')
            return replace(src, dest)
        with patch.object(config.os, 'replace', side_effect=fail_once):
            self.assertEqual(self.client.put(url, json=values).status_code, 500)
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)

    def test_default_home_updates_description_and_model_in_same_file(self):
        root, home, _ = self.fixture()
        response = self.client.post('/bots/register', json={'project': 'p', 'host_type': 'codex', 'profile': 'default', 'config_root': str(root)})
        url = '/members/' + response.json()['member_profile'] + '/config'
        values = self.payload(url)
        values.update(description='Default agent', model='new-default')
        self.assertEqual(self.client.put(url, json=values).status_code, 200)
        data = tomlkit.parse((root / 'config.toml').read_text())
        self.assertEqual(data['agents']['default']['description'], 'Default agent')
        self.assertEqual(data['model'], 'new-default')
        self.assertNotIn('config_file', data['agents']['default'])

    def test_override_and_agent_reference_conflicts_are_explicit(self):
        root, home, url = self.fixture()
        (home / 'AGENTS.override.md').write_text('Override instructions')
        values = self.payload(url)
        values['soul'] = 'Changed instructions'
        self.assertEqual(self.client.put(url, json=values).status_code, 409)
        (root / 'config.toml').write_text('[agents.dev-01]\nconfig_file="different.toml"\n')
        self.assertEqual(self.client.get(url).status_code, 409)


if __name__ == '__main__':
    unittest.main()
