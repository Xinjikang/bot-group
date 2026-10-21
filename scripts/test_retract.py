"""task#78（req#41 work 撤回）：撤回端点单测。

契约（ENGINEERING 35 节）：
- 权限：只许 sender 本人 / user 撤回，他人 403
- 留痕：写 messages.retract_by / retract_at（标记列，绝不 UPDATE content——append-only）
- 副作用：该消息造成的 pending inbox 行作废（expired）
- 已撤回的再撤 → 400
- 消息不存在 → 404
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from engine import auth, db, api  # noqa: E402


class RetractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t78_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        import sqlite3
        self.conn = sqlite3.connect(self.dbfile)
        self.conn.row_factory = sqlite3.Row
        db.init_schema(self.conn)
        # 造一条 bot 发的点对点消息 + 一条 user 发的 + pending inbox
        self.conn.execute(
            "INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id) "
            "VALUES(1, 'bot-group', '2026-09-20T10:00:00', 'architect', 'bot', '发错的内容', '[\"default\"]', NULL)")
        self.conn.execute("INSERT INTO inbox(message_id, profile, status) VALUES(1, 'default', 'pending')")
        self.conn.execute(
            "INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id) "
            "VALUES(2, 'bot-group', '2026-09-20T10:01:00', 'user', 'user', '主人发错', '[\"architect\"]', NULL)")
        self.conn.execute("INSERT INTO inbox(message_id, profile, status) VALUES(2, 'architect', 'pending')")
        self.conn.commit()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def _retract(self, mid, requester, kind="bot"):
        from unittest.mock import patch as _p
        with _p("engine.api._resolve_identity", return_value=(requester, kind)), \
             _p("engine.api.events.bus.publish"):
            return api.post_message_retract(mid, {}, x_group_profile=requester if requester != "user" else None,
                                            x_group_key=auth.bot_token(requester), authorization=None)

    def _row(self, mid):
        return self.conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()

    def test_self_retract_success(self):
        """sender 本人撤回成功，留痕不删 content。"""
        r = self._retract(1, "architect", kind="bot")
        self.assertTrue(r["retracted"])
        row = self._row(1)
        self.assertEqual(row["retract_by"], "architect")
        self.assertIsNotNone(row["retract_at"])
        self.assertEqual(row["content"], "发错的内容")  # content 原样保留（append-only）

    def test_retract_event_self_contained(self):
        """req#51 task#94：撤回事件自包含 retract_by/retract_at（前端就地 patch 用）。"""
        from unittest.mock import patch as _p
        captured = {}
        def _capture(etype, data):
            captured["type"] = etype
            captured["data"] = data
        with _p("engine.api._resolve_identity", return_value=("architect", "bot")), \
             _p("engine.api.events.bus.publish", side_effect=_capture):
            api.post_message_retract(1, {}, x_group_profile="architect", x_group_key=auth.bot_token("architect"), authorization=None)
        self.assertEqual(captured["type"], "message.retracted")
        self.assertEqual(captured["data"]["message_id"], 1)
        self.assertEqual(captured["data"]["retract_by"], "architect")
        self.assertIsNotNone(captured["data"]["retract_at"])

    def test_user_can_retract_any(self):
        """user 可撤回任何人的消息。"""
        r = self._retract(1, "user", kind="user")
        self.assertTrue(r["retracted"])
        self.assertEqual(self._row(1)["retract_by"], "user")

    def test_other_bot_retract_403(self):
        """bot 不能撤别人的发言。"""
        with self.assertRaises(Exception) as ctx:
            self._retract(1, "reviewer-02", kind="bot")
        # HTTPException 403
        self.assertIn("403", str(ctx.exception))

    def test_retract_already_retracted_400(self):
        self._retract(1, "architect", kind="bot")
        with self.assertRaises(Exception) as ctx:
            self._retract(1, "architect", kind="bot")
        self.assertIn("400", str(ctx.exception))

    def test_retract_nonexistent_404(self):
        with self.assertRaises(Exception) as ctx:
            self._retract(9999, "architect", kind="bot")
        self.assertIn("404", str(ctx.exception))

    def test_retract_expires_pending_inbox(self):
        """撤回后该消息的 pending inbox 作废。"""
        self._retract(1, "architect", kind="bot")
        row = self.conn.execute(
            "SELECT status FROM inbox WHERE message_id=1 AND profile='default'").fetchone()
        self.assertEqual(row["status"], "expired")

    def test_retract_does_not_touch_content(self):
        """append-only 红线：content 永不被 UPDATE。"""
        before = self._row(1)["content"]
        self._retract(1, "architect", kind="bot")
        self.assertEqual(self._row(1)["content"], before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
