"""Member history and trigger-independent topic delivery using an isolated database."""
import json
import unittest
from unittest.mock import patch

from engine import api, db, queue, router, wake
from scripts.test_abnormal_exit import AbnormalExitTests


class MemberAddTurnTests(unittest.TestCase):
    setUp = AbnormalExitTests.setUp

    def topic(self):
        from scripts.project_test_support import enable_members
        enable_members(self.conn, ('p',), ('a',))
        self.conn.executemany(
            'INSERT OR IGNORE INTO members(profile,display_name,role) VALUES(?,?,?)',
            [('a', 'a', 'dev'), ('default', 'default', 'dev')])
        self.conn.execute(
            "INSERT INTO threads(id,project,title,creator,members,status,created_at,last_active_at,turn_profile) "
            "VALUES(42,'p','test','user','[\"user\",\"a\"]','open','now','now','a')")
        self.conn.execute(
            "INSERT INTO messages(id,project,ts,sender,sender_type,content,thread_id) "
            "VALUES(1,'p','now','user','user','history',42)")
        self.conn.commit()

    def test_added_member_waits_then_reads_history(self):
        self.topic()
        with patch.object(api, '_conn', side_effect=db.get_connection):
            result = api.post_thread_members(42, {'add': ['default']})
            self.assertEqual(result['wake'], [])
            self.assertEqual(self.conn.execute("SELECT status FROM inbox WHERE profile='default'").fetchone()[0], 'pending')
            api.post_thread_members(42, {'add': ['default']})
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inbox WHERE profile='default'").fetchone()[0], 1)
            router._set_turn(self.conn, 42, 'default', None, 1)
            self.conn.commit()
            wake.enqueue(self.conn, 'default', 'chat', 'scan:group', thread_id=42, project='p')
            with patch.object(wake, 'session_busy', return_value=False), patch.object(wake, 'deliver', return_value={'ok': True}):
                self.assertTrue(queue.process_one(self.conn))
            inbox = api.get_inbox('default', kind='chat', thread_id=42, x_group_profile=None)
            self.assertIn('history', json.dumps(inbox))

    def test_all_triggers_discard_wrong_turn_and_release_queue(self):
        self.topic()
        self.conn.execute('UPDATE threads SET members=? WHERE id=42', (json.dumps(['user', 'a', 'default']),))
        self.conn.commit()
        for trigger in ('thread-member-add', '@msg', 'scan:group', 'thread-open', 'future-trigger'):
            with self.subTest(trigger=trigger):
                self.assertTrue(wake.enqueue(self.conn, 'default', 'chat', trigger, thread_id=42, project='p'))
                with patch.object(wake, 'deliver') as deliver:
                    self.assertFalse(queue.process_one(self.conn))
                    deliver.assert_not_called()
                row = self.conn.execute('SELECT status,ended_at FROM wake_jobs ORDER BY id DESC LIMIT 1').fetchone()
                self.assertEqual(row['status'], 'done')
                self.assertIsNotNone(row['ended_at'])

    def test_settled_topic_add_preserves_settlement(self):
        self.topic()
        self.conn.execute("UPDATE threads SET status='settled' WHERE id=42")
        self.conn.commit()
        with patch.object(api, '_conn', side_effect=db.get_connection):
            self.assertEqual(api.post_thread_members(42, {'add': ['default']})['wake'], [])
        self.assertEqual(self.conn.execute("SELECT status FROM inbox WHERE profile='default'").fetchone()[0], 'expired')

    def test_kick_then_rejoin_reads_history(self):
        """req#72 P2：踢人（pending→expired）后重加，补历史不再被 expired 行挡住，能重新读到历史。"""
        self.topic()
        with patch.object(api, '_conn', side_effect=db.get_connection):
            # 加 default → 收到历史 pending（message 1）
            api.post_thread_members(42, {'add': ['default']})
            self.assertEqual(
                self.conn.execute("SELECT status FROM inbox WHERE profile='default' AND message_id=1").fetchone()[0],
                'pending')
            # 踢 default → pending 变 expired
            api.post_thread_members(42, {'remove': ['default']})
            self.assertEqual(
                self.conn.execute("SELECT status FROM inbox WHERE profile='default' AND message_id=1").fetchone()[0],
                'expired')
            # 重加 default → 补历史：expired 行不命中 NOT EXISTS（只排 pending/seen），重新插 pending
            api.post_thread_members(42, {'add': ['default']})
        # 重加后 default 有一条 pending 历史（不是 0），读历史不再为空
        pending_rows = self.conn.execute(
            "SELECT COUNT(*) FROM inbox WHERE profile='default' AND status='pending'").fetchone()[0]
        self.assertEqual(pending_rows, 1)
        # get_inbox 能读到历史（走 chat 投递拿到 active window 后）
        router._set_turn(self.conn, 42, 'default', None, 1)
        self.conn.commit()
        wake.enqueue(self.conn, 'default', 'chat', 'scan:group', thread_id=42, project='p')
        with patch.object(wake, 'session_busy', return_value=False), patch.object(wake, 'deliver', return_value={'ok': True}):
            self.assertTrue(queue.process_one(self.conn))
        inbox = api.get_inbox('default', kind='chat', thread_id=42, x_group_profile=None)
        self.assertIn('history', json.dumps(inbox))


if __name__ == '__main__':
    unittest.main()
