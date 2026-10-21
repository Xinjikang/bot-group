"""req#86 task#149：内置 user 身份 + members 可追溯 + 存量数据归 bot_id 迁移测试。

覆盖：
- 内置 user 无条件 upsert：空库首启建行（host_type='builtin'）、存量库归一
  （#147 遗留的 hermes user 行转 builtin、不双行）、幂等（重复 init 不烧
  AUTOINCREMENT 号、不双行）
- members 补 created_at/source：存量行 source='legacy'，schema 列存在
- wake_runs/message_executions 存量归 bot_id：按 profile 反推回填、
  反推不上（profile 空）保留 NULL 待归属不归边、me 经 wake_runs JOIN 回填
- is_builtin_user 收敛出口行为等价（'user' 判定、None/其它名 False）

运行：python -B -m unittest scripts.test_builtin_user_migration -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import db  # noqa: E402


class BuiltinUserMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbfile = str(Path(self.tmp.name) / "group.db")
        self._env = os.environ.get("GROUP_DB")
        os.environ["GROUP_DB"] = self.dbfile
        self.conn = db.get_connection()
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        if self._env is None:
            os.environ.pop("GROUP_DB", None)
        else:
            os.environ["GROUP_DB"] = self._env
        self.tmp.cleanup()

    def test_empty_db_gets_builtin_user(self):
        row = self.conn.execute("SELECT * FROM bots WHERE profile='user'").fetchone()
        self.assertIsNotNone(row, "空库首启必须有内置 user")
        self.assertEqual(row["host_type"], "builtin")
        self.assertEqual(row["runtime"], "builtin")

    def test_legacy_hermes_user_row_converted_single(self):
        # 模拟 #147 遗留：members.user 被 _backfill 反推成 hermes Bot。
        # 重跑 init：该行应转 builtin 且不产生第二行。
        self.conn.execute(
            "UPDATE bots SET host_type='hermes', runtime='hermes' WHERE profile='user'")
        self.conn.commit()
        self.conn.close()
        self.conn = db.get_connection()
        db.init_schema(self.conn)
        rows = self.conn.execute(
            "SELECT id, host_type FROM bots WHERE profile='user'").fetchall()
        self.assertEqual(len(rows), 1, "归一后必须单行")
        self.assertEqual(rows[0]["host_type"], "builtin")

    def test_backfill_skips_user_profile(self):
        # members 有 user 行（历史遗留）时 _backfill_hermes_bots 不再为它反推 hermes Bot。
        self.conn.execute(
            "INSERT INTO members(profile, display_name, role) VALUES('user', '主人', '')")
        self.conn.commit()
        self.conn.close()
        self.conn = db.get_connection()
        db.init_schema(self.conn)
        hermes_user = self.conn.execute(
            "SELECT COUNT(*) c FROM bots WHERE profile='user' AND host_type='hermes'").fetchone()["c"]
        self.assertEqual(hermes_user, 0, "user 不应被反推成 hermes Bot")

    def test_init_does_not_burn_autoincrement(self):
        # 幂等性：重复 init 不烧 sqlite_sequence（iterdump 类断言不漂移）。
        seq_before = self.conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='bots'").fetchone()
        self.conn.close()
        self.conn = db.get_connection()
        db.init_schema(self.conn)
        seq_after = self.conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='bots'").fetchone()
        if seq_before is None:
            self.assertIsNone(seq_after)
        else:
            self.assertEqual(seq_before["seq"], seq_after["seq"])

    def test_members_traceability_columns(self):
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(members)")}
        self.assertIn("created_at", cols)
        self.assertIn("source", cols)
        # 老库行（无 created_at 记录）source 应为 legacy
        self.conn.execute(
            "INSERT INTO members(profile, display_name, role) VALUES('x', 'X', '')")
        row = self.conn.execute("SELECT created_at, source FROM members WHERE profile='x'").fetchone()
        self.assertEqual(row["source"], "legacy")
        self.assertEqual(row["created_at"], "")

    def test_wake_runs_backfill_and_orphan_null(self):
        # 造 hermes Bot + 两行 wake_runs（一行 profile 匹配、一行 profile 空）。
        self.conn.execute(
            "INSERT INTO bots(host_type, profile, config_root, runtime, display_name, role) "
            "VALUES('hermes', 'p1', ?, 'hermes', '', '')", (db._hermes_root(),))
        bid = self.conn.execute(
            "SELECT id FROM bots WHERE profile='p1' AND host_type='hermes'").fetchone()["id"]
        self.conn.execute(
            "INSERT INTO wake_runs(session, started_at, profile, project, bot_id) "
            "VALUES('s1', 1.0, 'p1', 'prj', NULL)")
        self.conn.execute(
            "INSERT INTO wake_runs(session, started_at, profile, project, bot_id) "
            "VALUES('s2', 2.0, NULL, 'prj', NULL)")  # profile 空 → 待归属
        self.conn.execute(
            "INSERT INTO messages(project, ts, sender, sender_type, content) "
            "VALUES('prj', '2026-09-29T00:00:00', 'x', 'bot', 'm')")
        mid = self.conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        self.conn.execute(
            "INSERT INTO message_executions(message_id, session, wake_started_at, sent_at, bot_id) "
            "VALUES(?, 's1', 1.0, 1.5, NULL)", (mid,))
        self.conn.execute(
            "INSERT INTO messages(project, ts, sender, sender_type, content) "
            "VALUES('prj', '2026-09-29T00:00:01', 'x', 'bot', 'm2')")
        mid2 = self.conn.execute("SELECT last_insert_rowid() id").fetchone()["id"]
        self.conn.execute(
            "INSERT INTO message_executions(message_id, session, wake_started_at, sent_at, bot_id) "
            "VALUES(?, 's2', 2.0, 2.5, NULL)", (mid2,))  # 关联待归属 run → 同样 NULL
        self.conn.commit()
        self.conn.close()
        self.conn = db.get_connection()
        db.init_schema(self.conn)
        r1 = self.conn.execute(
            "SELECT bot_id FROM wake_runs WHERE session='s1'").fetchone()
        r2 = self.conn.execute(
            "SELECT bot_id FROM wake_runs WHERE session='s2'").fetchone()
        m1 = self.conn.execute(
            "SELECT bot_id FROM message_executions WHERE session='s1'").fetchone()
        m2 = self.conn.execute(
            "SELECT bot_id FROM message_executions WHERE session='s2'").fetchone()
        self.assertEqual(r1["bot_id"], bid, "profile 匹配行归 bot_id")
        self.assertIsNone(r2["bot_id"], "profile 空行保留 NULL 待归属，不归边")
        self.assertEqual(m1["bot_id"], bid, "me 经 wake_runs JOIN 回填")
        self.assertIsNone(m2["bot_id"], "关联待归属 run 的 me 同样保留 NULL")

    def test_is_builtin_user(self):
        self.assertTrue(db.is_builtin_user("user"))
        self.assertTrue(db.is_builtin_user(db.BUILTIN_USER_PROFILE))
        self.assertFalse(db.is_builtin_user("be-dev-01"))
        self.assertFalse(db.is_builtin_user(None))
        self.assertFalse(db.is_builtin_user(""))

    def test_double_user_row_normalization(self):
        # P0 复现（architect 消息 5274）：builtin+hermes 双 user 行时，
        # 归一 UPDATE 会撞 UNIQUE → init_schema 崩。归一须先收敛双行。
        self.conn.execute(
            "INSERT INTO bots(host_type, profile, config_root, runtime, display_name, role) "
            "VALUES('hermes', 'user', ?, 'hermes', '主人', '')", (db._hermes_root(),))
        self.conn.commit()
        self.conn.close()
        self.conn = db.get_connection()
        db.init_schema(self.conn)  # 不应抛 UNIQUE
        rows = self.conn.execute(
            "SELECT id, host_type FROM bots WHERE profile='user' ORDER BY id").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["host_type"], "builtin")

    def test_sb_double_null_rows_do_not_crash_init(self):
        # sb 同绑定键的两条 NULL 行回填时应避免主键冲突，
        # ——同键多行跳过回填保留 NULL；不同 kind/thread 的唯一行正常回填。
        self.conn.execute(
            "INSERT INTO members(profile, display_name, role) VALUES('p2', 'P2', '')")
        bid = db._ensure_hermes_bot(self.conn, "p2")
        self.conn.execute(
            "INSERT INTO session_bindings(project, bot_id, profile, kind, thread_id, session_id, generation, switching, title, updated_at) "
            "VALUES('p', NULL, 'p2', 'work', 0, 'd1', 0, 0, '', 'now')")
        self.conn.execute(
            "INSERT INTO session_bindings(project, bot_id, profile, kind, thread_id, session_id, generation, switching, title, updated_at) "
            "VALUES('p', NULL, 'p2', 'work', 0, 'd2', 0, 0, '', 'now')")
        self.conn.commit()
        self.conn.close()
        self.conn = db.get_connection()
        db.init_schema(self.conn)  # 不应抛 IntegrityError
        dups = self.conn.execute(
            "SELECT bot_id FROM session_bindings WHERE session_id IN ('d1','d2')").fetchall()
        self.assertEqual(len(dups), 2, "双行保留不丢")
        self.assertTrue(all(r["bot_id"] is None for r in dups), "多行组跳过回填保留 NULL")
        # 单行正常回填不受影响
        solo = self.conn.execute(
            "INSERT INTO session_bindings(project, bot_id, profile, kind, thread_id, session_id, generation, switching, title, updated_at) "
            "VALUES('p', NULL, 'p2', 'chat', 7, 'solo', 0, 0, '', 'now')")
        self.conn.commit()
        self.conn.close()
        self.conn = db.get_connection()
        db.init_schema(self.conn)
        filled = self.conn.execute(
            "SELECT bot_id FROM session_bindings WHERE session_id='solo'").fetchone()
        self.assertEqual(filled["bot_id"], bid, "唯一行正常回填")


if __name__ == "__main__":
    unittest.main()
