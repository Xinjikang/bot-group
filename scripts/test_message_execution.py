"""Isolated HTTP/MCP contract and atomic message provenance tests."""
import ast
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from engine import api, auth, db, message_execution, reasoning, wake
from scripts import test_abnormal_exit as fixtures


class MessageExecutionTests(unittest.TestCase):
    def setUp(self):
        fixtures.AbnormalExitTests.setUp(self)
        self.conn.execute('INSERT INTO wake_runs(session,started_at,project,profile,thread_id) VALUES(?,?,?,?,?)',
                          ('dev-1-p-work', 100, 'p', 'dev-1', None))
        self.conn.commit()
        app = FastAPI()
        app.include_router(api.router_api)
        self.client = TestClient(app, client=('127.0.0.1', 4321))
        p = patch.object(wake, 'enqueue', return_value=False)
        p.start()
        self.addCleanup(p.stop)
        self.context = dict(profile='dev-1', project='p', thread_id=None,
                            session='dev-1-p-work', wake_started_at=100)

    def body(self, **extra):
        return dict(text='@dev-2 response', project='p', thread_id=None, need_reply=False,
                    execution=dict(session='dev-1-p-work', wake_started_at=100), **extra)

    def test_mcp_read_send_confirm_cycle_and_empty_page(self):
        cur = self.conn.execute("INSERT INTO messages(project,ts,sender,sender_type,content,mentions,need_reply) "
                                "VALUES('p',?,'user','user','@dev-1 reply','[\"dev-1\"]',1)", (db.now_iso(),))
        mid = cur.lastrowid
        self.conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(?,'dev-1','pending')", (mid,))
        self.conn.commit()
        headers = {'X-Group-Profile': 'dev-1', 'X-Group-Key': auth.bot_token('dev-1')}
        def request(method, path, **kwargs):
            response = self.client.request(method, path, headers=headers, **kwargs)
            return response.json() if response.is_success else {'error': response.json()}
        scope = dict(_profile=lambda: 'dev-1', _start_heartbeat=lambda: None,
                     _session_resolve=lambda: ('work', None, ''), _work_project=lambda: ('p', ''),
                     _execution_context=lambda: self.context,
                     _get=lambda path, params: request('GET', path, params=params),
                     _post=lambda path, body: request('POST', path, json=body))
        tree = ast.parse(Path('mcp/server.py').read_text(encoding='utf-8'))
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                     and n.name in {'group_read_inbox', 'group_send', 'group_mark_seen'}]
        for node in functions:
            node.decorator_list = []
        exec(compile(ast.Module(body=functions, type_ignores=[]), '<mcp>', 'exec'), scope)
        read, send, seen = (scope[name] for name in ('group_read_inbox', 'group_send', 'group_mark_seen'))
        first = read()
        self.assertEqual(first, read())
        self.assertEqual(first['pending_total'], 1)
        empty = read(after_id=mid, upper_id=mid)
        self.assertEqual(empty['messages'], [])
        self.assertFalse(empty['has_more'])
        self.assertEqual(empty['pending_total'], 1)
        self.assertIn('待确认', empty['note'])
        self.assertEqual(seen([mid])['blocked'], [mid])
        self.assertIn('error', send('x' * 2001, False))
        self.assertEqual(read()['pending_total'], 1)
        self.assertIn('message_id', send('@user 已处理', False))
        self.assertEqual(read()['pending_total'], 1)
        post = scope['_post']
        scope['_post'] = lambda path, body: {'error': 'confirmation unavailable'}
        self.assertIn('error', seen([mid]))
        self.assertEqual(read()['pending_total'], 1)
        scope['_post'] = post
        self.assertEqual(seen([mid]), {'seen': 1})
        self.assertEqual(read()['pending_total'], 0)
        self.assertEqual(read()['note'], '无未读消息')
        self.assertEqual(seen([mid]), {'seen': 0})

    def send(self, body, profile='dev-1'):
        return self.client.post('/msg', json=body, headers={'X-Group-Profile': profile, 'X-Group-Key': auth.bot_token(profile)})

    def test_http_only_status_output_still_has_persisted_link(self):
        with patch.object(api.events.bus, 'publish') as publish:
            result = self.send(self.body())
        self.assertEqual(result.status_code, 200, result.text)
        message_id = result.json()['message_id']
        link = self.conn.execute('SELECT * FROM message_executions WHERE message_id=?', (message_id,)).fetchone()
        self.assertEqual((link['session'], link['wake_started_at']), ('dev-1-p-work', 100))
        event = next(call.args[1] for call in publish.call_args_list if call.args[0] == 'message.new')
        self.assertEqual(event['execution'], self.body()['execution'])
        self.assertEqual((event['project'], event['thread_id'], event['sender']), ('p', None, 'dev-1'))
        with patch.object(reasoning, 'read_reasoning', return_value={'entries': [], 'latest_id': 0}):
            data = self.client.get('/reasoning', params=dict(profile='dev-1', session='dev-1-p-work', wake_started_at=100)).json()
        self.assertEqual(data['replies'][0]['message_id'], message_id)

    def test_missing_fields_and_bad_types_have_no_side_effects(self):
        original = self.body()
        cases = [{k: v for k, v in original.items() if k != key} for key in original]
        cases += [{**original, key: value} for key, value in [
            ('project', 123), ('thread_id', False), ('need_reply', 'false'),
            ('need_reply', 2), ('execution', {}), ('text', 123)]]
        for body in cases:
            with self.subTest(body=body):
                self.assertEqual(self.send(body).status_code, 400)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM message_executions').fetchone()[0], 0)

    def test_topic_reply_and_settlement_events_keep_exact_execution(self):
        now = db.now_iso()
        self.conn.execute('INSERT INTO threads(id,project,title,creator,members,created_at,last_active_at) '
                          'VALUES(42,?,?,?,?,?,?)', ('p', 'Topic', 'dev-1', '["dev-1","dev-2"]', now, now))
        self.conn.execute("UPDATE threads SET turn_profile='dev-1' WHERE id=42")
        execution = dict(session='dev-1-p-chat-42', wake_started_at=200)
        self.conn.execute('INSERT INTO wake_runs(session,started_at,project,profile,thread_id) VALUES(?,?,?,?,?)',
                          (execution['session'], 200, 'p', 'dev-1', 42))
        self.conn.commit()
        for text in ('Topic reply', '[收口] Topic conclusion'):
            with self.subTest(text=text), patch.object(api.events.bus, 'publish') as publish:
                result = self.send({**self.body(), 'thread_id': 42, 'text': text, 'execution': execution})
                self.assertEqual(result.status_code, 200, result.text)
                event = next(call.args[1] for call in publish.call_args_list if call.args[0] == 'message.new')
                self.assertEqual(event, dict(message_id=result.json()['message_id'], thread_id=42,
                                             sender='dev-1', project='p', execution=execution))
                link = self.conn.execute('SELECT session,wake_started_at FROM message_executions WHERE message_id=?',
                                         (result.json()['message_id'],)).fetchone()
                self.assertEqual(tuple(link), (execution['session'], 200))

    def test_wrong_scope_identity_and_ended_or_unknown_run(self):
        body = self.body()
        for bad in [{**body, 'project': 'q'}, {**body, 'thread_id': 8}, {**body, 'sender': 'dev-2'}]:
            self.assertEqual(self.send(bad).status_code, 403)
        self.assertEqual(self.send(body, 'dev-2').status_code, 403)
        self.assertEqual(self.send({**body, 'execution': dict(session='missing', wake_started_at=100)}).status_code, 409)
        self.conn.execute('UPDATE wake_runs SET ended_at=101')
        self.conn.commit()
        self.assertEqual(self.send(body).status_code, 409)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)

    def test_link_failure_rolls_back_message_and_inbox(self):
        self.conn.execute("CREATE TRIGGER fail_link BEFORE INSERT ON message_executions BEGIN SELECT RAISE(ABORT,'test failure'); END")
        self.conn.commit()
        with self.assertRaises(Exception):
            self.send(self.body())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM inbox').fetchone()[0], 0)

    def test_mcp_and_http_share_contract_and_need_no_response_parsing(self):
        tree = ast.parse(Path('mcp/server.py').read_text(encoding='utf-8'))
        names = {'_execution_context', '_session_resolve', '_work_project', 'group_send'}
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        send_function = next(n for n in functions if n.name == 'group_send')
        tool_arguments = {arg.arg for arg in send_function.args.args}
        self.assertEqual(tool_arguments, {'text', 'need_reply'})
        for node in functions:
            node.decorator_list = []
        scope = dict(os=os, json=json, _profile=lambda: 'dev-1',
                     _post=lambda path, body: self.send(body).json(),
                     _parent_cmdline=lambda: self.fail('must use execution metadata, not real Hermes ID parsing'))
        exec(compile(ast.Module(body=functions, type_ignores=[]), '<mcp>', 'exec'), scope)
        with patch.dict(os.environ, {message_execution.CONTEXT_ENV: json.dumps(self.context)}):
            one = scope['group_send']('@dev-2 MCP send', False)
            self.assertIn('message_id', one)
            with self.assertRaises(TypeError):
                scope['group_send']('@dev-2 wrong project', False, project='q')
        with patch.dict(os.environ, {message_execution.CONTEXT_ENV: json.dumps({**self.context, 'profile': 'dev-2'})}):
            self.assertIn('error', scope['group_send']('@dev-2 invalid context', False))
        two = self.send(self.body()).json()
        ids = [r['message_id'] for r in message_execution.replies(self.conn, 'dev-1-p-work', 100)]
        self.assertEqual(ids, [one['message_id'], two['message_id']])

    def test_disabled_member_and_superseded_run_are_rejected(self):
        self.conn.execute("UPDATE project_members SET active=0 WHERE profile='dev-1'")
        self.conn.commit()
        self.assertEqual(self.send(self.body()).status_code, 403)
        self.conn.execute("UPDATE project_members SET active=1 WHERE profile='dev-1'")
        self.conn.execute("INSERT INTO wake_runs(session,started_at,profile,project) VALUES('dev-1-p-work',101,'dev-1','p')")
        self.conn.commit()
        self.assertEqual(self.send(self.body()).status_code, 409)

    def test_schema_migration_is_repeatable(self):
        db.init_schema(self.conn)
        db.init_schema(self.conn)
        self.assertEqual(self.send(self.body()).status_code, 200)


if __name__ == '__main__':
    unittest.main()
