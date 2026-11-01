"""req#138 task#269：dsh 后端返修（F1/F3/F4/F5/F8/F10）单元回归。

覆盖：
- F1：ProfileConfigEdit 接受 persona/instructions（无 soul），DSH 保存回读一致。
- F3：项目覆盖 provider 后认证路由与运行 provider 一致（deepseek-account 正确 disabled）。
- F4：dsh_overlay 保留 insert 无 id 插件与 !!js 动态表达式，persona 不丢失。
- F5：项目覆盖 reasoning_effort 映射为原生 reasoningEffort。
- F8：桌面 bundled dsh.cmd 经卸载注册表发现（不依赖 DSH_BIN/PATH）。
- F10：_dsh_effective_exit 区分协议失败（exit 0 缺 final/坏 JSON → -1）。

运行：python -B -m unittest scripts.test_dsh_rework -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import api, db, discovery, dsh_auth, native_runtime, wake  # noqa: E402


class F1ProfileConfigEditTests(unittest.TestCase):
    """DSH 成员编辑请求体（persona/instructions，无 soul）不得 422。"""

    def test_dsh_body_without_soul_accepts_persona_instructions(self):
        body = {
            "description": "一个 DSH 助手",
            "persona": "你是测试人格",
            "instructions": "先思考再回答",
            "env": "DEEPSEEK_API_KEY=sk-test",
            "model": "deepseek-flash",
            "provider": "deepseek-account",
            "reasoning_effort": "medium",
            "auto_review": "off",
            "revision": "a" * 64,
        }
        model = api.ProfileConfigEdit.model_validate(body)
        self.assertEqual(model.persona, "你是测试人格")
        self.assertEqual(model.instructions, "先思考再回答")
        self.assertIsNone(model.soul)  # soul 不必填

    def test_hermes_body_still_accepts_soul(self):
        body = {
            "description": "hermes",
            "soul": "soul 正文",
            "env": "",
            "model": "",
            "provider": "",
            "revision": "a" * 64,
        }
        model = api.ProfileConfigEdit.model_validate(body)
        self.assertEqual(model.soul, "soul 正文")


class F3F4F5OverlayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.droot = Path(self.tmp.name) / "dsh"
        (self.droot / "profiles" / "desktop").mkdir(parents=True)
        self.patch = self.droot / "profiles" / "desktop" / "cordis.patch.yml"

    def tearDown(self):
        self.tmp.cleanup()

    def _bot(self):
        return {"runtime": "dsh", "config_root": str(self.droot), "profile": "desktop"}

    def test_f3_project_provider_overrides_auth_route(self):
        # 全局 account，项目覆盖 official → deepseek-account 应 disabled、provider 应 official。
        self.patch.write_text(
            "- id: agent-default-model\n"
            "  config:\n    provider: deepseek-account\n    model: deepseek-flash\n",
            encoding="utf-8")
        overlay = native_runtime.dsh_overlay(
            self._bot(), project_overrides={"provider": "deepseek-official"})
        by_id = {e.get("id"): e for e in overlay}
        self.assertEqual(by_id["agent-default-model"]["config"]["provider"], "deepseek-official")
        self.assertTrue(by_id["deepseek-account"].get("disabled"))

    def test_f4_preserves_insert_and_js_expression_and_persona(self):
        self.patch.write_text(
            "- id: system-prompt\n"
            "  config:\n    personaPrefix: 探针人格\n"
            "- id: agent-default-model\n"
            "  config:\n    cwd: !!js \"process.cwd()\"\n"
            "- insert:\n    - id: custom-plugin\n      name: fixture-plugin\n",
            encoding="utf-8")
        overlay = native_runtime.dsh_overlay(self._bot())
        text = native_runtime.overlay_yaml(overlay)
        # persona 不丢失
        self.assertIn("personaPrefix: 探针人格", text)
        # insert 无 id 条目保留
        self.assertIn("custom-plugin", text)
        self.assertIn("fixture-plugin", text)
        # !!js 动态表达式原样透传（YAML 标签保留，不被 JSON 丢弃）
        self.assertIn("!!js", text)
        self.assertIn("process.cwd()", text)

    def test_f5_reasoning_effort_maps_to_native_field(self):
        self.patch.write_text(
            "- id: agent-default-model\n"
            "  config:\n    provider: deepseek-account\n    model: deepseek-flash\n",
            encoding="utf-8")
        overlay = native_runtime.dsh_overlay(
            self._bot(), project_overrides={"reasoning_effort": "low"})
        text = native_runtime.overlay_yaml(overlay)
        self.assertIn("reasoningEffort: low", text)
        self.assertNotIn("reasoning_effort", text)

    def test_f4_deepseek_account_dynamic_config_preserved(self):
        # req#138 返修①：deepseek-account 控制项不能整项替换丢 !!js 动态标签。
        self.patch.write_text(
            "- id: agent-default-model\n"
            "  config:\n    provider: deepseek-account\n    model: deepseek-flash\n"
            "- id: deepseek-account\n"
            "  config:\n    accountId: !!js process.env.ACCOUNT_ID\n",
            encoding="utf-8")
        text = native_runtime.overlay_yaml(native_runtime.dsh_overlay(self._bot()))
        self.assertIn("!!js process.env.ACCOUNT_ID", text)
        self.assertNotIn("'[expression]'", text)
        self.assertNotIn('"[expression]"', text)
        # 控制字段 disabled 仍正确合并
        self.assertIn("disabled: false", text)

    def test_f4_duplicate_agent_default_model_project_override_on_last(self):
        # req#138 返修②：重复 agent-default-model 时项目覆盖落在末位（原生生效项）。
        self.patch.write_text(
            "- id: agent-default-model\n"
            "  config:\n    provider: deepseek-account\n    model: deepseek-flash\n"
            "- id: agent-default-model\n"
            "  config:\n    provider: deepseek-account\n    model: deepseek-chat\n",
            encoding="utf-8")
        overlay = native_runtime.dsh_overlay(
            self._bot(), project_overrides={"provider": "deepseek-official"})
        adm = [e for e in overlay if e.get("id") == "agent-default-model"]
        self.assertEqual(len(adm), 2)
        # 末位（生效项）为项目覆盖 provider，认证服务 disabled 一致
        self.assertEqual(adm[-1]["config"]["provider"], "deepseek-official")
        by_id = {e.get("id"): e for e in overlay}
        self.assertTrue(by_id["deepseek-account"].get("disabled"))


class F8BundledEntryTests(unittest.TestCase):
    def test_bundled_entry_from_registry(self):
        root = Path(tempfile.mkdtemp(prefix="dsh_install_"))
        entry = root / "resources" / "runtime" / "cli" / "bin" / "dsh.cmd"
        entry.parent.mkdir(parents=True)
        entry.write_text("@echo off\n", encoding="utf-8")

        registry = {
            "DisplayName": "DeepSeek Harness 0.2.1-rc.5",
            "InstallLocation": str(root),
        }

        def qve(_sk, name):
            if name in registry:
                return (registry[name], 0)
            raise OSError("not found")

        dummy = object()
        with patch("winreg.OpenKey", return_value=dummy), \
             patch("winreg.QueryInfoKey", return_value=(1, 0)), \
             patch("winreg.EnumKey", return_value="deepseek"), \
             patch("winreg.QueryValueEx", side_effect=qve):
            found = discovery._dsh_bundled_entry()
        self.assertEqual(found, str(entry))


class F10EffectiveExitTests(unittest.TestCase):
    def test_exit0_no_final_is_protocol_failure(self):
        self.assertEqual(wake._dsh_effective_exit("dsh", 0, had_final=False, corrupt_count=0), -1)

    def test_exit0_corrupt_json_is_protocol_failure(self):
        self.assertEqual(wake._dsh_effective_exit("dsh", 0, had_final=True, corrupt_count=3), -1)

    def test_exit0_clean_is_zero(self):
        self.assertEqual(wake._dsh_effective_exit("dsh", 0, had_final=True, corrupt_count=0), 0)

    def test_nonzero_exit_kept(self):
        self.assertEqual(wake._dsh_effective_exit("dsh", 2, had_final=False, corrupt_count=0), 2)

    def test_non_dsh_runtime_ignores_protocol_flags(self):
        self.assertEqual(wake._dsh_effective_exit("codex", 0, had_final=False, corrupt_count=5), 0)


if __name__ == "__main__":
    unittest.main()
