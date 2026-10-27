"""gw_probe.py 隔离门禁单元测试（不启动真实网关）。

覆盖审查节点 1321 指出的三条 false-green：
  - check_mcp_empty：info / mcp_servers 缺失、None、类型异常必须判失败（不得当空）
  - config_mcp_issue：mcp_servers 必须显式 `{}`；缺失/null/[]/[fake]/块条目一律拒绝
  - heartbeat_gate：证据不可读或增量非 0 必须非零退出码

依赖探针运行时（GW_PROBE_ROOT 下的 HERMES_HOME/installs）。环境未安装时整个模块跳过。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("GW_PROBE_ROOT", str(ROOT / ".probe-worktrees"))
sys.path.insert(0, str(ROOT / "scripts"))

try:
    import gw_probe
except AssertionError as exc:  # 探针运行时未安装（非本机环境）
    raise unittest.SkipTest(f"probe runtime not available: {exc}")


class CheckMcpEmptyTest(unittest.TestCase):
    """严格空 MCP 断言：只有「显式 list 且为空」才通过。"""

    def test_explicit_empty_list_passes(self):
        self.assertEqual(gw_probe.check_mcp_empty({"info": {"mcp_servers": []}}, "create"), "")

    def test_missing_info_rejected(self):
        self.assertIn("info", gw_probe.check_mcp_empty({}, "create"))

    def test_info_not_dict_rejected(self):
        for value in (None, [], "x", 0, True):
            with self.subTest(value=value):
                self.assertIn("not a dict",
                              gw_probe.check_mcp_empty({"info": value}, "create"))

    def test_missing_mcp_servers_key_rejected(self):
        self.assertIn("mcp_servers", gw_probe.check_mcp_empty({"info": {}}, "create"))

    def test_none_value_rejected(self):
        self.assertIn("not a list",
                      gw_probe.check_mcp_empty({"info": {"mcp_servers": None}}, "create"))

    def test_mapping_value_rejected(self):
        self.assertIn("not a list",
                      gw_probe.check_mcp_empty({"info": {"mcp_servers": {}}}, "create"))

    def test_string_value_rejected(self):
        self.assertIn("not a list",
                      gw_probe.check_mcp_empty({"info": {"mcp_servers": ""}}, "create"))

    def test_non_empty_list_rejected(self):
        issue = gw_probe.check_mcp_empty(
            {"info": {"mcp_servers": [{"name": "task_queue", "tools": 34}]}}, "create")
        self.assertIn("not empty", issue)

    def test_snapshot_not_dict_rejected(self):
        for value in (None, [], "x"):
            with self.subTest(value=value):
                self.assertIn("not a dict", gw_probe.check_mcp_empty(value, "create"))

    def test_stage_appears_in_message(self):
        self.assertIn("resume", gw_probe.check_mcp_empty({"info": {}}, "resume"))


class ConfigMcpIssueTest(unittest.TestCase):
    """配置门禁：必须显式写 mcp_servers: {}，其他形态全部拒绝。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._orig_home = gw_probe.HERMES_HOME
        gw_probe.HERMES_HOME = self.home

    def tearDown(self):
        gw_probe.HERMES_HOME = self._orig_home
        self.tmp.cleanup()

    def _write(self, body: str) -> None:
        (self.home / "config.yaml").write_text(body, encoding="utf-8")

    def test_missing_file_rejected(self):
        self.assertIn("missing", gw_probe.config_mcp_issue())

    def test_explicit_empty_map_passes(self):
        self._write('model:\n  default: "x"\nmcp_servers: {}\n')
        self.assertEqual(gw_probe.config_mcp_issue(), "")

    def test_empty_map_with_spaces_and_comment_passes(self):
        self._write("mcp_servers: { }  # 显式关闭\n")
        self.assertEqual(gw_probe.config_mcp_issue(), "")

    def test_quoted_key_passes(self):
        # YAML 语义：`"mcp_servers": {}` 与裸键等价，逐行匹配会误杀
        self._write('"mcp_servers": {}\n')
        self.assertEqual(gw_probe.config_mcp_issue(), "")

    def test_missing_key_rejected(self):
        self._write('model:\n  default: "x"\n')
        self.assertIn("key missing", gw_probe.config_mcp_issue())

    def test_null_rejected(self):
        self._write("mcp_servers: null\n")
        self.assertIn("empty mapping", gw_probe.config_mcp_issue())

    def test_empty_list_rejected(self):
        self._write("mcp_servers: []\n")
        self.assertIn("empty mapping", gw_probe.config_mcp_issue())

    def test_inline_list_rejected(self):
        self._write("mcp_servers: [fake]\n")
        self.assertIn("empty mapping", gw_probe.config_mcp_issue())

    def test_block_entry_rejected(self):
        self._write("mcp_servers:\n  fake:\n    command: python\n")
        self.assertIn("must be empty", gw_probe.config_mcp_issue())

    def test_empty_value_rejected(self):
        self._write("mcp_servers:\n")
        self.assertIn("empty mapping", gw_probe.config_mcp_issue())

    def test_duplicate_key_bypass_rejected(self):
        # node 1326 实测绕过：第一条 {} + 第二条非空，逐行扫描只看第一条
        self._write("mcp_servers: {}\nmcp_servers:\n  fake:\n    command: python\n")
        self.assertIn("duplicate key", gw_probe.config_mcp_issue())

    def test_duplicate_key_both_empty_rejected(self):
        self._write("mcp_servers: {}\nmcp_servers: {}\n")
        self.assertIn("duplicate key", gw_probe.config_mcp_issue())

    def test_toplevel_not_mapping_rejected(self):
        self._write("- mcp_servers\n")
        self.assertIn("not a mapping", gw_probe.config_mcp_issue())


class HeartbeatGateTest(unittest.TestCase):
    """心跳门禁：空 home 按 0 基线放行；存在但读不了/不一致才非零。"""

    @staticmethod
    def snap(count: int, exists: bool = True, readable: bool = True) -> dict:
        return {"exists": exists, "readable": readable, "heartbeat_count": count}

    def test_delta_zero_passes(self):
        self.assertEqual(gw_probe.heartbeat_gate(0, self.snap(33), self.snap(33)),
                         (0, "pass", 0))

    def test_empty_home_no_mcp_log_passes(self):
        # 空 home（清理后首次运行）：没有 MCP 日志就是没有 MCP，按 0 基线放行
        self.assertEqual(gw_probe.heartbeat_gate(0, self.snap(0, exists=False, readable=False),
                                                 self.snap(0, exists=False, readable=False)),
                         (0, "no-mcp-log", 0))

    def test_delta_positive_fails(self):
        self.assertEqual(gw_probe.heartbeat_gate(0, self.snap(33), self.snap(34)),
                         (8, "violation", 1))

    def test_unreadable_before_fails(self):
        self.assertEqual(
            gw_probe.heartbeat_gate(0, self.snap(0, readable=False), self.snap(0)),
            (8, "unknown", None))

    def test_unreadable_after_fails(self):
        self.assertEqual(
            gw_probe.heartbeat_gate(0, self.snap(33), self.snap(0, readable=False)),
            (8, "unknown", None))

    def test_one_side_missing_is_unknown(self):
        self.assertEqual(
            gw_probe.heartbeat_gate(0, self.snap(33), self.snap(0, exists=False, readable=False)),
            (8, "unknown", None))

    def test_prior_failure_code_preserved(self):
        self.assertEqual(gw_probe.heartbeat_gate(4, self.snap(33), self.snap(34)),
                         (4, "violation", 1))


class StopVerdictTest(unittest.TestCase):
    """gateway stop 判定：rc=0 或明确「无网关运行」都算干净起点（干净环境必须能启动）。"""

    def test_rc_zero_is_clean(self):
        self.assertEqual(gw_probe.stop_verdict(0, "✓ Stopped gateway for this profile"),
                         (True, "rc0"))

    def test_no_gateway_message_is_clean(self):
        for text in ("No gateway running", "gateway not running", "no running gateway"):
            with self.subTest(text=text):
                self.assertEqual(gw_probe.stop_verdict(1, text), (True, "no-gateway"))

    def test_unknown_nonzero_fails(self):
        self.assertEqual(gw_probe.stop_verdict(1, ""), (False, "failed"))
        self.assertEqual(gw_probe.stop_verdict(2, "Traceback (most recent call last)"),
                         (False, "failed"))

    def test_timeout_none_fails(self):
        self.assertEqual(gw_probe.stop_verdict(None, "gateway stop timeout(180s)"),
                         (False, "failed"))


class McpLogSnapshotTest(unittest.TestCase):
    """心跳证据可读性：缺失/不可读必须 readable=False，不得按 0 次计。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._orig_home = gw_probe.HERMES_HOME
        gw_probe.HERMES_HOME = self.home

    def tearDown(self):
        gw_probe.HERMES_HOME = self._orig_home
        self.tmp.cleanup()

    def test_missing_log_unreadable(self):
        snap = gw_probe.mcp_log_snapshot()
        self.assertFalse(snap["exists"])
        self.assertFalse(snap["readable"])

    def test_counts_heartbeat_lines(self):
        (self.home / "logs").mkdir()
        (self.home / "logs" / "mcp-stderr.log").write_text(
            'x POST http://127.0.0.1:8180/heartbeat "HTTP/1.1 200 OK"\n'
            'y POST http://127.0.0.1:8180/heartbeat "HTTP/1.1 200 OK"\n'
            'z other line\n', encoding="utf-8")
        snap = gw_probe.mcp_log_snapshot()
        self.assertTrue(snap["readable"])
        self.assertEqual(snap["heartbeat_count"], 2)

    def test_directory_instead_of_file_unreadable(self):
        (self.home / "logs" / "mcp-stderr.log").mkdir(parents=True)
        snap = gw_probe.mcp_log_snapshot()
        self.assertTrue(snap["exists"])
        self.assertFalse(snap["readable"])


class AlignmentTest(unittest.TestCase):
    """实例归属三方对齐：解析 gateway.pid start_time 与 Win32 CreationDate 并逐项判定。"""

    HOME = Path(r"E:\Code\.bot-group-resources\dcc5f7fd43510c8f"
                r"\ws_6043478a1e82665dc05e9604\test-data\task-250\gw-probe-home")

    def _pid_meta(self, **over):
        meta = {
            "pid": 2604,
            "start_time": 179310864340,
            "hermes_home": str(self.HOME),
            "argv": [r"E:\Code\bot-group\.probe-worktrees\hermes-v0225\hermes_cli\main.py",
                     "gateway", "run", "--quiet", "--idle-exit"],
        }
        meta.update(over)
        return meta

    @staticmethod
    def _win(created: str = "20261027214403.396228+480", cmd: str = "-m hermes_cli.main") -> str:
        return (f"CommandLine=\"{cmd}\\Python314\\python.exe\" {cmd} gateway run\r\n"
                f"CreationDate={created}\r\n")

    def test_pid_start_time_millis_epoch(self):
        self.assertEqual(gw_probe._parse_pid_start_time(179310864340).isoformat(),
                         "2026-10-27T13:44:03.400000+00:00")

    def test_pid_start_time_seconds_and_filetime(self):
        self.assertEqual(gw_probe._parse_pid_start_time(1793108643).isoformat(),
                         "2026-10-27T13:44:03+00:00")
        # FILETIME(100ns since 1601) 解出的年份应远早于 epoch 解释
        self.assertIsNotNone(gw_probe._parse_pid_start_time(133800000000000000))

    def test_win32_creation_parsed_with_offset(self):
        dt = gw_probe._parse_win32_creation("20261027214403.396228+480")
        self.assertEqual(dt.isoformat(), "2026-10-27T21:44:03.396228+08:00")

    def test_win32_creation_invalid(self):
        for value in ("", "2026-10-27 21:44:03", "20261027214403"):
            with self.subTest(value=value):
                self.assertIsNone(gw_probe._parse_win32_creation(value))

    def test_aligned(self):
        rec = gw_probe.alignment_verdict(self._pid_meta(), self._win(), self.HOME)
        self.assertEqual(rec["verdict"], "aligned")
        self.assertLessEqual(rec["start_time_delta_s"], 5.0)
        self.assertEqual(rec["argv_entry"], "main.py")
        self.assertEqual(rec["win32_entry"], "hermes_cli.main")
        self.assertTrue(rec["checks"]["argv_entry_equivalent"])

    def test_home_mismatch_is_mismatched(self):
        rec = gw_probe.alignment_verdict(
            self._pid_meta(hermes_home=r"E:\Code\bot-group\.probe-worktrees\gw-probe-home"),
            self._win(), self.HOME)
        self.assertEqual(rec["verdict"], "mismatched")
        self.assertFalse(rec["checks"]["hermes_home_matches"])

    def test_no_win32_output_is_mismatched(self):
        # 查不到进程信息就无法证明归属，不能算 aligned
        rec = gw_probe.alignment_verdict(self._pid_meta(), "", self.HOME)
        self.assertEqual(rec["verdict"], "mismatched")
        self.assertIsNone(rec["start_time_delta_s"])

    def test_missing_argv_is_partial(self):
        rec = gw_probe.alignment_verdict(self._pid_meta(argv=None), self._win(), self.HOME)
        self.assertEqual(rec["verdict"], "partial")
        self.assertIsNone(rec["checks"]["argv_entry_equivalent"])


class ForensicsWiringTest(unittest.TestCase):
    """instance_forensics 必须把 start_time 送进对齐判定（漏字段会让对齐永远 false）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._orig = (gw_probe.HERMES_HOME, gw_probe.subprocess.run)
        gw_probe.HERMES_HOME = self.home

    def tearDown(self):
        gw_probe.HERMES_HOME, gw_probe.subprocess.run = self._orig
        self.tmp.cleanup()

    def test_pid_file_start_time_feeds_alignment(self):
        meta = {"pid": 4242, "kind": "hermes-gateway", "start_time": 179310967322,
                "hermes_home": str(self.home),
                "argv": [str(self.home / "hermes_cli" / "main.py"), "gateway", "run"]}
        (self.home / "gateway.pid").write_text(json.dumps(meta), encoding="utf-8")

        class _R:
            returncode = 0
            stdout = ("CommandLine=python.exe -m hermes_cli.main gateway run\r\n"
                      "CreationDate=20261027220113.218528+480\r\n")
            stderr = ""

        gw_probe.subprocess.run = lambda *a, **k: _R()
        rec = gw_probe.instance_forensics({"instance_id": "x", "api_origin": "127.0.0.1:1"})
        self.assertIn("start_time", rec["pid_file"])
        self.assertIsNotNone(rec["alignment"]["pid_start_time_parsed"])
        self.assertEqual(rec["alignment"]["verdict"], "aligned")


class AlignmentGateTest(unittest.TestCase):
    """归属不一致必须是失败门禁（rc=10），不能只打日志继续提交。"""

    def test_mismatched_raises(self):
        with self.assertRaises(gw_probe.InstanceMismatch):
            gw_probe.assert_alignment_or_raise({"alignment": {"verdict": "mismatched"}})

    def test_aligned_and_partial_pass(self):
        for verdict in ("aligned", "partial"):
            with self.subTest(verdict=verdict):
                gw_probe.assert_alignment_or_raise({"alignment": {"verdict": verdict}})

    def test_missing_alignment_does_not_raise(self):
        gw_probe.assert_alignment_or_raise({})


class EvidenceMetaTest(unittest.TestCase):
    """每条证据必须能追溯到脚本 hash 与提交（reviewer 曾因无 commit/hash 打回）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = (gw_probe.EVIDENCE_DIR, gw_probe._EVIDENCE_META, gw_probe._evidence_seq)
        gw_probe.EVIDENCE_DIR = Path(self.tmp.name)
        gw_probe._EVIDENCE_META = None
        gw_probe._evidence_seq = 0

    def tearDown(self):
        gw_probe.EVIDENCE_DIR, gw_probe._EVIDENCE_META, gw_probe._evidence_seq = self._orig
        self.tmp.cleanup()

    def test_meta_has_commit_and_script_hash(self):
        meta = gw_probe.evidence_meta()
        self.assertEqual(len(meta["script_sha256_16"]), 16)
        self.assertIsNotNone(meta["commit"])
        self.assertIn("hermes_home", meta)

    def test_saved_evidence_wraps_meta(self):
        p = gw_probe.save_evidence("demo", {"k": 1})
        doc = json.loads(p.read_text(encoding="utf-8"))
        self.assertIn("meta", doc)
        self.assertEqual(doc["data"], {"k": 1})
        self.assertEqual(len(doc["meta"]["script_sha256_16"]), 16)


class StopInstanceTest(unittest.TestCase):
    """stop-after：pid 存活 / gateway.pid / 端口 LISTENING 三方全干净才算释放（未知不算）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._orig = (gw_probe.HERMES_HOME, gw_probe.stop_existing_gateway,
                      gw_probe._pid_alive, gw_probe._port_listening)
        gw_probe.HERMES_HOME = self.home
        gw_probe.stop_existing_gateway = lambda: {"rc": 0, "stopped": True}

    def tearDown(self):
        (gw_probe.HERMES_HOME, gw_probe.stop_existing_gateway,
         gw_probe._pid_alive, gw_probe._port_listening) = self._orig
        self.tmp.cleanup()

    def _stub(self, alive, listening):
        gw_probe._pid_alive = lambda pid: alive
        gw_probe._port_listening = lambda port: listening

    def test_fully_released_is_clean(self):
        self._stub(False, False)
        rec = gw_probe.stop_instance({"api_origin": "127.0.0.1:58123"}, 4242)
        self.assertTrue(rec["clean"])
        self.assertEqual(rec["port"], 58123)

    def test_pid_still_alive_not_clean(self):
        self._stub(True, False)
        self.assertFalse(
            gw_probe.stop_instance({"api_origin": "127.0.0.1:58123"}, 4242)["clean"])

    def test_port_still_listening_not_clean(self):
        self._stub(False, True)
        self.assertFalse(
            gw_probe.stop_instance({"api_origin": "127.0.0.1:58123"}, 4242)["clean"])

    def test_unknown_query_state_not_clean(self):
        # tasklist/netstat 查询失败返回 None：未知，不能当作已释放
        self._stub(None, None)
        self.assertFalse(
            gw_probe.stop_instance({"api_origin": "127.0.0.1:58123"}, 4242)["clean"])

    def test_gateway_pid_file_left_not_clean(self):
        self._stub(False, False)
        (self.home / "gateway.pid").write_text("4242", encoding="utf-8")
        rec = gw_probe.stop_instance({"api_origin": "127.0.0.1:58123"}, 4242)
        self.assertFalse(rec["clean"])
        self.assertEqual(rec["verdict"], "failed")

    def test_ensure_failure_path_is_clean(self):
        # ensure 失败时 info 空、pid/端口未知：gateway.pid 不存在即为已释放，
        # 不能因为「查不到 pid」就把清理判成失败（node 1330 的语义要求）。
        self._stub(None, None)
        rec = gw_probe.stop_instance({}, None)
        self.assertEqual(rec["verdict"], "clean")
        self.assertTrue(rec["clean"])

    def test_unknown_state_verdict_is_unknown(self):
        self._stub(None, False)
        self.assertEqual(
            gw_probe.stop_instance({"api_origin": "127.0.0.1:58123"}, 4242)["verdict"],
            "unknown")


if __name__ == "__main__":
    unittest.main(verbosity=2)
