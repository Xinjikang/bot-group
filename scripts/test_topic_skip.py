"""Topic completion contract using isolated SQLite and mocked wake delivery."""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import router


class TopicSkipTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(Path('engine/schema.sql').read_text(encoding='utf-8'))
        from engine import db
        db.init_schema(self.conn)
        from scripts.project_test_support import enable_members
        for profile in ('a', 'b', 'user'):
            self.conn.execute('INSERT INTO members(profile,display_name) VALUES(?,?)', (profile, profile))
        self.conn.execute("INSERT INTO threads(id,title,creator,members,created_at,last_active_at,round,turn_profile) VALUES(1,'test','user',?, 'now','now',1,'a')", (json.dumps(['a','b','user']),))
        enable_members(self.conn, ('xjk',))
        self.conn.execute("INSERT INTO messages(id,ts,sender,sender_type,content,thread_id) VALUES(1,'now','user','user','opening',1)")
        for profile in ('a', 'b'):
            self.conn.execute('INSERT INTO inbox(message_id,profile) VALUES(1,?)', (profile,))
        self.conn.commit()
        self.patcher = patch.object(router.wake, 'enqueue', return_value=True)
        self.enqueue = self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.conn.close()

    def send(self, sender, text):
        return router.route_message(self.conn, sender, text, 'bot', 1)

    def test_silent_then_pass_finishes_round(self):
        result = self.send('a', None)
        self.assertIsNone(result['message_id'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM messages').fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT status FROM inbox WHERE profile='a'").fetchone()[0], 'seen')
        self.assertEqual(self.conn.execute("SELECT status FROM inbox WHERE profile='b'").fetchone()[0], 'pending')
        self.assertEqual(router._next_speaker(self.conn, 1)['profile'], 'b')
        with self.assertRaises(router.RouteError):
            self.send('a', None)
        self.conn.rollback()
        self.send('b', 'pass')
        self.assertIsNone(router._next_speaker(self.conn, 1))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM inbox').fetchone()[0], 2)
        self.assertEqual(self.enqueue.call_count, 1)

    def test_mention_rejects_skips_accepts_answer(self):
        self.conn.execute('UPDATE threads SET turn_parent_id=1 WHERE id=1')
        self.conn.commit()
        for text in (None, 'pass'):
            with self.assertRaises(router.RouteError):
                self.send('a', text)
            self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM messages').fetchone()[0], 1)
        result = self.send('a', 'Here is my answer')
        self.assertEqual(self.conn.execute('SELECT parent_id FROM messages WHERE id=?', (result['message_id'],)).fetchone()[0], 1)

    def test_pass_variants_remain_compatible(self):
        self.send('a', 'pass. No additional information from me today.')
        self.send('b', 'pass')
        self.assertIsNone(router._next_speaker(self.conn, 1))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM inbox WHERE profile='b'").fetchone()[0], 1)

    def test_substantive_opinion_continues_discussion(self):
        self.send('a', 'I have a new question about the implementation')
        self.send('b', 'pass')
        self.assertEqual(router._next_speaker(self.conn, 1)['round'], 2)

    def test_api_preserves_explicit_null_without_message_event(self):
        from engine import api
        from unittest.mock import MagicMock
        connection = MagicMock()
        connection.execute.return_value.fetchone.return_value = {'project': 'xjk'}
        with patch.object(api, '_resolve_identity', return_value=('a', 'bot')), \
             patch.object(api, '_conn', return_value=connection), \
             patch('engine.message_execution.validate', return_value={'session': 'a-xjk-chat-1', 'wake_started_at': 100}), \
             patch.object(api.router, 'route_message', return_value={'message_id': None}) as route, \
             patch.object(api.events.bus, 'publish') as publish:
            api.post_msg({'text': None, 'thread_id': 1, 'project': 'xjk', 'need_reply': False,
                          'execution': {'session': 'a-xjk-chat-1', 'wake_started_at': 100}})
            self.assertIsNone(route.call_args.args[2])
            publish.assert_called_once_with('thread.updated', {'thread_id': 1})
            with self.assertRaises(api.HTTPException):
                api.post_msg({'thread_id': 1})

    def test_invalid_scope_cannot_skip(self):
        for sender, kind, thread in [('a','bot',None), ('user','user',1), ('outsider','bot',1)]:
            with self.assertRaises(router.RouteError):
                router.route_message(self.conn, sender, None, kind, thread)
            self.conn.rollback()

    def test_settled_topic_resumes_from_user_message(self):
        self.send('a', None)
        self.send('b', 'pass')
        row = self.conn.execute('SELECT status,closed_at,close_conclusion FROM threads WHERE id=1').fetchone()
        self.assertEqual(row['status'], 'settled')
        self.assertIsNone(row['closed_at'])
        with self.assertRaises(router.RouteError):
            self.send('a', 'unsolicited follow-up')
        self.conn.rollback()
        result = router.route_message(self.conn, 'user', '@b please continue', 'user', 1)
        self.assertEqual(self.conn.execute('SELECT status,round FROM threads WHERE id=1').fetchone()[:], ('open', 2))
        self.assertEqual(router._next_speaker(self.conn, 1)['parent_id'], result['message_id'])
        self.assertEqual(router._next_speaker(self.conn, 1)['profile'], 'b')

    def test_manual_settle_close_and_reopen_are_distinct(self):
        from engine import api
        from unittest.mock import MagicMock
        conn = MagicMock(wraps=self.conn)
        conn.close = MagicMock()
        with patch.object(api, '_conn', return_value=conn), patch.object(api.events.bus, 'publish'):
            api.post_thread_settle(1, {})
            row = self.conn.execute('SELECT status,close_conclusion FROM threads WHERE id=1').fetchone()
            self.assertEqual(row['status'], 'settled')
            self.assertFalse(row['close_conclusion'])
            api.post_thread_close(1, {'conclusion': 'final decision'})
            row = self.conn.execute('SELECT status,close_conclusion FROM threads WHERE id=1').fetchone()
            self.assertEqual(tuple(row), ('closed', 'final decision'))
            for sender, kind in [('a','bot'), ('user','user')]:
                with self.assertRaises(router.RouteError):
                    router.route_message(self.conn, sender, 'hello', kind, 1)
                self.conn.rollback()
            api.post_thread_reopen(1)
            self.assertEqual(self.conn.execute('SELECT status FROM threads WHERE id=1').fetchone()[0], 'settled')
            self.assertIsNone(router._next_speaker(self.conn, 1))

    def test_idle_settles_user_topic_without_waking_creator(self):
        from engine import scanner
        self.conn.execute("UPDATE threads SET last_active_at='2026-01-01T00:00:00' WHERE id=1")
        self.conn.commit()
        scanner.scan_thread_idle(self.conn)
        self.assertEqual(self.conn.execute('SELECT status FROM threads WHERE id=1').fetchone()[0], 'settled')
        self.enqueue.assert_not_called()

    def test_user_interjection_preserves_active_turn_then_mentions(self):
        self.conn.execute("UPDATE threads SET turn_wake_at='original' WHERE id=1")
        self.conn.commit()
        first = router.route_message(self.conn, 'user', '@b please answer', 'user', 1)
        second = router.route_message(self.conn, 'user', '@a another question', 'user', 1)
        turn = self.conn.execute('SELECT round,turn_profile,turn_wake_at FROM threads WHERE id=1').fetchone()
        self.assertEqual(tuple(turn), (1, 'a', 'original'))
        self.assertEqual(self.conn.execute('SELECT round FROM messages WHERE id=?', (first['message_id'],)).fetchone()[0], 1)
        self.send('a', 'finishing my current statement')
        self.assertEqual(router._next_speaker(self.conn, 1)['parent_id'], first['message_id'])
        self.send('b', 'answer to first interjection')
        self.assertEqual(router._next_speaker(self.conn, 1)['parent_id'], second['message_id'])
        self.send('a', 'answer to second interjection')
        self.assertEqual(router._next_speaker(self.conn, 1), {'profile': 'b', 'parent_id': None, 'round': 1, 'depth': 0})

    def test_queue_waits_for_busy_session_without_blocking_other_topic(self):
        from engine import queue
        self.conn.execute("INSERT INTO threads(id,title,creator,members,created_at,last_active_at,round,turn_profile) VALUES(2,'other','user','[\"b\",\"user\"]','now','now',1,'b')")
        for profile, topic in [('a', 1), ('b', 2)]:
            self.conn.execute("INSERT INTO wake_jobs(profile,kind,trigger,thread_id,enqueued_at) VALUES(?,'chat','thread-open',?,'now')", (profile, topic))
        self.conn.commit()
        with patch.object(router.wake, 'session_busy', side_effect=lambda p, *args: p == 'a'), \
             patch.object(router.wake, 'deliver', return_value={'ok': True}) as deliver:
            self.assertTrue(queue.process_one(self.conn))
            self.assertEqual(deliver.call_args.args[0], 'b')
            self.assertEqual(self.conn.execute("SELECT status FROM wake_jobs WHERE profile='a'").fetchone()[0], 'queued')
        self.conn.execute("UPDATE threads SET turn_profile='b' WHERE id=1")
        self.conn.commit()
        with patch.object(router.wake, 'deliver') as deliver:
            self.assertFalse(queue.process_one(self.conn))
            deliver.assert_not_called()

    def test_session_process_matching_is_exact_and_failure_is_unknown(self):
        from types import SimpleNamespace
        from engine import db
        from unittest.mock import MagicMock
        connection = MagicMock(wraps=self.conn)
        connection.close = MagicMock()
        processes = [(123, ['hermes', '-p', 'a', 'chat', '-c', 'a-xjk-chat-1']),
                     (456, ['hermes', '-p', 'a', 'chat', '-c', 'a-xjk-chat-10'])]
        with patch.object(router.wake.process_query, 'list_processes', return_value=processes), \
             patch.object(db, 'get_connection', return_value=connection):
            self.assertEqual(router.wake.find_bot_processes('a', 'a-xjk-chat-1'), [123])
        with patch.object(router.wake.process_query, 'list_processes', return_value=None):
            self.assertIsNone(router.wake.find_bot_processes('a', 'a-xjk-chat-1'))

    def test_spawn_guard_and_exit_record_use_same_run(self):
        from unittest.mock import MagicMock
        from engine import db, events, heartbeat
        temporary = tempfile.TemporaryDirectory(prefix='topic-spawn-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.conn.execute("UPDATE projects SET root=?,directory_mode='shared' WHERE name='xjk'", (str(root),))
        self.conn.commit()
        environment = patch.dict(os.environ, {
            'HERMES_HOME': str(root / 'hermes'),
            'HERMES_MANAGED_DIR': str(root / 'managed'),
            'GROUP_DATA_DIR': str(root / 'data'),
            'GROUP_DB': str(root / 'group.db'),
        })
        environment.start()
        self.addCleanup(environment.stop)
        conn = MagicMock(wraps=self.conn)
        conn.close = MagicMock()
        proc = MagicMock()
        proc.pid = 123
        def start_child(*args, **kwargs):
            context = json.loads(kwargs['env']['XDG_GROUP_ENGINE_EXECUTION'])
            row = self.conn.execute('SELECT profile,project,thread_id FROM wake_runs WHERE session=? AND started_at=?',
                                    (context['session'], context['wake_started_at'])).fetchone()
            self.assertEqual(tuple(row), ('a', 'xjk', 1))
            self.assertEqual(context['thread_id'], 1)
            return proc
        proc.returncode = 0   # req#39：_watch_session 改 proc.wait()+returncode，废弃 poll
        try:
            with patch.object(router.wake, '_workdir_for', return_value=str(root)), \
                 patch('engine.project_rules.sync'), \
                 patch('engine.session_adapter.available', return_value=False), \
                 patch.object(router.wake, '_hermes_bin', return_value='hermes'), \
                 patch.object(router.wake, 'find_bot_processes', return_value=[]), \
                 patch.object(router.wake.subprocess, 'Popen', side_effect=start_child) as popen, \
                 patch.object(router.wake.threading, 'Thread') as thread, \
                 patch.object(db, 'get_connection', return_value=conn), \
                 patch.object(events.bus, 'publish') as publish, \
                 patch.object(heartbeat, 'on_exit'):
                result = router.wake._spawn('a', 'chat', 'signal', 1, 'xjk')
                self.assertTrue(result['ok'], result)
                self.assertTrue(router.wake._spawn('a', 'chat', 'signal', 1, 'xjk')['busy'])
                self.assertEqual(popen.call_count, 1)
                start = publish.call_args.args[1]['wake_started_at']
                thread.call_args.kwargs['target']()
                ended = self.conn.execute('SELECT ended_at FROM wake_runs WHERE started_at=?', (start,)).fetchone()[0]
                self.assertIsNotNone(ended)
                self.assertEqual(publish.call_args.args[0], 'wake.finished')
                self.assertEqual(publish.call_args.args[1]['wake_started_at'], start)
                self.assertFalse(router.wake.session_busy('a', 'chat', 1, 'xjk'))
        finally:
            router.wake._running_sessions.clear()


if __name__ == '__main__':
    unittest.main()
