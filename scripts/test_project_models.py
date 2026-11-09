"""Isolated API and launch coverage for project/member model overrides."""
import json
from engine import auth, project_models
from scripts import test_project_roles, test_runtime_fork
import unittest
from unittest.mock import patch


class ProjectModelTests(unittest.TestCase):
    setUp = test_project_roles.ProjectRoleTests.setUp
    join = test_project_roles.ProjectRoleTests.join

    def test_unsaved_hermes_settings_inherit_all_displayed_host_fields(self):
        from engine import hermes_gateway_sessions
        from unittest.mock import Mock
        member = self.join(host='hermes')
        home = self.root / 'hermes'
        home.mkdir(exist_ok=True)
        config = home / 'config.yaml'
        config.write_text('model:\n  default: host-pro\n  provider: host-cloud\nagent:\n  reasoning_effort: max\n', encoding='utf-8')
        original = config.read_bytes()
        before = [tuple(row) for row in self.conn.execute('SELECT * FROM settings')]
        args = project_models.arguments(self.conn, 'p', member)
        self.assertEqual(args, ['--model', 'host-pro', '--provider', 'host-cloud', '--reasoning', 'max'])
        snapshot = dict(session_id='existing-session', revision=1, execution_generation=1,
                        running=False, pending=False, info=dict(source='acp', cwd='/workspace',
                        edit_approval_policy='workspace_session', model='old-flash'))
        client = Mock()
        client.rpc.return_value = snapshot
        result = hermes_gateway_sessions.configure(client, snapshot, workdir='/workspace',
            model_args=args, server={'command': 'python', 'args': [], 'env': {}})
        changes = [call.kwargs for call in client.rpc.call_args_list if call.args[0] == 'session.mutate']
        self.assertIn({'model': 'host-pro', 'provider': 'host-cloud'},
                      [call['payload'] for call in changes if call['operation'] == 'model'])
        self.assertIn({'effort': 'max'},
                      [call['payload'] for call in changes if call['operation'] == 'reasoning'])
        self.assertEqual(result['session_id'], 'existing-session')
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual([tuple(row) for row in self.conn.execute('SELECT * FROM settings')], before)

    def test_hermes_partial_override_merges_and_host_changes_apply_next_wake(self):
        member = self.join(host='hermes')
        home = self.root / 'hermes'
        home.mkdir(exist_ok=True)
        config = home / 'config.yaml'
        config.write_text('model:\n  default: host-first\n  provider: host-cloud\nagent:\n  reasoning_effort: high\n', encoding='utf-8')
        current, _ = project_models.read(self.conn, 'p', member)
        project_models.save(self.conn, 'p', member, dict(model='project-model', provider='',
            reasoning_effort='', revision=current['revision']))
        self.assertEqual(project_models.arguments(self.conn, 'p', member),
                         ['--model', 'project-model', '--provider', 'host-cloud', '--reasoning', 'high'])
        config.write_text('model:\n  default: host-next\n  provider: next-cloud\nagent:\n  reasoning_effort: false\n', encoding='utf-8')
        self.assertEqual(project_models.arguments(self.conn, 'p', member),
                         ['--model', 'project-model', '--provider', 'next-cloud', '--reasoning', 'none'])
        current, _ = project_models.read(self.conn, 'p', member)
        project_models.save(self.conn, 'p', member, dict(model='', provider='',
            reasoning_effort='', revision=current['revision']))
        self.assertEqual(project_models.arguments(self.conn, 'p', member),
                         ['--model', 'host-next', '--provider', 'next-cloud', '--reasoning', 'none'])

    def test_unsaved_named_hermes_profile_uses_its_physical_config(self):
        member = self.join(host='hermes', profile='review')
        folder = self.root / 'hermes' / 'profiles' / 'review'
        folder.mkdir(parents=True)
        (folder / 'config.yaml').write_text('model:\n  default: named-model\n  provider: named-cloud\nagent:\n  reasoning_effort: low\n', encoding='utf-8')
        (self.root / 'hermes' / 'config.yaml').write_text('model: root-model\n', encoding='utf-8')
        self.assertEqual(project_models.arguments(self.conn, 'p', member),
                         ['--model', 'named-model', '--provider', 'named-cloud', '--reasoning', 'low'])

    def test_unsaved_hermes_missing_default_fails_before_gateway_use(self):
        member = self.join(host='hermes')
        home = self.root / 'hermes'
        home.mkdir(exist_ok=True)
        (home / 'config.yaml').write_text('model: {}\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'configured default model'):
            project_models.arguments(self.conn, 'p', member)

    def test_scope_conflict_permissions_and_clear(self):
        member = self.join()
        self.join('q')
        other = self.join(profile='other')
        config_path = self.root / 'codex' / 'config.toml'
        original_config = config_path.read_bytes()
        url = f'/projects/p/members/{member}/model'
        initial = self.client.get(url).json()
        values = dict(model='chosen', provider='openai', reasoning_effort='high', revision=initial['revision'])
        self.assertEqual(self.client.put(url, json=values, headers={'X-Group-Profile': member, 'X-Group-Key': auth.bot_token(member)}).status_code, 403)
        saved = self.client.put(url, json=values)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(self.client.get(f'/projects/q/members/{member}/model').json()['model'], '')
        self.assertEqual(self.client.get(f'/projects/p/members/{other}/model').json()['model'], '')
        self.assertEqual(self.client.get(f'/projects/missing/members/{member}/model').status_code, 404)
        self.assertEqual(self.client.put(url, json=values).status_code, 409)
        self.assertEqual(project_models.arguments(self.conn, 'p', member),
                         ['--model', 'chosen', '-c', 'model_provider="openai"', '-c', 'model_reasoning_effort="high"'])
        values.update(model='', provider='', reasoning_effort='', revision=saved.json()['revision'])
        self.assertEqual(self.client.put(url, json=values).status_code, 200)
        self.assertEqual(project_models.arguments(self.conn, 'p', member), [])
        self.assertEqual(config_path.read_bytes(), original_config)

    def test_validation_and_hermes_arguments(self):
        for runtime in ('codex', 'hermes'):
            member = self.join(host=runtime)
            url = f'/projects/p/members/{member}/model'
            values = dict(model='test', provider='custom', reasoning_effort='none',
                          revision=self.client.get(url).json()['revision'])
            self.assertEqual(self.client.put(url, json=values).status_code, 400 if runtime == 'codex' else 200)
            if runtime == 'hermes':
                self.assertEqual(project_models.arguments(self.conn, 'p', member),
                                 ['--model', 'test', '--provider', 'custom', '--reasoning', 'none'])
                values['revision'] = self.client.get(url).json()['revision']
            values['model'] = 'bad\nvalue'
            self.assertEqual(self.client.put(url, json=values).status_code, 400)

    def test_hermes_clear_and_provider_only_pin_current_host_default(self):
        member = self.join(host='hermes')
        home = self.root / 'hermes'
        home.mkdir(exist_ok=True)
        config = home / 'config.yaml'
        config.write_text('model:\n  default: host-default\n  provider: original\n', encoding='utf-8')
        original = config.read_bytes()
        url = f'/projects/p/members/{member}/model'
        for model, provider in [('project-model', 'custom'), ('', 'custom'), ('', '')]:
            values = dict(model=model, provider=provider, reasoning_effort='',
                          revision=self.client.get(url).json()['revision'])
            self.assertEqual(self.client.put(url, json=values).status_code, 200)
            args = project_models.arguments(self.conn, 'p', member)
            self.assertEqual(args[:2], ['--model', model or 'host-default'])
            self.assertEqual(config.read_bytes(), original)
        config.write_text('model:\n  default: changed-default\n', encoding='utf-8')
        self.assertEqual(project_models.arguments(self.conn, 'p', member), ['--model', 'changed-default'])
        for raw in ('model:\n  model: alias-model\n', 'model:\n  name: alias-model\n',
                    'model:\n  default:\n    model: alias-model\n    provider: nested\n'):
            config.write_text(raw, encoding='utf-8')
            self.assertEqual(project_models.arguments(self.conn, 'p', member), ['--model', 'alias-model'])


class ProjectModelLaunchTests(unittest.TestCase):
    setUp = test_runtime_fork.RuntimeForkTests.setUp
    tearDown = test_runtime_fork.RuntimeForkTests.tearDown
    _register_bot = test_runtime_fork.RuntimeForkTests._register_bot
    _spawn_capture = test_runtime_fork.RuntimeForkTests._spawn_capture
    def test_override_reaches_codex_and_hermes_spawn(self):
        for runtime in ('codex', 'hermes'):
            with self.subTest(runtime=runtime):
                profile = runtime + '-model'
                self._register_bot(host_type=runtime, profile=profile)
                self.conn.execute('INSERT INTO members(profile,display_name) VALUES(?,?)', (profile, profile))
                self.conn.commit()
                current, _ = project_models.read(self.conn, 'bot-group', profile)
                project_models.save(self.conn, 'bot-group', profile,
                    dict(model='project-model', provider='custom', reasoning_effort='high', revision=current['revision']))
                with patch('engine.codex_auth.sync', return_value=False):
                    result, captured = self._spawn_capture(profile=profile)
                self.assertTrue(result['ok'], result)
                cmd = captured['cmd']
                self.assertEqual(cmd[cmd.index('--model') + 1], 'project-model')
                if runtime == 'codex':
                    self.assertIn('model_provider="custom"', cmd)
                else:
                    self.assertEqual(cmd[cmd.index('--provider') + 1], 'custom')


if __name__ == '__main__':
    unittest.main()
