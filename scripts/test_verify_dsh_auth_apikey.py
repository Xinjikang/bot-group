"""req#137 task#265：verify_dsh_auth_apikey 三态合成判定回归。

覆盖 assess 的成功 / 全失败 / 单路失败 / 路由错漏 / 漏切回五态，及 provider 从主 profile 配置
确认（非注册表硬套）/ overlay 三服务按 id 取 + disabled 顶层：
- 三来源均成功（exit0/DSH_NATIVE_OK/无 auth_error）+ 路由正确 → passed=true；
- 三来源全失败（401）→ passed=false + diagnosis_ok=true（service 已确认时 401 根因单列）；
- service 未确认（provider/baseURL 未从配置确认）→ 两源 401 也不诊断（不归因 key-invalid）；
- 单路失败（native 401）→ passed=false + diagnosis_ok=false（非全 401）；
- 路由错/漏切回 → switch=false（不通过）。

运行：python -B -m unittest scripts.test_verify_dsh_auth_apikey -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import verify_dsh_auth_apikey as V  # noqa: E402


ROUTES_OK = ('account', 'apikey', 'apikey', 'account')


class AssessSynthesisTest(unittest.TestCase):
    """成功 / 全失败 / 单路失败 / 路由错漏 / 漏切回合成回归。"""

    def _ok(self):
        return {'answer': 'DSH_NATIVE_OK', 'exit_code': 0, 'timed_out': False, 'auth_error': False}

    def _fail(self):
        return {'answer': '[unexpected]', 'exit_code': 1, 'timed_out': False, 'auth_error': True}

    def _binding(self):
        return {'credentials_ref': 'refs.DEEPSEEK_API_KEY', 'service_confirmed': True}

    def test_success_all_three(self):
        r = V.assess(self._ok(), self._ok(), self._ok(), self._ok(), self._binding(), True, ROUTES_OK)
        self.assertTrue(r['passed'])               # 三来源成功 → passed
        self.assertTrue(r['switch_account_to_apikey'])
        self.assertTrue(r['account_back_ok'])      # 切回 account 成功
        self.assertFalse(r['diagnosis_ok'])        # 无 401 → 无诊断

    def test_all_failure_401(self):
        # 两 API-key 源 401（key 对已确认端点无效）→ 非 passed + 401 根因诊断
        r = V.assess(self._ok(), self._fail(), self._fail(), self._ok(), self._binding(), True, ROUTES_OK)
        self.assertFalse(r['passed'])              # 非 passed
        self.assertTrue(r['switch_account_to_apikey'])
        self.assertTrue(r['diagnosis_ok'])         # 两 API-key 源 401 = key 对已确认端点无效

    def test_service_not_confirmed(self):
        # 未从配置确认 provider/baseURL → 两源 401 也不诊断（不得归因 key-invalid）
        b = {'credentials_ref': 'refs.DEEPSEEK_API_KEY', 'service_confirmed': False}
        r = V.assess(self._ok(), self._fail(), self._fail(), self._ok(), b, True, ROUTES_OK)
        self.assertFalse(r['passed'])
        self.assertFalse(r['diagnosis_ok'])        # service 未确认 → 不诊断 key-invalid

    def test_single_failure(self):
        # 单路失败（env 401 + native ok）→ 非 passed + 无诊断（非全 401）
        r = V.assess(self._ok(), self._fail(), self._ok(), self._ok(), self._binding(), True, ROUTES_OK)
        self.assertFalse(r['passed'])              # 单路失败 → 非 passed
        self.assertFalse(r['diagnosis_ok'])        # 非全 401 → 无诊断
        self.assertTrue(r['switch_account_to_apikey'])

    def test_route_error_switch_fails(self):
        # 路由错：account 路由解析为 apikey → switch 不通过
        r = V.assess(self._ok(), self._ok(), self._ok(), self._ok(), self._binding(), True,
                     ('apikey', 'apikey', 'apikey', 'account'))
        self.assertFalse(r['switch_account_to_apikey'])
        self.assertFalse(r['passed'])

    def test_route_missing_switchback(self):
        # 漏切回：env 路由仍 account → switch 不通过
        r = V.assess(self._ok(), self._ok(), self._ok(), self._ok(), self._binding(), True,
                     ('account', 'account', 'apikey', 'account'))
        self.assertFalse(r['switch_account_to_apikey'])
        self.assertFalse(r['passed'])

    def test_missing_switchback_account(self):
        # 漏切回 account 第四段（末段仍 apikey）→ switch 不通过
        r = V.assess(self._ok(), self._ok(), self._ok(), self._ok(), self._binding(), True,
                     ('account', 'apikey', 'apikey', 'apikey'))
        self.assertFalse(r['switch_account_to_apikey'])
        self.assertFalse(r['passed'])

    def test_switchback_account_fails(self):
        # 切回 account 第四段调用失败 → switch 不通过
        r = V.assess(self._ok(), self._ok(), self._ok(), self._fail(), self._binding(), True, ROUTES_OK)
        self.assertFalse(r['switch_account_to_apikey'])
        self.assertFalse(r['passed'])


class DerivationTest(unittest.TestCase):
    """provider 从主 profile 配置确认（非注册表硬套）+ 配置读取。"""

    def test_provider_from_profile_config(self):
        # 官方 API-key 路由 provider → 确认
        self.assertEqual(V._api_key_provider('deepseek-official'), 'deepseek-official')
        # 账号路由 provider → API-key 未选中 → None（unknown）
        self.assertIsNone(V._api_key_provider('deepseek-account'))
        # 缺省/未知 → None
        self.assertIsNone(V._api_key_provider(''))
        self.assertIsNone(V._api_key_provider(None))

    def test_read_profile_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            prof = home / 'profiles' / 'desktop'
            prof.mkdir(parents=True)
            (prof / 'cordis.patch.yml').write_text(
                "- id: agent-default-model\n"
                "  config:\n"
                "    provider: deepseek-account\n"
                "    model: deepseek-flash\n"
                "- id: llm-deepseek\n"
                "  config:\n"
                "    baseURL: https://gateway.internal/anthropic/v1\n",
                encoding='utf-8')
            provider, model, baseurl = V._read_profile_config(home)
            self.assertEqual(provider, 'deepseek-account')
            self.assertEqual(model, 'deepseek-flash')
            self.assertEqual(baseurl, 'https://gateway.internal/anthropic/v1')


class OverlayEvidenceTest(unittest.TestCase):
    """overlay 构建证据：按 id 取三服务 + disabled 取顶层。"""

    def test_three_services_by_id(self):
        overlay = [
            {'id': 'credentials', 'disabled': False, 'config': {'path': '/tmp/x', 'watch': False}},
            {'id': 'authorization', 'disabled': False},
            {'id': 'deepseek-account', 'disabled': False},
            {'id': 'session-log-deepseek', 'disabled': True},
        ]
        by_id = {e['id']: e for e in V._overlay_evidence(overlay)}
        self.assertEqual(set(by_id), {'credentials', 'authorization', 'deepseek-account'})
        self.assertFalse(by_id['credentials']['disabled'])        # 顶层 disabled=False
        self.assertFalse(by_id['authorization']['disabled'])
        self.assertFalse(by_id['deepseek-account']['disabled'])

    def test_disabled_top_level_not_config(self):
        overlay = [
            {'id': 'deepseek-account', 'disabled': True, 'config': {'disabled': False}},
            {'id': 'credentials', 'disabled': False, 'config': {'path': '/tmp/x'}},
        ]
        by_id = {e['id']: e for e in V._overlay_evidence(overlay)}
        self.assertTrue(by_id['deepseek-account']['disabled'])   # 取顶层 True，非 config False
        self.assertTrue(by_id['authorization']['present'] is False)  # 缺 authorization → present=False


if __name__ == '__main__':
    unittest.main()
