"""test_is_chat_process：不 import 整个 server.py（它依赖 mcp.server.fastmcp，本地测试
环境装了 mcp 包导致 mcp/server.py 被误当 mcp 包的 server 子模块——正是基线那个
test_image_browser ImportError 的根因）。用 AST 抽取 _is_chat_process/_reject_if_chat
源码段独立执行，绕开 FastMCP 依赖。"""
import ast
import os
import shlex
import sys
import textwrap
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import process_query

_SRC = (Path(__file__).resolve().parents[1] / 'mcp/server.py').read_text(encoding='utf-8')
tree = ast.parse(_SRC)

# 抽取 _parent_cmdline / _session_resolve / _self_session / _is_chat_process / _reject_if_chat 的函数定义源码
wanted = {"_parent_cmdline", "_session_resolve", "_self_session", "_is_chat_process", "_reject_if_chat"}
chunks = []
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
        chunks.append(ast.get_source_segment(_SRC, node))
assert len(chunks) == len(wanted), f"抽取到 {len(chunks)} 段，期望 {len(wanted)}"

mod = types.ModuleType("under_test")
mod.__dict__["os"] = os
mod.__dict__["sys"] = sys
mod.__dict__["re"] = __import__("re")
mod.__dict__["subprocess"] = __import__("subprocess")
mod.__dict__["functools"] = __import__("functools")
mod.__dict__["_CHAT_MODE"] = False
mod.__dict__["_execution_context"] = lambda: {}
mod.__dict__["process_query"] = process_query
mod.__dict__["_profile"] = lambda: 'default'
exec(compile("\n\n".join(chunks), "<extracted>", "exec"), mod.__dict__)


class IsChatProcessTests(unittest.TestCase):
    def setUp(self):
        self._orig_query = process_query.command_line
        self._orig_getppid = os.getppid

    def tearDown(self):
        process_query.command_line = self._orig_query
        os.getppid = self._orig_getppid

    def _mock(self, cmdline: str | None):
        os.getppid = lambda: 12345
        process_query.command_line = lambda *a, **k: shlex.split(cmdline) if cmdline else cmdline

    def test_work_session_not_chat(self):
        self._mock('hermes -p default chat --in E:\\x -c default-bot-group-work --create-if-missing')
        self.assertFalse(mod._is_chat_process())

    def test_chat_session_is_chat(self):
        self._mock('hermes -p default chat --in E:\\x -c default-bot-group-chat-10044 --create-if-missing')
        self.assertTrue(mod._is_chat_process())

    def test_no_c_flag_not_chat(self):
        """桌面版 gateway 无 -c 会话名 → 不误伤。"""
        self._mock('hermes gateway run')
        self.assertFalse(mod._is_chat_process())

    def test_process_query_fail_fail_open(self):
        """反查失败 → 放行（fail-open）。"""
        self._mock(None)
        self.assertFalse(mod._is_chat_process())

    def test_no_commandline_line(self):
        self._mock("")
        self.assertFalse(mod._is_chat_process())

    def test_ppid_invalid(self):
        os.getppid = lambda: 0
        self.assertFalse(mod._is_chat_process())

    def test_reject_if_chat_blocks_chat_process(self):
        self._mock('hermes -p x chat --in E:\\x -c x-y-chat-99')
        @mod._reject_if_chat
        def guarded():
            return {"ok": True}
        r = guarded()
        self.assertIn("error", r)
        self.assertEqual(r["error"], "PERMISSION_DENIED")
        self.assertEqual(r["reason"], "CHAT_MODE_RESTRICTED")
        self.assertEqual(r["action"], "guarded")
        self.assertIn("scan:req", r["next_step"])

    def test_all_execution_tools_reject_before_taskpool(self):
        """真实函数及装饰器组合在两种 chat 识别入口都应提前返回。"""
        calls = {
            "task_claim": dict(project="alpha"),
            "task_log": dict(project="alpha", task_id=1, type="dev"),
            "task_complete": dict(project="alpha", task_id=1),
            "task_block": dict(project="alpha", task_id=1, reason="test"),
        }
        for env_chat, process_chat in ((True, False), (False, True)):
            namespace = dict(mod.__dict__)
            pool = Mock()
            registered = []
            def register(condition):
                def decorate(fn):
                    if condition:
                        registered.append(fn.__name__)
                    return fn
                return decorate
            namespace.update(_CHAT_MODE=env_chat, _is_chat_process=lambda: process_chat,
                             _cond_tool=register, taskpool=pool, _role=lambda: "default")
            guard = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_reject_if_chat")
            nodes = [guard] + [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in calls]
            exec(compile(ast.Module(body=nodes, type_ignores=[]), "<guarded-tools>", "exec"), namespace)
            self.assertEqual(set(registered), set() if env_chat else set(calls))
            for name, kwargs in calls.items():
                with self.subTest(env_chat=env_chat, action=name):
                    result = namespace[name](**kwargs)
                    self.assertEqual(result["error"], "PERMISSION_DENIED")
                    self.assertEqual(result["reason"], "CHAT_MODE_RESTRICTED")
                    self.assertEqual(result["action"], name)
                    self.assertIn(name, result["message"])
                    self.assertIn("scan:req", result["next_step"])
            self.assertEqual(pool.mock_calls, [])

    def test_task_create_and_task_update_open_in_chat(self):
        """#199：task_create/task_update 开放 CHAT（无 @_cond_tool/@_reject_if_chat 守卫）。"""
        for name in ("task_create", "task_update"):
            node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
            decorators = [ast.get_source_segment(_SRC, d) for d in node.decorator_list]
            src = "\n".join(decorators)
            self.assertNotIn("_reject_if_chat", src, f"{name} 不应有 @_reject_if_chat")
            self.assertNotIn("_cond_tool", src, f"{name} 不应有 @_cond_tool")

    def test_reject_if_chat_allows_work_process(self):
        """话题尚未收口时，work 进程仍可调用任务工具。"""
        self._mock('hermes -p x chat --in E:\\x -c x-y-work --create-if-missing')
        @mod._reject_if_chat
        def guarded():
            return {"ok": True}
        self.assertEqual(guarded(), {"ok": True})

    def test_reject_if_chat_allows_fail_open(self):
        self._mock(None)
        @mod._reject_if_chat
        def guarded():
            return {"ok": True}
        self.assertEqual(guarded(), {"ok": True})


if __name__ == "__main__":
    unittest.main(verbosity=2)
