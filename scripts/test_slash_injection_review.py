"""Regression checks for slash/injection review findings; isolated storage only."""
import ast
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from engine import db, wake, session_adapter, scanner, steer_ledger, commands
from scripts import test_abnormal_exit as fixture


class ReviewTests(unittest.TestCase):
    setUp = fixture.AbnormalExitTests.setUp

    def test_command_catalog_and_topic_feedback(self):
        from engine import api
        menu = api.get_commands()['commands']
        self.assertEqual([item['name'] for item in menu], ['/new', '/stop', '/steer', '/help'])
        self.assertTrue(menu[0]['requires_target'])
        self.assertTrue(menu[1]['requires_target'])
        self.assertTrue(menu[2]['requires_target'])
        self.assertFalse(menu[3]['requires_target'])
        with patch.object(session_adapter, 'switch_session', return_value={
                'ok': True, 'session_id': 'new', 'generation': 1}) as switch:
            result = commands.dispatch(self.conn, 'p', 'user', 'user', '/new @dev-1', 900)
        self.assertIn('@dev-1 的当前话题会话已切换', result['detail'])
        self.assertEqual(switch.call_args.args[3:5], ('chat', 900))

    def test_switch_waits_for_entire_spawn_then_observes_busy(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        results = []
        def spawn(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test timeout')
            wake._running_sessions.add('dev-1-p-work')
            return {'ok': True}
        def switch():
            conn = db.get_connection()
            try:
                results.append(session_adapter.switch_session(conn, 'p', 'dev-1', 'work', None,
                    'dev-1-p-work', busy_check=wake.session_busy))
            finally:
                conn.close()
                finished.set()
        with patch.object(wake, '_spawn_in_workspace', side_effect=spawn), patch.object(session_adapter, 'create_session') as create:
            t1 = threading.Thread(target=lambda: wake._spawn('dev-1','work',None,None,'p'))
            t2 = threading.Thread(target=switch)
            t1.start()
            try:
                self.assertTrue(entered.wait(2))
                t2.start()
                self.assertFalse(finished.wait(.1))
            finally:
                release.set()
                t1.join(3)
                if t2.ident: t2.join(3)
            self.assertFalse(results[0]['ok'])
            create.assert_not_called()

    def test_real_id_process_survives_loss_of_memory_state(self):
        self.conn.execute("INSERT INTO wake_runs(session,started_at,profile,project,runtime_session_id) VALUES('dev-1-p-work',100,'dev-1','p','real-id')")
        self.conn.commit()
        processes = [(123, ['hermes', '-p', 'dev-1', 'chat', '-c', 'real-id']),
                     (456, ['hermes', '-p', 'dev-1', 'chat', '-c', 'unrelated'])]
        with patch.object(wake.process_query, 'list_processes', return_value=processes):
            self.assertEqual(wake.find_bot_processes('dev-1','dev-1-p-work'), [123])
            self.assertTrue(wake.session_busy('dev-1','work',None,'p'))

    def test_long_running_inbox_is_not_failed(self):
        self.conn.execute("INSERT INTO messages(id,project,ts,sender,sender_type,content,mentions,need_reply) VALUES(900,'p','2000-01-01T00:00:00','user','user','long task','[\"dev-1\"]',1)")
        self.conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(900,'dev-1','pending')")
        self.conn.commit()
        with patch.object(wake, 'session_busy', return_value=True), patch.object(scanner, '_finalize_stale_group') as fail:
            scanner.scan_inbox(self.conn)
            fail.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT status FROM inbox WHERE message_id=900').fetchone()[0], 'pending')

    def test_snapshot_and_injection_keep_attachment_reference(self):
        self.conn.execute("INSERT INTO messages(id,project,ts,sender,sender_type,content,mentions,need_reply) VALUES(900,'p','now','user','user','see image','[\"dev-1\"]',1)")
        self.conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(900,'dev-1','pending')")
        self.conn.execute("INSERT INTO attachments(id,project,owner,filename,stored_name,mime,size,sha256,width,height,created_at,message_id,position) VALUES('image-id','p','user','image.png','image.png','image/png',1,'hash',1,1,'now',900,0)")
        self.conn.commit()
        snapshot = wake._snapshot_pending(self.conn, 'dev-1', 'work', None, 'p')
        self.assertEqual(snapshot[0]['attachments'][0]['id'], 'image-id')
        self.assertIn('image-id', wake._compose_injection(snapshot, 'work'))

    def test_mcp_preserves_pagination_even_empty_last_page(self):
        tree = ast.parse(Path('mcp/server.py').read_text(encoding='utf-8'))
        fn = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='group_read_inbox')
        fn.decorator_list = []
        scope = dict(_profile=lambda:'dev-1', _start_heartbeat=lambda:None,
                     _session_resolve=lambda:('work',None,''), _work_project=lambda:('p',''))
        exec(compile(ast.Module(body=[fn],type_ignores=[]), '<mcp>', 'exec'), scope)
        for messages in ([dict(message_id=100,project='p',thread_id=None)], []):
            response = dict(messages=messages, has_more=bool(messages), after_id=100, upper_id=200)
            scope['_get'] = lambda *args: response
            actual = scope['group_read_inbox'](0,200)
            for key in ('has_more','after_id','upper_id'):
                self.assertEqual(actual[key], response[key])

    def test_unavailable_steer_is_terminal_and_explicit(self):
        result = commands.dispatch(self.conn, 'p', 'user', 'user', '/steer @dev-1 correct this', None)
        self.assertFalse(result['ok'])
        op = steer_ledger.get_operation(self.conn, result['operations'][0])
        self.assertEqual(op['status'], 'undelivered')
        self.assertEqual(op['text'], 'correct this')

    def test_passwordless_user_commands_reach_dispatch(self):
        from engine import api
        with patch.object(api, '_resolve_identity', return_value=('', 'dev')):
            result = api.post_msg({'sender':'user', 'text':'/help', 'project':'p'}, None, None)
            with patch.object(session_adapter,'switch_session',return_value={'ok':True,'session_id':'new','generation':1}):
                new = api.post_msg({'sender':'user','text':'/new @dev-1','project':'p'},None,None)
                self.assertTrue(new['ok'])
            steer = api.post_msg({'sender':'user','text':'/steer @dev-1 correction','project':'p'},None,None)
            self.assertEqual(steer['command'],'/steer')
            self.assertFalse(steer['ok'])
        self.assertEqual(result['command'], '/help')
        self.assertTrue(result['ok'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)

    def test_live_topic_execution_protects_turn_and_idle(self):
        from engine import router, queue
        self.conn.execute("INSERT INTO threads(id,project,title,creator,members,status,created_at,last_active_at,turn_profile,turn_wake_at) VALUES(900,'p','review','user','[\"dev-1\",\"dev-2\"]','open','now','2000-01-01T00:00:00','dev-1','2000-01-01T00:00:00')")
        self.conn.execute("INSERT INTO wake_runs(session,started_at,profile,project,thread_id) VALUES('dev-1-p-chat-900',100,'dev-1','p',900)")
        self.conn.commit()
        with patch.object(wake, 'session_busy', side_effect=lambda p,*args:p=='dev-1'):
            self.assertTrue(wake.topic_busy(self.conn,900,'p'))
            self.assertEqual(scanner.scan_thread_idle(self.conn),0)
            self.assertEqual(router.advance_timed_out_turns(self.conn),0)
            with patch.object(router,'_next_speaker',return_value=None):
                self.assertEqual(scanner.scan_thread_all_responded(self.conn),0)
        row = self.conn.execute('SELECT status,turn_profile FROM threads WHERE id=900').fetchone()
        self.assertEqual(tuple(row),('open','dev-1'))
        self.conn.execute("UPDATE threads SET turn_profile='dev-2' WHERE id=900")
        self.conn.commit()
        wake.enqueue(self.conn,'dev-2','chat','thread-open',thread_id=900,project='p')
        with patch.object(wake,'session_busy',side_effect=lambda p,*args:p=='dev-1'), \
             patch.object(wake,'deliver') as deliver:
            self.assertFalse(queue.process_one(self.conn))
            deliver.assert_not_called()
            result=wake._spawn('dev-2','chat',None,900,'p')
            self.assertTrue(result.get('busy'),result)
        with patch.object(wake,'session_busy',return_value=False), \
             patch.object(wake,'deliver',return_value={'ok':True}) as deliver:
            self.assertTrue(queue.process_one(self.conn))
            deliver.assert_called_once()

    def test_engine_mcp_starts_heartbeat_without_reading_inbox(self):
        tree=ast.parse(Path('mcp/server.py').read_text(encoding='utf-8'))
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='main')
        calls=[]
        scope=dict(_execution_context=lambda:{'profile':'dev-1'},
                   _start_heartbeat=lambda:calls.append('heartbeat'),
                   mcp=SimpleNamespace(run=lambda **kw:calls.append('serve')))
        exec(compile(ast.Module(body=[fn],type_ignores=[]),'<mcp>','exec'),scope)
        scope['main']()
        self.assertEqual(calls,['heartbeat','serve'])

    @unittest.skipUnless(os.environ.get('HERMES_TEST_SOURCE'), 'requires installed Hermes source for isolated real SessionDB test')
    def test_real_hermes_new_title_and_read_stop_target(self):
        # Hermes and maintenance tools use overlapping top-level package names.
        # Run the external source integration in its own interpreter namespace.
        if os.environ.get('HERMES_SESSION_TEST_CHILD') != '1':
            result = subprocess.run(
                [sys.executable, '-X', 'utf8', '-B', '-m', 'unittest', self.id(), '-v'],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, 'HERMES_SESSION_TEST_CHILD': '1'},
                capture_output=True, text=True, encoding='utf-8', timeout=30,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return
        from engine import reasoning, stop
        home = Path(self.tmp.name) / 'hermes'
        with patch.object(session_adapter,'_hermes_home',return_value=home), \
             patch.object(session_adapter,'_hermes_src',return_value=Path(os.environ['HERMES_TEST_SOURCE'])):
            logical = 'dev-1-p-work'
            old = session_adapter.resolve_or_create(self.conn,'p','dev-1','work',None,logical)
            first = session_adapter.switch_session(self.conn,'p','dev-1','work',None,logical,busy_check=lambda *a:False)
            self.assertTrue(first['ok'],first)
            second = session_adapter.switch_session(self.conn,'p','dev-1','work',None,logical,busy_check=lambda *a:False)
            self.assertTrue(second['ok'],second)
            path = session_adapter.profile_state_db('dev-1')
            with closing(sqlite3.connect(path)) as hermes, hermes:
                self.assertEqual(hermes.execute('SELECT COUNT(DISTINCT title) FROM sessions').fetchone()[0],3)
            with self.assertRaises(ValueError):
                session_adapter.create_session('dev-1',logical)
            with closing(sqlite3.connect(path)) as hermes, hermes:
                self.assertEqual(hermes.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],3)
                for sid in (old,second['session_id']):
                    hermes.execute("INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,'user','wake',100)",(sid,))
                    hermes.execute("INSERT INTO messages(session_id,role,content,reasoning,timestamp) VALUES(?,'assistant','','thinking',101)",(sid,))
            with patch.object(reasoning,'_profile_state_db',return_value=path):
                result=reasoning.read_reasoning('dev-1',logical,wake_started_at=100,hermes_session_id=second['session_id'])
                self.assertEqual(len(result['entries']),1)
            with patch.object(stop,'_profile_state_db',return_value=path):
                self.assertEqual(stop._soft_delete_tail('dev-1',logical),2)
            with closing(sqlite3.connect(path)) as hermes, hermes:
                self.assertEqual(hermes.execute('SELECT COUNT(*) FROM messages WHERE session_id=? AND active=1',(old,)).fetchone()[0],2)

    def test_steer_end_generation_and_ttl_cleanup(self):
        self.conn.execute("INSERT INTO session_bindings(project,profile,kind,thread_id,session_id,generation,title,updated_at) VALUES('p','dev-1','work',0,'sid',2,'dev-1-p-work','now')")
        self.conn.execute("INSERT INTO wake_runs(session,started_at,profile,project,ended_at) VALUES('dev-1-p-work',100,'dev-1','p',101)")
        self.conn.commit()
        stale = steer_ledger.new_operation(self.conn,'p','dev-1','work',None,'text','old',1,100)
        ended = steer_ledger.new_operation(self.conn,'p','dev-1','work',None,'text','sid',2,100)
        delivering = steer_ledger.new_operation(self.conn,'p','dev-1','work',None,'text','sid',2,100)
        steer_ledger.record_delivery(self.conn,delivering['operation_id'],'sid',2,100)
        steer_ledger.reconcile_targets(self.conn)
        self.assertEqual(steer_ledger.get_operation(self.conn,stale['operation_id'])['status'],'generation_stale')
        self.assertEqual(steer_ledger.get_operation(self.conn,ended['operation_id'])['status'],'target_ended')
        self.assertEqual(steer_ledger.get_operation(self.conn,delivering['operation_id'])['status'],'pending_verify')
        self.assertEqual(steer_ledger.expire_stale(self.conn, now=time.time()+3600),1)


if __name__ == '__main__':
    unittest.main()
