"""req#88 task#153：codex 会话映射（session_bindings runtime 列 + thread_id 回填 + /new 清绑定审计）。

契约（docs/design/SLASH-COMMAND-DESIGN-2026-09-27.md：会话与停止）：
1. session_bindings 加 runtime 列逐行写读校验（不靠 bots.runtime 推导）。
2. codex 收到 thread.started 即保存绑定（runtime='codex' 快照 + generation 条件守卫防旧运行迟到回写）。
3. /new 对 codex 必做 = 弃旧 thread 清空绑定（session_id=''）+ 落 session_switch_ops 审计（committed）。
4. codex 行 switching 恒 0 不读写。
5. codex 无会话创建动作：绑定执行后回填，非执行前创建。

mock 隔离库（不进 Hermes 原生），系统 python 可跑。
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from engine import db, session_adapter, commands, wake  # noqa: E402


def _register_codex_bot(conn, project="bot-group", profile="cx-bot"):
    cur = conn.execute(
        "INSERT INTO bots(host_type, profile, config_root, runtime, display_name, role) "
        "VALUES('codex', ?, 'C:/codex-root', 'codex', ?, '')",
        (profile, profile),
    )
    bot_id = cur.lastrowid
    conn.execute(
        "INSERT INTO project_members(project, profile, bot_id, active) VALUES(?,?,?,1)",
        (project, profile, bot_id),
    )
    conn.commit()
    return bot_id


class CodexSessionMappingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t153_")
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

    # ---------- 1. runtime 列 ----------

    def test_schema_has_runtime_column(self):
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(session_bindings)")}
        self.assertIn("runtime", cols)

    def test_runtime_column_defaults_hermes(self):
        self.conn.execute(
            "INSERT INTO session_bindings(project, profile, kind, thread_id, session_id, updated_at) "
            "VALUES('bot-group','h-bot','work',0,'sid-1',?)", (db.now_iso(),),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT runtime FROM session_bindings WHERE session_id='sid-1'"
        ).fetchone()
        self.assertEqual(row["runtime"], "hermes")

    # ---------- 2. codex_thread_id 逐行校验 ----------

    def test_codex_thread_id_reads_only_codex_rows(self):
        """runtime='codex' 才返回 thread_id；hermes 行不返回（防异种 ID 撞车）。"""
        bot = _register_codex_bot(self.conn)
        # 先写一条 hermes 行（残留异种 ID），再写 codex 行
        self.conn.execute(
            "INSERT INTO session_bindings(project, bot_id, profile, kind, thread_id, session_id, runtime, updated_at) "
            "VALUES('bot-group', ?, 'cx-bot', 'work', 0, 'hermes-sid', 'hermes', ?)",
            (bot, db.now_iso()),
        )
        self.conn.commit()
        # hermes 行不返回（不是 codex thread_id）
        self.assertIsNone(session_adapter.codex_thread_id(self.conn, "bot-group", "cx-bot", "work", None))
        # 改成 codex 行后返回
        self.conn.execute(
            "UPDATE session_bindings SET runtime='codex', session_id='th-001' WHERE session_id='hermes-sid'"
        )
        self.conn.commit()
        self.assertEqual(
            session_adapter.codex_thread_id(self.conn, "bot-group", "cx-bot", "work", None),
            "th-001",
        )

    def test_codex_thread_id_empty_session_is_none(self):
        """/new 清空后 session_id='' 视为无绑定 → 首建。"""
        bot = _register_codex_bot(self.conn)
        self.conn.execute(
            "INSERT INTO session_bindings(project, bot_id, profile, kind, thread_id, session_id, runtime, updated_at) "
            "VALUES('bot-group', ?, 'cx-bot', 'work', 0, '', 'codex', ?)",
            (bot, db.now_iso()),
        )
        self.conn.commit()
        self.assertIsNone(session_adapter.codex_thread_id(self.conn, "bot-group", "cx-bot", "work", None))

    def test_codex_thread_id_no_bot_returns_none(self):
        """非 codex Bot（或无 bot）→ 不命中。"""
        self.assertIsNone(session_adapter.codex_thread_id(self.conn, "bot-group", "ghost", "work", None))

    # ---------- 3. codex_save_thread 回填 + generation 守卫 ----------

    def test_save_thread_first_creation_inserts_codex_row(self):
        """首建：无绑定行 → INSERT runtime='codex' + generation=0。"""
        bot = _register_codex_bot(self.conn)
        saved = session_adapter.codex_save_thread(
            self.conn, "bot-group", "cx-bot", "work", None, "th-new", bot_id=bot)
        self.assertTrue(saved)
        row = self.conn.execute(
            "SELECT session_id, runtime, generation, switching FROM session_bindings "
            "WHERE project='bot-group' AND bot_id=?", (bot,),
        ).fetchone()
        self.assertEqual(row["session_id"], "th-new")
        self.assertEqual(row["runtime"], "codex")
        self.assertEqual(row["generation"], 0)
        self.assertEqual(row["switching"], 0)  # codex 行 switching 恒 0

    def test_save_thread_guards_stale_generation(self):
        """旧运行迟到回写：generation 不匹配 → 拒绝，不覆盖（第四节第 6 条）。"""
        bot = _register_codex_bot(self.conn)
        # spawn 时有 generation=2 的绑定（/new 已弃旧 thread 递增过）
        self.conn.execute(
            "INSERT INTO session_bindings(project, bot_id, profile, kind, thread_id, session_id, runtime, generation, updated_at) "
            "VALUES('bot-group', ?, 'cx-bot', 'work', 0, 'old-th', 'codex', 2, ?)",
            (bot, db.now_iso()),
        )
        self.conn.commit()
        # 旧运行回填时 spawn_gen 已过期（假设 spawn 时 gen=1）
        saved = session_adapter.codex_save_thread(
            self.conn, "bot-group", "cx-bot", "work", None, "stale-th", bot_id=bot, spawn_gen=1)
        self.assertFalse(saved)
        row = self.conn.execute(
            "SELECT session_id FROM session_bindings WHERE project='bot-group' AND bot_id=?", (bot,),
        ).fetchone()
        self.assertEqual(row["session_id"], "old-th")  # 未被覆盖

    def test_save_thread_matching_generation_updates(self):
        """generation 匹配 → 原子 UPDATE 回填。"""
        bot = _register_codex_bot(self.conn)
        self.conn.execute(
            "INSERT INTO session_bindings(project, bot_id, profile, kind, thread_id, session_id, runtime, generation, updated_at) "
            "VALUES('bot-group', ?, 'cx-bot', 'work', 0, '', 'codex', 1, ?)",
            (bot, db.now_iso()),
        )
        self.conn.commit()
        saved = session_adapter.codex_save_thread(
            self.conn, "bot-group", "cx-bot", "work", None, "th-live", bot_id=bot, spawn_gen=1)
        self.assertTrue(saved)
        row = self.conn.execute(
            "SELECT session_id FROM session_bindings WHERE project='bot-group' AND bot_id=?", (bot,),
        ).fetchone()
        self.assertEqual(row["session_id"], "th-live")

    def test_save_thread_row_vanished_after_new(self):
        """spawn 时有行、回填时已无行（/new 清空）→ 拒，不复活已弃绑定。"""
        bot = _register_codex_bot(self.conn)
        # 无行但 spawn_gen 非 None（说明 spawn 时有行被 /new 删了）
        saved = session_adapter.codex_save_thread(
            self.conn, "bot-group", "cx-bot", "work", None, "ghost-th", bot_id=bot, spawn_gen=0)
        self.assertFalse(saved)
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM session_bindings WHERE project='bot-group' AND bot_id=?", (bot,),
        ).fetchone()
        self.assertEqual(row["n"], 0)

    # ---------- 4. codex_new 清空绑定 + 审计 ----------

    def test_codex_new_clears_binding_and_audits(self):
        """/new：清空绑定 session_id=''，generation+1，落 committed 审计。"""
        bot = _register_codex_bot(self.conn)
        self.conn.execute(
            "INSERT INTO session_bindings(project, bot_id, profile, kind, thread_id, session_id, runtime, generation, switching, updated_at) "
            "VALUES('bot-group', ?, 'cx-bot', 'work', 0, 'th-old', 'codex', 3, 1, ?)",
            (bot, db.now_iso()),
        )
        self.conn.commit()
        result = session_adapter.codex_new(self.conn, "bot-group", "cx-bot", "work", None, "title")
        self.assertTrue(result["ok"])
        self.assertEqual(result["old_session_id"], "th-old")
        self.assertEqual(result["new_session_id"], "")
        row = self.conn.execute(
            "SELECT session_id, generation, switching, runtime FROM session_bindings "
            "WHERE project='bot-group' AND bot_id=?", (bot,),
        ).fetchone()
        self.assertEqual(row["session_id"], "")
        self.assertEqual(row["generation"], 4)  # 递增
        self.assertEqual(row["switching"], 0)  # codex 行 switching 恒 0
        self.assertEqual(row["runtime"], "codex")
        # 审计行：committed，old=new 存 thread_id（清空后 new 空）
        op = self.conn.execute(
            "SELECT status, old_session_id, new_session_id FROM session_switch_ops WHERE operation_id=?",
            (result["operation_id"],),
        ).fetchone()
        self.assertEqual(op["status"], "committed")
        self.assertEqual(op["old_session_id"], "th-old")
        self.assertEqual(op["new_session_id"], "")

    def test_codex_new_non_codex_target_rejected(self):
        """目标不是 codex Bot → 拒绝。"""
        result = session_adapter.codex_new(self.conn, "bot-group", "ghost", "work", None, "title")
        self.assertFalse(result["ok"])

    # ---------- 5. commands /new 分叉 ----------

    def test_commands_new_routes_codex(self):
        """commands /new 对 codex Bot 走 codex_new（不触发 Hermes switch_session）。"""
        _register_codex_bot(self.conn)
        with patch.object(session_adapter, "switch_session") as mock_switch, \
             patch.object(wake, "session_busy", return_value=False):
            mock_switch.side_effect = AssertionError("codex /new 不得走 hermes switch_session")
            result = commands._handle_new(self.conn, "bot-group", ["cx-bot"], "", None)
        self.assertTrue(result["ok"])
        self.assertIn("codex", result["detail"])
        # 无旧绑定行时 codex_new 只落审计不建行（幂等清空语义）
        op = self.conn.execute(
            "SELECT status FROM session_switch_ops WHERE operation_id=?",
            (result["operation_id"],),
        ).fetchone()
        self.assertEqual(op["status"], "committed")

    def test_commands_new_routes_hermes(self):
        """hermes Bot 仍走 switch_session（不误入 codex_new）。"""
        self.conn.execute(
            "INSERT INTO bots(host_type, profile, config_root, runtime, display_name, role) "
            "VALUES('hermes', 'h-bot', 'C:/hermes-root', 'hermes', 'h-bot', '')",
        )
        self.conn.commit()
        with patch.object(session_adapter, "switch_session") as mock_switch, \
             patch.object(session_adapter, "codex_new") as mock_codex:
            mock_switch.return_value = {"ok": True, "session_id": "s", "generation": 1, "operation_id": "op"}
            result = commands._handle_new(self.conn, "bot-group", ["h-bot"], "", None)
            mock_switch.assert_called_once()
            mock_codex.assert_not_called()
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
