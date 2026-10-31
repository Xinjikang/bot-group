"""req#137 task#265：verify_dsh_auth_apikey 三态合成判定回归。

覆盖 assess 的成功 / 全失败 / 单路失败三态，及 provider 派生 / key 归属服务核对（非硬编码）：
- 三来源均成功（exit0/DSH_NATIVE_OK/无 auth_error）→ passed=true；
- 三来源全失败（401）→ passed=false + diagnosis_ok=true（401 根因单列）；
- 单路失败（native 401）→ passed=false + diagnosis_ok=false（非全 401）。

运行：python -B -m unittest scripts.test_verify_dsh_auth_apikey -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import verify_dsh_auth_apikey as V  # noqa: E402


class AssessThreeStateTest(unittest.TestCase):
    """成功 / 全失败 / 单路失败三态合成回归。"""

    def _ok(self):
        return {'answer': 'DSH_NATIVE_OK', 'exit_code': 0, 'timed_out': False, 'auth_error': False}

    def _fail(self):
        return {'answer': '[unexpected]', 'exit_code': 1, 'timed_out': False, 'auth_error': True}

    def _binding(self):
        return {'credentials_ref': 'refs.DEEPSEEK_API_KEY', 'key_service_matched': True}

    def test_success_all_three(self):
        r = V.assess(self._ok(), self._ok(), self._ok(), self._binding(), True)
        self.assertTrue(r['passed'])               # 三来源成功 → passed
        self.assertTrue(r['switch_account_to_apikey'])  # 三段均实际调用
        self.assertFalse(r['diagnosis_ok'])        # 无 401 → 无诊断

    def test_all_failure_401(self):
        r = V.assess(self._fail(), self._fail(), self._fail(), self._binding(), True)
        self.assertFalse(r['passed'])              # 全失败 → 非 passed
        self.assertTrue(r['switch_account_to_apikey'])
        self.assertTrue(r['diagnosis_ok'])         # 全 401 → 401 根因诊断通过

    def test_single_failure(self):
        r = V.assess(self._ok(), self._ok(), self._fail(), self._binding(), True)
        self.assertFalse(r['passed'])              # 单路失败 → 非 passed
        self.assertFalse(r['diagnosis_ok'])        # 非全 401 → 无诊断


class DerivationTest(unittest.TestCase):
    """provider 派生 + key 归属服务核对（非硬编码）。"""

    def test_provider_derived_from_key(self):
        self.assertEqual(V._api_key_provider('refs.DEEPSEEK_API_KEY'), 'deepseek-official')
        self.assertIsNone(V._api_key_provider(None))

    def test_key_service_matched(self):
        # 无 $DEEPSEEK_BASE_URL → 官方默认端点，base_url 一致
        self.assertTrue(V._key_service_matched('refs.DEEPSEEK_API_KEY',
                                               'https://api.deepseek.com/anthropic', ''))
        # 自定义端点（代理）：服务读 $DEEPSEEK_BASE_URL，非硬编码 api.deepseek.com 子串
        self.assertTrue(V._key_service_matched('refs.DEEPSEEK_API_KEY',
                                               'https://proxy.example.com/v1', 'https://proxy.example.com/v1'))
        # base_url 与服务不一致 → 不匹配
        self.assertFalse(V._key_service_matched('refs.DEEPSEEK_API_KEY',
                                                'https://api.deepseek.com/anthropic', 'https://proxy.example.com/v1'))
        # 无 key 引用 → 不匹配
        self.assertFalse(V._key_service_matched(None, 'https://api.deepseek.com/anthropic', ''))


if __name__ == '__main__':
    unittest.main()
