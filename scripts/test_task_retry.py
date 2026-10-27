"""Focused task retry binding and terminal-state contracts."""
import unittest
import subprocess
from unittest.mock import patch, MagicMock
from engine.task_retry import retry_action, select_task
from engine import task_retry, taskpool, queue, wake, db
from scripts import test_abnormal_exit as fixtures


class TaskRetryTests(unittest.TestCase):
    def setUp(self):
        fixtures.AbnormalExitTests.setUp(self)
        runtime = patch.dict('os.environ', {
            'GROUP_DATA_DIR': self.tmp.name,
            'HERMES_MANAGED_DIR': self.tmp.name + '/managed',
        })
        runtime.start()
        self.addCleanup(runtime.stop)

    def test_binding_is_exact_owner_and_project(self):
        self.assertEqual(select_task(self.conn, 'dev-1', 'p'), 1)
        self.assertEqual(select_task(self.conn, 'dev-1', 'q'), 3)
        self.assertIsNone(select_task(self.conn, 'dev-1', 'p', preferred=2))

    def test_pending_before_claim_is_bound(self):
        self.conn.execute("UPDATE tasks SET status='pending',claimed_by='',next_bot='dev-1' WHERE id=1")
        self.assertEqual(select_task(self.conn, 'dev-1', 'p'), 1)

    def test_three_total_attempts(self):
        task = dict(self.conn.execute('SELECT * FROM tasks WHERE id=1').fetchone())
        self.assertEqual([retry_action(task, 'dev-1', n) for n in (1,2,3)], ['retry','retry','block'])

    def test_bot_block_cancel_done_handoff_and_stop_win(self):
        task = dict(self.conn.execute('SELECT * FROM tasks WHERE id=1').fetchone())
        self.assertEqual(retry_action(task, 'dev-1', 1, interrupted=True), 'preserve')
        for status in ('blocked', 'cancelled', 'done'):
            task['status'] = status
            self.assertEqual(retry_action(task, 'dev-1', 1), 'preserve')
        task.update(status='pending', next_bot='dev-2')
        self.assertEqual(retry_action(task, 'dev-1', 1), 'preserve')

    def test_pending_failure_is_durable_and_blocks_on_third(self):
        self.conn.execute("UPDATE tasks SET status='pending',claimed_by='',next_bot='dev-1' WHERE id=1")
        self.conn.commit()
        for attempt in range(1,4):
            result = task_retry.finish('dev-1','p',1,str(attempt),f'failure-{attempt}')
            self.assertEqual(result['blocked'], [1] if attempt == 3 else [])
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 1 if attempt == 3 else 0)
        report = self.conn.execute('SELECT content FROM messages').fetchone()[0]
        for attempt in range(1,4):
            self.assertIn(f'failure-{attempt}', report)
        self.assertEqual(self.conn.execute('SELECT status FROM tasks WHERE id=1').fetchone()[0], 'blocked')
        self.assertEqual(self.conn.execute('SELECT status FROM tasks WHERE id=2').fetchone()[0], 'in_progress')

    def test_duplicate_failure_is_counted_once(self):
        for _ in range(2):
            task_retry.finish('dev-1','p',1,'same-attempt','error')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE event_type='execution.failed'").fetchone()[0],1)

    def test_bot_block_is_immediate_and_never_retried(self):
        result = taskpool.task_block('p','dev-1',1,'needs a decision',kind='decision')
        self.assertTrue(result['blocked'])
        self.assertEqual(task_retry.finish('dev-1','p',1,'later','error'), {'blocked': [], 'retry': []})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE event_type='execution.failed'").fetchone()[0],0)

    def test_same_owner_handoff_is_success(self):
        floor = self.conn.execute('SELECT MAX(id) FROM events').fetchone()[0]
        taskpool._log_event(self.conn,'dev-1','node','p',task_id=1)
        self.conn.commit()
        self.assertEqual(task_retry.finish('dev-1','p',1,'new','exit=0',event_floor=floor), {'blocked': [], 'retry': []})

    def test_fast_exit_does_not_lose_retry_to_queue_success(self):
        wake.enqueue(self.conn,'dev-1','work','scan:task',project='p')
        job = self.conn.execute("SELECT id FROM wake_jobs WHERE profile='dev-1'").fetchone()[0]
        self.conn.execute("UPDATE wake_jobs SET status='running' WHERE id=?",(job,))
        self.conn.commit()
        task_retry.finish('dev-1','p',1,'fast','exit=1',job_id=job)
        queue._mark(self.conn,job,'done','pid 1')
        self.assertEqual(self.conn.execute('SELECT status FROM wake_jobs WHERE id=?',(job,)).fetchone()[0], 'failed')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wake_jobs WHERE profile='dev-1' AND status='queued'").fetchone()[0],1)

    def test_pending_spawn_failure_retries_immediately(self):
        self.conn.execute("UPDATE tasks SET status='pending',claimed_by='',next_bot='dev-1' WHERE id=1")
        self.conn.commit()
        with patch.object(wake,'session_busy',return_value=False), patch.object(wake,'_hermes_bin',return_value='hermes'), \
             patch.object(wake.subprocess,'Popen',side_effect=OSError('cannot start model')):
            result = wake._spawn('dev-1','work','signal',None,'p')
        self.assertFalse(result['ok'])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wake_jobs WHERE trigger='retry:task:1'").fetchone()[0],1)
        from engine.execution_errors import MESSAGES
        self.assertIn(MESSAGES['unknown'], self.conn.execute("SELECT detail FROM events WHERE event_type='execution.failed'").fetchone()[0])
        self.assertIn('cannot start model', self.conn.execute('SELECT diagnostic FROM execution_errors').fetchone()[0])

    def test_resume_gets_new_budget(self):
        for attempt in range(3):
            task_retry.finish('dev-1','p',1,str(attempt),'error')
        with patch.object(taskpool,'_conn',side_effect=db.get_connection):
            self.assertTrue(taskpool.admin_task_resume('p',1,next_bot='dev-1')['resumed'])
        result = task_retry.finish('dev-1','p',1,'resumed','error')
        self.assertEqual(result['retry'], [1])

    def test_timeout_stops_process_before_retry(self):
        callbacks = []
        proc = MagicMock(pid=888, returncode=-9)
        proc.wait.side_effect = [subprocess.TimeoutExpired('hermes',3600), None]
        def make_thread(target, **kwargs):
            callbacks.append(target)
            return MagicMock()
        with patch.object(wake,'session_busy',return_value=False), patch.object(wake,'_hermes_bin',return_value='hermes'), \
             patch.object(wake.subprocess,'Popen',return_value=proc), patch('threading.Thread',side_effect=make_thread), \
             patch('engine.stop._kill_process_tree',return_value=True) as kill, patch('engine.heartbeat.on_exit'):
            wake._spawn('dev-1','work','signal',None,'p')
            callbacks[0]()
            kill.assert_called_once_with(888)
        from engine.execution_errors import MESSAGES
        self.assertIn(MESSAGES['timeout'], self.conn.execute("SELECT detail FROM events WHERE event_type='execution.failed'").fetchone()[0])
        self.assertEqual(self.conn.execute('SELECT category FROM execution_errors').fetchone()[0], 'timeout')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wake_jobs WHERE trigger='retry:task:1'").fetchone()[0],1)

    def test_parallel_chat_exit_cleans_only_its_heartbeat(self):
        self.conn.execute("INSERT INTO threads(id,project,title,creator,members,status,created_at,last_active_at,turn_profile) VALUES(100,'p','chat','user','[\"dev-1\"]','open','now','now','dev-1')")
        self.conn.commit()
        callbacks = []
        def make_thread(target, **kwargs):
            callbacks.append(target)
            return MagicMock()
        wake._running_sessions.add('dev-1-p-work')
        with patch.object(wake,'session_busy',return_value=False), patch.object(wake,'_hermes_bin',return_value='hermes'), \
             patch.object(wake.subprocess,'Popen',return_value=MagicMock(pid=888,returncode=0)), \
             patch('threading.Thread',side_effect=make_thread), patch('engine.heartbeat.on_exit') as exited:
            self.assertTrue(wake._spawn('dev-1','chat','signal',100,'p')['ok'])
            callbacks[0]()
            exited.assert_called_once_with('dev-1',pid=888,thread_id=100)
        self.assertIn('dev-1-p-work',wake._running_sessions)

    def test_notification_uses_configured_owner_not_role_or_default_name(self):
        self.conn.execute("UPDATE members SET role='observer' WHERE profile='default'")
        self.conn.execute("INSERT INTO members(profile,display_name,role) VALUES('lead-a','Lead A','orchestrator')")
        self.conn.execute("INSERT INTO members(profile,display_name,role) VALUES('lead-b','Lead B','orchestrator')")
        self.conn.execute("INSERT INTO project_members(project,profile,active) VALUES('p','lead-b',0)")
        self.conn.execute("INSERT INTO project_members(project,profile,active) VALUES('p','lead-a',1)")
        taskpool._save_project_owner(self.conn,'p','lead-a')
        self.conn.commit()
        for i in range(3):
            task_retry.finish('dev-1','p',1,str(i),'error')
        report = self.conn.execute('SELECT mentions FROM messages').fetchone()[0]
        self.assertEqual(report, '["lead-a"]')
        message = self.conn.execute('SELECT * FROM messages').fetchone()
        self.assertEqual((message['sender'], message['sender_type']), ('engine', 'engine'))
        self.assertIsNone(message['need_reply'])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM inbox WHERE profile='lead-a' AND status='pending'"
        ).fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM wake_jobs WHERE profile='lead-a' AND kind='work' AND trigger='@msg'"
        ).fetchone()[0], 1)
        injected = wake._compose_injection(
            wake._snapshot_pending(self.conn, 'lead-a', 'work', None, 'p'), 'work')
        self.assertIn('发送者=engine（engine） 回复要求=无要求', injected)
        self.assertNotIn('无需回复', injected)

    def test_missing_owner_reports_block_to_user(self):
        self.conn.execute("DELETE FROM settings WHERE key='owner_profile' AND project='p'")
        self.conn.commit()
        for i in range(3):
            task_retry.finish('dev-1','p',1,str(i),'error')
        self.assertEqual(self.conn.execute('SELECT status FROM tasks WHERE id=1').fetchone()[0], 'blocked')
        self.assertEqual(self.conn.execute('SELECT mentions FROM messages').fetchone()[0], '["user"]')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inbox WHERE profile='user'").fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
