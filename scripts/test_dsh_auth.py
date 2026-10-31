"""req#137 task#265：dsh 认证与凭据接入测试。

覆盖（对应验收标准）：
- 账号 / API-key(.env) / API-key(界面原生凭据) 三种来源的路由解析
- overlay 条目：账号恢复 credentials/authorization/deepseek-account，API-key 保持
  deepseek-account 关闭（.env 无需 credentials、界面凭据显式恢复 credentials）
- 认证失败识别（MISSING_CREDENTIAL / AUTH / final 文本）+ 脱敏
- 凭据临时副本字节一致 + 原文件保持原值 + Windows ACL 限制调用

运行：python -B -m unittest scripts.test_dsh_auth -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import dsh_auth  # noqa: E402


ACCOUNT_PATCH = """\
- id: agent-default-model
  config:
    provider: deepseek-account
    model: deepseek-flash
- id: llm-deepseek
  config:
    baseUrl: https://api.deepseek.com
- id: deepseek-account
  disabled: true
  config:
    accountId: test-account
"""

APIKEY_PATCH = """\
- id: agent-default-model
  config:
    provider: deepseek-official
    model: deepseek-chat
- id: llm-deepseek
  config:
    baseUrl: https://api.deepseek.com
"""


class DshAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.droot = Path(self.tmp.name) / "dsh"
        (self.droot / "profiles" / "desktop").mkdir(parents=True)
        self.patch = self.droot / "profiles" / "desktop" / "cordis.patch.yml"
        self.envfile = self.droot / ".env"
        self.credfile = self.droot / ".credentials.yaml"
        self.bot = {"config_root": str(self.droot), "profile": "desktop"}

    def tearDown(self):
        self.tmp.cleanup()

    def _account(self):
        self.patch.write_text(ACCOUNT_PATCH, encoding="utf-8")
        self.credfile.write_text("account: saved-login\n", encoding="utf-8")

    def _apikey(self, *, env_key=None, native=False):
        self.patch.write_text(APIKEY_PATCH, encoding="utf-8")
        if env_key:
            self.envfile.write_text(f"DEEPSEEK_API_KEY={env_key}\n", encoding="utf-8")
        if native:
            self.credfile.write_text("key: ui-saved\n", encoding="utf-8")

    def test_resolve_account_route(self):
        self._account()
        info = dsh_auth.resolve(self.bot)
        self.assertEqual(info["route"], "account")
        self.assertEqual(info["provider"], "deepseek-account")
        self.assertEqual(info["model"], "deepseek-flash")
        self.assertEqual(info["api_key_source"], None)
        self.assertIsNotNone(info["credentials_file"])

    def test_resolve_apikey_env_route(self):
        self._apikey(env_key="sk-test123456")
        info = dsh_auth.resolve(self.bot)
        self.assertEqual(info["route"], "apikey")
        self.assertEqual(info["provider"], "deepseek-official")
        self.assertEqual(info["api_key_source"], "env")

    def test_resolve_apikey_native_route(self):
        self._apikey(native=True)
        info = dsh_auth.resolve(self.bot)
        self.assertEqual(info["route"], "apikey")
        self.assertEqual(info["api_key_source"], "native")
        self.assertIsNotNone(info["credentials_file"])

    def test_overlay_account_route_recovers_three(self):
        self._account()
        entries = dsh_auth.overlay_entries(self.bot)
        self.assertIn({'id': 'credentials', 'disabled': False,
                       'config': {'path': str(self.credfile), 'watch': False}}, entries)
        self.assertIn({'id': 'authorization', 'disabled': False}, entries)
        # deepseek-account 按 id 合并为唯一条目：最终 disabled=False + 保留 accountId
        dsa = [e for e in entries if isinstance(e, dict) and e.get('id') == 'deepseek-account']
        self.assertEqual(len(dsa), 1)
        self.assertFalse(dsa[0]['disabled'])
        self.assertEqual(dsa[0]['config']['accountId'], 'test-account')
        self.assertTrue(any(e.get('id') == 'agent-default-model'
                            for e in entries if isinstance(e, dict)))

    def test_overlay_apikey_merges_conflicting_disabled(self):
        # 原配置 deepseek-account disabled:false + apikey 路由 → 最终 disabled=True、accountId 保留
        self.patch.write_text(
            "- id: agent-default-model\n  config:\n    provider: deepseek-official\n"
            "    model: deepseek-chat\n"
            "- id: deepseek-account\n  disabled: false\n  config:\n    accountId: test-account\n",
            encoding="utf-8")
        self.envfile.write_text("DEEPSEEK_API_KEY=sk-test123456\n", encoding="utf-8")
        entries = dsh_auth.overlay_entries(self.bot)
        dsa = [e for e in entries if isinstance(e, dict) and e.get('id') == 'deepseek-account']
        self.assertEqual(len(dsa), 1)
        self.assertTrue(dsa[0]['disabled'])
        self.assertEqual(dsa[0]['config']['accountId'], 'test-account')

    def test_overlay_apikey_env_keeps_account_disabled(self):
        self._apikey(env_key="sk-test123456")
        entries = dsh_auth.overlay_entries(self.bot)
        self.assertIn({'id': 'deepseek-account', 'disabled': True}, entries)
        self.assertFalse(any(e.get('id') == 'credentials' for e in entries if isinstance(e, dict)))
        self.assertFalse(any(e.get('id') == 'authorization' for e in entries if isinstance(e, dict)))

    def test_overlay_apikey_native_recovers_credentials_only(self):
        self._apikey(native=True)
        entries = dsh_auth.overlay_entries(self.bot)
        self.assertIn({'id': 'deepseek-account', 'disabled': True}, entries)
        self.assertIn({'id': 'credentials', 'disabled': False,
                       'config': {'path': str(self.credfile), 'watch': False}}, entries)
        self.assertFalse(any(e.get('id') == 'authorization' for e in entries if isinstance(e, dict)))

    def test_overlay_disables_telemetry(self):
        self._apikey(env_key="sk-test123456")
        entries = dsh_auth.overlay_entries(self.bot)
        self.assertIn({'id': 'session-log-deepseek', 'disabled': True}, entries)
        self.assertIn({'id': 'session-telemetry-otel', 'disabled': True}, entries)

    def _fake_run(self, restrict_rc=0, acl_stdout='DOMAIN\\user:(OI)(CI)F\n'):
        def run(args, **kwargs):
            if '/inheritance:r' in args:
                return type('R', (), {'returncode': restrict_rc, 'stdout': '', 'stderr': ''})()
            return type('R', (), {'returncode': 0, 'stdout': acl_stdout, 'stderr': ''})()
        return run

    def test_copy_credentials_byte_identical_and_original_kept(self):
        self._account()
        original = self.credfile.read_bytes()
        private = Path(self.tmp.name) / "private"
        private.mkdir()
        with patch.object(dsh_auth, '_windows_identity', return_value='DOMAIN\\user'), \
             patch.object(dsh_auth.subprocess, 'run', side_effect=self._fake_run()):
            dest = dsh_auth.copy_credentials(str(self.droot), str(private))
        self.assertIsNotNone(dest)
        self.assertEqual(Path(dest).read_bytes(), original)
        self.assertEqual(self.credfile.read_bytes(), original)  # 原文件保持原值

    def test_copy_credentials_icacls_failure_aborts(self):
        self._account()
        private = Path(self.tmp.name) / "private"
        private.mkdir()
        with patch.object(dsh_auth, '_windows_identity', return_value='DOMAIN\\user'), \
             patch.object(dsh_auth.subprocess, 'run', side_effect=self._fake_run(restrict_rc=1)):
            with self.assertRaises(dsh_auth.AuthSyncError):
                dsh_auth.copy_credentials(str(self.droot), str(private))
        self.assertFalse((private / 'credentials.yaml').exists())  # 无副本残留

    def test_copy_credentials_missing_identity_aborts(self):
        self._account()
        private = Path(self.tmp.name) / "private"
        private.mkdir()
        with patch.object(dsh_auth, '_windows_identity', return_value=''):
            with self.assertRaises(dsh_auth.AuthSyncError):
                dsh_auth.copy_credentials(str(self.droot), str(private))
        self.assertFalse((private / 'credentials.yaml').exists())

    def test_copy_credentials_broad_ace_aborts(self):
        self._account()
        private = Path(self.tmp.name) / "private"
        private.mkdir()
        with patch.object(dsh_auth, '_windows_identity', return_value='DOMAIN\\user'), \
             patch.object(dsh_auth.subprocess, 'run',
                          side_effect=self._fake_run(acl_stdout='BUILTIN\\Users:(OI)(CI)(RX)\n')):
            with self.assertRaises(dsh_auth.AuthSyncError):
                dsh_auth.copy_credentials(str(self.droot), str(private))
        self.assertFalse((private / 'credentials.yaml').exists())

    def test_copy_credentials_missing_returns_none(self):
        private = Path(self.tmp.name) / "private"
        private.mkdir()
        self.assertIsNone(dsh_auth.copy_credentials(str(self.droot), str(private)))

    def test_is_auth_error(self):
        self.assertTrue(dsh_auth.is_auth_error({"type": "MISSING_CREDENTIAL"}))
        self.assertTrue(dsh_auth.is_auth_error({"type": "AUTH"}))
        self.assertTrue(dsh_auth.is_auth_error({"type": "final", "text": "AUTH: Authentication Fails ... is invalid"}))
        self.assertTrue(dsh_auth.is_auth_error({"type": "error", "message": "401 unauthorized"}))
        self.assertFalse(dsh_auth.is_auth_error({"type": "final", "text": "正常完成"}))
        self.assertFalse(dsh_auth.is_auth_error({"type": "tool_call"}))
        self.assertFalse(dsh_auth.is_auth_error("not-a-dict"))

    def test_sanitize_redacts_credentials(self):
        msg = dsh_auth.sanitize("failed: sk-abcdef123456 DEEPSEEK_API_KEY=xyz bearer abc")
        self.assertNotIn("sk-abcdef", msg)
        self.assertNotIn("DEEPSEEK_API_KEY=xyz", msg)
        self.assertIn("[REDACTED]", msg)


if __name__ == "__main__":
    unittest.main()
