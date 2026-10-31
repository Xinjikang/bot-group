"""req#137 task#268：DSH Auto 审批接入单元测试（mock 隔离库，不起真实 DSH）。

覆盖：overlay insert 结构、适配插件源码与证据解析（session preset / pre-execute 计数）、
启用状态读写（side file fail-closed）、保存校验（插件禁用/未安装拒绝启用）、
审计落库（审查尝试 vs 工具主体执行独立计数）、失败分类 auto_unavailable、
wake fail-closed 判读（证据缺失阻止执行）。
"""
import os
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import db, dsh_auto, execution_errors, native_runtime


class AutoOverlayTests(unittest.TestCase):
    """overlay insert 与适配插件结构。"""

    def test_overlay_inserts_structure(self):
        inserts = dsh_auto.overlay_inserts("C:/tmp/auto-enable-adapter.mjs")
        self.assertEqual(len(inserts), 1)
        entries = inserts[0]["insert"]
        self.assertEqual(entries[0]["id"], "auto-review")
        self.assertEqual(entries[0]["name"], "@deepseek-ai/dsh-experimental-auto-review")
        self.assertFalse(entries[0]["disabled"])
        self.assertEqual(entries[1]["id"], "auto-enable-adapter")
        self.assertIn("auto-enable-adapter.mjs", entries[1]["name"])

    def test_adapter_source_semantics(self):
        src = dsh_auto.adapter_source()
        self.assertIn('ctx.on("session/created"', src)
        self.assertIn('permissionPresets.set(session, "auto")', src)
        self.assertIn('ctx.on("tools/pre-execute"', src)
        self.assertIn('[auto-enable]', src)

    def test_dsh_overlay_appends_auto_when_enabled(self):
        bot = {"id": 1, "runtime": "dsh", "config_root": "C:/x", "profile": "desktop"}
        overlay = native_runtime.dsh_overlay(
            bot, auto_review={"adapter_path": "C:/tmp/adapter.mjs"})
        self.assertTrue(any(isinstance(e, dict) and e.get("insert") for e in overlay))
        inserts = [e["insert"] for e in overlay if isinstance(e, dict) and e.get("insert")]
        ids = [entry["id"] for group in inserts for entry in group]
        self.assertIn("auto-review", ids)
        self.assertIn("auto-enable-adapter", ids)

    def test_dsh_overlay_no_auto_by_default(self):
        bot = {"id": 1, "runtime": "dsh", "config_root": "C:/x", "profile": "desktop"}
        overlay = native_runtime.dsh_overlay(bot)
        inserts = [e["insert"] for e in overlay if isinstance(e, dict) and e.get("insert")]
        ids = [entry["id"] for group in inserts for entry in group]
        self.assertNotIn("auto-review", ids)


class PresetEvidenceTests(unittest.TestCase):
    def test_confirmed(self):
        ev = dsh_auto.preset_evidence([
            "[auto-enable] session/created -> preset=auto",
            "[auto-enable] tools/pre-execute preset=auto name=mcp__group_list_members",
            "[auto-enable] tools/pre-execute preset=auto name=mcp__group_send",
        ])
        self.assertTrue(ev["confirmed"])
        self.assertEqual(ev["session_preset"], "auto")
        self.assertEqual(ev["preexec_count"], 2)

    def test_not_confirmed(self):
        ev = dsh_auto.preset_evidence([
            "[auto-enable] session/created -> preset=workspace-write",
            "[auto-enable] tools/pre-execute preset=workspace-write name=x",
        ])
        self.assertFalse(ev["confirmed"])
        self.assertEqual(ev["session_preset"], "workspace-write")

    def test_empty(self):
        ev = dsh_auto.preset_evidence([])
        self.assertFalse(ev["confirmed"])
        self.assertIsNone(ev["session_preset"])
        self.assertEqual(ev["preexec_count"], 0)


class EnabledStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "profiles" / "desktop").mkdir(parents=True)
        self.bot = {"config_root": str(self.root), "profile": "desktop"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_is_off(self):
        self.assertFalse(dsh_auto.read_enabled(self.bot))

    def test_on_roundtrip(self):
        dsh_auto.write_enabled(self.bot, True)
        self.assertTrue(dsh_auto.read_enabled(self.bot))
        self.assertEqual(dsh_auto.side_path(self.bot).read_text().strip(), "on")

    def test_off_removes(self):
        dsh_auto.write_enabled(self.bot, True)
        dsh_auto.write_enabled(self.bot, False)
        self.assertFalse(dsh_auto.read_enabled(self.bot))
        self.assertFalse(dsh_auto.side_path(self.bot).exists())

    def test_garbage_content_is_off(self):
        dsh_auto.side_path(self.bot).write_text("something-else")
        self.assertFalse(dsh_auto.read_enabled(self.bot))


class PluginStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bot = {"config_root": str(self.root), "profile": "desktop"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_inherit_bundle_ok(self):
        self.assertEqual(dsh_auto.plugin_state(self.bot, [])["state"], "ok")

    def test_disabled(self):
        state = dsh_auto.plugin_state(self.bot, [{"id": "auto-review", "disabled": True}])
        self.assertEqual(state["state"], "disabled")

    def test_custom_name_not_installed(self):
        state = dsh_auto.plugin_state(
            self.bot, [{"id": "auto-review", "name": "@scope/not-installed"}])
        self.assertEqual(state["state"], "load_failed")

    def test_custom_name_installed(self):
        pkg = self.root / "profiles" / "desktop" / "node_modules" / "@scope" / "installed"
        pkg.mkdir(parents=True)
        state = dsh_auto.plugin_state(
            self.bot, [{"id": "auto-review", "name": "@scope/installed"}])
        self.assertEqual(state["state"], "ok")


class AuditTests(unittest.TestCase):
    """审计落库：审查尝试 vs 工具主体执行独立计数 + 事务边界。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["GROUP_DB"] = str(Path(self.tmp.name) / "group.db")
        conn = db.get_connection()
        db.init_schema(conn)
        conn.close()

    def tearDown(self):
        os.environ.pop("GROUP_DB", None)
        self.tmp.cleanup()

    def test_audit_counts(self):
        conn = db.get_connection()
        try:
            rid = dsh_auto.audit(conn, session="s1", wake_started_at=123.0, enabled=True,
                                 evidence={"preexec_count": 3, "confirmed": True},
                                 tool_calls=3, completed_results=2, ts=db.now_iso())
            conn.commit()
            row = conn.execute("SELECT * FROM dsh_auto_audit WHERE id=?", (rid,)).fetchone()
            self.assertEqual(row["enabled"], 1)
            self.assertEqual(row["preset_confirmed"], 1)
            self.assertEqual(row["review_count"], 3)
            self.assertEqual(row["executed_count"], 2)
            self.assertEqual(row["blocked_count"], 1)
        finally:
            conn.close()

    def test_blocked_never_negative(self):
        conn = db.get_connection()
        try:
            rid = dsh_auto.audit(conn, session="s2", wake_started_at=124.0, enabled=True,
                                 evidence={"preexec_count": 0, "confirmed": False},
                                 tool_calls=0, completed_results=5, ts=db.now_iso())
            conn.commit()
            row = conn.execute("SELECT * FROM dsh_auto_audit WHERE id=?", (rid,)).fetchone()
            self.assertEqual(row["blocked_count"], 0)
            self.assertEqual(row["preset_confirmed"], 0)
        finally:
            conn.close()


class ClassificationTests(unittest.TestCase):
    def test_auto_unavailable_category(self):
        self.assertEqual(
            execution_errors.classify(dsh_auto.AUDIT_FAIL_TEXT), "auto_unavailable")
        self.assertIn("auto_unavailable", execution_errors.MESSAGES)

    def test_auto_fail_not_swallowed_by_other_categories(self):
        # fail-closed 文本不得落入 init/incomplete/unknown
        text = dsh_auto.AUDIT_FAIL_TEXT + "; dsh run ended without a final event"
        self.assertEqual(execution_errors.classify(text), "auto_unavailable")


class ProfileConfigAutoTests(unittest.TestCase):
    """dsh_profile_config 的 auto_review 字段读写与保存校验。"""

    def setUp(self):
        from engine import dsh_profile_config
        self.dsh_profile_config = dsh_profile_config
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "profiles" / "desktop").mkdir(parents=True)
        self.patch = self.root / "profiles" / "desktop" / "cordis.patch.yml"
        self.patch.write_text(
            "- id: agent-default-model\n"
            "  config:\n    provider: deepseek-account\n    model: deepseek-flash\n",
            encoding="utf-8")
        (self.root / ".env").write_text("DEEPSEEK_API_KEY=x\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
        os.environ["GROUP_DB"] = str(self.root / "group.db")
        self.bot = {"id": 1, "runtime": "dsh",
                    "config_root": str(self.root), "profile": "desktop"}

    def tearDown(self):
        os.environ.pop("GROUP_DB", None)
        self.tmp.cleanup()

    def _read(self):
        return self.dsh_profile_config.read(self.bot)

    def _save(self, overrides):
        current = self._read()
        values = {k: current[k] for k in ("revision", "description", "persona",
                                          "instructions", "model", "provider",
                                          "reasoning_effort", "env", "auto_review")}
        values.update(overrides)
        return self.dsh_profile_config.save(self.bot, values)

    def test_default_off(self):
        self.assertEqual(self._read()["auto_review"], "off")
        self.assertEqual(self._read()["plugins"]["auto_review"]["plugin"], "auto-review")

    def test_enable_roundtrip(self):
        self._save({"auto_review": "on"})
        self.assertEqual(self._read()["auto_review"], "on")
        self.assertTrue(dsh_auto.read_enabled(self.bot))

    def test_disable_roundtrip(self):
        self._save({"auto_review": "on"})
        self._save({"auto_review": "off"})
        self.assertEqual(self._read()["auto_review"], "off")
        self.assertFalse(dsh_auto.read_enabled(self.bot))

    def test_enable_rejected_when_plugin_disabled(self):
        self.patch.write_text(
            "- id: auto-review\n  disabled: true\n"
            "- id: agent-default-model\n"
            "  config:\n    provider: deepseek-account\n    model: deepseek-flash\n",
            encoding="utf-8")
        with self.assertRaises(Exception) as ctx:
            self._save({"auto_review": "on"})
        self.assertIn("无法启用 Auto 审批", str(ctx.exception))
        self.assertEqual(self._read()["auto_review"], "off")

    def test_enable_rejected_when_custom_plugin_missing(self):
        self.patch.write_text(
            "- id: auto-review\n  name: '@deepseek-ai/custom-missing'\n"
            "- id: agent-default-model\n"
            "  config:\n    provider: deepseek-account\n    model: deepseek-flash\n",
            encoding="utf-8")
        with self.assertRaises(Exception) as ctx:
            self._save({"auto_review": "on"})
        self.assertIn("无法启用 Auto 审批", str(ctx.exception))

    def test_invalid_value_rejected(self):
        with self.assertRaises(Exception) as ctx:
            self._save({"auto_review": "maybe"})
        self.assertIn("只接受 on/off", str(ctx.exception))

    def test_other_fields_save_alongside_auto(self):
        self._save({"auto_review": "on", "model": "deepseek-reasoner"})
        view = self._read()
        self.assertEqual(view["auto_review"], "on")
        self.assertEqual(view["model"], "deepseek-reasoner")


if __name__ == "__main__":
    unittest.main()
