"""Static provider scanning uses synthetic metadata and secrets in temporary homes."""
from io import StringIO
from pathlib import Path
import tempfile
import unittest

from dotenv import dotenv_values
from engine import profile_credentials as credentials


def seed_catalog(root):
    cli = root / 'hermes-agent' / 'hermes_cli'
    cli.mkdir(parents=True, exist_ok=True)
    (cli / 'auth.py').write_text('''raise RuntimeError("must never execute")
_REGISTRY_ROWS = (
    ("openai", "OpenAI", BASE_URL, ("OPENAI_API_KEY",), "OPENAI_BASE_URL"),
    ("ollama-cloud", "Ollama", BASE_URL, ("OLLAMA_API_KEY",), "OLLAMA_BASE_URL"),
    ("deepseek", "DeepSeek", BASE_URL, ("DEEPSEEK_API_KEY",)),
    ProviderConfig("special", "Special", "api_key", api_key_env_vars=("SPECIAL_TOKEN",)),
)
''', encoding='utf-8')
    (cli / 'providers.py').write_text('HERMES_OVERLAYS = {"ollama-cloud": HermesOverlay(extra_env_vars=("OLLAMA_ALT_TOKEN",), base_url_env_var="OLLAMA_BASE_URL")}\n', encoding='utf-8')
    (cli / 'config_defaults.py').write_text('''OPTIONAL_ENV_VARS = {
"OPENAI_API_KEY": _prov("key"), "OLLAMA_API_KEY": _prov("key"),
"OLLAMA_BASE_URL": _base_url("Ollama"), "FEISHU_APP_SECRET": _msg("channel"),
"WEIXIN_TOKEN": _msg("channel"), "BROWSER_TIMEOUT": _setting("timeout"),
"WEB_SEARCH_KEY": _tool("search"), "CUSTOM_MODEL_OPTION": _env("option", category="provider"),
}
''', encoding='utf-8')


class ProviderCredentialTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        seed_catalog(self.root)

    def copy(self, config, raw):
        env, provisioned = credentials.copy_environment(self.root, self.root, config, raw)
        return dotenv_values(stream=StringIO(env.decode()), interpolate=False), provisioned

    def test_metadata_union_and_no_execution(self):
        (self.root / 'models_dev_cache.json').write_text('{"new-provider":{"env":["NEW_PROVIDER_TOKEN"]}}')
        values, ready = self.copy({'model': {'provider': 'ollama-cloud'}},
            'OLLAMA_API_KEY=ollama-secret\nDEEPSEEK_API_KEY=deepseek-secret\n'
            'OLLAMA_BASE_URL=https://example.test/v1\nNEW_PROVIDER_TOKEN=new-secret\n'
            'FEISHU_APP_SECRET=channel\nWEIXIN_TOKEN=channel\nBROWSER_TIMEOUT=60\nUNKNOWN_API_KEY=ignored\n')
        self.assertEqual(set(values), {'OLLAMA_API_KEY', 'DEEPSEEK_API_KEY', 'OLLAMA_BASE_URL', 'NEW_PROVIDER_TOKEN'})
        self.assertTrue(ready)

    def test_custom_names_inline_and_legacy(self):
        config = {'model': {'provider': 'company', 'key_env': 'COMPANY_CREDENTIAL'},
                  'providers': {'company': {'base_url': 'https://example.test', 'api_key_env': 'COMPANY_CREDENTIAL'}},
                  'custom_providers': [{'name': 'legacy', 'key_env': 'LEGACY_VALUE'}],
                  'model_aliases': {'small': {'key_env': 'ALIAS_VALUE'}}}
        values, ready = self.copy(config, 'export COMPANY_CREDENTIAL=" a=b#c "\nLEGACY_VALUE=legacy\nALIAS_VALUE=alias\n')
        self.assertEqual(values, {'COMPANY_CREDENTIAL': ' a=b#c ', 'LEGACY_VALUE': 'legacy', 'ALIAS_VALUE': 'alias'})
        self.assertTrue(ready)
        self.assertTrue(self.copy({'model': {'api_key': 'inline-value'}}, '')[1])

    def test_credential_round_trip_and_unrelated_key_is_not_ready(self):
        raw = "OLLAMA_API_KEY='line1\nline2\\\\path\\\'quoted'\n"
        values, ready = self.copy({'model': {'provider': 'ollama-cloud'}}, raw)
        self.assertEqual(values, dotenv_values(stream=StringIO(raw), interpolate=False))
        self.assertTrue(ready)
        self.assertFalse(self.copy({'model': {'provider': 'ollama-cloud'}}, 'DEEPSEEK_API_KEY=other')[1])

    def test_incompatible_metadata_fails_explicitly(self):
        (self.root / 'hermes-agent/hermes_cli/auth.py').write_text('_REGISTRY_ROWS = build_registry()')
        with self.assertRaises(ValueError):
            self.copy({}, 'OPENAI_API_KEY=secret')


if __name__ == '__main__':
    unittest.main()
