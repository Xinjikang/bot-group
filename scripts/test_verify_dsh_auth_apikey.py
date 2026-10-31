"""req#137 task#265：verify_dsh_auth_apikey 三态合成判定回归。

覆盖 assess 的成功 / 全失败 / 单路失败 / 路由错漏 / 漏切回五态，provider 从主 profile 配置
确认、失败分类区分（missing_credential / auth / init，不把任意失败归因 key-invalid），及
overlay 三服务按 id 取 + disabled 顶层：
- 三来源均成功（exit0/DSH_NATIVE_OK/无失败）+ 路由正确 → passed=true；
- 三来源全失败（auth）→ passed=false + diagnosis_ok=true（service 已确认时 401 根因单列）；
- service 未确认（provider/baseURL 未从配置确认）→ 两源 auth 也不诊断（不归因 key-invalid）；
- missing_credential / init 失败 → 非 passed + 不诊断 key-invalid（归因区分）；
- 单路失败（native auth）→ passed=false + diagnosis_ok=false（非全 auth）；
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
        return {'answer': 'DSH_NATIVE_OK', 'exit_code': 0, 'timed_out': False, 'failure_kind': None}

    def _fail(self):
        return {'answer': '[unexpected]', 'exit_code': 1, 'timed_out': False, 'failure_kind': 'auth'}

    def _missing(self):
        return {'answer': '[unexpected]', 'exit_code': 1, 'timed_out': False, 'failure_kind': 'missing_credential'}

    def _init(self):
        return {'answer': '[unexpected]', 'exit_code': 1, 'timed_out': False, 'failure_kind': 'init'}

    def _binding(self):
        return {'api_key_present': True, 'service_confirmed': True}

    def test_success_all_three(self):
        r = V.assess(self._ok(), self._ok(), self._ok(), self._ok(), self._binding(), True, ROUTES_OK)
        self.assertTrue(r['passed'])               # 三来源成功 → passed
        self.assertTrue(r['switch_account_to_apikey'])
        self.assertTrue(r['account_back_ok'])      # 切回 account 成功
        self.assertFalse(r['diagnosis_ok'])        # 无 401 → 无诊断

    def test_all_failure_401(self):
        # 两 API-key 源 auth（key 对已确认端点无效）→ 非 passed + 401 根因诊断
        r = V.assess(self._ok(), self._fail(), self._fail(), self._ok(), self._binding(), True, ROUTES_OK)
        self.assertFalse(r['passed'])              # 非 passed
        self.assertTrue(r['switch_account_to_apikey'])
        self.assertTrue(r['diagnosis_ok'])         # 两 API-key 源 auth = key 对已确认端点无效

    def test_service_not_confirmed(self):
        # 未从配置确认 provider/baseURL → 两源 auth 也不诊断（不得归因 key-invalid）
        b = {'api_key_present': True, 'service_confirmed': False}
        r = V.assess(self._ok(), self._fail(), self._fail(), self._ok(), b, True, ROUTES_OK)
        self.assertFalse(r['passed'])
        self.assertFalse(r['diagnosis_ok'])        # service 未确认 → 不诊断 key-invalid

    def test_missing_credential_not_key_invalid(self):
        # 两源 missing_credential（凭据缺失，非 key 无效）→ 不诊断 key-invalid
        r = V.assess(self._ok(), self._missing(), self._missing(), self._ok(), self._binding(), True, ROUTES_OK)
        self.assertFalse(r['passed'])
        self.assertFalse(r['diagnosis_ok'])        # missing_credential ≠ auth → 不归因 key-invalid

    def test_init_error_not_key_invalid(self):
        # 两源 init（初始化失败，非认证失败）→ 不诊断 key-invalid
        r = V.assess(self._ok(), self._init(), self._init(), self._ok(), self._binding(), True, ROUTES_OK)
        self.assertFalse(r['passed'])
        self.assertFalse(r['diagnosis_ok'])        # init ≠ auth → 不归因 key-invalid

    def test_single_failure(self):
        # 单路失败（env auth + native ok）→ 非 passed + 无诊断（非全 auth）
        r = V.assess(self._ok(), self._fail(), self._ok(), self._ok(), self._binding(), True, ROUTES_OK)
        self.assertFalse(r['passed'])              # 单路失败 → 非 passed
        self.assertFalse(r['diagnosis_ok'])        # 非全 auth → 无诊断
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
    """失败归因区分 + 显式 profile 配置读取（不跨 profiles 混取、不取无关插件 baseURL）。"""

    def test_attribution_distinguishes_failure(self):
        self.assertEqual(V._attribution('auth', 'auth'), 'key-invalid')
        self.assertEqual(V._attribution('missing_credential', 'missing_credential'), 'missing-credential')
        self.assertEqual(V._attribution('init', 'init'), 'init-error')
        self.assertIsNone(V._attribution(None, None))
        self.assertEqual(V._attribution('auth', 'missing_credential'), 'mixed')

    def test_read_profile_config_specific_profile(self):
        # 两个相互冲突 profile：只取显式选定的 profile，不被未选定 profile 的 provider/model 覆盖
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            profa = home / 'profiles' / 'a'
            profa.mkdir(parents=True)
            (profa / 'cordis.patch.yml').write_text(
                "- id: agent-default-model\n"
                "  config:\n    provider: deepseek-account\n    model: deepseek-flash\n"
                "- id: llm-deepseek\n"
                "  config:\n    baseURL: https://a.internal/anthropic/v1\n",
                encoding='utf-8')
            profb = home / 'profiles' / 'b'
            profb.mkdir(parents=True)
            (profb / 'cordis.patch.yml').write_text(
                "- id: agent-default-model\n"
                "  config:\n    provider: deepseek-official\n    model: deepseek-v4-flash\n"
                "- id: ui-settings\n"  # 无关插件 baseURL，不应被取
                "  config:\n    baseURL: https://unrelated.internal/\n",
                encoding='utf-8')
            provider, model, baseurl = V._read_profile_config(home, 'a')
            self.assertEqual(provider, 'deepseek-account')       # 只取选定 profile A，不被 B 覆盖
            self.assertEqual(model, 'deepseek-flash')
            self.assertEqual(baseurl, 'https://a.internal/anthropic/v1')  # 取所选适配器，非无关插件

    def test_read_profile_config_selected_adapter_baseurl(self):
        # 选定 profile 内：baseURL 取所选适配器（llm-deepseek），非无关插件的 config.baseURL
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            prof = home / 'profiles' / 'a'
            prof.mkdir(parents=True)
            (prof / 'cordis.patch.yml').write_text(
                "- id: agent-default-model\n"
                "  config:\n    provider: deepseek-official\n    model: deepseek-flash\n"
                "- id: ui-settings\n"  # 无关插件 baseURL 不应被取
                "  config:\n    baseURL: https://unrelated.internal/\n"
                "- id: llm-deepseek\n"  # 所选适配器 baseURL 应被取
                "  config:\n    baseURL: https://gateway.internal/anthropic/v1\n",
                encoding='utf-8')
            provider, model, baseurl = V._read_profile_config(home, 'a')
            self.assertEqual(provider, 'deepseek-official')
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
