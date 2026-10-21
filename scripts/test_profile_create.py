"""Profile creation uses temporary homes, synthetic credentials and an isolated DB."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import tomlkit
from engine import auth, bot_identity, discovery, profile_create
from scripts import test_project_roles


class ProfileCreateTests(unittest.TestCase):
    setUp = test_project_roles.ProjectRoleTests.setUp

    def fixture(self, host='codex'):
        root = self.root / host
        root.mkdir(exist_ok=True)
        env = patch.dict(os.environ, {'CODEX_HOME': str(self.root / 'codex')})
        env.start()
        self.addCleanup(env.stop)
        if host == 'codex':
            (root / 'config.toml').write_text('model="base-model"\napproval_policy="on-request"\n[agents.old]\ndescription="old"\n', encoding='utf-8')
            (root / 'auth.json').write_text('{"OPENAI_API_KEY":"synthetic-key"}', encoding='utf-8')
        else:
            (root / 'config.yaml').write_text('model:\n  default: base-model\nplatforms:\n  feishu:\n    token: synthetic-channel\n', encoding='utf-8')
            (root / '.env').write_text('OPENAI_API_KEY=synthetic-key\nFEISHU_APP_SECRET=synthetic-channel\n', encoding='utf-8')
        (root / 'SOUL.md').write_text('Source identity', encoding='utf-8')
        return root, {'project': 'p', 'host_type': host, 'profile': 'new-dev', 'display_name': '新成员',
                      'source': {'profile': 'default', 'config_root': str(root)},
                      'reuse_auth': True, 'request_id': 'creation-1'}

    def create(self, body, status=200):
        response = self.client.post('/profiles/create', json=body)
        self.assertEqual(response.status_code, status, response.text)
        return response

    def test_codex_independent_home_and_repeat(self):
        root, body = self.fixture()
        before = (root / 'config.toml').read_bytes()
        result = self.create(body).json()
        home = Path(result['home'])
        config = tomlkit.parse((home / 'config.toml').read_text())
        self.assertEqual(config['model'], 'base-model')
        self.assertEqual(config['approval_policy'], 'on-request')
        policy = config['permissions'][config['default_permissions']]
        self.assertEqual(policy['filesystem'][self.root.as_posix()], 'write')
        self.assertNotIn('agents', config)
        self.assertIn('新成员', (home / 'AGENTS.md').read_text(encoding='utf-8'))
        self.assertTrue((home / 'skills').is_dir())
        self.assertTrue((home / 'memories').is_dir())
        self.assertEqual((home / 'auth.json').read_bytes(), (root / 'auth.json').read_bytes())
        self.assertEqual((root / 'config.toml').read_bytes(), before)
        self.assertTrue(result['auth_copied'])
        bot = dict(self.conn.execute('SELECT * FROM bots WHERE id=?', (result['bot_id'],)).fetchone())
        self.assertEqual(bot_identity.codex_settings(bot), (str(home), None))
        self.assertEqual(self.create(body).json()['bot_id'], result['bot_id'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM project_members').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM project_permissions').fetchone()[0], 0)
        self.assertNotIn('synthetic-key', json.dumps(result))
        self.create({**body, 'request_id': 'another-request'}, 409)

    def test_codex_worktree_profile_receives_shared_test_reports_directory(self):
        root, body = self.fixture()
        trees = self.root / 'trees'
        self.conn.execute("UPDATE projects SET directory_mode='worktree',worktree_root=? WHERE name='p'", (str(trees),))
        self.conn.commit()
        result = self.create(body).json()
        config = tomlkit.parse((Path(result['home']) / 'config.toml').read_text())
        policy = config['permissions'][config['default_permissions']]
        self.assertEqual(policy['filesystem'], {
            (trees / result['member_profile']).as_posix(): 'write',
            (self.root / 'reports').as_posix(): 'write'})

    def test_codex_named_config_merges_runtime_defaults(self):
        root, body = self.fixture()
        (root / 'review.config.toml').write_text('model="review-model"\nmodel_reasoning_effort="high"\n', encoding='utf-8')
        body['source']['profile'] = 'review'
        result = self.create(body).json()
        config = tomlkit.parse((Path(result['home']) / 'config.toml').read_text())
        self.assertEqual(config['model'], 'review-model')
        self.assertEqual(config['approval_policy'], 'on-request')

    def test_hermes_model_auth_without_persistent_engine_mcp(self):
        root, body = self.fixture('hermes')
        result = self.create(body).json()
        home = Path(result['home'])
        _, config = profile_create.files._yaml((home / 'config.yaml').read_bytes())
        self.assertEqual(config['model']['default'], 'base-model')
        self.assertNotIn('platforms', config)
        self.assertNotIn('mcp_servers', config)
        self.assertIn('OPENAI_API_KEY=', (home / '.env').read_text())
        self.assertNotIn('FEISHU', (home / '.env').read_text())
        self.assertNotIn('Source identity', (home / 'SOUL.md').read_text(encoding='utf-8'))
        self.assertIn('new-dev', [c['profile'] for c in discovery._scan_hermes()])

    def test_new_hermes_profile_can_save_first_model_and_provider(self):
        root, body = self.fixture('hermes')
        for index, source_config in enumerate((None, 'terminal:\n  backend: local\n', 'model:\n  default: base-model\n')):
            with self.subTest(source_config=source_config):
                if source_config is not None:
                    (root / 'config.yaml').write_text(source_config, encoding='utf-8')
                result = self.create({**body, 'profile': f'first-save-{index}',
                                     'source': body['source'] if source_config is not None else None}).json()
                home = Path(result['home'])
                before = profile_create.files._snapshot(home)
                path = '/members/' + result['member_profile'] + '/config'
                current = self.client.get(path)
                self.assertEqual(current.status_code, 200, current.text)
                values = {**current.json(), 'model': 'deepseek-v4-pro:0813', 'provider': 'ollama-cloud', 'reasoning_effort': 'high'}
                saved = self.client.put(path, json=values)
                self.assertEqual(saved.status_code, 200, saved.text)
                after = self.client.get(path).json()
                for key in ('model', 'provider', 'reasoning_effort'):
                    self.assertEqual(after[key], values[key])
                config = profile_create.files._yaml((home / 'config.yaml').read_bytes())[1]
                original = profile_create.files._yaml(before['config.yaml'])[1]
                self.assertEqual(config.get('mcp_servers'), original.get('mcp_servers'))
                for name in ('SOUL.md', '.env', 'profile.yaml'):
                    self.assertEqual((home / name).read_bytes(), before[name])
                backup = home / saved.json()['backup']
                self.assertEqual((backup / 'config.yaml').read_bytes(), before['config.yaml'])

    def test_codex_always_inherits_primary_auth(self):
        root, body = self.fixture()
        for index, changes in enumerate(({'source': None}, {'reuse_auth': False}, {})):
            if not changes:
                body.pop('reuse_auth')
            result = self.create({**body, **changes, 'profile': f'blank-{index}'}).json()
            self.assertTrue(result['auth_copied'])
            self.assertEqual((Path(result['home']) / 'auth.json').read_bytes(), (root / 'auth.json').read_bytes())
            self.assertEqual(result['setup_command'], '')

    def test_codex_auth_comes_from_primary_when_config_source_is_another_profile(self):
        root, body = self.fixture()
        other = root / 'profiles' / 'other'
        other.mkdir(parents=True)
        (other / 'config.toml').write_text('model="other-model"\n', encoding='utf-8')
        (other / 'auth.json').write_text('{"OPENAI_API_KEY":"other-key"}', encoding='utf-8')
        body['source'] = {'profile': 'other', 'config_root': str(other)}
        result = self.create(body).json()
        home = Path(result['home'])
        self.assertEqual((home / 'auth.json').read_bytes(), (root / 'auth.json').read_bytes())
        self.assertEqual(tomlkit.parse((home / 'config.toml').read_text())['model'], 'other-model')

    def test_missing_primary_auth_guidance_targets_primary_home(self):
        root, body = self.fixture()
        (root / 'auth.json').unlink()
        result = self.create(body).json()
        self.assertFalse(result['auth_copied'])
        self.assertIn(str(root), result['setup_command'])
        self.assertNotIn(result['home'], result['setup_command'])
        self.assertIn('codex login', result['setup_command'])

    def test_hermes_endpoint_alone_requires_setup_in_correct_home(self):
        root, body = self.fixture('hermes')
        (root / '.env').write_text('OPENAI_BASE_URL=https://example.test\n', encoding='utf-8')
        result = self.create(body).json()
        self.assertFalse(result['auth_copied'])
        self.assertIn(str(root), result['setup_command'])
        self.assertIn('hermes -p new-dev setup', result['setup_command'])

    def test_validation_auth_and_source_boundaries(self):
        root, body = self.fixture()
        for name in ('../escape', 'default', 'user', 'con', 'profiles', 'UPPER', 'x/y'):
            self.create({**body, 'profile': name}, 400)
        self.create({**body, 'source': {'profile': 'default', 'config_root': str(self.root)}}, 409)
        self.create({**body, 'project': 'missing'}, 404)
        self.conn.execute("UPDATE projects SET archived=1 WHERE name='p'")
        self.conn.commit()
        self.create(body, 404)
        response = self.client.post('/profiles/create', json=body, headers={'X-Group-Profile': 'bot-1', 'X-Group-Key': auth.bot_token('bot-1')})
        self.assertEqual(response.status_code, 403)
        self.assertFalse((root / 'profiles').exists())

    def test_conflict_preserves_existing_directory(self):
        root, body = self.fixture()
        home = root / 'profiles' / body['profile']
        home.mkdir(parents=True)
        (home / 'keep.txt').write_text('keep')
        self.create(body, 409)
        self.assertEqual((home / 'keep.txt').read_text(), 'keep')

    def test_db_failure_rolls_back_published_home_and_retry_works(self):
        root, body = self.fixture()
        original = bot_identity.enroll
        calls = []
        def fail_final(*args):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError('synthetic failure')
            return original(*args)
        with patch.object(bot_identity, 'enroll', side_effect=fail_final):
            self.create(body, 400)
        self.assertFalse((root / 'profiles' / body['profile']).exists())
        self.assertEqual(list((root / 'profiles').iterdir()), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM bots WHERE profile='new-dev'").fetchone()[0], 0)
        self.create(body)

    def test_bad_source_does_not_leak_credentials_or_publish(self):
        root, body = self.fixture()
        (root / 'config.toml').write_text('model = [synthetic-secret', encoding='utf-8')
        response = self.create(body, 400)
        self.assertNotIn('synthetic-secret', response.text)
        self.assertFalse((root / 'profiles').exists())


if __name__ == '__main__':
    unittest.main()
