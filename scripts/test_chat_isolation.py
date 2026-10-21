"""Chat identity must fail closed, including concurrent topic windows."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
from engine import api, db


class ChatIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="chat_isolation_")
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(db, "db_path", return_value=str(Path(self.tmp.name) / "group.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = sqlite3.connect(db.db_path())
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        db.init_schema(self.conn)
        for tid in (100, 200):
            self.conn.execute(
                "INSERT INTO threads(id,project,title,creator,members,status,created_at,last_active_at) "
                "VALUES(?, 'bot-group', ?, 'architect', '[\"default\"]', 'open', ?, ?)",
                (tid, str(tid), db.now_iso(), db.now_iso()))
        for mid, tid in ((1, 100), (2, 100), (3, 200)):
            self.conn.execute(
                "INSERT INTO messages(id,project,ts,sender,sender_type,content,mentions,thread_id) "
                "VALUES(?, 'bot-group', ?, 'architect', 'bot', ?, '[\"default\"]', ?)",
                (mid, db.now_iso(), str(mid), tid))
            self.conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(?,'default','pending')", (mid,))
        self.conn.commit()

    def inbox(self, tid=None, kind="chat"):
        return api.get_inbox(profile="default", limit=100, kind=kind,
                             thread_id=tid, x_group_profile="default", project="bot-group")

    def window(self, tid, until="2099-01-01T00:00:00", profile="default"):
        self.conn.execute(
            "INSERT INTO wake_jobs(profile,project,kind,trigger,thread_id,status,enqueued_at,chat_context_until) "
            "VALUES(?, 'bot-group', 'chat', 'scan:group', ?, 'done', ?, ?)",
            (profile, tid, db.now_iso(), until))
        self.conn.commit()

    def assert_error(self, tid, status):
        with self.assertRaises(HTTPException) as caught:
            self.inbox(tid)
        self.assertEqual(caught.exception.status_code, status)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inbox WHERE status='pending'").fetchone()[0], 3)

    def test_exact_topic_despite_newer_other_window(self):
        self.window(100)
        self.window(200)
        self.assertEqual([m['message_id'] for m in self.inbox(100)['messages']], [1, 2])
        self.assertEqual([m['message_id'] for m in self.inbox(200)['messages']], [3])

    def test_missing_identity_with_active_window_errors(self):
        self.window(200)
        self.assert_error(None, 400)

    def test_missing_identity_without_window_errors(self):
        self.assert_error(None, 400)

    def test_missing_topic_errors(self):
        self.window(200)
        self.assert_error(999, 404)

    def test_missing_window_errors(self):
        self.window(200)
        self.assert_error(100, 409)

    def test_expired_window_errors(self):
        self.window(100, until="2020-01-01T00:00:00")
        self.window(200)
        self.assert_error(100, 409)

    def test_other_profile_window_cannot_authorize(self):
        self.window(100, profile="architect")
        self.assert_error(100, 409)

    def test_closed_or_settled_topic_errors(self):
        self.window(100)
        for status in ("closed", "settled"):
            with self.subTest(status=status):
                self.conn.execute("UPDATE threads SET status=? WHERE id=100", (status,))
                self.conn.commit()
                self.assert_error(100, 409)

    def test_valid_topic_without_unread_returns_empty(self):
        self.window(100)
        self.conn.execute("UPDATE inbox SET status='seen' WHERE message_id IN (1,2)")
        self.conn.commit()
        self.assertEqual(self.inbox(100)['messages'], [])

    def test_work_scope_unchanged(self):
        self.window(100)
        self.assertEqual(self.inbox(kind="work")['messages'], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
