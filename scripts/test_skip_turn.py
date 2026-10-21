"""task#102（req#56 点2）：话题@ need_reply=false 自动跳过发言位（不发工具）。

话题轮次跳过契约：
- 核心收窄到 router 发言位推进逻辑：need_reply=false 的 @ 边不产生发言位，
  被 @ 者不发言也不阻塞 turn（自动跳过，不等超时），复用 _next_speaker/_set_turn/_clear_turn。
- need_reply=true（默认）的 @ 边仍产生发言位，被 @ 者必须实质回应。
- 不新增 MCP 工具。
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from engine import db, router  # noqa: E402


class NeedReplyEdgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t102_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        import sqlite3
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        db.init_schema(c)
        self.conn = c
        c.execute("INSERT INTO members(profile, display_name, role) VALUES('architect', '架构师', 'architect')")
        c.execute("INSERT INTO members(profile, display_name, role) VALUES('default', '蛇蛇', 'default')")
        c.execute("INSERT INTO members(profile, display_name, role) VALUES('reviewer-02', '周岚', 'reviewer-02')")
        # open 话题，round=1，turn 空（让 _next_speaker 走主循环 DFS）
        c.execute(
            "INSERT INTO threads(id, project, title, creator, members, status, created_at, last_active_at, round, turn_profile, turn_parent_id, turn_wake_at) "
            "VALUES(100, 'bot-group', '讨论', 'architect', '[\"architect\",\"default\",\"reviewer-02\"]', 'open', "
            "'2026-09-21T10:00:00', '2026-09-21T10:00:00', 1, '', NULL, '')")
        # 开场消息 round 0
        c.execute(
            "INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id, round, depth, need_reply) "
            "VALUES(1, 'bot-group', '2026-09-21T10:00:00', 'architect', 'bot', '开个会', '[\"default\"]', 100, 0, 0, 1)")
        # round 1 architect 独立发言 @default（need_reply 可变）
        c.execute(
            "INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id, round, depth, need_reply) "
            "VALUES(2, 'bot-group', '2026-09-21T10:01:00', 'architect', 'bot', '@default 说下', '[\"default\"]', 100, 1, 0, 1)")
        from scripts.project_test_support import enable_members
        enable_members(c, ('bot-group',))
        c.commit()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def _set_need_reply(self, msg_id, val):
        self.conn.execute("UPDATE messages SET need_reply=? WHERE id=?", (val, msg_id))
        self.conn.commit()

    def test_need_reply_false_edge_no_turn_slot(self):
        """need_reply=0 的 @ 边 → _dfs_subtree 跳过，不产生发言位。"""
        self._set_need_reply(2, 0)
        r = router._dfs_subtree(self.conn, 100, 2, 1, 0, set())
        self.assertIsNone(r)

    def test_need_reply_true_edge_produces_turn_slot(self):
        """need_reply=1 的 @ 边 → 仍产生发言位（parent_id 指向 @ 消息）。"""
        self._set_need_reply(2, 1)
        r = router._dfs_subtree(self.conn, 100, 2, 1, 0, set())
        self.assertIsNotNone(r)
        self.assertEqual(r["profile"], "default")
        self.assertEqual(r["parent_id"], 2)

    def test_next_speaker_skips_need_reply_false_edge(self):
        """need_reply=0：_next_speaker 主循环里该 @ 边不再成为发言位（不返回 parent_id=2）。"""
        # 让 architect 已独立发言（消息2 就是），default/reviewer-02 也已独立发言，
        # 这样主循环走到 architect 的 DFS 子树时才轮到 @ 边。
        self.conn.execute(
            "INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id, round, depth, need_reply) "
            "VALUES(3, 'bot-group', '2026-09-21T10:02:00', 'default', 'bot', '我表态', '[]', 100, 1, 0, 1)")
        self.conn.execute(
            "INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id, round, depth, need_reply) "
            "VALUES(4, 'bot-group', '2026-09-21T10:03:00', 'reviewer-02', 'bot', '我表态', '[]', 100, 1, 0, 1)")
        self._set_need_reply(2, 0)
        self.conn.commit()
        nxt = router._next_speaker(self.conn, 100)
        # @ 边（parent_id=2）被跳过，不会返回 default 的 @ 接话位
        if nxt is not None:
            self.assertNotEqual(nxt["parent_id"], 2)

    def test_next_speaker_keeps_need_reply_true_edge(self):
        """need_reply=1：_next_speaker 仍返回 @ 边（parent_id=2）。"""
        self.conn.execute(
            "INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id, round, depth, need_reply) "
            "VALUES(3, 'bot-group', '2026-09-21T10:02:00', 'default', 'bot', '我表态', '[]', 100, 1, 0, 1)")
        self.conn.execute(
            "INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id, round, depth, need_reply) "
            "VALUES(4, 'bot-group', '2026-09-21T10:03:00', 'reviewer-02', 'bot', '我表态', '[]', 100, 1, 0, 1)")
        self._set_need_reply(2, 1)
        self.conn.commit()
        nxt = router._next_speaker(self.conn, 100)
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt["parent_id"], 2)
        self.assertEqual(nxt["profile"], "default")


if __name__ == "__main__":
    unittest.main(verbosity=2)
