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
            from scripts.test_profile_credentials import seed_catalog
            seed_catalog(root)
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
        self.assertEqual(config['approvals_reviewer'], 'auto_review')
        self.assertEqual(config['sandbox_mode'], 'workspace-write')
        self.assertEqual(config['sandbox_workspace_write']['writable_roots'], [self.root.as_posix()])
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

    def test_standalone_creation_then_explicit_enrollment(self):
        for host in ('codex', 'hermes'):
            with self.subTest(host=host):
                root, body = self.fixture(host)
                body.pop('project')
                body['profile'] = f'standalone-{host}'
                before = self.conn.execute('SELECT COUNT(*) FROM project_members').fetchone()[0]
                result = self.create(body).json()
                self.assertIsNone(result['project'])
                self.assertIsNone(result['member_profile'])
                self.assertTrue(Path(result['home']).is_dir())
                self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM project_members').fetchone()[0], before)
                self.assertEqual(self.create(body).json()['bot_id'], result['bot_id'])
                candidates = self.client.get('/profiles/candidates').json()['candidates']
                candidate = next(c for c in candidates if c['bot_id'] == result['bot_id'])
                self.assertFalse(candidate['already_project_member'])
                if host == 'codex':
                    config = tomlkit.parse((Path(result['home']) / 'config.toml').read_text())
                    self.assertEqual(config['approvals_reviewer'], 'auto_review')
                    self.assertEqual(config['sandbox_mode'], 'workspace-write')
                    self.assertNotIn('sandbox_workspace_write', config)
                joined = self.client.post('/bots/register', json={
                    'project': 'p', 'host_type': host, 'profile': body['profile'], 'config_root': result['config_root']})
                self.assertEqual(joined.status_code, 200, joined.text)
                self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM project_members').fetchone()[0], before + 1)
                candidate = next(c for c in self.client.get('/profiles/candidates?project=p').json()['candidates'] if c['bot_id'] == result['bot_id'])
                self.assertTrue(candidate['already_project_member'])
                if host == 'codex':
                    config = tomlkit.parse((Path(result['home']) / 'config.toml').read_text())
                    self.assertIn(self.root.as_posix(), config['sandbox_workspace_write']['writable_roots'])

    def test_standalone_invalid_project_and_candidate_auth(self):
        root, body = self.fixture()
        for project, status in [('missing', 404), ('   ', 400)]:
            self.create({**body, 'project': project}, status)
        self.assertFalse((root / 'profiles' / body['profile']).exists())
        response = self.client.get('/profiles/candidates', headers={
            'X-Group-Profile': 'bot-1', 'X-Group-Key': auth.bot_token('bot-1')})
        self.assertEqual(response.status_code, 403)

    def test_codex_worktree_profile_receives_shared_test_reports_directory(self):
        root, body = self.fixture()
        trees = self.root / 'trees'
        self.conn.execute("UPDATE projects SET directory_mode='worktree',worktree_root=? WHERE name='p'", (str(trees),))
        self.conn.commit()
        result = self.create(body).json()
        config = tomlkit.parse((Path(result['home']) / 'config.toml').read_text())
        self.assertEqual(config['sandbox_workspace_write']['writable_roots'], [
            (trees / result['member_profile']).as_posix(), (self.root / 'reports').as_posix()])

    def test_codex_named_config_merges_runtime_defaults(self):
        root, body = self.fixture()
        (root / 'review.config.toml').write_text('model="review-model"\nmodel_reasoning_effort="high"\n', encoding='utf-8')
        body['source']['profile'] = 'review'
        result = self.create(body).json()
        config = tomlkit.parse((Path(result['home']) / 'config.toml').read_text())
        self.assertEqual(config['model'], 'review-model')
        self.assertEqual(config['approval_policy'], 'on-request')

    def test_windows_default_creation_has_sandbox_and_only_project_grants(self):
        root, body = self.fixture()
        (root / 'config.toml').write_text(
            'model="private-model"\napproval_policy="never"\n'
            'sandbox_mode="danger-full-access"\n[windows]\nsandbox="mxc"\n', encoding='utf-8')
        before = (root / 'config.toml').read_bytes()
        with patch.object(profile_create.sys, 'platform', 'win32'):
            result = self.create({**body, 'source': None}).json()
        home = Path(result['home'])
        config = tomlkit.parse((home / 'config.toml').read_text())
        self.assertEqual(config['windows']['sandbox'], 'unelevated')
        self.assertEqual(config['approval_policy'], 'on-request')
        self.assertEqual(config['approvals_reviewer'], 'auto_review')
        self.assertNotIn('model', config)
        self.assertEqual(config['sandbox_mode'], 'workspace-write')
        self.assertEqual(config['sandbox_workspace_write'], {'writable_roots': [self.root.as_posix()]})
        self.assertNotIn('default_permissions', config)
        self.assertNotIn('permissions', config)
        self.assertEqual((root / 'config.toml').read_bytes(), before)
        created = (home / 'config.toml').read_bytes()
        self.create({**body, 'source': None})
        self.assertEqual((home / 'config.toml').read_bytes(), created)

    def test_windows_source_sandbox_choices_and_features_survive_creation(self):
        root, body = self.fixture()
        cases = [
            '[windows]\nsandbox="elevated"\n',
            '[windows]\nsandbox="unelevated"\n',
            '[windows]\nsandbox="mxc"\n',
            '[features]\nprefer_mxc=true\n',
            '[features]\nexperimental_windows_sandbox=true\n',
            '[features]\nelevated_windows_sandbox=true\n',
            '[windows]\nsandbox="elevated"\n[features]\nprefer_mxc=false\n',
        ]
        for i, settings in enumerate(cases):
            with self.subTest(settings=settings), patch.object(profile_create.sys, 'platform', 'win32'):
                source = ('approval_policy="never"\nsandbox_mode="workspace-write"\n'
                          '[sandbox_workspace_write]\nnetwork_access=false\n' + settings)
                (root / 'config.toml').write_text(source, encoding='utf-8')
                result = self.create({**body, 'profile': f'sandbox-{i}'}).json()
                config = tomlkit.parse((Path(result['home']) / 'config.toml').read_text())
                expected = tomlkit.parse(source)
                expected_windows = expected.get('windows', {'sandbox': 'elevated' if i == 5 else 'unelevated'})
                self.assertEqual(config['windows'], expected_windows)
                expected_features = {k: v for k, v in expected.get('features', {}).items() if k == 'prefer_mxc'}
                self.assertEqual(config.get('features', {}), expected_features)
                self.assertEqual(config['approval_policy'], 'on-request')
                self.assertEqual(config['approvals_reviewer'], 'auto_review')
                self.assertEqual(config['sandbox_mode'], 'workspace-write')
                self.assertEqual(config['sandbox_workspace_write']['writable_roots'], [self.root.as_posix()])
                self.assertEqual((root / 'config.toml').read_text(encoding='utf-8'), source)

    def test_windows_named_and_default_selectors_keep_sandbox_overrides(self):
        root, body = self.fixture()
        for i, (selected, named_file) in enumerate(((True, True), (True, False), (False, True), (False, False))):
            with self.subTest(selected=selected, named_file=named_file):
                base = ('profile="review"\n[windows]\nsandbox="elevated"\n'
                        '[features]\nprefer_mxc=true\nunrelated_feature=true\n')
                override = '[windows]\nsandbox="unelevated"\n[features]\nprefer_mxc=false\n'
                named = root / 'review.config.toml'
                if named_file:
                    named.write_text(override, encoding='utf-8')
                else:
                    named.unlink(missing_ok=True)
                    base += '[profiles.review.windows]\nsandbox="unelevated"\n[profiles.review.features]\nprefer_mxc=false\n'
                (root / 'config.toml').write_text(base, encoding='utf-8')
                source = {'profile': 'review' if selected else 'default', 'config_root': str(root)}
                with patch.object(profile_create.sys, 'platform', 'win32'):
                    result = self.create({**body, 'source': source, 'profile': f'layer-{i}'}).json()
                config = tomlkit.parse((Path(result['home']) / 'config.toml').read_text())
                self.assertEqual(config['windows']['sandbox'], 'unelevated')
                self.assertEqual(config['features'], {'prefer_mxc': False})
                self.assertNotIn('profile', config)
                self.assertNotIn('profiles', config)
                self.assertEqual((root / 'config.toml').read_text(encoding='utf-8'), base)

    def test_sandbox_defaults_are_platform_scoped_and_independent_home_isolated(self):
        root, body = self.fixture()
        (root / 'config.toml').write_text('[windows]\nsandbox="mxc"\n', encoding='utf-8')
        other = root / 'profiles' / 'isolated'
        other.mkdir(parents=True)
        (other / 'config.toml').write_text('approval_policy="never"\n', encoding='utf-8')
        with patch.object(profile_create.sys, 'platform', 'win32'):
            result = self.create({**body, 'source': {'profile': 'isolated', 'config_root': str(other)}}).json()
        config = tomlkit.parse((Path(result['home']) / 'config.toml').read_text())
        self.assertEqual(config['windows']['sandbox'], 'unelevated')
        self.assertEqual(config['approval_policy'], 'on-request')
        self.assertEqual(config['approvals_reviewer'], 'auto_review')
        with patch.object(profile_create.sys, 'platform', 'linux'):
            result = self.create({**body, 'source': None, 'profile': 'linux-default'}).json()
        config = tomlkit.parse((Path(result['home']) / 'config.toml').read_text())
        self.assertNotIn('windows', config)
        self.assertEqual(config['sandbox_mode'], 'workspace-write')
        self.assertEqual(config['approvals_reviewer'], 'auto_review')

    def test_source_permissions_cannot_disable_new_home_sandbox_or_auto_review(self):
        root, body = self.fixture()
        sources = (
            'approval_policy="never"\napprovals_reviewer="user"\nsandbox_mode="danger-full-access"\n',
            'approval_policy="never"\ndefault_permissions=":danger-full-access"\n',
            'default_permissions="open"\n[permissions.open]\nextends=":danger-full-access"\n',
            'sandbox_mode="read-only"\n[windows]\nsandbox="disabled"\n'
            '[features]\nexperimental_windows_sandbox=false\nelevated_windows_sandbox=false\n',
            'sandbox_mode="workspace-write"\n[sandbox_workspace_write]\n'
            'writable_roots=["C:/unrelated"]\nnetwork_access=true\n',
            '[approval_policy.granular]\nsandbox_approval=false\nrules=false\n',
        )
        for i, source in enumerate(sources):
            with self.subTest(source=source), patch.object(profile_create.sys, 'platform', 'win32'):
                (root / 'config.toml').write_text(source, encoding='utf-8')
                result = self.create({**body, 'profile': f'policy-{i}'}).json()
                config = tomlkit.parse((Path(result['home']) / 'config.toml').read_text())
                self.assertEqual(config['approval_policy'], 'on-request')
                self.assertEqual(config['approvals_reviewer'], 'auto_review')
                self.assertEqual(config['sandbox_mode'], 'workspace-write')
                self.assertEqual(config['windows']['sandbox'], 'unelevated')
                self.assertEqual(config['sandbox_workspace_write'], {'writable_roots': [self.root.as_posix()]})
                self.assertNotIn('permissions', config)
                self.assertNotIn('default_permissions', config)
                self.assertNotIn('experimental_windows_sandbox', config.get('features', {}))
                self.assertEqual((root / 'config.toml').read_text(encoding='utf-8'), source)

    def test_invalid_sandbox_source_rolls_back_without_exposing_contents(self):
        root, body = self.fixture()
        for source in ('windows="synthetic-secret"', 'features="synthetic-secret"',
                       'profile="../synthetic-secret"'):
            with self.subTest(source=source), patch.object(profile_create.sys, 'platform', 'win32'):
                (root / 'config.toml').write_text(source, encoding='utf-8')
                response = self.create(body, 400)
                self.assertNotIn('synthetic-secret', response.text)
                self.assertFalse((root / 'profiles' / body['profile']).exists())
                self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM bots WHERE profile='new-dev'").fetchone()[0], 0)

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

    def test_hermes_default_source_and_custom_provider_copy(self):
        root, body = self.fixture('hermes')
        (root / 'config.yaml').write_text('model:\n  provider: company\n  default: custom-model\nproviders:\n  company:\n    base_url: https://example.test/v1\n    key_env: COMPANY_VALUE\n    transport: anthropic_messages\n', encoding='utf-8')
        (root / '.env').write_text('COMPANY_VALUE=synthetic-company\nOLLAMA_API_KEY=synthetic-ollama\nFEISHU_APP_SECRET=channel\nWEIXIN_TOKEN=channel\n', encoding='utf-8')
        result = self.create({**body, 'source': None, 'reuse_auth': False}).json()
        home = Path(result['home'])
        config = profile_create.files._yaml((home / 'config.yaml').read_bytes())[1]
        self.assertEqual(config['providers']['company']['key_env'], 'COMPANY_VALUE')
        self.assertEqual(config['providers']['company']['transport'], 'anthropic_messages')
        env = (home / '.env').read_text()
        self.assertIn('COMPANY_VALUE=', env)
        self.assertIn('OLLAMA_API_KEY=', env)
        self.assertNotIn('FEISHU', env)
        self.assertNotIn('WEIXIN', env)
        self.assertTrue(result['auth_copied'])
        self.assertNotIn('synthetic-company', json.dumps(result))

    def test_hermes_selected_profile_is_the_only_credential_source(self):
        root, body = self.fixture('hermes')
        other = root / 'profiles' / 'source'
        other.mkdir(parents=True)
        (other / 'config.yaml').write_text('model:\n  provider: ollama-cloud\n  default: source-model\n')
        (other / '.env').write_text('OLLAMA_API_KEY=source-secret\n')
        result = self.create({**body, 'source': {'profile': 'source', 'config_root': str(root)}}).json()
        env = (Path(result['home']) / '.env').read_text()
        self.assertIn('source-secret', env)
        self.assertNotIn('OPENAI_API_KEY', env)

    def test_hermes_incompatible_install_rolls_back_creation(self):
        root, body = self.fixture('hermes')
        (root / 'hermes-agent/hermes_cli/auth.py').write_text('_REGISTRY_ROWS = unsupported()')
        response = self.create(body, 400)
        self.assertIn('适配', response.text)
        self.assertFalse((root / 'profiles' / body['profile']).exists())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM bots WHERE profile='new-dev'").fetchone()[0], 0)

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
