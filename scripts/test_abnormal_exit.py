"""req#64: exit classification, exact ownership, wake evidence and reaper wiring."""
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from engine import db, taskpool, wake, heartbeat


class AbnormalExitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        host = patch.dict(os.environ, {'HERMES_HOME': self.tmp.name})
        host.start()
        self.addCleanup(host.stop)
        p = patch.object(db,'db_path',return_value=os.path.join(self.tmp.name,'group.db'))
        p.start()
        self.addCleanup(p.stop)
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        db.init_schema(self.conn)
        for profile in ('dev-1','dev-2','default'):
            self.conn.execute('INSERT INTO members(profile,display_name,role) VALUES(?,?,?)',
                              (profile,profile,'dev' if profile.startswith('dev') else 'orchestrator'))
        for project in ('p','q'):
            self.conn.execute("INSERT OR IGNORE INTO members(profile,display_name,role) VALUES('user','User','user')")
            self.conn.execute('INSERT INTO projects(name,root,created_at) VALUES(?,?,?)',(project,self.tmp.name,db.now_iso()))
            taskpool._save_project_owner(self.conn, project, 'default')
        for tid,project,owner in ((1,'p','dev-1'),(2,'p','dev-2'),(3,'q','dev-1')):
            self.conn.execute('INSERT INTO requirements(id,project,title,created_at,updated_at) VALUES(?,?,?,?,?)',
                              (tid,project,'requirement',db.now_iso(),db.now_iso()))
            self.conn.execute("INSERT INTO tasks(id,project,requirement_id,title,status,claimed_by,created_at) "
                              "VALUES(?,?,?,'task','in_progress',?,?)",(tid,project,tid,owner,db.now_iso()))
            taskpool._log_event(self.conn,owner,'claim',project,requirement_id=tid,task_id=tid)
        self.conn.commit()
        from scripts.project_test_support import enable_members
        enable_members(self.conn, ('p', 'q'))
        self.conn.commit()
        self.conn.execute("UPDATE tasks SET briefing_status='ready',description='Implement task',design_basis='Approved test specification',acceptance_criteria='Verify the expected execution outcome'")
        self.conn.commit()
        self.start = time.time()-2
        wake._shutting_down.clear()
        wake._running_sessions.clear()
        wake._intentional_stops.clear()
        self.addCleanup(wake._shutting_down.clear)
        self.addCleanup(wake._running_sessions.clear)
        self.addCleanup(wake._intentional_stops.clear)

    def status(self,tid):
        return self.conn.execute('SELECT status FROM tasks WHERE id=?',(tid,)).fetchone()[0]

    def handle(self,code=1,kind='work',floor=0):
        return wake._handle_task_exit('dev-1','p',kind,'dev-1-p-work',self.start,time.time(),code,floor,task_id=1)

    def test_exact_owner_project_block_and_single_report(self):
        self.assertEqual(self.handle()['retry'],[1])
        self.start += 1
        self.assertEqual(self.handle()['retry'],[1])
        self.start += 1
        self.assertEqual(self.handle()['blocked'],[1])
        self.assertEqual([self.status(i) for i in (1,2,3)],['blocked','in_progress','in_progress'])
        self.assertEqual(self.conn.execute('SELECT block_kind FROM tasks WHERE id=1').fetchone()[0],'technical')
        self.assertEqual(self.conn.execute('SELECT status FROM requirements WHERE id=1').fetchone()[0],'blocked')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE event_type='block'").fetchone()[0],1)
        msg = self.conn.execute('SELECT * FROM messages').fetchone()
        self.assertEqual((msg['sender'],msg['sender_type'],msg['project']),('engine','engine','p'))
        self.assertIsNone(msg['need_reply'])
        self.assertIn('dev-1', msg['content'])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inbox WHERE profile='default'").fetchone()[0],1)
        self.assertEqual(self.handle()['blocked'],[])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0],1)

    def test_old_claim_still_retries_when_bound_to_attempt(self):
        floor = self.conn.execute('SELECT MAX(id) FROM events').fetchone()[0]
        self.assertEqual(self.handle(floor=floor)['retry'],[1])
        self.conn.execute("UPDATE events SET ts='2000-01-01T00:00:00'")
        self.conn.commit()
        self.assertEqual(self.handle()['blocked'],[])
        self.assertEqual(self.status(1),'in_progress')

    def test_no_claim_event_still_retries_bound_task(self):
        self.conn.execute('DELETE FROM events WHERE task_id=1')
        self.conn.commit()
        self.assertEqual(self.handle()['retry'],[1])

    def test_handed_off_or_finished_task_preserved(self):
        for status in ('pending','done','cancelled','blocked'):
            self.conn.execute('UPDATE tasks SET status=? WHERE id=1',(status,))
            self.conn.commit()
            self.assertEqual(self.handle()['blocked'],[])
            self.assertEqual(self.status(1),status)

    def test_normal_chat_manual_stop_shutdown_preserved(self):
        self.assertEqual(self.handle(code=0)['retry'],[1])
        self.assertEqual(self.handle(kind='chat')['blocked'],[])
        wake._running_sessions.update(('dev-1-p-work','dev-2-p-work'))
        wake.mark_intentional_stop('dev-1', 'dev-1-p-work')
        self.assertEqual(wake._intentional_stops,{'dev-1-p-work'})
        self.assertEqual(self.handle()['blocked'],[])
        wake._intentional_stops.clear()
        wake._shutting_down.set()
        self.assertEqual(self.handle()['blocked'],[])

    def test_reaper_records_exit_and_blocks_after_work(self):
        callbacks = []
        proc = MagicMock(pid=777,returncode=1)
        def thread(target, **kwargs):
            callbacks.append(target)
            return MagicMock()
        with patch.object(wake,'session_busy',return_value=False), \
             patch('engine.session_adapter.available', return_value=False), \
             patch.object(wake,'_hermes_bin',return_value='hermes'), \
             patch.object(wake.subprocess,'Popen',return_value=proc) as spawned, \
             patch('threading.Thread',side_effect=thread), \
             patch.object(heartbeat,'on_exit') as exited:
            result = wake._spawn('dev-1','work','signal',None,'p')
            self.assertTrue(result['ok'], result)
            kwargs = spawned.call_args.kwargs
            self.assertEqual(kwargs['env']['HERMES_EPHEMERAL_SYSTEM_PROMPT'],
                             wake.wake_system_prompt('work', 'p', kwargs['cwd']))
            self.assertIn('dev-1-p-work', spawned.call_args.args[0])
            taskpool._log_event(self.conn,'dev-1','claim','p',requirement_id=1,task_id=1)
            self.conn.commit()
            callbacks[0]()
            exited.assert_called_once_with('dev-1', pid=777, thread_id=None)
        proc.wait.assert_called_once()
        self.assertEqual(self.status(1),'in_progress')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events WHERE event_type='execution.failed'").fetchone()[0],1)
        row = self.conn.execute('SELECT ended_at,exit_code FROM wake_runs').fetchone()
        self.assertIsNotNone(row['ended_at'])
        self.assertEqual(row['exit_code'],1)
        self.assertNotIn('dev-1-p-work',wake._running_sessions)


if __name__ == '__main__':
    unittest.main()
