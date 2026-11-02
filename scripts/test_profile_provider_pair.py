"""Creation source previews and explicit model/provider pairs use isolated homes."""
import unittest
from pathlib import Path

import yaml

from engine import auth
from scripts import test_profile_create, test_dsh_profile_create


class HermesProviderPairTests(unittest.TestCase):
    setUp = test_profile_create.ProfileCreateTests.setUp
    fixture = test_profile_create.ProfileCreateTests.fixture

    def test_source_preview_and_explicit_pair_round_trip(self):
        root, body = self.fixture('hermes')
        source = root / 'config.yaml'
        source.write_text('model:\n  default: source-model\n  provider: openai\n  base_url: https://example.test/v1\n', encoding='utf-8')
        before = source.read_bytes()
        preview = self.client.post('/profiles/source-settings', json={
            'host_type': 'hermes', 'source': body['source']})
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json(), {'model': 'source-model', 'provider': 'openai'})
        body.update(model='chosen-model', provider='openrouter')
        response = self.client.post('/profiles/create', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        config = yaml.safe_load((Path(response.json()['home']) / 'config.yaml').read_text(encoding='utf-8'))
        self.assertEqual(config['model']['default'], 'chosen-model')
        self.assertEqual(config['model']['provider'], 'openrouter')
        self.assertEqual(source.read_bytes(), before)

    def test_scalar_source_and_blank_pair_validation(self):
        root, body = self.fixture('hermes')
        (root / 'config.yaml').write_text('model: scalar-model\n', encoding='utf-8')
        preview = self.client.post('/profiles/source-settings', json={'host_type': 'hermes'})
        self.assertEqual(preview.json(), {'model': 'scalar-model', 'provider': ''})
        for model, provider in [('only-model', ''), ('', 'openai'), (' ', 'openai')]:
            response = self.client.post('/profiles/create', json={**body, 'model': model, 'provider': provider})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertFalse((root / 'profiles' / body['profile']).exists())
        response = self.client.post('/profiles/create', json={**body, 'model': 'new-model', 'provider': 'openai'})
        self.assertEqual(response.status_code, 200, response.text)
        config = yaml.safe_load((Path(response.json()['home']) / 'config.yaml').read_text(encoding='utf-8'))
        self.assertEqual(config['model']['provider'], 'openai')

    def test_preview_auth_source_boundary_and_parser_redaction(self):
        root, body = self.fixture('hermes')
        request = {'host_type': 'hermes', 'source': body['source']}
        response = self.client.post('/profiles/source-settings', json=request, headers={
            'X-Group-Profile': 'bot-1', 'X-Group-Key': auth.bot_token('bot-1')})
        self.assertEqual(response.status_code, 403, response.text)
        response = self.client.post('/profiles/source-settings', json={
            **request, 'source': {**body['source'], 'profile': 'missing'}})
        self.assertEqual(response.status_code, 409, response.text)
        (root / 'config.yaml').write_text('model: [synthetic-secret', encoding='utf-8')
        response = self.client.post('/profiles/source-settings', json=request)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertNotIn('synthetic-secret', response.text)


class DshProviderPairTests(unittest.TestCase):
    setUp = test_dsh_profile_create.DshProfileCreateTests.setUp
    fixture = test_dsh_profile_create.DshProfileCreateTests.fixture

    def test_preview_empty_source_and_explicit_pair(self):
        from engine import native_runtime
        root, source, body = self.fixture()
        request = {'host_type': 'dsh', 'source': body['source']}
        preview = self.client.post('/profiles/source-settings', json=request)
        self.assertEqual(preview.json(), {'model': 'source-model', 'provider': 'deepseek-account'})
        (source / 'cordis.patch.yml').write_text('[]\n', encoding='utf-8')
        self.assertEqual(self.client.post('/profiles/source-settings', json=request).json(),
                         {'model': '', 'provider': ''})
        self.assertEqual(self.client.post('/profiles/create', json={**body, 'provider': ''}).status_code, 400)
        response = self.client.post('/profiles/create', json={**body, 'provider': 'deepseek-official'})
        self.assertEqual(response.status_code, 200, response.text)
        bot = {'config_root': str(root), 'profile': body['profile']}
        overlay = native_runtime.dsh_overlay(bot)
        entries = {e['id']: e for e in overlay if 'id' in e}
        self.assertEqual(entries['agent-default-model']['config'],
                         {'model': body['model'], 'provider': 'deepseek-official'})
        self.assertTrue(entries['deepseek-account']['disabled'])

    def test_last_model_entry_is_previewed_and_overridden(self):
        root, source, body = self.fixture()
        with (source / 'cordis.patch.yml').open('a', encoding='utf-8') as stream:
            stream.write('\n- id: agent-default-model\n  config:\n    provider: deepseek-official\n    model: last-model\n')
        preview = self.client.post('/profiles/source-settings', json={'host_type': 'dsh', 'source': body['source']})
        self.assertEqual(preview.json(), {'model': 'last-model', 'provider': 'deepseek-official'})
        response = self.client.post('/profiles/create', json={**body, 'provider': 'deepseek-account'})
        self.assertEqual(response.status_code, 200, response.text)
        entries = yaml.safe_load((Path(response.json()['home']) / 'cordis.patch.yml').read_text(encoding='utf-8'))
        model = next(e['config'] for e in reversed(entries) if e.get('id') == 'agent-default-model')
        self.assertEqual(model, {'model': body['model'], 'provider': 'deepseek-account'})


if __name__ == '__main__':
    unittest.main()
