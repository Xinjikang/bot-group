"""Run with python -m unittest scripts.test_topic_boundary_regressions.

Isolated regressions for work exits, speaking rights and member transactions.
"""
import json
import sqlite3
import unittest
from unittest.mock import patch

from engine import api, db, heartbeat, router
from scripts import test_member_add_turn as fixtures


class TopicBoundaryTests(unittest.TestCase):
    setUp = fixtures.MemberAddTurnTests.setUp
    topic = fixtures.MemberAddTurnTests.topic

    def prepare(self):
        self.topic()
        self.conn.execute('UPDATE threads SET members=?,round=1 WHERE id=42',
                          ('["user","a","default"]',))
        self.conn.commit()

    def test_work_exit_preserves_live_chat_turn(self):
        self.prepare()
        with patch.object(heartbeat, '_beats', {}):
            heartbeat.beat('a', 42, 101)
            heartbeat.beat('a', None, 102)
            heartbeat.on_exit('a', pid=102, thread_id=None)
            self.assertEqual(heartbeat.pids_alive('a', 42), [101])
            row = self.conn.execute('SELECT turn_profile,turn_skipped FROM threads WHERE id=42').fetchone()
            self.assertEqual(row['turn_profile'], 'a')
            self.assertEqual(json.loads(row['turn_skipped'] or '[]'), [])
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM wake_jobs').fetchone()[0], 0)

    def test_chat_exit_advances_only_its_topic(self):
        self.prepare()
        self.conn.execute(
            "INSERT INTO threads(id,project,title,creator,members,status,created_at,last_active_at,turn_profile,round) "
            "VALUES(43,'p','other','user','[\"user\",\"a\"]','open','now','now','a',1)")
        self.conn.commit()
        with patch.object(heartbeat, '_beats', {}):
            heartbeat.beat('a', 42, 101)
            heartbeat.beat('a', 43, 103)
            heartbeat.beat('a', None, 102)
            heartbeat.on_exit('a', pid=101, thread_id=42)
            self.assertEqual(self.conn.execute('SELECT turn_profile FROM threads WHERE id=42').fetchone()[0], 'default')
            self.assertEqual(self.conn.execute('SELECT turn_profile FROM threads WHERE id=43').fetchone()[0], 'a')
            self.assertEqual(set(heartbeat.pids_alive('a')), {102, 103})

    def test_out_of_turn_content_is_rejected_before_writes(self):
        self.prepare()
        for content in ('A substantive response', 'pass', None):
            with self.subTest(content=content):
                with self.assertRaises(router.RouteError) as caught:
                    router.route_message(self.conn, 'default', content, 'bot', 42, 'p')
                self.assertEqual(caught.exception.status, 409)
                self.conn.rollback()
                self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 1)
                self.assertEqual(self.conn.execute('SELECT turn_profile FROM threads WHERE id=42').fetchone()[0], 'a')

    def test_current_speaker_and_user_interjection_still_work(self):
        self.prepare()
        user = router.route_message(self.conn, 'user', 'Additional context', 'user', 42, 'p')
        self.assertGreater(user['message_id'], 1)
        self.assertEqual(self.conn.execute('SELECT turn_profile FROM threads WHERE id=42').fetchone()[0], 'a')
        result = router.route_message(self.conn, 'a', 'My substantive response', 'bot', 42, 'p')
        self.assertGreater(result['message_id'], user['message_id'])

    def test_membership_read_and_inbox_changes_share_write_lock(self):
        self.topic()
        case = self
        checked = []

        class ObservedConnection:
            def __init__(self):
                self.real = db.get_connection()

            def execute(self, sql, *args):
                if sql.startswith('SELECT * FROM threads') or sql.startswith('UPDATE inbox SET'):
                    other = db.get_connection()
                    try:
                        other.execute('PRAGMA busy_timeout=0')
                        with case.assertRaises(sqlite3.OperationalError):
                            other.execute("UPDATE threads SET members='[\"user\"]' WHERE id=42")
                        checked.append(sql)
                    finally:
                        other.close()
                return self.real.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.real, name)

        with patch.object(api, '_conn', side_effect=ObservedConnection):
            api.post_thread_members(42, {'add': ['default'], 'remove': ['a']})
        self.assertEqual(len(checked), 2)
        self.assertEqual(json.loads(self.conn.execute('SELECT members FROM threads WHERE id=42').fetchone()[0]), ['user', 'default'])
        with patch.object(api, '_conn', side_effect=db.get_connection):
            api.post_thread_members(42, {'remove': ['default']})
        self.assertEqual(json.loads(self.conn.execute('SELECT members FROM threads WHERE id=42').fetchone()[0]), ['user'])
        self.assertEqual(self.conn.execute("SELECT status FROM inbox WHERE profile='default'").fetchone()[0], 'expired')


if __name__ == '__main__':
    unittest.main()
