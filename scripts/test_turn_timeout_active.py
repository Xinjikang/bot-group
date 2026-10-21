"""task#87（req#50 收口判定纳入活跃状态）：超时弃权只作用于「读都没读」的 bot。

契约（req#50 第2条）：
- 超时机制只留给「读都没读」的 bot
- 已读/正在思考的 bot 不按超时弃权收口
- 判定事实源 = 心跳表（活跃 = 有心跳 pid）
"""
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from engine import db, router  # noqa: E402


class TurnTimeoutActiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t87_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        import sqlite3
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        db.init_schema(c)
        self.conn = c
        c.execute("INSERT INTO projects(name,root,created_at) VALUES('bot-group',?,'now')", (self.tmp,))
        # 两个成员
        c.execute("INSERT INTO members(profile, display_name, role) VALUES('architect', '架构师', 'architect')")
        c.execute("INSERT INTO members(profile, display_name, role) VALUES('default', '蛇蛇', 'default')")
        # 一个 open 话题，当前 turn 轮到 default，且已超时（turn_wake_at 很早）
        c.execute("INSERT INTO threads(id, project, title, creator, members, status, created_at, last_active_at, round, turn_profile, turn_parent_id, turn_wake_at) "
                  "VALUES(100, 'bot-group', '讨论', 'architect', '[\"architect\",\"default\"]', 'open', '2026-09-21T10:00:00', '2026-09-21T10:00:00', 1, 'default', NULL, '2020-01-01T00:00:00')")
        # 开场消息（thread 需要一条消息让 _next_speaker 有据可依）
        c.execute("INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id, round, depth) "
                  "VALUES(1, 'bot-group', '2026-09-21T10:00:00', 'architect', 'bot', '开个会', '[\"default\"]', 100, 0, 0)")
        c.commit()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def _turn_profile(self):
        return self.conn.execute("SELECT turn_profile FROM threads WHERE id=100").fetchone()["turn_profile"]

    def test_inactive_bot_times_out(self):
        """无心跳（真没读）→ 超时弃权，turn 推进。"""
        with patch("engine.heartbeat.pids_alive", return_value=[]):
            n = router.advance_timed_out_turns(self.conn)
        self.assertGreaterEqual(n, 1)

    def test_active_bot_not_skipped(self):
        """有心跳（正在思考）→ 不弃权，turn 保持原 profile。"""
        with patch("engine.heartbeat.pids_alive", return_value=[12345]):
            n = router.advance_timed_out_turns(self.conn)
        self.assertEqual(n, 0)
        self.assertEqual(self._turn_profile(), "default")

    def test_not_yet_timed_out(self):
        """未超时 → 不推进（无论活跃与否）。"""
        self.conn.execute("UPDATE threads SET turn_wake_at=? WHERE id=100",
                          (db.now_iso(),))
        self.conn.commit()
        with patch("engine.heartbeat.pids_alive", return_value=[]):
            n = router.advance_timed_out_turns(self.conn)
        self.assertEqual(n, 0)
        self.assertEqual(self._turn_profile(), "default")

    def test_active_bot_pids_alive_not_skipped(self):
        """pids_alive 返回活跃 pid → 不弃权。"""
        with patch("engine.heartbeat.pids_alive", return_value=[12345]):
            n = router.advance_timed_out_turns(self.conn)
        self.assertEqual(n, 0)
        self.assertEqual(self._turn_profile(), "default")

    def test_stale_heartbeat_not_active(self):
        """过期心跳（pids_alive 返回空）→ 按超时弃权。"""
        with patch("engine.heartbeat.pids_alive", return_value=[]):
            n = router.advance_timed_out_turns(self.conn)
        self.assertGreaterEqual(n, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
