"""数据模型迁移测试：全局 active → project_members（req#27 task#56，规格 ENGINEERING 28.1）。

覆盖：老库迁移（补数据 + 重建表删列）、幂等（二次跑不变）、全新库（无 active 列跳过）、
迁移后语义（无记录=启用）。

跑法：python -B -m unittest scripts.test_project_members_migration -v
"""
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import db  # noqa: E402


def _make_old_db():
    """构造一个「老库」：members 带 active 列、有项目、有停用数据。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE members (
            profile TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE projects (
            name TEXT PRIMARY KEY,
            root TEXT NOT NULL,
            worktree_root TEXT DEFAULT '',
            description TEXT DEFAULT '',
            archived INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        INSERT INTO members VALUES('a','A','dev',1);
        INSERT INTO members VALUES('b','B','dev',0);
        INSERT INTO members VALUES('c','C','dev',0);
        INSERT INTO members VALUES('user','主人','',1);
        INSERT INTO projects VALUES('xjk','E:/xjk','', '', 0, 'now');
        INSERT INTO projects VALUES('cicp','E:/cicp','', '', 0, 'now');
        INSERT INTO projects VALUES('old','E:/old','', '', 1, 'now');  -- 归档
        """
    )
    return conn


class ProjectMembersMigrationTests(unittest.TestCase):
    def setUp(self):
        self.conn = _make_old_db()
        self.addCleanup(self.conn.close)

    def _run_migration(self):
        db._migrate_project_members(self.conn)
        self.conn.commit()

    def _cols(self):
        return {r["name"] for r in self.conn.execute("PRAGMA table_info(members)")}

    def _pm(self):
        return {
            (r["project"], r["profile"]): r["active"]
            for r in self.conn.execute("SELECT * FROM project_members")
        }

    def _ensure_pm_table(self):
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS project_members ("
            "project TEXT NOT NULL, profile TEXT NOT NULL, "
            "active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (project, profile))"
        )
        self.conn.commit()

    # 迁移前先建 project_members 表（模拟 schema.sql 已建）——统一在 _run_migration 里做
    def _run(self):
        self._ensure_pm_table()
        self._run_migration()

    def test_disabled_users_are_expanded_to_all_active_projects(self):
        self._run()
        pm = self._pm()
        # b、c 停用 → 每个非归档项目一条 active=0；归档项目 old 不插
        for proj in ("xjk", "cicp"):
            self.assertEqual(pm[(proj, "b")], 0)
            self.assertEqual(pm[(proj, "c")], 0)
        self.assertNotIn(("old", "b"), pm)
        self.assertNotIn(("old", "c"), pm)

    def test_enabled_users_get_no_rows(self):
        self._run()
        pm = self._pm()
        self.assertNotIn(("xjk", "a"), pm)     # active=1 → 不插，靠默认启用
        self.assertNotIn(("xjk", "user"), pm)  # user 不参与

    def test_active_column_is_removed(self):
        """消费点全部切完（_CONSUMERS_MIGRATED=True，task#57 已开闸）：删列。"""
        self._run()
        self.assertNotIn("active", self._cols())
        self.assertEqual(self._cols(), {"profile", "display_name", "role"})

    def test_member_rows_preserved(self):
        self._run()
        rows = {
            r["profile"]: r["display_name"]
            for r in self.conn.execute("SELECT * FROM members")
        }
        self.assertEqual(rows, {"a": "A", "b": "B", "c": "C", "user": "主人"})

    def test_migration_is_idempotent(self):
        self._run()
        pm_before = self._pm()
        self._run()                      # 二次跑
        self._run()                      # 三次跑
        self.assertEqual(self._pm(), pm_before)
        self.assertNotIn("active", self._cols())   # 已开闸：删列生效

    def test_delete_gate_open_removes_column_idempotently(self):
        """开闸后（消费点切完，task#57 已置 True）：删列生效且幂等。"""
        self._run()
        self._run()
        self.assertNotIn("active", self._cols())
        self.assertEqual(len(self._pm()), 4)   # b/c × xjk/cicp

    def test_idempotent_on_fresh_schema(self):
        """全新库（members 无 active 列）跑迁移 = 无事发生且不报错。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript((ROOT / "engine" / "schema.sql").read_text(encoding="utf-8"))
        conn.execute("INSERT INTO members(profile, display_name, role) VALUES('x','X','dev')")
        conn.commit()
        db._migrate_project_members(conn)   # 不应抛错
        self.assertNotIn("active", {r["name"] for r in conn.execute("PRAGMA table_info(members)")})
        conn.close()

    def test_init_schema_migrates_old_db(self):
        """真实路径：老库直接走 init_schema 全流程（建表+迁移）后语义正确。"""
        import os
        from unittest.mock import patch
        old = _make_old_db()
        self._ensure_pm_table()  # schema.sql 的 project_members 表
        # 把老库写到临时文件，patch db_path 让 init_schema 作用于它
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="migrate-test-"))
        dbfile = tmp / "group.db"
        old.commit()
        old.close()
        # 内存库已 close，用文件方式重造：直接把内存库内容复制太绕，改用文件库重来
        conn = sqlite3.connect(dbfile)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            """
            CREATE TABLE members (profile TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE projects (name TEXT PRIMARY KEY, root TEXT NOT NULL,
                worktree_root TEXT DEFAULT '', description TEXT DEFAULT '',
                archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
            INSERT INTO members VALUES('b','B','dev',0);
            INSERT INTO projects VALUES('xjk','E:/xjk','', '', 0, 'now');
            """
        )
        conn.commit()
        with patch.object(db, "db_path", return_value=str(dbfile)):
            c2 = db.get_connection()
            db.init_schema(c2)
            cols = {r["name"] for r in c2.execute("PRAGMA table_info(members)")}
            self.assertNotIn("active", cols)   # task#57 已开闸：迁移直接删列
            pm = {(r["project"], r["profile"]): r["active"]
                  for r in c2.execute("SELECT * FROM project_members")}
            self.assertEqual(pm.get(("xjk", "b")), 0)
            # 迁移后新语义：members 无 active 列；无记录 = 启用
            c2.execute("INSERT INTO members(profile, display_name, role) VALUES('n','N','dev')")
            c2.commit()
            db.init_schema(c2)   # 再跑一遍不炸
            c2.close()
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)