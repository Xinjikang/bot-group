"""Live harness dispatch regression; all fixtures synthetic, no CLI/model calls."""
import tempfile
import sqlite3
import io
from contextlib import closing
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine import api, db, wake
from scripts import verify_project_templates_live as live


class Dispatched(BaseException):
    """Stop at the native execution boundary before any child is started."""


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='template_live_dispatch_')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.codex = self.base / 'codex'; self.codex.mkdir()
        self.hermes = self.base / 'hermes'; self.hermes.mkdir()
        env = patch.dict(live.os.environ, {'GROUP_DB': str(self.base / 'group.db'),
                                          'CODEX_HOME': str(self.codex), 'HERMES_HOME': str(self.hermes)})
        env.start(); self.addCleanup(env.stop)
        self.conn = db.get_connection(); self.addCleanup(self.conn.close)
        db.init_schema(self.conn)
        app = FastAPI(); app.include_router(api.router_api)
        app.dependency_overrides[api._resolve_identity] = lambda: ('user', 'user')
        self.client = TestClient(app); self.addCleanup(self.client.close)

    def test_stale_topic_jobs_never_select_another_native_turn(self):
        actors = []
        for name, runtime in [('software', 'codex'), ('research', 'hermes'), ('writing', 'codex')]:
            setup = live.setup_project(self.client, self.conn, self.base, name, self.codex, self.hermes)
            # Reproduce a prior topic follow-up queued before the next scenario.
            self.conn.execute("INSERT INTO wake_jobs(profile,project,kind,trigger,status,enqueued_at) "
                "VALUES(?,?,'chat','stale-test','queued',?)", (setup['actor'], setup['project'], db.now_iso()))
            self.conn.commit()

            def boundary(profile, kind, thread_id=None, project=None, **kwargs):
                topic = thread_id
                self.assertEqual((profile, kind, project), (setup['actor'], 'chat', setup['project']))
                self.assertEqual(self.conn.execute('SELECT project FROM threads WHERE id=?', (topic,)).fetchone()[0], project)
                self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wake_jobs WHERE status='queued'").fetchone()[0], 0)
                native = self.conn.execute('SELECT b.runtime FROM members m JOIN bots b ON b.id=m.bot_id WHERE m.profile=?', (profile,)).fetchone()[0]
                self.assertEqual(native, runtime)
                self.assertTrue(kwargs['trigger_msg_id'])
                actors.append(profile)
                raise Dispatched()

            with patch.object(wake, 'deliver', side_effect=boundary) as native:
                with self.assertRaises(Dispatched):
                    live.execute(self.client, self.conn, setup, name, self.hermes)
                native.assert_called_once()
        self.assertEqual(actors[0], actors[2])  # Same Codex Bot can join both projects.
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM wake_runs').fetchone()[0], 0)

    def test_native_budget_checked_before_fourth_dispatch(self):
        setup = live.setup_project(self.client, self.conn, self.base, 'software', self.codex, self.hermes)
        self.conn.executemany('INSERT INTO wake_runs(session,started_at,ended_at,exit_code) VALUES(?,?,?,0)',
                             [('prior-' + str(i), i, i + 1) for i in range(3)])
        self.conn.commit()
        with patch.object(wake, 'deliver') as native:
            with self.assertRaisesRegex(AssertionError, 'Three native executions'):
                live.execute(self.client, self.conn, setup, 'software', self.hermes)
            native.assert_not_called()

    def test_real_queue_registers_window_required_by_chat_inbox(self):
        setup = live.setup_project(self.client, self.conn, self.base, 'research', self.codex, self.hermes)
        topic = self.client.post('/thread', json={'project': setup['project'], 'creator': 'user',
            'title': 'Window fixture', 'members': [setup['actor']], 'opening': 'Synthetic fixture'}).json()['thread_id']
        incoming = self.client.post('/msg', json={'project': setup['project'], 'sender': 'user',
            'thread_id': topic, 'text': '@' + setup['actor'] + ' synthetic check', 'need_reply': True}).json()['message_id']
        params = {'profile': setup['actor'], 'kind': 'chat', 'thread_id': topic, 'project': setup['project'], 'limit': 100}
        self.assertEqual(self.client.get('/inbox', params=params).status_code, 409)
        with patch.object(wake, 'deliver', return_value={'ok': True}) as native:
            job_id = live.dispatch_topic(self.conn, setup['actor'], setup['project'], topic)
        native.assert_called_once()
        self.assertEqual(native.call_args.kwargs['job_id'], job_id)
        response = self.client.get('/inbox', params=params)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(incoming, [m['message_id'] for m in response.json()['messages']])

    def test_missing_hermes_topic_reply_preserves_native_usage_and_error(self):
        setup = live.setup_project(self.client, self.conn, self.base, 'research', self.codex, self.hermes)
        topic = self.client.post('/thread', json={'project': setup['project'], 'creator': 'user',
            'title': 'Diagnostic fixture', 'members': [setup['actor']], 'opening': 'Synthetic fixture'}).json()['thread_id']
        incoming = self.client.post('/msg', json={'project': setup['project'], 'sender': 'user',
            'thread_id': topic, 'text': '@' + setup['actor'] + ' synthetic check', 'need_reply': True}).json()['message_id']
        self.conn.execute('INSERT INTO wake_runs(session,project,profile,thread_id,runtime,started_at,ended_at,exit_code,runtime_session_id) '
            'VALUES(?,?,?,?,?,1,2,0,?)', ('fixture', setup['project'], setup['actor'], topic, 'hermes', 'fixture-title'))
        self.conn.commit()
        state = self.hermes / 'state.db'
        with closing(sqlite3.connect(state)) as native, native:
            native.execute('CREATE TABLE sessions(id,title,model,input_tokens,output_tokens)')
            native.execute("INSERT INTO sessions VALUES('native-id','fixture-title','fixture-model',123,45)")
            native.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,role,tool_name,content)')
            native.execute("INSERT INTO messages(role,tool_name,content) VALUES('tool','mcp_task_queue_group_send','Synthetic MCP timeout')")
            native.execute("INSERT INTO messages(role,content) VALUES('assistant','Synthetic send failed')")
        saved = live.native_diagnostics(self.conn, self.hermes)
        state.unlink()  # Production harness deletes the temporary state after collecting.
        self.assertEqual(saved[0]['sessions'][0]['id'], 'native-id')
        self.assertEqual(saved[0]['sessions'][0]['input_tokens'], 123)
        self.assertEqual(saved[0]['mcp_tools'], ['mcp_task_queue_group_send'])
        self.assertEqual(saved[0]['runtime_messages'][0]['content'], 'Synthetic MCP timeout')
        self.assertEqual(saved[0]['topic_replies'], [])
        self.assertIn({'message_id': incoming, 'status': 'pending'}, saved[0]['pending'])
        self.assertEqual(saved[0]['exit_code'], 0)

    def test_absent_native_state_keeps_scoped_execution_diagnostic(self):
        self.conn.execute("INSERT INTO wake_runs(session,runtime,started_at,ended_at,exit_code,project,profile) "
                          "VALUES('missing','hermes',1,2,1,'synthetic','synthetic')")
        self.conn.commit()
        saved = live.native_diagnostics(self.conn, self.hermes)
        self.assertEqual(saved[0]['exit_code'], 1)
        self.assertIn('collection_error', saved[0])
        self.assertFalse((self.hermes / 'state.db').exists())

    def test_synthetic_research_nested_result_preserves_strict_values(self):
        result = {
            'template': 'research', 'project_name': 'Synthetic research',
            'rule_token': 'RULE_fixture', 'closing_role': '调研负责人', 'member_count': 4,
            'scenario': {'costs': {'A': 2, 'B': 3}, 'sources': ['S1', 'S2'],
                         'recommendation': 'A', 'inference_boundary': 'Synthetic inputs only'},
        }
        setup = {'display_name': result['project_name'], 'rule_token': result['rule_token'], 'roles': [1, 2, 3, 4]}
        details = live.validate_reply(result, setup, 'research')
        self.assertEqual(details['costs'], {'A': 2, 'B': 3})
        root_result = {**result, **details, 'scenario': 'research'}
        self.assertEqual(live.validate_reply(root_result, setup, 'research'), details)
        result['scenario']['costs']['A'] = 20
        with self.assertRaisesRegex(AssertionError, 'Research costs mismatch'):
            live.validate_reply(result, setup, 'research')
        root_result['rule_token'] = 'wrong-token'
        with self.assertRaisesRegex(AssertionError, 'Project rule token mismatch'):
            live.validate_reply(root_result, setup, 'research')

    def test_writing_layouts_keep_source_check_required(self):
        setup = {'display_name': 'Synthetic writing', 'rule_token': 'RULE_fixture', 'roles': [1, 2, 3, 4]}
        base = {'template': 'writing', 'project_name': setup['display_name'], 'rule_token': setup['rule_token'],
                'closing_role': '主编', 'member_count': 4}
        details = {'article': '新成员加入项目。角色授予相应权限。', 'fact_check': 'S1 confirms role grants.'}
        for result in ({**base, **details, 'scenario': 'writing'}, {**base, 'scenario': details}):
            self.assertEqual(live.validate_reply(result, setup, 'writing'), details)
        with self.assertRaisesRegex(AssertionError, 'Writing source check incomplete'):
            live.validate_reply({**base, **details, 'fact_check': 'Unknown source'}, setup, 'writing')

    def test_writing_scope_copies_only_codex_fixture_authentication(self):
        source = self.base / 'synthetic-auth'; source.mkdir()
        (source / 'auth.json').write_text('{"synthetic":true}', encoding='utf-8')
        target = self.base / 'writing-homes'; target.mkdir()
        codex, hermes = live.temporary_homes(target, source, self.base / 'absent-hermes-source', ['writing'])
        self.assertEqual((codex / 'auth.json').read_bytes(), (source / 'auth.json').read_bytes())
        self.assertFalse((hermes / '.env').exists())


class CommandLineTests(unittest.TestCase):
    def test_live_requires_output_and_explicit_authentication_sources(self):
        absolute = str(Path.cwd() / 'reports/tests/unused.json')
        cases = [[], ['--evidence', 'relative.json'],
                 ['--evidence', absolute, '--scenarios', 'writing'],
                 ['--evidence', absolute, '--scenarios', 'research']]
        for args in cases:
            with self.subTest(args=args), \
                 patch.object(live.sys, 'argv', ['probe', '--live', *args]), \
                 patch.object(live.sys, 'stderr', io.StringIO()), \
                 patch.object(live, 'host_snapshot') as snapshot, \
                 patch.object(live, 'temporary_homes') as homes:
                with self.assertRaises(SystemExit) as raised:
                    live.main()
                self.assertEqual(raised.exception.code, 2)
                snapshot.assert_not_called()
                homes.assert_not_called()

    def test_no_live_flag_does_not_read_authentication_or_write_output(self):
        with patch.object(live.sys, 'argv', ['probe']), \
             patch.object(live, 'host_snapshot') as snapshot, \
             patch.object(live, 'temporary_homes') as homes:
            live.main()
            snapshot.assert_not_called()
            homes.assert_not_called()


if __name__ == '__main__':
    unittest.main()
