"""task#69（req#37）：/inbox/seen 窄闸门单测。

契约（req#37 三方定稿）：
- 只拦「thread_id IS NULL 且 profile 在被标消息的 mentions 里」的点对点消息
- 放行条件：该 profile 在同项目 @ 消息之后（消息 ID 严格大于）发过
  thread_id IS NULL + sender=profile + mentions 含原发送者 的落库消息
- 否则拒绝标已读、保持 pending（返回 blocked 列表）
- bot 话题未读由实际发言清理
- 非被 @ 的消息不进闸
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from engine import db, api  # noqa: E402


class InboxSeenGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t69_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        import sqlite3
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        db.init_schema(c)
        self.conn = c
        # 造一个被 @ 的点对点消息（user @ architect）
        c.execute(
            "INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id) "
            "VALUES(1, 'bot-group', '2026-09-20T10:00:00', 'user', 'user', '@architect 帮看下', '[\"architect\"]', NULL)")
        # 该消息的 inbox 行（architect 待看）
        c.execute("INSERT INTO inbox(message_id, profile, status) VALUES(1, 'architect', 'pending')")
        c.commit()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def _seen(self, profile, mids, kind="bot"):
        # 直调 api.post_inbox_seen（bot 身份用 x_group_profile）
        return api.post_inbox_seen(
            body={"profile": profile, "message_ids": mids},
            x_group_profile=profile if kind == "bot" else None,
            x_group_key=api.auth.bot_token(profile),
            authorization=None,
        )

    def test_blocked_when_no_reply(self):
        """未回复 → 拒绝标已读，保持 pending。"""
        r = self._seen("architect", [1])
        self.assertEqual(r["seen"], 0)
        self.assertEqual(r["blocked"], [1])
        row = self.conn.execute("SELECT status FROM inbox WHERE message_id=1").fetchone()
        self.assertEqual(row["status"], "pending")

    def test_allowed_after_reply(self):
        """@ 消息之后发过提及原发送者的回复 → 放行。"""
        self.conn.execute(
            "INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id) "
            "VALUES(2, 'bot-group', '2026-09-20T10:05:00', 'architect', 'bot', '@user 看了', '[\"user\"]', NULL)")
        self.conn.commit()
        r = self._seen("architect", [1])
        self.assertEqual(r["seen"], 1)
        self.assertNotIn("blocked", r)
        row = self.conn.execute("SELECT status FROM inbox WHERE message_id=1").fetchone()
        self.assertEqual(row["status"], "seen")

    def test_reply_before_mentions_not_allowed(self):
        """回复 ts 早于 @ 消息 → 不算实质回复，仍拦。"""
        self.conn.execute(
            "INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id) "
            "VALUES(0, 'bot-group', '2026-09-20T09:00:00', 'architect', 'bot', '@user 早回了', '[\"user\"]', NULL)")
        self.conn.commit()
        r = self._seen("architect", [1])
        self.assertEqual(r["blocked"], [1])

    def test_same_second_reply_allowed(self):
        self.conn.execute(
            "INSERT INTO messages(project,ts,sender,sender_type,content,mentions) "
            "VALUES('bot-group','2026-09-20T10:00:00','architect','bot','reply','[\"user\"]')")
        self.conn.commit()
        self.assertEqual(self._seen('architect', [1]), {'seen': 1})

    def test_clock_rollback_does_not_hide_later_reply(self):
        self.conn.execute(
            "INSERT INTO messages(project,ts,sender,sender_type,content,mentions) "
            "VALUES('bot-group','2026-09-20T09:00:00','architect','bot','reply','[\"user\"]')")
        self.conn.commit()
        self.assertEqual(self._seen('architect', [1]), {'seen': 1})

    def test_other_project_reply_does_not_clear_original(self):
        self.conn.execute(
            "INSERT INTO messages(id,project,ts,sender,sender_type,content,mentions) "
            "VALUES(2,'other','2026-09-20T10:00:00','user','user','request','[\"architect\"]')")
        self.conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(2,'architect','pending')")
        self.conn.execute(
            "INSERT INTO messages(project,ts,sender,sender_type,content,mentions) "
            "VALUES('other','2026-09-20T10:05:00','architect','bot','reply','[\"user\"]')")
        self.conn.commit()
        result = self._seen('architect', [1, 2])
        self.assertEqual(result['seen'], 1)
        self.assertEqual(result['blocked'], [1])
        self.assertEqual([r[0] for r in self.conn.execute('SELECT status FROM inbox ORDER BY message_id')], ['pending','seen'])

    def test_older_id_with_later_timestamp_still_blocked(self):
        self.conn.execute(
            "INSERT INTO messages(id,project,ts,sender,sender_type,content,mentions) "
            "VALUES(0,'bot-group','2026-09-20T11:00:00','architect','bot','old reply','[\"user\"]')")
        self.conn.commit()
        self.assertEqual(self._seen('architect', [1])['blocked'], [1])

    def test_reply_without_mentioning_sender_blocked(self):
        """回复了但没 @ 原发送者 → 不算实质回复。"""
        self.conn.execute(
            "INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id) "
            "VALUES(2, 'bot-group', '2026-09-20T10:05:00', 'architect', 'bot', '看到了', '[\"reviewer-02\"]', NULL)")
        self.conn.commit()
        r = self._seen("architect", [1])
        self.assertEqual(r["blocked"], [1])

    def test_thread_message_requires_speech(self):
        """req#63：bot 话题未读由实际发言清理。"""
        self.conn.execute(
            "INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id) "
            "VALUES(3, 'bot-group', '2026-09-20T10:00:00', 'user', 'user', '@architect 话题里', '[\"architect\"]', 100)")
        self.conn.execute("INSERT INTO inbox(message_id, profile, status) VALUES(3, 'architect', 'pending')")
        self.conn.commit()
        r = self._seen("architect", [3])
        self.assertEqual(r["seen"], 0)
        self.assertEqual(r["blocked"], [3])

    def test_not_mentioned_not_gated(self):
        """消息没 @ 该 profile → 不进闸（照旧放行）。"""
        self.conn.execute(
            "INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id) "
            "VALUES(4, 'bot-group', '2026-09-20T10:00:00', 'user', 'user', '@reviewer-02 看下', '[\"reviewer-02\"]', NULL)")
        self.conn.execute("INSERT INTO inbox(message_id, profile, status) VALUES(4, 'architect', 'pending')")
        self.conn.commit()
        r = self._seen("architect", [4])
        self.assertEqual(r["seen"], 1)

    def test_mentions_json_array_exact(self):
        """mentions 精确匹配（user 名不误匹配 reviewer 前缀等）。"""
        # 原发送者 user；回复 mentions 用 json_each 精确匹配
        self.conn.execute(
            "INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id) "
            "VALUES(5, 'bot-group', '2026-09-20T10:05:00', 'architect', 'bot', '@user 精确', '[\"user\"]', NULL)")
        self.conn.commit()
        r = self._seen("architect", [1])
        self.assertEqual(r["seen"], 1)

    def test_user_mentioned_not_gated(self):
        """user 被 @ 且尚未回复时允许标记已读；回复门禁仅约束 Bot。"""
        self.conn.execute(
            "INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id) "
            "VALUES(6, 'bot-group', '2026-09-20T10:00:00', 'architect', 'bot', '@user 确认下', '[\"user\"]', NULL)")
        self.conn.execute("INSERT INTO inbox(message_id, profile, status) VALUES(6, 'user', 'pending')")
        self.conn.commit()
        r = self._seen("user", [6], kind="user")
        self.assertEqual(r["seen"], 1)
        self.assertNotIn("blocked", r)

    def test_need_reply_zero_exempts(self):
        """req#55：原发送者声明 need_reply=0 → 静默标已读，豁免窄闸门。"""
        self.conn.execute(
            "INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id, need_reply) "
            "VALUES(7, 'bot-group', '2026-09-20T10:00:00', 'reviewer-02', 'bot', '@architect 此后不用回', '[\"architect\"]', NULL, 0)")
        self.conn.execute("INSERT INTO inbox(message_id, profile, status) VALUES(7, 'architect', 'pending')")
        self.conn.commit()
        r = self._seen("architect", [7])
        self.assertEqual(r["seen"], 1)
        self.assertNotIn("blocked", r)
        row = self.conn.execute("SELECT status FROM inbox WHERE message_id=7").fetchone()
        self.assertEqual(row["status"], "seen")

    def test_need_reply_default_one_still_gated(self):
        """req#55：need_reply 缺省=1（必须回复）→ 窄闸门照旧拦截。"""
        self.conn.execute(
            "INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id) "
            "VALUES(8, 'bot-group', '2026-09-20T10:00:00', 'reviewer-02', 'bot', '@architect 回一下', '[\"architect\"]', NULL)")
        self.conn.execute("INSERT INTO inbox(message_id, profile, status) VALUES(8, 'architect', 'pending')")
        self.conn.commit()
        r = self._seen("architect", [8])
        self.assertEqual(r["blocked"], [8])


if __name__ == "__main__":
    unittest.main(verbosity=2)
