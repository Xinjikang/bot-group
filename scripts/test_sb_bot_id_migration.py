"""req#86 task#148：session_bindings 主键 profile → bot_id + wake_runs 列改名迁移测试。

覆盖：
- 老形态 session_bindings 表（主键 (project,profile,kind,thread_id)）重建为
  (project,bot_id,kind,thread_id)，bot_id 按「反推优先」回填，内容零丢失
- 孤儿行（members 无对应 profile）保留且 bot_id 置 NULL，不静默丢
- 幂等：已迁移表二次 init_schema 不再重建
- wake_runs.hermes_session_id → runtime_session_id 列改名 + 兼容期双名兜底
- pm 同 (project,profile) 的两条 NULL 行回填时应避免 UNIQUE 冲突
  （组内多行时跳过回填保留 NULL，不静默归边/丢行）

运行：python -B -m unittest scripts.test_sb_bot_id_migration -v
"""
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import db  # noqa: E402


def _make_old_db(path: str) -> None:
    """手工建 req#86 之前的库形态（老 pm + 老 sb + 老 wake_runs 列名）。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        # bots 表（#147 已交付的形态）
        conn.execute("""
            CREATE TABLE bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                host_type TEXT NOT NULL,
                profile TEXT NOT NULL,
                config_root TEXT NOT NULL,
                runtime TEXT NOT NULL DEFAULT 'hermes',
                display_name TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT '',
                UNIQUE(host_type, config_root, profile)
            )
        """)
        # members 花名册
        conn.execute("CREATE TABLE members(profile TEXT PRIMARY KEY, display_name TEXT, role TEXT)")
        for p in ("a", "b"):
            conn.execute("INSERT INTO members VALUES(?,?,?)", (p, p, ""))
        # 老 pm（#147 之前的形态）
        conn.execute("CREATE TABLE project_members(project TEXT, profile TEXT, active INTEGER, PRIMARY KEY(project,profile))")
        conn.execute("INSERT INTO project_members VALUES('p','a',1)")
        # 老 sb（req#83 形态，主键 profile）
        conn.execute("""
            CREATE TABLE session_bindings (
                project TEXT NOT NULL,
                profile TEXT NOT NULL,
                kind TEXT NOT NULL,
                thread_id INTEGER NOT NULL DEFAULT 0,
                session_id TEXT NOT NULL,
                generation INTEGER NOT NULL DEFAULT 0,
                title TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (project, profile, kind, thread_id)
            )
        """)
        conn.execute("INSERT INTO session_bindings VALUES('p','a','work',0,'sid-a',0,'title-a','2026-09-28T00:00:00')")
        conn.execute("INSERT INTO session_bindings VALUES('p','ghost','work',0,'sid-g',0,'title-g','2026-09-28T00:00:00')")  # 孤儿
        # 老 wake_runs（hermes_session_id 列名）
        conn.execute("CREATE TABLE wake_runs(session TEXT, started_at REAL, hermes_session_id TEXT, PRIMARY KEY(session, started_at))")
        conn.execute("INSERT INTO wake_runs VALUES('s1', 1.0, 'hs-1')")
        conn.commit()
    finally:
        conn.close()


class SBBotIdMigrationTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.dbfile = str(Path(self.tmp.name) / "group.db")
        _make_old_db(self.dbfile)
        import os
        self._env = os.environ.get("GROUP_DB")
        os.environ["GROUP_DB"] = self.dbfile
        self.conn = db.get_connection()
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        import os
        if self._env is None:
            os.environ.pop("GROUP_DB", None)
        else:
            os.environ["GROUP_DB"] = self._env
        self.tmp.cleanup()

    def test_sb_rebuild_keys_and_backfill(self):
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(session_bindings)")}
        self.assertIn("bot_id", cols)
        pk = [r["name"] for r in self.conn.execute("PRAGMA table_info(session_bindings)") if r["pk"]]
        self.assertEqual(pk, ["project", "bot_id", "kind", "thread_id"])
        row = self.conn.execute(
            "SELECT sb.profile, sb.bot_id, sb.session_id, sb.title FROM session_bindings sb WHERE sb.profile='a'"
        ).fetchone()
        bot = self.conn.execute("SELECT id FROM bots WHERE profile='a'").fetchone()
        self.assertEqual(row["bot_id"], bot["id"])
        self.assertEqual(row["session_id"], "sid-a")
        self.assertEqual(row["title"], "title-a")
        # 兼容列保留（旧消费点按 profile 读取承接）
        self.assertIn("profile", cols)

    def test_orphan_row_preserved_with_null_bot_id(self):
        row = self.conn.execute("SELECT bot_id, session_id FROM session_bindings WHERE profile='ghost'").fetchone()
        self.assertIsNotNone(row, "孤儿行不得静默丢")
        self.assertIsNone(row["bot_id"])
        self.assertEqual(row["session_id"], "sid-g")

    def test_wake_runs_column_renamed_and_backfilled(self):
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(wake_runs)")]
        self.assertIn("runtime_session_id", cols)
        self.assertNotIn("hermes_session_id", cols)
        row = self.conn.execute("SELECT runtime_session_id FROM wake_runs WHERE session='s1'").fetchone()
        self.assertEqual(row["runtime_session_id"], "hs-1")

    def test_idempotent_second_init(self):
        # 已迁移表二次 init_schema：不炸、不重复、行数不变
        before = self.conn.execute("SELECT COUNT(*) c FROM session_bindings").fetchone()["c"]
        self.conn.close()
        self.conn = db.get_connection()
        db.init_schema(self.conn)
        after = self.conn.execute("SELECT COUNT(*) c FROM session_bindings").fetchone()["c"]
        self.assertEqual(before, after)

    def test_pm_double_null_rows_do_not_crash_init(self):
        # 外部直写造成同 (project,profile) 的两条 NULL bot_id 行，
        # init_schema 回填按 profile 判重跳过多行组（保留 NULL），不撞 UNIQUE。
        conn2 = sqlite3.connect(self.dbfile)
        conn2.execute("INSERT INTO project_members(project,profile,active,bot_id) VALUES('p','a',0,NULL)")
        conn2.commit()
        conn2.close()
        self.conn.close()
        self.conn = db.get_connection()
        db.init_schema(self.conn)  # 不应抛 UNIQUE 异常
        rows = self.conn.execute(
            "SELECT bot_id FROM project_members WHERE project='p' AND profile='a'"
        ).fetchall()
        self.assertEqual(len(rows), 2, "双行都保留不丢")
        # 组内多行 → 全部跳过回填（含本来的 1 行：判重在迁移时统一处理）
        non_null = [r["bot_id"] for r in rows if r["bot_id"] is not None]
        self.assertLessEqual(len(non_null), 1, "最多一行回填，另一行保 NULL")


if __name__ == "__main__":
    unittest.main()
