"""req#76 task#129 单测：需求归档（requirements.archived + req_archive + /tasks 过滤）。

契约（req#76）：
- requirements 加 archived 列（幂等迁移，仿 projects.archived）
- req_archive 只允许 done/cancelled 归档，open/blocked 归档返回 error
- unarchive 恢复 archived=0
- GET /tasks 默认过滤 archived=1，include_archived=True 返回全部
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from engine import api, db, taskpool  # noqa: E402


class ReqArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t129_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        import sqlite3
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        db.init_schema(c)
        # 四种状态需求各一（archived 默认 0）
        for rid, status in [(1, "open"), (2, "done"), (3, "cancelled"), (4, "blocked")]:
            c.execute(
                "INSERT INTO requirements(id, project, title, status, priority, created_at, updated_at) "
                "VALUES(?, 'bot-group', ?, ?, 0, 'now', 'now')",
                (rid, f"req{rid}", status),
            )
        c.commit()
        c.close()

    def tearDown(self):
        self._patcher.stop()

    def _conn(self):
        import sqlite3
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        return c

    def _archived(self, rid):
        with self._conn() as c:
            return c.execute("SELECT archived FROM requirements WHERE id=?", (rid,)).fetchone()[0]

    def test_archive_done_ok(self):
        r = taskpool.req_archive("bot-group", 2, archived=True)
        self.assertTrue(r["archived"])
        self.assertEqual(self._archived(2), 1)

    def test_archive_cancelled_ok(self):
        r = taskpool.req_archive("bot-group", 3, archived=True)
        self.assertTrue(r["archived"])
        self.assertEqual(self._archived(3), 1)

    def test_archive_open_rejected(self):
        r = taskpool.req_archive("bot-group", 1, archived=True)
        self.assertIn("error", r)
        self.assertEqual(self._archived(1), 0)

    def test_archive_blocked_rejected(self):
        r = taskpool.req_archive("bot-group", 4, archived=True)
        self.assertIn("error", r)
        self.assertEqual(self._archived(4), 0)

    def test_unarchive_restores(self):
        taskpool.req_archive("bot-group", 2, archived=True)
        r = taskpool.req_archive("bot-group", 2, archived=False)
        self.assertFalse(r["archived"])
        self.assertEqual(self._archived(2), 0)

    def test_not_found(self):
        r = taskpool.req_archive("bot-group", 9999, archived=True)
        self.assertIn("error", r)

    def test_get_tasks_default_filters_archived(self):
        """归档 done 需求后，GET /tasks 默认不再返回它。"""
        taskpool.req_archive("bot-group", 2, archived=True)
        with patch.object(api, "_conn", side_effect=db.get_connection):
            data = api.get_tasks()
        ids = [r["id"] for r in data["requirements"]]
        self.assertNotIn(2, ids)
        self.assertIn(1, ids)  # 未归档的还在

    def test_get_tasks_include_archived(self):
        """include_archived=True 返回全部（含已归档）。"""
        taskpool.req_archive("bot-group", 2, archived=True)
        taskpool.req_archive("bot-group", 3, archived=True)
        with patch.object(api, "_conn", side_effect=db.get_connection):
            data = api.get_tasks(include_archived=True)
        ids = [r["id"] for r in data["requirements"]]
        self.assertIn(2, ids)
        self.assertIn(3, ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
