"""发送意图保留、self-mention 删除，以及历史 nullable 迁移回归。

契约（req#61 / 话题 10063 定稿）：
- route_message 保留发送方 need_reply，接收者投递层统一计算有效值。
- @ 了自己的把 self-mention 从 mentions 里删掉再落库（user 除外）
- messages.need_reply 去 NOT NULL（三态 NULL/0/1），表重建迁移：
  - 老库（NOT NULL）→ 重建后可空、数据保留、外键仍生效、id 序列不冲突
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from engine import db, router  # noqa: E402


def _mk_thread(c, tid=100):
    c.execute(
        "INSERT INTO threads(id, project, title, creator, members, status, created_at, last_active_at, round, turn_profile, turn_parent_id, turn_wake_at) "
        "VALUES(?, 'bot-group', '讨论', 'architect', '[\"architect\",\"default\",\"reviewer-02\"]', 'open', "
        "'2026-09-22T10:00:00', '2026-09-22T10:00:00', 1, 'default', NULL, '')",
        (tid,),
    )


class NeedReplyNullableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t109_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        db.init_schema(c)
        self.conn = c
        for p, dn, role in [("architect", "架构师", "architect"),
                            ("default", "蛇蛇", "default"),
                            ("reviewer-02", "周岚", "reviewer")]:
            c.execute("INSERT INTO members(profile, display_name, role) VALUES(?,?,?)", (p, dn, role))
        from scripts.project_test_support import enable_members
        enable_members(c, ('bot-group',))
        c.commit()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def _last_msg(self):
        return self.conn.execute(
            "SELECT mentions, need_reply FROM messages ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def test_no_mention_topic_preserves_zero(self):
        """无 @ 时也保存发送方原值。"""
        _mk_thread(self.conn)
        router.route_message(
            self.conn, "default", "独立发言表态", "bot", 100,
            project="bot-group", need_reply=0,
        )
        row = self._last_msg()
        self.assertEqual(row["need_reply"], 0)

    def test_no_mention_topic_preserves_one(self):
        """原值与投递给接收者的有效值分开。"""
        _mk_thread(self.conn)
        router.route_message(
            self.conn, "default", "轮到我了", "bot", 100,
            project="bot-group", need_reply=1,
        )
        self.assertEqual(self._last_msg()["need_reply"], 1)

    def test_mention_keeps_bot_value_one(self):
        """有 @ 消息：落 bot 传的 1。"""
        _mk_thread(self.conn)
        router.route_message(
            self.conn, "default", "@architect 请审", "bot", 100,
            project="bot-group", need_reply=1,
        )
        self.assertEqual(self._last_msg()["need_reply"], 1)

    def test_mention_keeps_bot_value_zero(self):
        """有 @ 消息：落 bot 传的 0。"""
        _mk_thread(self.conn)
        router.route_message(
            self.conn, "default", "@architect 收到不用回", "bot", 100,
            project="bot-group", need_reply=0,
        )
        self.assertEqual(self._last_msg()["need_reply"], 0)

    def test_self_mention_removed_from_mentions(self):
        """@ 了自己 + 别人 → mentions 落库删掉 self，need_reply 按删后（非空）落传值。"""
        _mk_thread(self.conn)
        router.route_message(
            self.conn, "default", "@default @architect 一起看", "bot", 100,
            project="bot-group", need_reply=1,
        )
        row = self._last_msg()
        self.assertEqual(json.loads(row["mentions"]), ["architect"])
        self.assertEqual(row["need_reply"], 1)

    def test_pure_self_mention_keeps_raw_requirement(self):
        """只 @ 自己清除 mentions，保存原始要求。"""
        _mk_thread(self.conn)
        router.route_message(
            self.conn, "default", "@default 备忘", "bot", 100,
            project="bot-group", need_reply=1,
        )
        row = self._last_msg()
        self.assertEqual(json.loads(row["mentions"]), [])
        self.assertEqual(row["need_reply"], 1)


class NeedReplyMigrationTests(unittest.TestCase):
    """表重建迁移：老库（NOT NULL）→ 可空，数据/外键/id 序列全保留。"""

    _LEGACY_COLS = [
        ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        ("project", "TEXT NOT NULL DEFAULT 'xjk'"),
        ("ts", "TEXT NOT NULL"),
        ("sender", "TEXT NOT NULL"),
        ("sender_type", "TEXT NOT NULL"),
        ("content", "TEXT NOT NULL"),
        ("mentions", "TEXT NOT NULL DEFAULT '[]'"),
        ("thread_id", "INTEGER"),
        ("parent_id", "INTEGER"),
        ("round", "INTEGER NOT NULL DEFAULT 0"),
        ("depth", "INTEGER NOT NULL DEFAULT 0"),
        ("retract_by", "TEXT"),
        ("retract_at", "TEXT"),
        ("need_reply", "INTEGER NOT NULL DEFAULT 1"),  # 老库 NOT NULL
    ]

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t109m_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        col_sql = ", ".join(f"{n} {ddl}" for n, ddl in self._LEGACY_COLS)
        c.execute(f"CREATE TABLE messages ({col_sql})")
        c.execute("CREATE TABLE inbox(message_id INTEGER REFERENCES messages(id), profile TEXT)")
        c.execute("INSERT INTO messages(ts, sender, sender_type, content, mentions, need_reply) "
                  "VALUES('2026-09-22T10:00:00', 'architect', 'bot', '开个会', '[\"default\"]', 1)")
        c.execute("INSERT INTO messages(ts, sender, sender_type, content, mentions, need_reply) "
                  "VALUES('2026-09-22T10:01:00', 'default', 'bot', '收到不用回', '[]', 0)")
        c.execute("INSERT INTO inbox(message_id, profile) VALUES(1, 'default')")
        c.commit()
        self.conn = c

    def tearDown(self):
        self.conn.close()

    def _notnull(self):
        cols = {r["name"]: r for r in self.conn.execute("PRAGMA table_info(messages)").fetchall()}
        return cols["need_reply"]["notnull"]

    def test_migration_makes_need_reply_nullable(self):
        """老库 NOT NULL → 迁移后 notnull=0。"""
        self.assertEqual(self._notnull(), 1)  # 前置：确实是老库 NOT NULL
        db._migrate_messages_need_reply_nullable(self.conn)
        self.assertEqual(self._notnull(), 0)

    def test_migration_preserves_data_and_null_insert(self):
        """数据保留 + 可插 null + id 序列不冲突。"""
        db._migrate_messages_need_reply_nullable(self.conn)
        rows = self.conn.execute("SELECT id, need_reply FROM messages ORDER BY id").fetchall()
        self.assertEqual([r["id"] for r in rows], [1, 2])
        self.assertEqual([r["need_reply"] for r in rows], [1, 0])
        # 可插 null
        self.conn.execute(
            "INSERT INTO messages(ts, sender, sender_type, content, mentions, need_reply) "
            "VALUES('2026-09-22T10:02:00', 'default', 'bot', '无@发言', '[]', NULL)")
        self.conn.commit()
        new_id = self.conn.execute("SELECT MAX(id) FROM messages").fetchone()[0]
        self.assertEqual(new_id, 3)  # 序列不冲突
        self.assertIsNone(self.conn.execute(
            "SELECT need_reply FROM messages WHERE id=3").fetchone()[0])

    def test_migration_preserves_foreign_key(self):
        """外键仍生效：插入无效 message_id 被拦。"""
        db._migrate_messages_need_reply_nullable(self.conn)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO inbox(message_id, profile) VALUES(999, 'x')")
            self.conn.commit()

    def test_migration_idempotent(self):
        """幂等：已可空后再跑不报错、数据不变。"""
        db._migrate_messages_need_reply_nullable(self.conn)
        db._migrate_messages_need_reply_nullable(self.conn)
        rows = self.conn.execute("SELECT id FROM messages ORDER BY id").fetchall()
        self.assertEqual([r["id"] for r in rows], [1, 2])

    def test_preserves_indexes_triggers_and_sequence_high_water(self):
        self.conn.execute("CREATE INDEX custom_messages_ts ON messages(ts)")
        self.conn.execute("CREATE TABLE audit(id INTEGER)")
        self.conn.execute("CREATE TRIGGER audit_message AFTER INSERT ON messages BEGIN INSERT INTO audit VALUES(new.id); END")
        self.conn.execute("UPDATE sqlite_sequence SET seq=100 WHERE name='messages'")
        self.conn.commit()
        db._migrate_messages_need_reply_nullable(self.conn)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='custom_messages_ts'").fetchone())
        self.conn.execute("INSERT INTO messages(ts,sender,sender_type,content) VALUES('now','bot','bot','test')")
        self.assertEqual(self.conn.execute("SELECT id FROM audit").fetchone()[0], 101)

    def test_failed_rebuild_rolls_back_and_restores_foreign_keys(self):
        def authorizer(action, arg1, arg2, database, source):
            if action == sqlite3.SQLITE_DROP_TABLE and arg1 == 'messages':
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        self.conn.set_authorizer(authorizer)
        with self.assertRaises(sqlite3.DatabaseError):
            db._migrate_messages_need_reply_nullable(self.conn)
        self.conn.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        self.assertEqual(self._notnull(), 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 2)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='messages_new'").fetchone())


if __name__ == "__main__":
    unittest.main(verbosity=2)
