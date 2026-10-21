"""req#52 根因A：scanner scan_inbox 来源1 点对点 pending 加 30min 时效。

契约：work 唤醒仅处理点对点消息；陈旧消息阈值为 30 分钟。
- 点对点 pending 超 30min 未读 → 不再作为唤醒源（陈旧 @ 不是活弹药，不让扫描器永动机）
- 30min 内的点对点 pending → 照旧唤醒
- 话题消息（thread_id 非空）不受此时效影响

req#82 R2 演进（task#140）：「全陈旧不唤醒」改为「全陈旧 → 显式 failed 终态 + 通知」，
判定粒度沿用 MAX（整组，非逐条），新鲜优先清理语义不变。见 test_message_stale_failure.py。
"""
import os
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from engine import db, scanner, taskpool  # noqa: E402


class P2pStaleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t52_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        import sqlite3
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        db.init_schema(c)
        self.conn = c
        for p, dn, role in [("architect", "架构师", "architect"),
                            ("default", "蛇蛇", "default")]:
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

    def _wake_work(self):
        return [r["profile"] for r in self.conn.execute(
            "SELECT profile FROM wake_jobs WHERE kind='work' ORDER BY id").fetchall()]

    def test_stale_p2p_not_woken(self):
        """点对点 pending 全陈旧（超 30min）→ 不唤醒接收者本人，转 failed 终态。"""
        self._insert_p2p(1, "architect", "2020-01-01T00:00:00")  # 很久以前
        scanner.scan_inbox(self.conn)
        # 陈旧接收者 architect 本人不被唤醒（其 pending 已转 failed，不再是唤醒源）
        self.assertNotIn("architect", self._wake_work())
        # req#82 R2：整组陈旧转显式 failed 终态（不再静默丢）
        self.assertEqual(self.conn.execute(
            "SELECT status FROM inbox WHERE profile='architect'").fetchone()["status"], "failed")

    def test_fresh_p2p_woken(self):
        """点对点 pending 30min 内 → 照旧唤醒。"""
        self._insert_p2p(1, "architect", db.now_iso())  # 刚刚
        scanner.scan_inbox(self.conn)
        self.assertEqual(self._wake_work(), ["architect"])

    def test_mixed_stale_and_fresh_woken(self):
        """陈旧消息与新消息并存时，仍须唤醒处理有效的新消息。"""
        self._insert_p2p(1, "architect", "2020-01-01T00:00:00")  # 陈旧 40min+
        self._insert_p2p(2, "architect", db.now_iso())           # 新鲜 1min
        scanner.scan_inbox(self.conn)
        self.assertEqual(self._wake_work(), ["architect"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
