"""req#86 task#147：pm 关联键 profile → bot_id 表重建迁移测试。

覆盖：老形态 pm 表（主键 (project,profile)）重建为主键 (project,bot_id) +
兼容列回填；孤儿行（members 无对应 profile）保留且 bot_id 置 NULL；
幂等（主键判定跳过二次重建）；生产库异构 bots 表（带 created_at）兼容。

跑法：python -B -m unittest scripts.test_pm_bot_id_migration -v
"""
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import db  # noqa: E402

SCHEMA = (ROOT / "engine" / "schema.sql").read_text(encoding="utf-8")


def _make_legacy_db():
    """老形态库：pm 主键 (project,profile)，members/bots 已有数据。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    # 造老形态 pm 表（schema.sql 建的是新形态，手动重建回老形态）
    conn.execute("DROP TABLE project_members")
    conn.execute(
        "CREATE TABLE project_members ("
        "project TEXT NOT NULL, profile TEXT NOT NULL, "
        "active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (project, profile))"
    )
    conn.execute("INSERT INTO projects(name,root,created_at) VALUES('xjk','.','now')")
    conn.execute("INSERT INTO members(profile,display_name,role) VALUES('a','A','dev')")
    conn.execute("INSERT INTO members(profile,display_name,role) VALUES('b','B','dev')")
    # 老库 bots 表已由早期实现反推过（有数据）——重建按既有 bots 行关联
    conn.execute(
        "INSERT INTO bots(host_type,profile,config_root,runtime,display_name,role) "
        "VALUES('hermes','a',?,'hermes','A','dev')", (db._hermes_root(),)
    )
    conn.execute("INSERT INTO project_members VALUES('xjk','a',1)")
    conn.execute("INSERT INTO project_members VALUES('xjk','b',0)")
    # 孤儿行：members 里无 'ghost'
    conn.execute("INSERT INTO project_members VALUES('xjk','ghost',1)")
    conn.commit()
    return conn


class PmBotIdMigrationTests(unittest.TestCase):
    def _run(self, conn):
        db._backfill_hermes_bots(conn)
        db._migrate_pm_bot_id(conn)
        conn.commit()

    def test_rebuild_changes_pk_and_backfills_bot_id(self):
        conn = _make_legacy_db()
        self.addCleanup(conn.close)
        self._run(conn)
        pk = [r["name"] for r in conn.execute("PRAGMA table_info(project_members)") if r["pk"]]
        self.assertEqual(pk, ["project", "bot_id"])
        rows = {r["profile"]: (r["active"], r["bot_id"]) for r in conn.execute("SELECT * FROM project_members")}
        self.assertEqual(rows["a"][1], 1)          # bots 既有行 id=1
        self.assertEqual(rows["b"][1], 2)          # backfill 反推后关联
        self.assertEqual(rows["a"][0], 1)
        self.assertEqual(rows["b"][0], 0)          # 停用语义保留
        self.assertIsNone(rows["ghost"][1])        # 孤儿不静默归边/丢行
        self.assertEqual(rows["ghost"][0], 1)

    def test_backfill_creates_bot_for_new_member(self):
        conn = _make_legacy_db()
        self.addCleanup(conn.close)
        self._run(conn)
        # backfill 为 members 无 bots 行的 profile 反推 Bot（b 的 Bot 由本迁移补）
        bots = {r["profile"] for r in conn.execute("SELECT profile FROM bots")}
        self.assertEqual(bots, {"a", "b"})

    def test_idempotent(self):
        conn = _make_legacy_db()
        self.addCleanup(conn.close)
        self._run(conn)
        before = [tuple(r) for r in conn.execute("SELECT * FROM project_members")]
        self._run(conn)   # 二次跑：主键已是 (project,bot_id)，跳过重建
        after = [tuple(r) for r in conn.execute("SELECT * FROM project_members")]
        self.assertEqual(before, after)

    def test_heterogeneous_bots_created_at(self):
        """真库形态（bots 带 created_at NOT NULL）：backfill 分叉写入不炸。"""
        conn = _make_legacy_db()
        self.addCleanup(conn.close)
        conn.execute("DROP TABLE bots")
        conn.execute("""
            CREATE TABLE bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                host_type TEXT NOT NULL DEFAULT 'hermes',
                profile TEXT NOT NULL,
                config_root TEXT NOT NULL,
                runtime TEXT NOT NULL DEFAULT 'hermes',
                display_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(host_type, config_root, profile)
            )
        """)
        conn.execute(
            "INSERT INTO bots(host_type,profile,config_root,runtime,display_name,role,created_at) "
            "VALUES('hermes','a',?,'hermes','A','dev','now')", (db._hermes_root(),)
        )
        conn.commit()
        self._run(conn)
        n = conn.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
        self.assertEqual(n, 2)   # a 既有 + b 反推补（created_at 由分叉写入）
        rows = {r["profile"]: r["bot_id"] for r in conn.execute("SELECT * FROM project_members")}
        self.assertIsNotNone(rows["b"])


if __name__ == "__main__":
    unittest.main(verbosity=2)