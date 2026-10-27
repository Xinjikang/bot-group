"""gw_probe.py 隔离门禁单元测试（不启动真实网关）。

覆盖审查节点 1321 指出的三条 false-green：
  - check_mcp_empty：info / mcp_servers 缺失、None、类型异常必须判失败（不得当空）
  - config_mcp_issue：mcp_servers 必须显式 `{}`；缺失/null/[]/[fake]/块条目一律拒绝
  - heartbeat_gate：证据不可读或增量非 0 必须非零退出码

依赖探针运行时（GW_PROBE_ROOT 下的 HERMES_HOME/installs）。环境未安装时整个模块跳过。
"""
from __future__ import annotations

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

    def test_missing_key_rejected(self):
        self._write('model:\n  default: "x"\n')
        self.assertIn("key missing", gw_probe.config_mcp_issue())

    def test_null_rejected(self):
        self._write("mcp_servers: null\n")
        self.assertIn("must be exactly", gw_probe.config_mcp_issue())

    def test_empty_list_rejected(self):
        self._write("mcp_servers: []\n")
        self.assertIn("must be exactly", gw_probe.config_mcp_issue())

    def test_inline_list_rejected(self):
        self._write("mcp_servers: [fake]\n")
        self.assertIn("must be exactly", gw_probe.config_mcp_issue())

    def test_block_entry_rejected(self):
        self._write("mcp_servers:\n  fake:\n    command: python\n")
        self.assertIn("must be exactly", gw_probe.config_mcp_issue())

    def test_empty_value_rejected(self):
        self._write("mcp_servers:\n")
        self.assertIn("must be exactly", gw_probe.config_mcp_issue())


class HeartbeatGateTest(unittest.TestCase):
    """心跳门禁：可读 + 增量 0 才通过；未知与违规都要非零。"""

    @staticmethod
    def snap(count: int, readable: bool = True) -> dict:
        return {"readable": readable, "heartbeat_count": count}

    def test_delta_zero_passes(self):
        self.assertEqual(gw_probe.heartbeat_gate(0, self.snap(33), self.snap(33)),
                         (0, "pass", 0))

    def test_delta_positive_fails(self):
        self.assertEqual(gw_probe.heartbeat_gate(0, self.snap(33), self.snap(34)),
                         (8, "violation", 1))

    def test_unreadable_before_fails(self):
        self.assertEqual(gw_probe.heartbeat_gate(0, self.snap(0, False), self.snap(0)),
                         (8, "unknown", None))

    def test_unreadable_after_fails(self):
        self.assertEqual(gw_probe.heartbeat_gate(0, self.snap(33), self.snap(0, False)),
                         (8, "unknown", None))

    def test_prior_failure_code_preserved(self):
        self.assertEqual(gw_probe.heartbeat_gate(4, self.snap(33), self.snap(34)),
                         (4, "violation", 1))


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
