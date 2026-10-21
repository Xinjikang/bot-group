"""Isolated API and launch coverage for project/member model overrides."""
import json
from engine import auth, project_models
from scripts import test_project_roles, test_runtime_fork
import unittest
from unittest.mock import patch


class ProjectModelTests(unittest.TestCase):
    setUp = test_project_roles.ProjectRoleTests.setUp
    join = test_project_roles.ProjectRoleTests.join

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
