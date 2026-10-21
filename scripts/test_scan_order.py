"""task#88（req#50 扫描唤醒守序）：scanner scan_inbox 的 chat 唤醒只叫 turn_profile，不越序重算。

契约（req#50 第4条）：
- 多 bot 同时思考是 bug，本应引擎按树顺序唤醒
- 扫描器 scan:group 的 chat 唤醒只叫当前 turn_profile，不越序
- turn_profile 非空 → 只唤醒该人（不重算不改写 turn）
- turn_profile 空 → 才 _next_speaker 找下一个
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from engine import db, scanner  # noqa: E402


class ScanOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t88_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        import sqlite3
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        db.init_schema(c)
        self.conn = c
        # 三个成员
        for p, dn, role in [("architect", "架构师", "architect"),
                            ("default", "蛇蛇", "default"),
                            ("reviewer-02", "周岚", "reviewer")]:
            c.execute("INSERT INTO members(profile, display_name, role) VALUES(?,?,?)", (p, dn, role))
        # 一个 open 话题，当前 turn 轮到 default（正在思考）
        c.execute("INSERT INTO threads(id, project, title, creator, members, status, created_at, last_active_at, round, turn_profile, turn_parent_id, turn_wake_at) "
                  "VALUES(100, 'bot-group', '讨论', 'architect', '[\"architect\",\"default\",\"reviewer-02\"]', 'open', '2026-09-21T10:00:00', '2026-09-21T10:00:00', 1, 'default', NULL, '2026-09-21T10:05:00')")
        # 开场消息 + default 的 pending
        c.execute("INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id, round, depth) "
                  "VALUES(1, 'bot-group', '2026-09-21T10:00:00', 'architect', 'bot', '开个会', '[\"default\",\"reviewer-02\"]', 100, 0, 0)")
        c.execute("INSERT INTO inbox(message_id, profile, status) VALUES(1, 'default', 'pending')")
        from scripts.project_test_support import enable_members
        enable_members(c, ('bot-group',))
        c.commit()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def _wake_profiles(self):
        return [r["profile"] for r in self.conn.execute(
            "SELECT profile FROM wake_jobs WHERE kind='chat' ORDER BY id").fetchall()]

    def test_turn_occupied_does_not_wake(self):
        """req#59：turn 被 default 占着 → scanner 不空转唤醒它（删掉 pending 兜底唤醒）。"""
        with patch("engine.scanner.router._next_speaker") as mock_nxt:
            scanner.scan_inbox(self.conn)
            mock_nxt.assert_not_called()  # 不越序重算
        profiles = self._wake_profiles()
        self.assertEqual(profiles, [])  # turn 被占，不再拿 pending 当理由空转唤醒
        # turn 不被改写
        tp = self.conn.execute("SELECT turn_profile FROM threads WHERE id=100").fetchone()["turn_profile"]
        self.assertEqual(tp, "default")

    def test_turn_occupied_without_pending_still_no_wake(self):
        """req#59 空转场景：turn 被占且自己 pending 已空 → 更不该唤醒（这就是空转根因）。"""
        self.conn.execute("UPDATE inbox SET status='seen' WHERE profile='default'")
        self.conn.commit()
        scanner.scan_inbox(self.conn)
        self.assertEqual(self._wake_profiles(), [])

    def test_turn_empty_computes_next(self):
        """turn 空 → 才 _next_speaker 找下一个。"""
        self.conn.execute("UPDATE threads SET turn_profile='' WHERE id=100")
        self.conn.commit()
        scanner.scan_inbox(self.conn)
        profiles = self._wake_profiles()
        self.assertEqual(len(profiles), 1)  # 唤醒一个人
        # turn 被 set
        tp = self.conn.execute("SELECT turn_profile FROM threads WHERE id=100").fetchone()["turn_profile"]
        self.assertNotEqual(tp, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
