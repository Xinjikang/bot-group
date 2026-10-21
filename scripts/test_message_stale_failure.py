"""req#82 task#140：可靠性——整组陈旧显式失败终态（不静默丢）。

契约（docs/design/MESSAGE-INJECTION-DESIGN-2026-09-27.md：回复、确认与重复派发）：
- 现有 30min 判据作用于同一接收者/项目的最新有效 pending（scanner.py latest_by_recipient
  取 MAX 最新一条），终态化只改「整组陈旧后」的动作，不改判定粒度。
- 整组陈旧时从「静默不唤醒」改为「显式 failed 终态 + 通知」；新鲜优先清理语义原样保留
  （新鲜 @ 与陈旧并存 → 照旧唤醒，陈旧行由 bot 上线顺带清理）。
- 失败状态按 (message_id, recipient_profile) 隔离（inbox.status='failed'），原始消息保留
  （messages append-only 红线）。转 failed 后不再 pending，天然仅通知一次。
- 判定粒度 MAX（整组，非逐条）：不逐条判死「本可被新鲜 @ 顺带清理」的旧消息。
"""

import os
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from engine import db, scanner, taskpool, wake  # noqa: E402


class StaleGroupFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t140_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        import sqlite3
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        db.init_schema(c)
        self.conn = c
        for p, dn, role in [("architect", "架构师", "architect"),
                            ("default", "蛇蛇", "default"),
                            ("be-dev-01", "后端1", "be-dev")]:
            c.execute("INSERT INTO members(profile, display_name, role) VALUES(?,?,?)", (p, dn, role))
        from scripts.project_test_support import enable_members
        enable_members(c, ('bot-group',))
        taskpool._save_project_owner(c, 'bot-group', 'default')
        c.commit()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def _insert_p2p(self, mid, profile, ts):
        self.conn.execute(
            "INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id) "
            "VALUES(?, 'bot-group', ?, 'default', 'bot', 'hi', ?, NULL)",
            (mid, ts, json.dumps([profile])),
        )
        self.conn.execute("INSERT INTO inbox(message_id, profile, status) VALUES(?, ?, 'pending')", (mid, profile))
        self.conn.commit()

    def _inbox_status(self, profile):
        return [r["status"] for r in self.conn.execute(
            "SELECT status FROM inbox WHERE profile=? ORDER BY message_id", (profile,)).fetchall()]

    def test_stale_group_becomes_failed(self):
        """整组陈旧（超 30min）→ 转 failed 终态，不再 pending。"""
        self._insert_p2p(1, "be-dev-01", "2020-01-01T00:00:00")
        scanner.scan_inbox(self.conn)
        self.assertEqual(self._inbox_status("be-dev-01"), ["failed"])
        report = self.conn.execute("SELECT * FROM messages WHERE sender='engine'").fetchone()
        self.assertIsNotNone(report)
        self.assertEqual(report['sender_type'], 'engine')
        self.assertIsNone(report['need_reply'])
        self.assertIn('be-dev-01', report['content'])
        self.assertEqual(json.loads(report['mentions']), ['default'])
        self.assertEqual(self._inbox_status('default'), ['pending'])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM wake_jobs WHERE profile='default' AND kind='work' AND trigger='@msg'"
        ).fetchone()[0], 1)
        injected = wake._compose_injection(
            wake._snapshot_pending(self.conn, 'default', 'work', None, 'bot-group'), 'work')
        self.assertIn('发送者=engine（engine） 回复要求=无要求', injected)
        self.assertNotIn('无需回复', injected)

    def test_fresh_p2p_still_woken_not_failed(self):
        """30min 内 pending → 照旧唤醒，不转 failed。"""
        self._insert_p2p(1, "be-dev-01", db.now_iso())
        scanner.scan_inbox(self.conn)
        self.assertEqual(self._inbox_status("be-dev-01"), ["pending"])

    def test_mixed_stale_and_fresh_stays_pending(self):
        """MAX 判据：陈旧 + 新鲜并存 → 唤醒，整组不转 failed（新鲜优先清理）。"""
        self._insert_p2p(1, "be-dev-01", "2020-01-01T00:00:00")  # 陈旧
        self._insert_p2p(2, "be-dev-01", db.now_iso())           # 新鲜
        scanner.scan_inbox(self.conn)
        self.assertEqual(self._inbox_status("be-dev-01"), ["pending", "pending"])

    def test_failed_is_terminal_no_renotify(self):
        """转 failed 后不再 pending → 下轮扫描不重复通知（天然仅一次）。"""
        self._insert_p2p(1, "be-dev-01", "2020-01-01T00:00:00")
        scanner.scan_inbox(self.conn)
        n1 = self.conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE sender='engine'").fetchone()["n"]
        self.assertEqual(n1, 1)
        # 第二轮：failed 已非 pending，latest_by_recipient 不再命中该 profile，不重复转/通知
        scanner.scan_inbox(self.conn)
        n2 = self.conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE sender='engine'").fetchone()["n"]
        self.assertEqual(n1, n2)  # 通知消息数不再增加

    def test_failure_isolated_by_recipient(self):
        """失败按 (message_id, recipient_profile) 隔离：另一 profile 的 pending 不受影响。"""
        self._insert_p2p(1, "be-dev-01", "2020-01-01T00:00:00")  # 陈旧 → failed
        self._insert_p2p(2, "architect", "2020-01-01T00:00:00")   # 另一接收者，也陈旧 → failed
        self._insert_p2p(3, "default", db.now_iso())               # 新鲜 → 保留 pending
        scanner.scan_inbox(self.conn)
        self.assertEqual(self._inbox_status("be-dev-01"), ["failed"])
        self.assertEqual(self._inbox_status("architect"), ["failed"])
        # default 自己那条新鲜消息（message_id=3）仍 pending（通知消息会另插 inbox 行，不影响此断言）
        row = self.conn.execute(
            "SELECT status FROM inbox WHERE profile='default' AND message_id=3").fetchone()
        self.assertEqual(row["status"], "pending")

    def test_original_message_preserved(self):
        """终态化只改 inbox.status，原始消息保留（append-only 红线）。"""
        self._insert_p2p(1, "be-dev-01", "2020-01-01T00:00:00")
        scanner.scan_inbox(self.conn)
        row = self.conn.execute("SELECT content FROM messages WHERE id=1").fetchone()
        self.assertEqual(row["content"], "hi")


if __name__ == "__main__":
    unittest.main(verbosity=2)
