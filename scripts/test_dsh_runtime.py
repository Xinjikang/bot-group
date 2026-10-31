"""req#137 task#266：dsh 运行时（会话绑定 + 事件解析 + 命令/overlay 构建）单元回归。

覆盖：
1. dsh_save_session / dsh_new 的 generation 守卫（首建 INSERT、同代 UPDATE、/new 后拒旧回写）。
2. dsh_reasoning 事件落库与推理契约（thinking→reasoning、tool_call→tool_calls、final→finished、
   group_send tool_result→reply_message_id）。
3. native_runtime.dsh 命令（runtime_entry + --session-id 续跑 + --patch overlay + stdin 提示词）。
4. native_runtime.dsh_overlay（认证控制覆盖 + 项目级覆盖 + MCP insert + 动态表达式透传）。

mock 隔离库（不进原生），系统 python 可跑。
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from engine import db, session_adapter, dsh_reasoning, native_runtime  # noqa: E402


def _register_dsh_bot(conn, project="bot-group", profile="dsh-bot", entry="C:/dsh/dsh.cmd"):
    cur = conn.execute(
        "INSERT INTO bots(host_type, profile, config_root, runtime, runtime_entry, display_name, role) "
        "VALUES('dsh', ?, 'C:/dsh-home', 'dsh', ?, ?, '')",
        (profile, entry, profile),
    )
    bot_id = cur.lastrowid
    conn.execute(
        "INSERT INTO project_members(project, profile, bot_id, active) VALUES(?,?,?,1)",
        (project, profile, bot_id),
    )
    conn.commit()
    return bot_id


class DshSessionBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t266_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        self.conn = sqlite3.connect(self.dbfile)
        self.conn.row_factory = sqlite3.Row
        db.init_schema(self.conn)
        self.conn.execute(
            "INSERT INTO projects(name, root, worktree_root, created_at) VALUES('bot-group',?, '', ?)",
            (_ROOT, db.now_iso()),
        )
        self.conn.commit()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def test_first_spawn_inserts_binding(self):
        bot_id = _register_dsh_bot(self.conn)
        saved = session_adapter.dsh_save_session(
            self.conn, "bot-group", "dsh-bot", "work", None,
            "session-abc-1", bot_id=bot_id, spawn_gen=None)
        self.assertTrue(saved)
        row = self.conn.execute(
            "SELECT session_id, runtime, generation FROM session_bindings "
            "WHERE project='bot-group' AND bot_id=? AND kind='work' AND thread_id=0",
            (bot_id,)).fetchone()
        self.assertEqual(row["session_id"], "session-abc-1")
        self.assertEqual(row["runtime"], "dsh")
        self.assertEqual(row["generation"], 0)

    def test_resume_update_same_generation(self):
        bot_id = _register_dsh_bot(self.conn)
        self.assertTrue(session_adapter.dsh_save_session(
            self.conn, "bot-group", "dsh-bot", "work", None, "session-abc-1", bot_id=bot_id))
        saved = session_adapter.dsh_save_session(
            self.conn, "bot-group", "dsh-bot", "work", None,
            "session-abc-2", bot_id=bot_id, spawn_gen=0)
        self.assertTrue(saved)
        row = self.conn.execute(
            "SELECT session_id FROM session_bindings WHERE project='bot-group' AND bot_id=?",
            (bot_id,)).fetchone()
        self.assertEqual(row["session_id"], "session-abc-2")

    def test_stale_writeback_rejected_after_new(self):
        bot_id = _register_dsh_bot(self.conn)
        self.assertTrue(session_adapter.dsh_save_session(
            self.conn, "bot-group", "dsh-bot", "work", None, "session-abc-1", bot_id=bot_id))
        # /new：清空绑定 + generation 1
        result = session_adapter.dsh_new(self.conn, "bot-group", "dsh-bot", "work", None, "")
        self.assertTrue(result.get("ok"))
        # 旧运行（spawn_gen=0）迟到回写 → 拒
        saved = session_adapter.dsh_save_session(
            self.conn, "bot-group", "dsh-bot", "work", None,
            "session-stale", bot_id=bot_id, spawn_gen=0)
        self.assertFalse(saved)
        row = self.conn.execute(
            "SELECT session_id, generation FROM session_bindings WHERE project='bot-group' AND bot_id=?",
            (bot_id,)).fetchone()
        self.assertEqual(row["session_id"], "")
        self.assertEqual(row["generation"], 1)

    def test_read_binding_requires_dsh_runtime_row(self):
        bot_id = _register_dsh_bot(self.conn)
        # 异种 runtime 行（hermes 残留）不得被当 dsh session 去续跑
        self.conn.execute(
            "INSERT INTO session_bindings(project,bot_id,profile,kind,thread_id,session_id,runtime,updated_at) "
            "VALUES('bot-group',?,'dsh-bot','work',0,'hermes-sid','hermes',?)",
            (bot_id, db.now_iso()))
        self.conn.commit()
        self.assertIsNone(session_adapter.dsh_session_id(
            self.conn, "bot-group", "dsh-bot", "work", None, bot_id=bot_id))

    def test_new_audits_and_clears(self):
        bot_id = _register_dsh_bot(self.conn)
        self.assertTrue(session_adapter.dsh_save_session(
            self.conn, "bot-group", "dsh-bot", "chat", 7, "session-chat-1", bot_id=bot_id))
        result = session_adapter.dsh_new(self.conn, "bot-group", "dsh-bot", "chat", 7, "title")
        self.assertTrue(result.get("ok"))
        self.assertEqual(result["old_session_id"], "session-chat-1")
        self.assertEqual(result["new_session_id"], "")
        op = self.conn.execute(
            "SELECT status, old_session_id FROM session_switch_ops WHERE operation_id=?",
            (result["operation_id"],)).fetchone()
        self.assertEqual(op["status"], "committed")
        self.assertEqual(op["old_session_id"], "session-chat-1")


class DshReasoningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t266_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        self.conn = sqlite3.connect(self.dbfile)
        self.conn.row_factory = sqlite3.Row
        db.init_schema(self.conn)

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def _feed(self, events):
        for ev in events:
            dsh_reasoning.record("s1", 100.0, ev)

    def test_thinking_maps_to_reasoning(self):
        self._feed([
            {"type": "session", "sessionId": "session-x"},
            {"type": "thinking", "text": "先想一下"},
            {"type": "final", "text": "DSH_OK"},
        ])
        out = dsh_reasoning.read("s1", 100.0)
        kinds = [e["reasoning"] for e in out["entries"]]
        self.assertIn("先想一下", kinds)
        finished = [e for e in out["entries"] if e["finished"]]
        self.assertEqual(len(finished), 1)

    def test_tool_call_and_group_send_reply(self):
        self._feed([
            {"type": "session", "sessionId": "session-x"},
            {"type": "tool_call", "callId": "c1", "tool": "mcp__task_queue__group_send",
             "input": {"text": "hi"}},
            {"type": "tool_result", "callId": "c1", "status": "completed",
             "tool": "mcp__task_queue__group_send",
             "result": json.dumps({"message_id": 42, "ok": True})},
            {"type": "final", "text": "sent"},
        ])
        out = dsh_reasoning.read("s1", 100.0)
        tools = [e for e in out["entries"] if e["tool_name"]]
        self.assertEqual(tools[0]["tool_name"], "mcp__task_queue__group_send")
        self.assertEqual(tools[0]["tool_calls"][0]["name"], "mcp__task_queue__group_send")
        replies = [e for e in out["entries"] if e["reply_message_id"]]
        self.assertEqual(replies[0]["reply_message_id"], 42)

    def test_late_reads_after_id(self):
        self._feed([{"type": "final", "text": "one"}])
        out = dsh_reasoning.read("s1", 100.0)
        self._feed([{"type": "final", "text": "two"}])
        out2 = dsh_reasoning.read("s1", 100.0, after_id=out["latest_id"])
        self.assertEqual(len(out2["entries"]), 1)
        self.assertEqual(out2["entries"][0]["reasoning"], "two")


class DshCommandTests(unittest.TestCase):
    def test_command_with_resume_and_overlay(self):
        bot = {"runtime": "dsh", "config_root": "C:/dsh-home", "runtime_entry": "C:/dsh/dsh.cmd"}
        with patch("pathlib.Path.is_file", return_value=True):
            cmd, home = native_runtime.dsh(bot, overlay_path="C:/tmp/overlay.json",
                                           resume="session-abc-1")
        self.assertEqual(cmd[:2], ["C:/dsh/dsh.cmd", "--profile"])
        self.assertEqual(cmd[2], "headless")
        self.assertIn("--json", cmd)
        self.assertEqual(cmd[cmd.index("--patch") + 1], "C:/tmp/overlay.json")
        self.assertEqual(cmd[cmd.index("--session-id") + 1], "session-abc-1")
        self.assertEqual(cmd[-1], "-")  # task 读 stdin
        self.assertEqual(os.path.normpath(home), os.path.normpath("C:/dsh-home"))

    def test_command_requires_entry(self):
        bot = {"runtime": "dsh", "config_root": "C:/dsh-home", "runtime_entry": ""}
        with self.assertRaises(FileNotFoundError):
            native_runtime.dsh(bot)

    def test_overlay_merges_auth_and_project_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            prof = os.path.join(home, "profiles", "desktop")
            os.makedirs(prof)
            with open(os.path.join(prof, "cordis.patch.yml"), "w", encoding="utf-8") as f:
                f.write("- id: agent-default-model\n"
                        "  config:\n    provider: deepseek-account\n    model: deepseek-flash\n"
                        "- id: system-prompt\n"
                        "  config:\n    personaPrefix: 探针人格\n")
            bot = {"runtime": "dsh", "config_root": home, "profile": "desktop"}
            overlay = native_runtime.dsh_overlay(
                bot, credentials_path=None,
                mcp_entry={"id": "task-queue-mcp", "name": "@deepseek-ai/dsh-mcp-client",
                           "config": {"serverName": "task_queue"}},
                project_overrides={"model": "deepseek-v4-flash"})
            by_id = {e.get("id"): e for e in overlay}
            # 认证控制：deepseek-account 存在（账号路由恢复）
            self.assertIn("deepseek-account", by_id)
            self.assertFalse(by_id["deepseek-account"].get("disabled"))
            # 项目覆盖：agent-default-model.model 被覆盖，provider 保留
            self.assertEqual(by_id["agent-default-model"]["config"]["model"], "deepseek-v4-flash")
            self.assertEqual(by_id["agent-default-model"]["config"]["provider"], "deepseek-account")
            # profile 的 persona 条目保留
            self.assertEqual(by_id["system-prompt"]["config"]["personaPrefix"], "探针人格")
            # MCP insert 追加
            insert = next(e for e in overlay if "insert" in e)
            self.assertEqual(insert["insert"][0]["name"], "@deepseek-ai/dsh-mcp-client")


if __name__ == "__main__":
    unittest.main()
