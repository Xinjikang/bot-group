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
from datetime import datetime, timedelta, timezone
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

    def test_stale_pid_file_does_not_fail_cleanup(self):
        # 实测 22:15：网关已 stopped，gateway.pid 残留指向已死进程 -> 不能判 failed
        self._stub(False, False)
        (self.home / "gateway.pid").write_text(json.dumps({"pid": 13516}), encoding="utf-8")
        rec = gw_probe.stop_instance({"api_origin": "127.0.0.1:58123"}, 4242)
        self.assertTrue(rec["pid_file_stale"])
        self.assertEqual(rec["verdict"], "clean")

    def test_pid_file_with_live_process_fails(self):
        self._stub(True, False)  # 任意 pid 查询都返回存活
        (self.home / "gateway.pid").write_text(json.dumps({"pid": 13516}), encoding="utf-8")
        rec = gw_probe.stop_instance({"api_origin": "127.0.0.1:58123"}, 4242)
        self.assertFalse(rec["pid_file_stale"])
        self.assertEqual(rec["verdict"], "failed")

class InstallRecordTest(unittest.TestCase):
    """目标 install 由安装记录确定：无记录/多 install/记录越界都要明确失败，不猜。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._orig = gw_probe.HERMES_HOME
        gw_probe.HERMES_HOME = self.home
        os.environ.pop("GW_PROBE_INSTALL", None)

    def tearDown(self):
        gw_probe.HERMES_HOME = self._orig
        os.environ.pop("GW_PROBE_INSTALL", None)
        self.tmp.cleanup()

    def _make_install(self, install_id="aaaa1111", env_id="bbbb2222"):
        install = self.home / "installs" / install_id
        venv = install / "environments" / env_id / "venv"
        (venv / "Scripts").mkdir(parents=True, exist_ok=True)
        py = venv / "Scripts" / "python.exe"
        py.write_text("stub", encoding="utf-8")
        facts = {"packages": {"venv": {"environment": str(venv), "stamp": "deadbeef"}}}
        (install / "facts.json").write_text(json.dumps(facts), encoding="utf-8")
        return install, venv, py

    def test_missing_installs_dir(self):
        record, meta = gw_probe.install_record()
        self.assertIsNone(record)
        self.assertIn("installs dir missing", meta["reason"])

    def test_missing_facts_json(self):
        (self.home / "installs" / "aaaa1111").mkdir(parents=True)
        record, meta = gw_probe.install_record()
        self.assertIsNone(record)
        self.assertIn("facts.json unusable", meta["reason"])

    def test_single_install_resolved_from_facts(self):
        install, venv, py = self._make_install()
        record, meta = gw_probe.install_record()
        self.assertIsNotNone(record)
        self.assertEqual(meta["install_id"], install.name)
        self.assertEqual(meta["env_id"], venv.parent.name)
        self.assertEqual(Path(meta["python"]), py)
        self.assertIn("single install", meta["install_id_source"])

    def test_ambiguous_installs_require_explicit_selection(self):
        self._make_install("aaaa1111")
        self._make_install("cccc3333")
        record, meta = gw_probe.install_record()
        self.assertIsNone(record)
        self.assertIn("ambiguous", meta["reason"])
        os.environ["GW_PROBE_INSTALL"] = "cccc3333"
        record2, meta2 = gw_probe.install_record()
        self.assertIsNotNone(record2)
        self.assertEqual(meta2["install_id"], "cccc3333")
        self.assertEqual(meta2["install_id_source"], "GW_PROBE_INSTALL")

    def test_explicit_install_not_found(self):
        self._make_install("aaaa1111")
        os.environ["GW_PROBE_INSTALL"] = "nope"
        record, meta = gw_probe.install_record()
        self.assertIsNone(record)
        self.assertIn("not found", meta["reason"])

    def test_facts_venv_outside_install_rejected(self):
        install, _, _ = self._make_install()
        facts = {"packages": {"venv": {"environment": str(self.home / "elsewhere" / "venv")}}}
        (install / "facts.json").write_text(json.dumps(facts), encoding="utf-8")
        record, meta = gw_probe.install_record()
        self.assertIsNone(record)
        self.assertIn("outside the selected install", meta["reason"])

    def test_missing_venv_environment_field(self):
        install, _, _ = self._make_install()
        (install / "facts.json").write_text(json.dumps({"packages": {"venv": {}}}),
                                            encoding="utf-8")
        record, meta = gw_probe.install_record()
        self.assertIsNone(record)
        self.assertIn("no packages.venv.environment", meta["reason"])


class ProbePythonSelftestTest(unittest.TestCase):
    """自检两条：--version 的 Install directory + local 提交；能 import ruamel 读版本。"""

    SRC = Path(r"E:\Code\bot-group\.probe-worktrees\hermes-v0225")
    HEAD = "bf0203a794a07aac3ff764972ab9087baa4e4f4e"
    VERSION_OK = ("Hermes Agent v0.22.5 (2026.9.24) · stable · local bf0203a794a0\n"
                  "Install directory: E:\\Code\\bot-group\\.probe-worktrees\\hermes-v0225\n"
                  "Install method: git\nPython: 3.14.9")

    def _stub_runs(self, version_out, version_rc=0, ruamel_out="0.18.16", ruamel_rc=0):
        class R:
            def __init__(self, rc, out):
                self.returncode, self.stdout, self.stderr = rc, out, ""

        seq = [R(version_rc, version_out), R(ruamel_rc, ruamel_out)]
        orig = gw_probe.subprocess.run
        gw_probe.subprocess.run = lambda *a, **k: seq.pop(0)
        self.addCleanup(lambda: setattr(gw_probe.subprocess, "run", orig))

    def test_parses_version_install_dir_commit_and_ruamel(self):
        self._stub_runs(self.VERSION_OK)
        rec = gw_probe._selftest_probe_python(Path("x/python.exe"), self.SRC, commit=self.HEAD)
        self.assertEqual(rec["hermes_version"], "0.22.5")
        self.assertEqual(rec["hermes_commit"], "bf0203a794a0")
        self.assertTrue(rec["install_directory_matches_worktree"])
        self.assertTrue(rec["commit_matches_worktree"])
        self.assertEqual(rec["ruamel_version"], "0.18.16")
        self.assertTrue(rec["ok"])

    def test_wrong_install_directory_fails(self):
        out = self.VERSION_OK.replace(
            r"E:\Code\bot-group\.probe-worktrees\hermes-v0225",
            r"E:\home\admin\hermes\hermes-agent")
        self._stub_runs(out)
        rec = gw_probe._selftest_probe_python(Path("x/python.exe"), self.SRC, commit=self.HEAD)
        self.assertFalse(rec["install_directory_matches_worktree"])
        self.assertFalse(rec["ok"])

    def test_wrong_commit_fails(self):
        self._stub_runs(self.VERSION_OK)
        rec = gw_probe._selftest_probe_python(Path("x/python.exe"), self.SRC, commit="0" * 40)
        self.assertFalse(rec["commit_matches_worktree"])
        self.assertFalse(rec["ok"])

    def test_missing_ruamel_fails(self):
        self._stub_runs(self.VERSION_OK, ruamel_out="ModuleNotFoundError: No module named 'ruamel'",
                        ruamel_rc=1)
        rec = gw_probe._selftest_probe_python(Path("x/python.exe"), self.SRC, commit=self.HEAD)
        self.assertIsNone(rec["ruamel_version"])
        self.assertFalse(rec["ok"])

    def test_version_command_failure_fails(self):
        self._stub_runs("hermes: Could not determine home directory.", version_rc=1)
        rec = gw_probe._selftest_probe_python(Path("x/python.exe"), self.SRC, commit=self.HEAD)
        self.assertFalse(rec["ok"])


class StopOwnershipTest(unittest.TestCase):
    """停前归属校验：三方（pid 文件 / Win32 / 日志）实际判定，字段缺失判 unknown 不停止。"""

    CST = timezone(timedelta(hours=8))
    BASE = datetime(2026, 10, 27, 22, 18, 6, tzinfo=CST)
    START_CS = int(BASE.timestamp() * 100)              # Windows 上 gateway.pid 的厘秒
    WIN32 = ("CommandLine=\"C:\\...\\python.exe\" -m hermes_cli.main gateway run --quiet\r\n"
             "\r\nCreationDate=20261027221806.000000+480\r\n\r\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._orig = (gw_probe.HERMES_HOME, gw_probe._pid_alive, gw_probe._win32_process)
        gw_probe.HERMES_HOME = self.home
        (self.home / "logs").mkdir(parents=True, exist_ok=True)
        self.log_line("2026-10-27 22:18:07,000 INFO gateway.run: Auto-started gateway: exits "
                      "after 600s")
        self.log_line("2026-10-27 22:18:09,000 INFO gateway.run: inbound message: platform=local")
        gw_probe._pid_alive = lambda pid: True
        gw_probe._win32_process = lambda pid: {"rc": 0, "output": self.WIN32}

    def tearDown(self):
        (gw_probe.HERMES_HOME, gw_probe._pid_alive, gw_probe._win32_process) = self._orig
        self.tmp.cleanup()

    def log_line(self, text):
        with (self.home / "logs" / "gateway.log").open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")

    def _pid_file(self, **overrides):
        meta = {"pid": 4242, "kind": "hermes-gateway",
                "hermes_home": str(self.home), "start_time": self.START_CS,
                "argv": [r"E:\Code\bot-group\.probe-worktrees\hermes-v0225\hermes_cli\main.py",
                         "gateway", "run", "--quiet"]}
        meta.update(overrides)
        (self.home / "gateway.pid").write_text(json.dumps(meta), encoding="utf-8")
        return meta

    def test_absent_without_pid_file(self):
        rec = gw_probe.stop_ownership()
        self.assertEqual(rec["verdict"], "absent")

    def test_owned_when_three_way_aligned_and_log_covers(self):
        self._pid_file()
        rec = gw_probe.stop_ownership()
        self.assertEqual(rec["verdict"], "owned", rec.get("reason"))
        self.assertTrue(rec["log_covers_instance"])
        self.assertEqual(rec["alignment"]["verdict"], "aligned")

    def test_stale_when_process_exited_and_state_stopped(self):
        self._pid_file()
        (self.home / "gateway_state.json").write_text(
            json.dumps({"gateway_state": "stopped"}), encoding="utf-8")
        gw_probe._pid_alive = lambda pid: False
        rec = gw_probe.stop_ownership()
        self.assertEqual(rec["verdict"], "stale")

    def test_unknown_when_process_exited_but_state_running(self):
        self._pid_file()
        (self.home / "gateway_state.json").write_text(
            json.dumps({"gateway_state": "running"}), encoding="utf-8")
        gw_probe._pid_alive = lambda pid: False
        self.assertEqual(gw_probe.stop_ownership()["verdict"], "unknown")

    def test_unknown_when_liveness_query_failed(self):
        self._pid_file()
        gw_probe._pid_alive = lambda pid: None
        self.assertEqual(gw_probe.stop_ownership()["verdict"], "unknown")

    def test_unknown_when_field_missing(self):
        for field in ("start_time", "argv", "hermes_home"):
            with self.subTest(field=field):
                self._pid_file(**{field: None})
                rec = gw_probe.stop_ownership()
                self.assertEqual(rec["verdict"], "unknown", rec.get("reason"))
                self.assertIn(field, rec["missing_fields"])

    def test_unknown_when_win32_query_empty(self):
        self._pid_file()
        gw_probe._win32_process = lambda pid: {"rc": 1, "output": ""}
        self.assertEqual(gw_probe.stop_ownership()["verdict"], "unknown")

    def test_foreign_when_home_mismatch(self):
        self._pid_file(hermes_home=r"E:\home\admin\hermes\hermes-agent")
        self.assertEqual(gw_probe.stop_ownership()["verdict"], "foreign")

    def test_foreign_when_only_old_startup_exists(self):
        (self.home / "logs" / "gateway.log").write_text(
            "2026-10-27 10:00:00,000 INFO gateway.run: Auto-started gateway: old\n",
            encoding="utf-8")
        self._pid_file()
        rec = gw_probe.stop_ownership()
        self.assertEqual(rec["verdict"], "foreign")
        self.assertIs(rec["log_covers_instance"], False)
        self.assertIn("Auto-started inconsistent", rec["log_reason"])

    def test_owned_when_no_inbound_in_this_instance(self):
        # 上一实例的 inbound 残留在日志里，本实例刚启动尚无 inbound：不得因此判 foreign
        (self.home / "logs" / "gateway.log").write_text(
            "2026-10-27 21:00:00,000 INFO gateway.run: Auto-started gateway: old\n"
            "2026-10-27 21:00:05,000 INFO gateway.run: inbound message: platform=local\n"
            "2026-10-27 22:18:07,000 INFO gateway.run: Auto-started gateway: new\n",
            encoding="utf-8")
        self._pid_file()
        rec = gw_probe.stop_ownership()
        self.assertEqual(rec["verdict"], "owned", rec.get("reason"))
        self.assertIsNone(rec["gateway_log"]["last_inbound"])
        self.assertGreaterEqual(rec["gateway_log"]["events_before_since"], 2)

    def test_shutdown_event_participates_in_window(self):
        # 本实例的停止记录与启动记录同属一个窗口，必须都参与判定
        self.log_line("2026-10-27 22:20:00,000 INFO gateway.run: Gateway stopped "
                      "(total teardown 1.4s)")
        self._pid_file()
        rec = gw_probe.stop_ownership()
        self.assertEqual(rec["verdict"], "owned", rec.get("reason"))
        self.assertIn("last_shutdown", rec["log_start_deltas_s"])
        self.assertGreater(rec["log_start_deltas_s"]["last_shutdown"], 0)

    def test_unknown_when_state_file_missing(self):
        # 进程已退出但状态文件缺失：不得按 stale 放行 stop
        self._pid_file()
        gw_probe._pid_alive = lambda pid: False
        rec = gw_probe.stop_ownership()
        self.assertEqual(rec["verdict"], "unknown")
        self.assertIsNone(rec["gateway_state"])

    def test_owned_when_previous_inbound_within_sixty_seconds(self):
        (self.home / "logs" / "gateway.log").write_text(
            "2026-10-27 22:18:07,000 INFO gateway.run: Auto-started gateway: new\n"
            "2026-10-27 22:17:36,000 INFO gateway.run: inbound message: platform=local\n",
            encoding="utf-8")
        self._pid_file()
        rec = gw_probe.stop_ownership()
        self.assertEqual(rec["verdict"], "owned")
        self.assertIsNone(rec["gateway_log"]["last_inbound"])
        self.assertNotIn("last_inbound", rec["log_start_deltas_s"])

    def test_owned_when_previous_shutdown_within_sixty_seconds(self):
        (self.home / "logs" / "gateway.log").write_text(
            "2026-10-27 22:18:07,000 INFO gateway.run: Auto-started gateway: new\n"
            "2026-10-27 22:17:40,000 INFO gateway.run: Gateway stopped (total teardown 1.1s)\n",
            encoding="utf-8")
        self._pid_file()
        rec = gw_probe.stop_ownership()
        self.assertEqual(rec["verdict"], "owned")
        self.assertIsNone(rec["gateway_log"]["last_shutdown"])

    def test_previous_event_within_clock_tolerance_is_excluded(self):
        (self.home / "logs" / "gateway.log").write_text(
            "2026-10-27 22:18:07,000 INFO gateway.run: Auto-started gateway: new\n"
            "2026-10-27 22:18:03,000 INFO gateway.run: inbound message: platform=local\n",
            encoding="utf-8")
        self._pid_file()
        rec = gw_probe.stop_ownership()
        self.assertEqual(rec["verdict"], "owned", rec.get("reason"))
        self.assertIsNone(rec["gateway_log"]["last_inbound"])

    def test_recent_previous_instance_events_before_startup_are_excluded(self):
        (self.home / "logs" / "gateway.log").write_text(
            "2026-10-27 22:17:30 INFO Auto-started gateway: old\n"
            "2026-10-27 22:17:36 INFO inbound message: old\n"
            "2026-10-27 22:17:40 INFO Gateway stopped\n"
            "2026-10-27 22:18:07 INFO Auto-started gateway: current\n",
            encoding="utf-8")
        self._pid_file()
        rec = gw_probe.stop_ownership()
        self.assertEqual(rec["verdict"], "owned")
        self.assertIsNone(rec["gateway_log"]["last_inbound"])
        self.assertIsNone(rec["gateway_log"]["last_shutdown"])

    def test_startup_offset_uses_absolute_five_second_boundary(self):
        self._pid_file()
        for offset, expected in ((-6, "foreign"), (-5, "owned"), (5, "owned"), (6, "foreign")):
            with self.subTest(offset=offset):
                timestamp = (self.BASE + timedelta(seconds=offset)).strftime("%Y-%m-%d %H:%M:%S")
                (self.home / "logs" / "gateway.log").write_text(
                    f"{timestamp} INFO Auto-started gateway: current\n", encoding="utf-8")
                self.assertEqual(gw_probe.stop_ownership()["verdict"], expected)

    def test_unparsable_events_are_diagnostics(self):
        self.log_line("2026-99-27 22:18:09 INFO inbound message: invalid date")
        self.log_line("bad-timestamp INFO Gateway stopped")
        self._pid_file()
        rec = gw_probe.stop_ownership()
        self.assertEqual(rec["verdict"], "owned")
        self.assertEqual(rec["gateway_log"]["unparsable_events"], 2)

    def test_unknown_when_log_has_no_startup(self):
        (self.home / "logs" / "gateway.log").write_text(
            "2026-10-27 22:18:09 INFO inbound message: current\n", encoding="utf-8")
        self._pid_file()
        self.assertEqual(gw_probe.stop_ownership()["verdict"], "unknown")

    def test_unknown_when_instance_start_time_invalid(self):
        # start_time 无法解析：不得静默退回全局窗口
        self._pid_file()
        orig = gw_probe.log_instance_window
        gw_probe.log_instance_window = lambda since=None: {
            "exists": True, "readable": True, "since_invalid": True, "since": "not-a-date"}
        self.addCleanup(lambda: setattr(gw_probe, "log_instance_window", orig))
        rec = gw_probe.stop_ownership()
        self.assertEqual(rec["verdict"], "unknown")
        self.assertIn("invalid instance start_time", rec["log_reason"])

    def test_unknown_when_log_missing(self):
        (self.home / "logs" / "gateway.log").unlink()
        self._pid_file()
        self.assertEqual(gw_probe.stop_ownership()["verdict"], "unknown")

    def test_unknown_when_pid_file_not_object(self):
        (self.home / "gateway.pid").write_text("12345", encoding="utf-8")
        self.assertEqual(gw_probe.stop_ownership()["verdict"], "unknown")


class LogInstanceWindowTest(unittest.TestCase):
    """按最接近的启动记录分段，提取该实例的 inbound/停止事件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._orig = gw_probe.HERMES_HOME
        gw_probe.HERMES_HOME = self.home
        (self.home / "logs").mkdir(parents=True, exist_ok=True)
        (self.home / "logs" / "gateway.log").write_text(
            "2026-10-27 21:00:00,000 INFO gateway.run: Auto-started gateway: old\n"
            "2026-10-27 21:00:05,000 INFO gateway.run: inbound message: platform=local\n"
            "2026-10-27 21:30:00,000 INFO gateway.run: Gateway stopped (total teardown 1.2s)\n"
            "2026-10-27 22:18:07,000 INFO gateway.run: Auto-started gateway: new\n",
            encoding="utf-8")

    def tearDown(self):
        gw_probe.HERMES_HOME = self._orig
        self.tmp.cleanup()

    def test_filters_previous_instance_events(self):
        rec = gw_probe.log_instance_window("2026-10-27T14:18:06+00:00")
        self.assertEqual(rec["auto_started"], "2026-10-27 22:18:07")
        self.assertIsNone(rec["last_inbound"])
        self.assertIsNone(rec["last_shutdown"])
        self.assertEqual(rec["events_before_since"], 3)

    def test_without_since_returns_global_last(self):
        rec = gw_probe.log_instance_window()
        self.assertEqual(rec["auto_started"], "2026-10-27 22:18:07")
        self.assertEqual(rec["last_inbound"], "2026-10-27 21:00:05")
        self.assertEqual(rec["last_shutdown"], "2026-10-27 21:30:00")
        self.assertNotIn("since", rec)

    def test_invalid_since_returns_flag(self):
        # start_time 非法：显式标记，不静默退回全局窗口
        rec = gw_probe.log_instance_window("not-a-date")
        self.assertTrue(rec["since_invalid"])
        self.assertEqual(rec["since"], "not-a-date")
        self.assertNotIn("auto_started", rec)

    def test_naive_since_returns_flag(self):
        rec = gw_probe.log_instance_window("2026-10-27T14:18:06")
        self.assertTrue(rec["since_invalid"])

    def test_next_startup_ends_selected_window(self):
        with (self.home / "logs" / "gateway.log").open("a", encoding="utf-8") as fh:
            fh.write("2026-10-27 22:18:09 INFO inbound message: current\n"
                     "2026-10-27 22:18:10 INFO Gateway stopped\n"
                     "2026-10-27 22:18:12 INFO Auto-started gateway: next\n"
                     "2026-10-27 22:18:13 INFO inbound message: next\n"
                     "2026-10-27 22:18:14 INFO Gateway stopped\n")
        rec = gw_probe.log_instance_window("2026-10-27T14:18:06+00:00")
        self.assertEqual(rec["auto_started"], "2026-10-27 22:18:07")
        self.assertEqual(rec["last_inbound"], "2026-10-27 22:18:09")
        self.assertEqual(rec["last_shutdown"], "2026-10-27 22:18:10")

    def test_missing_log_reports_absent(self):
        (self.home / "logs" / "gateway.log").unlink()
        self.assertEqual(gw_probe.log_instance_window("2026-10-27T14:18:06+00:00"),
                         {"exists": False})


class StopRefusedTest(unittest.TestCase):
    """归属不是 absent/stale/owned 时 stop 必须拒绝执行，且不调用任何子进程。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._orig = (gw_probe.HERMES_HOME, gw_probe._pid_alive, gw_probe._win32_process,
                      gw_probe.probe_python, gw_probe.subprocess.run)
        gw_probe.HERMES_HOME = self.home
        (self.home / "logs").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        (gw_probe.HERMES_HOME, gw_probe._pid_alive, gw_probe._win32_process,
         gw_probe.probe_python, gw_probe.subprocess.run) = self._orig
        self.tmp.cleanup()

    def test_foreign_instance_refused_without_subprocess(self):
        gw_probe._pid_alive = lambda pid: True
        gw_probe._win32_process = lambda pid: {"rc": 0, "output": StopOwnershipTest.WIN32}
        (self.home / "gateway.pid").write_text(json.dumps({
            "pid": 1, "hermes_home": r"E:\home\admin\hermes\hermes-agent",
            "argv": ["x"], "start_time": StopOwnershipTest.START_CS}), encoding="utf-8")
        calls = []
        gw_probe.subprocess.run = lambda *a, **k: calls.append(a)
        rec = gw_probe.stop_existing_gateway()
        self.assertEqual(rec["verdict"], "refused")
        self.assertEqual(rec["ownership"]["verdict"], "foreign")
        self.assertEqual(calls, [])

    def test_unknown_pid_file_refused_without_subprocess(self):
        (self.home / "gateway.pid").write_text("{broken", encoding="utf-8")
        calls = []
        gw_probe.subprocess.run = lambda *a, **k: calls.append(a)
        rec = gw_probe.stop_existing_gateway()
        self.assertEqual(rec["verdict"], "refused")
        self.assertEqual(rec["ownership"]["verdict"], "unknown")
        self.assertEqual(calls, [])

    def test_absent_pid_file_proceeds_to_stop(self):
        # 无 pid 文件：没有实例可停，允许走 CLI（返回 no-gateway，幂等安全）
        calls = []

        class R:
            returncode, stdout, stderr = 0, "✗ No gateway running", ""

        gw_probe.subprocess.run = lambda *a, **k: (calls.append(a), R())[1]
        gw_probe.probe_python = lambda: Path(sys.executable)
        rec = gw_probe.stop_existing_gateway()
        self.assertEqual(rec["ownership"]["verdict"], "absent")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
