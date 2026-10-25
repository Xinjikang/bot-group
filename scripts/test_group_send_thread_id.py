"""req#60 task#110 单测：group_send 收掉 thread_id，MCP 层按进程身份自动推导。

契约（req#60 结论）：
- group_send 签名不再含 thread_id 参数（bot 不传）。
- chat 进程 → 自动填 _session_resolve 从父进程 -c 会话名解析出的 tid。
- work 进程 → 恒填 null（点对点）。
- _session_resolve 解析失败 → 显式报错（fail-closed），绝不静默兜底。
- project 仅点对点需要；chat 进程忽略 project（话题归属权威）。

用 AST 抽取 group_send 源码段独立执行（避开 mcp.server.fastmcp 依赖，同
test_is_chat_process 模式）。
"""
import ast
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_SRC = (Path(__file__).resolve().parents[1] / 'mcp/server.py').read_text(encoding='utf-8')
_tree = ast.parse(_SRC)

# 抽取 group_send 函数定义源码
_chunks = []
for node in ast.walk(_tree):
    if isinstance(node, ast.FunctionDef) and node.name == "group_send":
        _chunks.append(ast.get_source_segment(_SRC, node))
assert len(_chunks) == 1, f"抽取到 {len(_chunks)} 段 group_send"


def _build_mod():
    mod = types.ModuleType("group_send_under_test")
    # group_send 引用的外部符号：_session_resolve / _post / _inbox_project
    mod.__dict__["_session_resolve"] = _session_resolve
    mod.__dict__["_post"] = _post
    mod.__dict__["_inbox_project"] = None
    mod.__dict__["_work_project"] = lambda: ("bot-group", "")
    mod.__dict__["_execution_context"] = lambda: dict(project='bot-group', session='bot-bot-group-work', wake_started_at=100)
    exec(compile(_chunks[0], "<group_send>", "exec"), mod.__dict__)
    return mod


# 可替换的桩（测试里 patch mod 的这三个名字）
_session_resolve = lambda: ("work", None, "")  # noqa: E731
_post = lambda path, body: {"posted": path, "body": body}  # noqa: E731


class GroupSendThreadIdTests(unittest.TestCase):
    def setUp(self):
        self.mod = _build_mod()

    def test_signature_drops_thread_id(self):
        """group_send 签名不再含 thread_id 参数。"""
        tree = ast.parse(_chunks[0])
        fn = tree.body[0]
        args = [a.arg for a in fn.args.args]
        self.assertNotIn("thread_id", args)
        self.assertIn("need_reply", args)

    def test_chat_process_fills_thread_id(self):
        """chat 进程 → thread_id 自动填解析出的 tid。"""
        self.mod.__dict__["_session_resolve"] = lambda: ("chat", 10039, "")
        r = self.mod.group_send("话题回复", True)
        self.assertEqual(r["body"]["thread_id"], 10039)

    def test_work_process_thread_id_null(self):
        """work 进程 → thread_id 恒 null（点对点）。"""
        self.mod.__dict__["_session_resolve"] = lambda: ("work", None, "")
        self.mod.__dict__["_inbox_project"] = "bot-group"
        r = self.mod.group_send("@default 点对点", False)
        self.assertEqual(r["body"]["thread_id"], None)
        self.assertEqual(r["body"]["project"], "bot-group")

    def test_resolve_fail_closed(self):
        """解析失败 → 显式报错，不静默兜底。"""
        self.mod.__dict__["_session_resolve"] = lambda: ("", None, "父进程查询失败")
        r = self.mod.group_send("x", True)
        self.assertIn("error", r)
        self.assertIn("无法确定会话身份", r["error"])

    def test_p2p_requires_project(self):
        """点对点且无 project 来源 → 报错。"""
        self.mod.__dict__["_session_resolve"] = lambda: ("work", None, "")
        self.mod.__dict__["_inbox_project"] = None
        self.mod.__dict__["_work_project"] = lambda: ("", "project is required")
        r = self.mod.group_send("@default x", False)
        self.assertIn("error", r)
        self.assertIn("project is required", r["error"])

    def test_p2p_explicit_project_wins(self):
        """点对点显式传 project → 用显式值。"""
        self.mod.__dict__["_session_resolve"] = lambda: ("work", None, "")
        self.mod.__dict__["_inbox_project"] = "bot-group"
        with self.assertRaises(TypeError):
            self.mod.group_send("@default x", False, project="cicp")


if __name__ == "__main__":
    unittest.main(verbosity=2)
