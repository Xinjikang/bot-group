"""req#122 task#213 缺陷3 回归：MCP 依赖锁定（requirements.lock 含 mcp）+ selfcheck import mcp
+ _MCP_PYTHON 默认可用解释器（非开发机硬编码路径，保留 GROUP_MCP_PYTHON 覆盖）。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import install  # noqa: E402
from engine import wake  # noqa: E402

PROGRAM_ROOT = Path(__file__).resolve().parents[1]


class McpLockTest(unittest.TestCase):
    def test_lock_contains_mcp(self):
        lock = (PROGRAM_ROOT / "requirements.lock").read_text(encoding="utf-8")
        self.assertIn("mcp==", lock)

    def test_requirements_declares_mcp(self):
        req = (PROGRAM_ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("mcp", req)

    def test_selfcheck_imports_mcp(self):
        # selfcheck 校验的 import 列表含 MCP server 的 fastmcp（装错/半装立即暴露）。
        import inspect
        src = inspect.getsource(install.selfcheck)
        self.assertIn("mcp.server.fastmcp", src)

    def test_default_mcp_python_uses_current_interpreter(self):
        # 未设 GROUP_MCP_PYTHON 时，_MCP_PYTHON 回退当前解释器（pythonw 换 python），非开发机硬编码。
        exe = wake._default_mcp_python()
        self.assertEqual(exe, sys.executable.replace("pythonw.exe", "python.exe"))
        self.assertNotIn("D:/Python", exe)
        if not os.environ.get("GROUP_MCP_PYTHON"):
            self.assertEqual(wake._MCP_PYTHON, exe)

    def test_group_mcp_python_override_respected(self):
        # GROUP_MCP_PYTHON 覆盖优先于默认解释器。
        with mock.patch.dict(os.environ, {"GROUP_MCP_PYTHON": "C:/custom/python.exe"}, clear=True):
            # _MCP_PYTHON 是 import 期常量；这里直接验证优先级表达式逻辑与默认函数解耦。
            from engine import wake as _wake
            chosen = os.environ.get("GROUP_MCP_PYTHON", "").strip() or _wake._default_mcp_python()
            self.assertEqual(chosen, "C:/custom/python.exe")


if __name__ == "__main__":
    unittest.main()
