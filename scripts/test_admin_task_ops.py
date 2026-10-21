"""task#60：管理员手动 block/resume/cancel 单测（taskpool.admin_task_* + API 鉴权）。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from engine import db, taskpool  # noqa: E402


class AdminTaskOpsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t60_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        import sqlite3
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        db.init_schema(c)
        # 造数据：一个需求 + 三个任务（pending / in_progress / blocked）
        c.execute("INSERT INTO requirements(id, project, title, status, priority, created_at, updated_at) "
                  "VALUES(900, 'bot-group', 'T', 'open', 1, 'now', 'now')")
        c.execute("INSERT INTO tasks(id, project, requirement_id, title, status, next_bot, claimed_by, block_kind, priority, created_at) "
                  "VALUES(601,'bot-group',900,'t-pending','pending','be-dev-01','','',1,'now')")
        c.execute("INSERT INTO tasks(id, project, requirement_id, title, status, next_bot, claimed_by, block_kind, priority, created_at) "
                  "VALUES(602,'bot-group',900,'t-inprog','in_progress','','be-dev-01','','1','now')")
        c.execute("INSERT INTO tasks(id, project, requirement_id, title, status, next_bot, claimed_by, block_kind, priority, created_at) "
                  "VALUES(603,'bot-group',900,'t-blocked','blocked','be-dev-02','','technical','1','now')")
        c.execute("INSERT INTO tasks(id, project, requirement_id, title, status, next_bot, claimed_by, block_kind, priority, created_at) "
                  "VALUES(604,'bot-group',900,'t-done','done','','','','1','now')")
        from scripts.project_test_support import enable_members
        enable_members(c, ('bot-group',), ('be-dev-01', 'be-dev-02'))
        c.commit()
        c.close()

    def tearDown(self):
        self._patcher.stop()

    def _conn(self):
        import sqlite3
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        return c

    def test_block_pending(self):
        r = taskpool.admin_task_block("bot-group", 601, "主人手动")
        self.assertTrue(r["blocked"])
        self.assertNotIn("stop_profile", r)  # pending 无人在干
        with self._conn() as c:
            self.assertEqual(c.execute("SELECT status, block_kind FROM tasks WHERE id=601").fetchone()[0], "blocked")

    def test_block_in_progress_returns_stop_profile(self):
        """🔴核心：block 在干的活 → 返回领用者供 API 层停 bot。"""
        r = taskpool.admin_task_block("bot-group", 602)
        self.assertTrue(r["blocked"])
        self.assertEqual(r["stop_profile"], "be-dev-01")

    def test_block_done_rejected(self):
        r = taskpool.admin_task_block("bot-group", 604)
        self.assertIn("error", r)

    def test_resume_blocked(self):
        # 造数据时没插 members，resume 会因 next_bot 校验失败——先给 bot 记录
        c = self._conn()
        c.execute("INSERT OR REPLACE INTO members(profile, display_name, role) VALUES('be-dev-02','陆远','be-dev')")
        c.commit()
        c.close()
        r = taskpool.admin_task_resume("bot-group", 603)
        self.assertTrue(r["resumed"])
        self.assertEqual(r["next_bot"], "be-dev-02")
        with self._conn() as c:
            row = c.execute("SELECT status, block_kind, next_bot FROM tasks WHERE id=603").fetchone()
            self.assertEqual(row[0], "pending")
            self.assertEqual(row[1], "")
            self.assertEqual(row[2], "be-dev-02")

    def test_resume_non_blocked_rejected(self):
        r = taskpool.admin_task_resume("bot-group", 601)
        self.assertIn("error", r)

    def test_cancel_in_progress_returns_stop_profile(self):
        r = taskpool.admin_task_cancel("bot-group", 602)
        self.assertTrue(r["cancelled"])
        self.assertEqual(r["stop_profile"], "be-dev-01")
        with self._conn() as c:
            row = c.execute("SELECT status, next_bot, claimed_by FROM tasks WHERE id=602").fetchone()
            self.assertEqual(row[0], "cancelled")
            self.assertEqual(row[1], "")
            self.assertEqual(row[2], "")

    def test_cancel_done_rejected(self):
        r = taskpool.admin_task_cancel("bot-group", 604)
        self.assertIn("error", r)

    def test_not_found(self):
        r = taskpool.admin_task_block("bot-group", 9999)
        self.assertIn("error", r)

    def test_log_event_written(self):
        taskpool.admin_task_block("bot-group", 601, "主人的理由")
        with self._conn() as c:
            row = c.execute(
                "SELECT actor, event_type, detail FROM events WHERE task_id=601 AND event_type='block'").fetchone()
            self.assertEqual(row[0], "user")
            self.assertIn("主人的理由", row[2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
