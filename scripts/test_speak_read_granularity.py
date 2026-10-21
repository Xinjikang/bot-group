"""req#59 task#107 单测：发言即已读清理粒度收窄（router.route_message）。

契约（req#63 更新）：bot 实际发言时清本话题、本人发言时点以前的 pending。
发言以后的新插话保持 pending；其他话题和点对点未读保持原样。
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from engine import db, router  # noqa: E402


class SpeakReadGranularityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t107_")
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
        # open 话题，turn 轮到 default，唤醒时点 10:05:00
        c.execute(
            "INSERT INTO threads(id, project, title, creator, members, status, created_at, last_active_at, round, turn_profile, turn_parent_id, turn_wake_at) "
            "VALUES(100, 'bot-group', '讨论', 'architect', '[\"architect\",\"default\"]', 'open', "
            "'2026-09-21T10:00:00', '2026-09-21T10:05:00', 1, 'default', NULL, '2026-09-21T10:05:00')")
        # 开场消息（唤醒时点之前，default 名下 pending）
        c.execute(
            "INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id, round, depth, need_reply) "
            "VALUES(1, 'bot-group', '2026-09-21T10:00:00', 'architect', 'bot', '开个会', '[\"default\"]', 100, 0, 0, 1)")
        c.execute("INSERT INTO inbox(message_id, profile, status) VALUES(1, 'default', 'pending')")
        from scripts.project_test_support import enable_members
        enable_members(c, ('bot-group',))
        c.commit()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def _inbox_status(self, msg_id):
        return self.conn.execute(
            "SELECT status FROM inbox WHERE message_id=? AND profile='default'", (msg_id,)
        ).fetchone()

    def _speak(self, text="@architect 回你"):
        router.route_message(
            self.conn, "default", text, "bot", 100, project="bot-group", need_reply=1,
        )

    def test_speech_watermark_includes_pre_speech_interjection(self):
        """req#63：发言以前的插话归入本次已读，边界按发言时点。"""
        # user 插话：唤醒时点之后（10:06），给 default 插一条 pending
        self.conn.execute(
            "INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id, round, depth, need_reply) "
            "VALUES(2, 'bot-group', '2026-09-21T10:06:00', 'user', 'user', '补充一句', '[]', 100, 1, 0, 1)")
        self.conn.execute("INSERT INTO inbox(message_id, profile, status) VALUES(2, 'default', 'pending')")
        self.conn.commit()

        self._speak()

        # 唤醒时点之前（消息1，ts=10:00 <= 10:05）被清 seen
        self.assertEqual(self._inbox_status(1)["status"], "seen")
        # 唤醒后、发言前的消息归入本次已读。
        self.assertEqual(self._inbox_status(2)["status"], "seen")

    def test_no_interjection_clears_all(self):
        """无插话场景：唤醒时点之前的所有 pending 都被清（行为等同旧逻辑的常规路径）。"""
        self._speak()
        self.assertEqual(self._inbox_status(1)["status"], "seen")

    def test_no_turn_wake_at_falls_back_to_clear_all(self):
        """防御：turn_wake_at 为空 → 退回旧行为清全部。"""
        self.conn.execute("UPDATE threads SET turn_wake_at='' WHERE id=100")
        # 再插一条唤醒时点之后的插话，也应被清（fallback 清全部）
        self.conn.execute(
            "INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id, round, depth, need_reply) "
            "VALUES(2, 'bot-group', '2026-09-21T10:06:00', 'user', 'user', '补充一句', '[]', 100, 1, 0, 1)")
        self.conn.execute("INSERT INTO inbox(message_id, profile, status) VALUES(2, 'default', 'pending')")
        self.conn.commit()

        self._speak()
        self.assertEqual(self._inbox_status(1)["status"], "seen")
        self.assertEqual(self._inbox_status(2)["status"], "seen")


if __name__ == "__main__":
    unittest.main(verbosity=2)
