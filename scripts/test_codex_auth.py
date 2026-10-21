"""File-auth synchronization with synthetic tokens and isolated profile homes."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from engine import codex_auth


def credential(expiry, *, account='account', refresh='refresh'):
    payload = base64.urlsafe_b64encode(json.dumps({'exp': expiry}).encode()).decode().rstrip('=')
    return {'auth_mode': 'chatgpt', 'last_refresh': '2026-10-01T00:00:00Z',
            'tokens': {'account_id': account, 'access_token': f'header.{payload}.signature',
                       'refresh_token': refresh}}


class CodexAuthTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / 'profiles' / 'dev'
        self.home.mkdir(parents=True)
        env = patch.dict(os.environ, {'CODEX_HOME': str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.old = credential(time.time() - 60)
        self.new = credential(time.time() + 3600, refresh='new-refresh')
        self.write(self.root, self.new)
        self.write(self.home, self.old)

    def write(self, home, auth):
        (home / 'auth.json').write_text(json.dumps(auth), encoding='utf-8')

    def test_expired_copy_is_atomic_and_only_written_once(self):
        with patch.object(codex_auth.os, 'replace', wraps=os.replace) as replace:
            self.assertTrue(codex_auth.sync(self.home))
            self.assertFalse(codex_auth.sync(self.home))
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(codex_auth.read_auth(self.home)[1], self.new)
        self.assertEqual(list(self.home.glob('.auth-*.tmp')), [])

    def test_valid_local_token_kept_even_when_primary_differs(self):
        local = credential(time.time() + 900, refresh='local-refresh')
        self.write(self.home, local)
        with patch.object(codex_auth.os, 'replace') as replace:
            self.assertFalse(codex_auth.sync(self.home))
        replace.assert_not_called()
        self.assertEqual(codex_auth.read_auth(self.home)[1], local)

    def test_401_checks_different_credentials_even_before_expiry(self):
        self.write(self.home, credential(time.time() + 900))
        self.assertTrue(codex_auth.sync(self.home, unauthorized=True))
        self.assertFalse(codex_auth.sync(self.home, unauthorized=True))

    def test_primary_is_never_overwritten(self):
        before = (self.root / 'auth.json').read_bytes()
        self.assertFalse(codex_auth.sync(self.root, unauthorized=True))
        self.assertEqual((self.root / 'auth.json').read_bytes(), before)

    def test_missing_child_inherits_primary_and_api_key_is_preserved(self):
        (self.home / 'auth.json').unlink()
        self.assertTrue(codex_auth.sync(self.home))
        self.write(self.home, {'OPENAI_API_KEY': 'synthetic-key'})
        self.assertFalse(codex_auth.sync(self.home))

    def test_expired_primary_does_not_replace_child(self):
        self.write(self.root, self.old)
        before = (self.home / 'auth.json').read_bytes()
        with self.assertRaisesRegex(codex_auth.AuthSyncError, '主账户'):
            codex_auth.sync(self.home)
        self.assertFalse(codex_auth.sync(self.home, unauthorized=True))
        self.assertEqual((self.home / 'auth.json').read_bytes(), before)

    def test_missing_primary_retains_native_keyring_flow(self):
        (self.root / 'auth.json').unlink()
        with self.assertRaises(codex_auth.AuthSyncError):
            codex_auth.sync(self.home)
        (self.home / 'auth.json').unlink()
        self.assertFalse(codex_auth.sync(self.home))

    def test_unknown_token_format_retains_native_flow(self):
        self.write(self.home, {'tokens': {'access_token': 'opaque'}})
        self.assertFalse(codex_auth.sync(self.home))

    def test_different_account_is_not_silently_replaced(self):
        self.write(self.root, credential(time.time() + 3600, account='another'))
        with self.assertRaisesRegex(codex_auth.AuthSyncError, '身份不同'):
            codex_auth.sync(self.home)
        self.assertEqual(codex_auth.read_auth(self.home)[1], self.old)

    def test_malformed_source_and_write_failure_preserve_old_credentials(self):
        (self.root / 'auth.json').write_text('secret-invalid-json', encoding='utf-8')
        with self.assertRaises(codex_auth.AuthSyncError) as caught:
            codex_auth.sync(self.home)
        self.assertNotIn('secret', str(caught.exception))
        self.write(self.root, self.new)
        with patch.object(codex_auth.os, 'replace', side_effect=PermissionError('secret')):
            with self.assertRaises(codex_auth.AuthSyncError) as caught:
                codex_auth.sync(self.home)
        self.assertNotIn('secret', str(caught.exception))
        self.assertEqual(codex_auth.read_auth(self.home)[1], self.old)
        self.assertEqual(list(self.home.glob('.auth-*.tmp')), [])

    def test_concurrent_checks_of_one_profile_only_copy_once(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: codex_auth.sync(self.home), range(12)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(codex_auth.read_auth(self.home)[1], self.new)

    def test_native_update_during_copy_is_preserved(self):
        original = codex_auth.os.fsync
        native = credential(time.time() + 7200, refresh='native-refresh')

        def update(fd):
            original(fd)
            self.write(self.home, native)

        with patch.object(codex_auth.os, 'fsync', side_effect=update):
            self.assertFalse(codex_auth.sync(self.home))
        self.assertEqual(codex_auth.read_auth(self.home)[1], native)

    def test_only_runtime_auth_errors_trigger_recheck(self):
        message = 'Reconnecting... 1/5 (workspace routing discovery unauthorized (401))'
        for event in [{'type': 'error', 'message': message},
                      {'type': 'turn.failed', 'error': {'message': message}},
                      {'type': 'item.completed', 'item': {'type': 'error', 'message': message}}]:
            self.assertTrue(codex_auth.is_auth_error(event))
        for event in [{'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'message': message}},
                      {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': message}},
                      {'type': 'error', 'message': '429 rate limit'},
                      {'type': 'turn.failed', 'error': None}, []]:
            self.assertFalse(codex_auth.is_auth_error(event))


if __name__ == '__main__':
    unittest.main()
