"""req#63: topic/member isolation, user read bounds and bot speech semantics."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from engine import api, db, router, scanner


class TopicUnreadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patcher = patch.object(db, 'db_path', return_value=os.path.join(self.tmp.name, 'group.db'))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        db.init_schema(self.conn)
        for profile in ('user', 'bot', 'other'):
            self.conn.execute('INSERT INTO members(profile,display_name,role) VALUES(?,?,?)',
                              (profile, profile, profile))
        for tid in (1, 2):
            self.conn.execute(
                "INSERT INTO threads(id,project,title,creator,members,status,created_at,last_active_at,turn_wake_at) "
                "VALUES(?,'p','topic','user',?,'open','old','old','2000-01-01T00:00:00')",
                (tid, json.dumps(['user', 'bot', 'other'])))
        self.add_message(1, 1)
        self.add_message(2, 2)
        self.add_message(3, None)
        self.conn.commit()

    def add_message(self, mid, tid, need_reply=1):
        self.conn.execute(
            "INSERT INTO messages(id,project,ts,sender,sender_type,content,mentions,thread_id,need_reply) "
            "VALUES(?,'p',?,'other','bot','hello','[\"bot\"]',?,?)", (mid, db.now_iso(), tid, need_reply))
        for profile in ('user', 'bot'):
            self.conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(?,?,'pending')", (mid, profile))

    def status(self, mid, profile):
        return self.conn.execute('SELECT status FROM inbox WHERE message_id=? AND profile=?', (mid, profile)).fetchone()[0]

    def seen(self, body, identity=('', 'dev')):
        with patch.object(api, '_resolve_identity', return_value=identity):
            return api.post_inbox_seen(body, None, None)

    def test_summary_isolated_by_topic_and_profile(self):
        r = api.get_inbox_summary()
        self.assertEqual({(x['thread_id'], x['profile']): x['pending'] for x in r['threads']},
                         {(1,'user'):1, (1,'bot'):1, (2,'user'):1, (2,'bot'):1})
        self.assertEqual({x['profile']:x['pending'] for x in r['summary']}, {'user':3,'bot':3})

    def test_user_open_clears_only_own_topic(self):
        self.assertEqual(self.seen({'profile':'user','thread_id':1})['seen'], 1)
        self.assertEqual(self.status(1,'user'), 'seen')
        for mid,profile in ((1,'bot'),(2,'user'),(3,'user')):
            self.assertEqual(self.status(mid,profile), 'pending')
        self.assertEqual(self.seen({'profile':'user','thread_id':1})['seen'], 0)

    def test_rendered_bound_preserves_new_arrival(self):
        self.add_message(4, 1)
        self.conn.commit()
        self.seen({'profile':'user','thread_id':1,'through_id':1})
        self.assertEqual(self.status(4,'user'), 'pending')

    def test_bot_cannot_use_topic_open_or_spoof_user(self):
        with self.assertRaises(HTTPException) as exc:
            self.seen({'profile':'user','thread_id':1}, ('bot','bot'))
        self.assertEqual(exc.exception.status_code,403)
        result = self.seen({'profile':'user','message_ids':[1]}, ('bot','bot'))
        self.assertEqual(result['blocked'],[1])
        self.assertEqual(self.status(1,'user'),'pending')

    def test_user_cannot_clear_bot_by_topic(self):
        with self.assertRaises(HTTPException):
            self.seen({'profile':'bot','thread_id':1})

    def test_invalid_and_missing_topic(self):
        for tid in (True, '1', -1, 999):
            with self.subTest(tid=tid), self.assertRaises(HTTPException):
                self.seen({'profile':'user','thread_id':tid})

    def test_bot_speech_clears_before_speech_and_preserves_later(self):
        self.assertEqual(self.status(1,'bot'),'pending')
        router._set_turn(self.conn, 1, 'bot', None, 1)
        router.route_message(self.conn,'bot','Detailed response','bot',1,project='p')
        self.assertEqual(self.status(1,'bot'),'seen')
        self.assertEqual(self.status(2,'bot'),'pending')
        self.assertEqual(self.status(3,'bot'),'pending')
        self.add_message(20,1)
        self.conn.commit()
        self.assertEqual(self.status(20,'bot'),'pending')

    def test_null_need_reply_does_not_trigger_p2p_reminder(self):
        self.conn.execute('UPDATE messages SET need_reply=NULL WHERE id=3')
        self.conn.execute("UPDATE threads SET status='closed'")
        self.conn.commit()
        self.assertEqual(scanner.scan_inbox(self.conn),0)


if __name__ == '__main__':
    unittest.main()
