"""task#86（req#50 空 sender 拒落库）：POST /msg 入口 sender 非空校验。

契约（req#50 第3条）：
- 发送消息入口校验发送人非空，空则拒绝不落库，从源头杜绝坏消息
- bot 直调漏带 X-Group-Profile 身份头 → 400 拒绝
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from pathlib import Path
_ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, _ROOT)
from engine import auth, db, api  # noqa: E402


class EmptySenderRejectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t86_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        import sqlite3
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        db.init_schema(c)
        self.conn = c
        # members 表里插 default，让 @default 能被 parse_mentions 解析到
        c.execute("INSERT INTO members(profile, display_name, role) "
                  "VALUES('default', '蛇蛇', 'default')")
        from scripts.project_test_support import enable_members
        enable_members(c, ('bot-group',))
        c.commit()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def _count_msgs(self):
        return self.conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]

    def test_dev_empty_sender_rejected(self):
        """dev 放行分支 + 空 sender → 400，不落库。"""
        with patch("engine.api._resolve_identity", return_value=("", "dev")):
            with self.assertRaises(Exception) as ctx:
                api.post_msg(body={"text": "@default 你好", "project": "bot-group", "sender": ""},
                             x_group_profile=None, authorization=None)
            self.assertIn("400", str(ctx.exception))
        self.assertEqual(self._count_msgs(), 0)  # 未落库

    def test_bot_identity_header_still_works(self):
        """bot 带 X-Group-Profile header → 正常落库，sender=identity。"""
        self.conn.execute("INSERT INTO members(profile,display_name) VALUES('architect','Architect')")
        self.conn.execute("INSERT INTO project_members(project,profile,active) VALUES('bot-group','architect',1)")
        self.conn.execute("INSERT INTO wake_runs(session,started_at,profile,project) VALUES('architect-bot-group-work',100,'architect','bot-group')")
        self.conn.commit()
        with patch("engine.api._resolve_identity", return_value=("architect", "bot")):
            r = api.post_msg(body={"text": "@default 你好", "project": "bot-group", "thread_id": None,
                                  "need_reply": False, "execution": {"session": "architect-bot-group-work", "wake_started_at": 100}},
                             x_group_profile="architect", x_group_key=auth.bot_token("architect"), authorization=None)
            self.assertIsNotNone(r.get("message_id"))
        row = self.conn.execute("SELECT sender FROM messages").fetchone()
        self.assertEqual(row["sender"], "architect")

    def test_user_identity_still_works(self):
        """user 身份 → sender='user' 落库。"""
        with patch("engine.api._resolve_identity", return_value=("user", "user")):
            r = api.post_msg(body={"text": "@default 你好", "project": "bot-group", "sender": ""},
                             x_group_profile=None, authorization="Bearer x")
            self.assertIsNotNone(r.get("message_id"))
        row = self.conn.execute("SELECT sender FROM messages").fetchone()
        self.assertEqual(row["sender"], "user")

    def test_whitespace_sender_rejected(self):
        """纯空白 sender → 400。"""
        with patch("engine.api._resolve_identity", return_value=("", "dev")):
            with self.assertRaises(Exception) as ctx:
                api.post_msg(body={"text": "hi", "project": "bot-group", "sender": "   "},
                             x_group_profile=None, authorization=None)
            self.assertIn("400", str(ctx.exception))
        self.assertEqual(self._count_msgs(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
