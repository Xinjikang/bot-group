"""Scoped stop regression: isolated SQLite and mocked process/Hermes operations."""
import unittest
from unittest.mock import patch
from fastapi import HTTPException
from engine import api, commands, stop, wake, queue
from scripts import test_abnormal_exit as fixture


class StopScopeTests(unittest.TestCase):
    setUp = fixture.AbnormalExitTests.setUp

    def seed(self):
        for tid in (10, 11):
            self.conn.execute("INSERT INTO threads(id,project,title,creator,members,status,created_at,last_active_at) VALUES(?,'p','topic','user','[\"dev-1\"]','open','now','now')", (tid,))
        for mid, project, tid in ((100, 'p', None), (101, 'q', None), (102, 'p', 10), (103, 'p', 11)):
            self.conn.execute("INSERT INTO messages(id,project,thread_id,ts,sender,sender_type,content,mentions) VALUES(?,?,?,'now','user','user','test','[]')", (mid, project, tid))
            self.conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(?,'dev-1','pending')", (mid,))
            self.conn.execute("INSERT INTO wake_jobs(id,profile,project,kind,thread_id,trigger,trigger_msg_id,status,enqueued_at) VALUES(?,'dev-1',?,?,?,'@msg',?,'running','now')", (mid, project, 'chat' if tid else 'work', tid, mid))
        for ident, project in ((20, 'p'), (21, 'q')):
            self.conn.execute("INSERT INTO tasks(id,project,requirement_id,title,status,next_bot,created_at) VALUES(?,?,1,'pending task','pending','dev-1','now')", (ident, project))
        self.conn.commit()

    def call_stop(self, thread_id=None, dead=True):
        session = wake.session_name('dev-1', 'chat' if thread_id else 'work', thread_id, 'p')
        wake._running_sessions.update((session, 'dev-1-q-work'))
        with patch.object(wake, 'find_bot_processes', return_value=[123]) as find, \
             patch.object(stop, '_kill_process_tree', return_value=True) as kill, \
             patch.object(stop, '_wait_dead', return_value=dead) as wait, \
             patch.object(stop, '_soft_delete_tail', return_value=2) as rewind:
            result = stop.stop_profile('dev-1', 'p', thread_id)
        find.assert_called_once_with('dev-1', session=session)
        kill.assert_called_once_with(123)
        wait.assert_called_once_with('dev-1', session)
        self.assertEqual(wake._intentional_stops, {session})
        return result, rewind

    def test_work_stop_isolates_project_process_jobs_inbox_and_tasks(self):
        self.seed()
        result, rewind = self.call_stop()
        self.assertTrue(result['stopped'])
        rewind.assert_called_once_with('dev-1', 'dev-1-p-work')
        self.assertEqual(dict(self.conn.execute('SELECT id,status FROM wake_jobs')), {100:'cancelled',101:'running',102:'running',103:'running'})
        self.assertEqual(dict(self.conn.execute('SELECT message_id,status FROM inbox')), {100:'seen',101:'pending',102:'pending',103:'pending'})
        tasks = dict(self.conn.execute('SELECT id,status FROM tasks'))
        self.assertEqual([tasks[i] for i in (1,2,3,20,21)], ['pending','in_progress','in_progress','pending','pending'])
        self.assertEqual(result['pending_cancelled'], 0)

    def test_admin_block_and_cancel_preserve_pending_siblings(self):
        self.seed()
        with patch.object(wake, 'find_bot_processes', return_value=[]), \
             patch.object(stop, '_wait_dead', return_value=True), \
             patch.object(stop, '_soft_delete_tail', return_value=0):
            api.admin_block_task(1, {'project':'p'}, identity=('user','user'))
            tasks = dict(self.conn.execute('SELECT id,status FROM tasks'))
            self.assertEqual((tasks[1], tasks[20]), ('blocked','pending'))
            self.conn.execute("UPDATE tasks SET status='in_progress',claimed_by='dev-1' WHERE id=1")
            self.conn.commit()
            api.admin_cancel_task(1, {'project':'p'}, identity=('user','user'))
            tasks = dict(self.conn.execute('SELECT id,status FROM tasks'))
            self.assertEqual((tasks[1], tasks[20]), ('cancelled','pending'))

    def test_topic_stop_preserves_other_topics_work_and_tasks(self):
        self.seed()
        result, rewind = self.call_stop(10)
        self.assertTrue(result['stopped'])
        rewind.assert_not_called()
        self.assertEqual(dict(self.conn.execute('SELECT id,status FROM wake_jobs')), {100:'running',101:'running',102:'cancelled',103:'running'})
        self.assertEqual(dict(self.conn.execute('SELECT message_id,status FROM inbox')), {100:'pending',101:'pending',102:'seen',103:'pending'})
        self.assertEqual(self.conn.execute('SELECT status FROM tasks WHERE id=1').fetchone()[0], 'in_progress')
        self.assertEqual(self.conn.execute('SELECT status FROM tasks WHERE id=20').fetchone()[0], 'pending')

    def test_unknown_exit_never_rewinds(self):
        self.seed()
        result, rewind = self.call_stop(dead=False)
        self.assertFalse(result['stopped'])
        self.assertEqual(result['soft_deleted'], -1)
        rewind.assert_not_called()

    def test_api_missing_or_mismatched_scope_has_no_process_effect(self):
        self.seed()
        with patch.object(wake, 'find_bot_processes') as find:
            with self.assertRaises(HTTPException):
                api.post_stop({'profile':'dev-1'}, identity=('user', 'user'))
            with self.assertRaises(HTTPException):
                api.post_stop({'profile':'dev-1','project':'q','thread_id':10}, identity=('user', 'user'))
            with self.assertRaises(ValueError):
                stop.stop_profile('dev-1', '', None)
            find.assert_not_called()

    def test_slash_stop_forwards_exact_scope_and_actual_outcome(self):
        with patch.object(stop, 'stop_profile', return_value={'stopped':True}) as halt:
            result = commands.dispatch(self.conn, 'p', 'user', 'user', '/stop @dev-1', 10)
            self.assertTrue(result['ok'])
            halt.assert_called_once_with('dev-1', 'p', 10)
            for text in ('/stop', '/stop @dev-1 extra', '/stop @dev-1 @dev-2'):
                with self.assertRaises(commands.CommandError):
                    commands.dispatch(self.conn, 'p', 'user', 'user', text, None)
            self.assertEqual(halt.call_count, 1)
        with patch.object(stop, 'stop_profile', return_value={'stopped':False}):
            self.assertFalse(commands.dispatch(self.conn, 'p', 'user', 'user', '/stop @dev-1', None)['ok'])

    def test_cancelled_claim_cannot_spawn(self):
        self.seed()
        self.conn.execute("UPDATE wake_jobs SET status='cancelled' WHERE id=100")
        self.conn.commit()
        with patch.object(wake, '_spawn_in_workspace') as spawn:
            result = wake._spawn('dev-1','work','signal',None,'p',job_id=100)
        self.assertFalse(result['ok'])
        spawn.assert_not_called()

    def test_queue_preserves_cancelled_delivery(self):
        self.seed()
        self.conn.execute("UPDATE wake_jobs SET status='queued' WHERE id=100")
        self.conn.commit()
        def delivered(*args, **kwargs):
            self.conn.execute("UPDATE wake_jobs SET status='cancelled' WHERE id=100")
            self.conn.commit()
            return {'ok':False}
        with patch.object(wake, 'session_busy', return_value=False), \
             patch.object(wake, 'deliver', side_effect=delivered), \
             patch('engine.task_retry.finish') as retry:
            queue.process_one(self.conn)
        self.assertEqual(self.conn.execute('SELECT status FROM wake_jobs WHERE id=100').fetchone()[0], 'cancelled')
        retry.assert_not_called()


if __name__ == '__main__':
    unittest.main()
