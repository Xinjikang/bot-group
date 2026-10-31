"""req#137 task#265：verify_dsh_auth_apikey 三态合成判定回归。

覆盖 assess 的成功 / 全失败 / 单路失败 / 路由错漏四态，及 provider 派生 / key 归属服务独立核对
（非硬编码、非自比较）/ overlay 三服务按 id 取 + disabled 顶层：
- 三来源均成功（exit0/DSH_NATIVE_OK/无 auth_error）+ 路由正确 → passed=true；
- 三来源全失败（401）→ passed=false + diagnosis_ok=true（401 根因单列）；
- 单路失败（native 401）→ passed=false + diagnosis_ok=false（非全 401）；
- 路由错/漏切回 → switch=false（不通过）。

运行：python -B -m unittest scripts.test_verify_dsh_auth_apikey -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import verify_dsh_auth_apikey as V  # noqa: E402


ROUTES_OK = ('account', 'apikey', 'apikey')


class AssessSynthesisTest(unittest.TestCase):
    """成功 / 全失败 / 单路失败 / 路由错漏合成回归。"""

    def _ok(self):
        return {'answer': 'DSH_NATIVE_OK', 'exit_code': 0, 'timed_out': False, 'auth_error': False}

    def _fail(self):
        return {'answer': '[unexpected]', 'exit_code': 1, 'timed_out': False, 'auth_error': True}

    def _binding(self):
        return {'credentials_ref': 'refs.DEEPSEEK_API_KEY', 'key_service_matched': True}

    def test_success_all_three(self):
        r = V.assess(self._ok(), self._ok(), self._ok(), self._binding(), True, ROUTES_OK)
        self.assertTrue(r['passed'])               # 三来源成功 → passed
        self.assertTrue(r['switch_account_to_apikey'])
        self.assertFalse(r['diagnosis_ok'])        # 无 401 → 无诊断

    def test_all_failure_401(self):
        r = V.assess(self._fail(), self._fail(), self._fail(), self._binding(), True, ROUTES_OK)
        self.assertFalse(r['passed'])              # 全失败 → 非 passed
        self.assertTrue(r['switch_account_to_apikey'])
        self.assertTrue(r['diagnosis_ok'])         # 全 401 → 401 根因诊断通过

    def test_single_failure(self):
        r = V.assess(self._ok(), self._ok(), self._fail(), self._binding(), True, ROUTES_OK)
        self.assertFalse(r['passed'])              # 单路失败 → 非 passed
        self.assertFalse(r['diagnosis_ok'])        # 非全 401 → 无诊断

    def test_route_error_switch_fails(self):
        # 路由错：account 路由解析为 apikey → switch 不通过
        r = V.assess(self._ok(), self._ok(), self._ok(), self._binding(), True,
                     ('apikey', 'apikey', 'apikey'))
        self.assertFalse(r['switch_account_to_apikey'])
        self.assertFalse(r['passed'])

    def test_route_missing_switchback(self):
        # 漏切回：env 路由仍 account → switch 不通过
        r = V.assess(self._ok(), self._ok(), self._ok(), self._binding(), True,
                     ('account', 'account', 'apikey'))
        self.assertFalse(r['switch_account_to_apikey'])
        self.assertFalse(r['passed'])


class DerivationTest(unittest.TestCase):
    """provider 派生 + key 归属服务独立核对（非硬编码、非自比较）。"""

    def test_provider_derived_from_key(self):
        self.assertEqual(V._api_key_provider('refs.DEEPSEEK_API_KEY'), 'deepseek-official')
        self.assertIsNone(V._api_key_provider(None))

    def test_key_service_status(self):
        # 官方端点（无 $DEEPSEEK_BASE_URL）→ 主机含 key 服务 → True
        self.assertTrue(V._key_service_status('refs.DEEPSEEK_API_KEY',
                                             'https://api.deepseek.com/anthropic', ''))
        # 自定义端点（代理）→ 缺独立依据 → None（unknown）
        self.assertIsNone(V._key_service_status('refs.DEEPSEEK_API_KEY',
                                                'https://proxy.example.com/v1', 'https://proxy.example.com/v1'))
        # 未知 key 服务 → None
        self.assertIsNone(V._key_service_status('refs.OPENAI_API_KEY',
                                               'https://api.deepseek.com/anthropic', ''))
        # 无 key 引用 → None
        self.assertIsNone(V._key_service_status(None, 'https://api.deepseek.com/anthropic', ''))


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
