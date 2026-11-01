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
        self.assertIn('ctx.on("tools/execute"', src)  # node1630：主体入口
        self.assertIn('[auto-enable]', src)
        # req#138 F2：current≠auto 时阻断（不 next）；current==auto 时 next() 放行。
        self.assertIn('return next()', src)
        self.assertIn('blocked += 1', src)
        self.assertIn('return;  // fail-closed', src)
        # node1638：原生模块加载（createRequire 解析锚点 + isAgentLoopRequest 联合识别）。
        self.assertIn('createRequire(', src)
        self.assertIn('@deepseek-ai/dsh-llm', src)
        self.assertIn('isAgentLoopRequest(options) === true', src)
        self.assertIn('process.argv[1]', src)

    def test_adapter_blocks_on_non_auto(self):
        # req#138 F2 语义：适配插件在 current≠auto 分支必须阻断（不 next）。
        src = dsh_auto.adapter_source()
        # 非 auto 分支：先打 BLOCK 行，再 state()，再 return（无 next()）。
        self.assertIn('[auto-enable] BLOCK preset=', src)
        self.assertIn('preset !== "auto"', src)
        # next() 只出现在 tools/pre-execute 放行分支 + tools/execute 主体入口分支，
        # 非 auto 阻断分支无 next()。
        self.assertEqual(src.count('return next()'), 2)

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
            "[auto-enable] state preset=auto preexec=0 review=0 blocked=0",
            "[auto-enable] state preset=auto preexec=1 review=1 blocked=0",
            "[auto-enable] state preset=auto preexec=2 review=2 blocked=0",
        ])
        self.assertTrue(ev["confirmed"])
        self.assertEqual(ev["session_preset"], "auto")
        self.assertEqual(ev["preexec_count"], 2)
        self.assertEqual(ev["review_count"], 2)
        self.assertEqual(ev["blocked_count"], 0)

    def test_not_confirmed(self):
        ev = dsh_auto.preset_evidence([
            "[auto-enable] state preset=workspace-write preexec=0 review=0 blocked=0",
            "[auto-enable] state preset=workspace-write preexec=1 review=0 blocked=1",
        ])
        self.assertFalse(ev["confirmed"])
        self.assertEqual(ev["session_preset"], "workspace-write")
        self.assertEqual(ev["blocked_count"], 1)

    def test_empty(self):
        ev = dsh_auto.preset_evidence([])
        self.assertFalse(ev["confirmed"])
        self.assertIsNone(ev["session_preset"])
        self.assertEqual(ev["preexec_count"], 0)
        self.assertEqual(ev["review_count"], 0)
        self.assertEqual(ev["blocked_count"], 0)

    def test_review_count_from_llm_stream(self):
        # req#138 F7：review_count 来自 llm/stream 拦截到的实际审查请求，非 preexec 近似。
        ev = dsh_auto.preset_evidence([
            "[auto-enable] state preset=auto preexec=3 review=2 blocked=0",
        ])
        self.assertEqual(ev["review_count"], 2)
        self.assertEqual(ev["preexec_count"], 3)

    def test_truncated_tail_keeps_cumulative_state(self):
        # req#138 F6：41 次工具日志挤掉 session/created 后，末位累积状态行仍保留完整计数
        # 与 preset 确认（引擎只留 40 行滚动尾部，喂入尾部也能判 confirmed + preexec=41）。
        full = ["[auto-enable] state preset=auto preexec=0 review=0 blocked=0"]
        for i in range(1, 42):
            full.append(f"[auto-enable] state preset=auto preexec={i} review={i} blocked=0")
        tail = full[-40:]  # 模拟滚动尾部：丢掉开头 session/created 行
        ev = dsh_auto.preset_evidence(tail)
        self.assertTrue(ev["confirmed"])
        self.assertEqual(ev["session_preset"], "auto")
        self.assertEqual(ev["preexec_count"], 41)

    def test_state_line_survives_ordinary_tail(self):
        # req#138 F6（manager node1593 探针）：state 行后跟 41 条普通日志，40 行诊断尾部
        # 会把 state 行截掉——必须用读取线程内流式维护的 state_lines，而非截断尾部。
        state_lines = ["[auto-enable] state preset=auto preexec=41 review=41 blocked=0"]
        ordinary_tail = ["ordinary diagnostic line"] * 41
        ev_state = dsh_auto.preset_evidence(state_lines)
        self.assertTrue(ev_state["confirmed"])
        self.assertEqual(ev_state["preexec_count"], 41)
        self.assertEqual(ev_state["review_count"], 41)
        # 误用截断尾部 → 无 state 行 → 不 confirmed（证明必须用流式维护的 state_lines）
        ev_tail = dsh_auto.preset_evidence(ordinary_tail)
        self.assertFalse(ev_tail["confirmed"])
        self.assertEqual(ev_tail["preexec_count"], 0)

    def test_blocked_count_survives_truncation(self):
        # req#138 F7：阻断计数同样来自末位累积状态，尾部截断不丢。
        lines = ["[auto-enable] state preset=auto preexec=0 review=0 blocked=0"] + [
            f"[auto-enable] state preset=auto preexec={i} review=0 blocked=0" for i in range(1, 3)
        ] + ["[auto-enable] state preset=n/a preexec=3 review=0 blocked=1"]
        ev = dsh_auto.preset_evidence(lines[-2:])
        self.assertEqual(ev["blocked_count"], 1)
        self.assertFalse(ev["confirmed"])


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
                                 evidence={"preexec_count": 3, "review_count": 2,
                                           "entry_count": 2, "blocked_count": 1,
                                           "confirmed": True},
                                 tool_calls=3, completed_results=2, ts=db.now_iso())
            conn.commit()
            row = conn.execute("SELECT * FROM dsh_auto_audit WHERE id=?", (rid,)).fetchone()
            self.assertEqual(row["enabled"], 1)
            self.assertEqual(row["preset_confirmed"], 1)
            # req#138 F7：review_count 来自 llm/stream 实际审查请求（2），非 preexec 近似（3）。
            self.assertEqual(row["review_count"], 2)
            # executed_count = 主体入口 entry（真实函数进入处），非 completed_results。
            self.assertEqual(row["executed_count"], 2)
            # req#138 F7：blocked_count 来自适配插件独立计数，非 tool_calls-completed 反推。
            self.assertEqual(row["blocked_count"], 1)
        finally:
            conn.close()

    def test_review_count_falls_back_to_preexec(self):
        # 旧 state 行无 review=（兼容）：review_count 回退 preexec。
        conn = db.get_connection()
        try:
            rid = dsh_auto.audit(conn, session="s4", wake_started_at=126.0, enabled=True,
                                 evidence={"preexec_count": 4, "blocked_count": 0,
                                           "confirmed": True},
                                 tool_calls=4, completed_results=4, ts=db.now_iso())
            conn.commit()
            row = conn.execute("SELECT * FROM dsh_auto_audit WHERE id=?", (rid,)).fetchone()
            self.assertEqual(row["review_count"], 4)
        finally:
            conn.close()

    def test_review_count_zero_not_fallback(self):
        # req#138 node1603：review=0 是合法值（审查异常/跳过实际审查数为 0），不得被
        # `or` 短路成 preexec（0 次审查写成 1 次）。
        conn = db.get_connection()
        try:
            rid = dsh_auto.audit(conn, session="s5", wake_started_at=127.0, enabled=True,
                                 evidence={"preexec_count": 3, "review_count": 0,
                                           "blocked_count": 0, "confirmed": True},
                                 tool_calls=3, completed_results=0, ts=db.now_iso())
            conn.commit()
            row = conn.execute("SELECT * FROM dsh_auto_audit WHERE id=?", (rid,)).fetchone()
            self.assertEqual(row["review_count"], 0)  # 不回退 preexec=3
        finally:
            conn.close()

    def test_entry_count_and_call_ids_from_adapter(self):
        # req#138 node1630：主体入口计数（entry）从 state 行 entry=（tools/execute 主体入口，
        # 真实函数进入处）提取，callId 关联从 [auto-enable] ENTRY/BLOCK 行提取（两者独立）。
        ev = dsh_auto.preset_evidence([
            "[auto-enable] ENTRY name=probe callId=c1",  # tools/execute 主体入口 callId
            "[auto-enable] BLOCK preset=n/a name=x callId=c2",
            "[auto-enable] state preset=auto preexec=2 review=0 entry=1 blocked=1",
        ])
        self.assertEqual(ev["entry_count"], 1)  # 主体入口 1（state entry=，tools/execute）
        self.assertEqual(ev["call_ids"], ["c1", "c2"])

    def test_audit_executed_is_entry_not_completed(self):
        # req#138 node1621：executed_count = 主体入口 entry（真实函数进入处），非 completed
        # （completed 漏「进入主体后失败 entry=1 completed=0」）。entry=3 但 completed=1 时
        # executed 记 entry=3。
        conn = db.get_connection()
        try:
            rid = dsh_auto.audit(conn, session="s6", wake_started_at=128.0, enabled=True,
                                 evidence={"preexec_count": 3, "review_count": 1,
                                           "entry_count": 3, "blocked_count": 0,
                                           "confirmed": True},
                                 tool_calls=3, completed_results=1, ts=db.now_iso())
            conn.commit()
            row = conn.execute("SELECT * FROM dsh_auto_audit WHERE id=?", (rid,)).fetchone()
            self.assertEqual(row["executed_count"], 3)  # entry=3（主体入口），非 completed=1
        finally:
            conn.close()

    def test_review_fixture_exact_policy_prefix_not_bare(self):
        # req#138 node1618：审查夹具识别用 REVIEW_POLICY 原文前缀（「REVIEW_POLICY\nYou are the
        # final authorization reviewer...」），非裸「REVIEW_POLICY」（裸前缀会误拦 REVIEW_POLICY_EXTRA
        # 等「同前缀+temp0 非审查」请求——isAgentLoopRequest 是 dsh-llm 模块级导出、本地 .mjs
        # 裸说明符解析失败，故以原文前缀 + temperature 0 复合识别等价区分）。
        from scripts import verify_dsh_auto_layers as _v
        src = _v.REVIEW_FIXTURE
        self.assertIn('"REVIEW_POLICY" + String.fromCharCode(10) + "You are the final authorization reviewer"',
                      src)
        self.assertNotIn('startsWith("REVIEW_POLICY")', src)  # 不再裸前缀

    def test_blocked_from_evidence_not_derived(self):
        # req#138 F7：blocked 不再由 tool_calls-completed 反推（否则审查拒绝与适配阻断混同）。
        conn = db.get_connection()
        try:
            rid = dsh_auto.audit(conn, session="s3", wake_started_at=125.0, enabled=True,
                                 evidence={"preexec_count": 5, "entry_count": 1,
                                           "blocked_count": 0, "confirmed": True},
                                 tool_calls=5, completed_results=1, ts=db.now_iso())
            conn.commit()
            row = conn.execute("SELECT * FROM dsh_auto_audit WHERE id=?", (rid,)).fetchone()
            # 旧口径 blocked = 5-1 = 4；新口径按适配插件独立计数 = 0（审查拒绝不计 blocked）。
            self.assertEqual(row["blocked_count"], 0)
            self.assertEqual(row["review_count"], 5)
            self.assertEqual(row["executed_count"], 1)  # entry=1（主体入口）
        finally:
            conn.close()

    def test_blocked_never_negative(self):
        conn = db.get_connection()
        try:
            rid = dsh_auto.audit(conn, session="s2", wake_started_at=124.0, enabled=True,
                                 evidence={"preexec_count": 0, "blocked_count": 0,
                                           "confirmed": False},
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
