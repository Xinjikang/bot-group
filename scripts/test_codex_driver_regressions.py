"""Isolated regressions for Bot identity, native launch/stop/reset and reasoning."""
import json
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from engine import api, bot_identity, codex_reasoning, commands, db, discovery, session_adapter, stop, wake


class CodexDriverRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'codex'
        self.home.mkdir()
        env = patch.dict(os.environ, {'GROUP_DB': str(self.root / 'group.db'),
                                     'HERMES_HOME': str(self.root / 'hermes'),
                                     'CODEX_HOME': str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        adapter = patch.object(session_adapter, 'available', return_value=False)
        adapter.start()
        self.addCleanup(adapter.stop)
        wake._running_sessions.clear()
        wake._native_procs.clear()
        wake._running_wakes.clear()
        self.addCleanup(wake._running_sessions.clear)
        self.addCleanup(wake._native_procs.clear)
        self.addCleanup(wake._running_wakes.clear)
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        db.init_schema(self.conn)
        for project in ('p', 'q'):
            self.conn.execute("INSERT INTO projects(name,root,created_at,directory_mode) VALUES(?,?,?,'shared')",
                              (project, str(self.root), db.now_iso()))
        self.conn.commit()
        app = FastAPI()
        app.include_router(api.router_api)
        self.client = TestClient(app, client=('127.0.0.1', 4321))
        self.addCleanup(self.client.close)

    def test_native_cli_discovery_without_engine_path(self):
        local = self.root / 'Local'
        executable = local / 'OpenAI' / 'Codex' / 'bin' / 'installed-version' / 'codex.exe'
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b'fixture')
        with patch.dict(os.environ, {'CODEX_BIN': '', 'LOCALAPPDATA': str(local)}), \
             patch.object(wake.sys, 'platform', 'win32'), patch.object(wake.shutil, 'which', return_value=None):
            self.assertEqual(wake._codex_bin(), str(executable))
            with patch.dict(os.environ, {'CODEX_BIN': 'explicit-codex.exe'}):
                self.assertEqual(wake._codex_bin(), 'explicit-codex.exe')
            executable.unlink()
            with self.assertRaisesRegex(FileNotFoundError, 'CODEX_BIN'):
                wake._codex_bin()

    def register(self, host='codex', profile='default', project='p', name='知夏', root=None):
        result = self.client.post('/bots/register', json={'host_type': host, 'profile': profile,
            'config_root': str(root or self.home), 'project': project, 'display_name': name})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def test_same_host_profile_has_distinct_routing_and_permissions(self):
        hermes = self.register('hermes', root=self.root / 'hermes', name='蛇蛇')
        codex = self.register()
        hk, ck = hermes['member_profile'], codex['member_profile']
        self.assertNotEqual(hk, ck)
        from engine.mentions import parse_mentions
        self.assertEqual(parse_mentions(f'@{hk} @{ck}', self.conn, 'p'), [hk, ck])
        self.assertEqual(wake._bot_runtime(self.conn, 'p', hk), 'hermes')
        self.assertEqual(wake._bot_runtime(self.conn, 'p', ck), 'codex')
        self.conn.execute('INSERT INTO project_permissions VALUES(?,?,?)', ('p', ck, 'task.create'))
        self.conn.commit()
        self.assertEqual(self.conn.execute('SELECT profile FROM project_permissions').fetchone()[0], ck)
        self.assertTrue(wake.enqueue(self.conn, ck, 'work', 'test', project='p'))
        self.assertTrue(wake.enqueue(self.conn, hk, 'work', 'test', project='p'))
        db.init_schema(self.conn)
        self.assertFalse(self.conn.execute("SELECT 1 FROM bots WHERE host_type='hermes' AND profile=?", (ck,)).fetchone())
        self.assertEqual(bot_identity.resolve(self.conn, 'p', ck)['id'], codex['bot_id'])

    def test_reuse_preserves_name_handle_and_explicit_membership(self):
        first = self.register(name='夏夏')
        second = self.register(project='q', name='改名请求')
        self.assertEqual(first['bot_id'], second['bot_id'])
        self.assertEqual(first['member_profile'], second['member_profile'])
        rows = self.client.get('/members?project=q&active_only=true').json()['members']
        self.assertEqual([(r['display_name'], r['bot_id']) for r in rows], [('夏夏', first['bot_id'])])
        self.register(project='q')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM project_members').fetchone()[0], 2)

    def test_builtin_user_is_addressable_without_resource_row(self):
        from engine.mentions import parse_mentions
        self.assertFalse(self.conn.execute("SELECT 1 FROM members WHERE profile='user'").fetchone())
        self.assertEqual(parse_mentions('@user reply', self.conn, 'p'), ['user'])

    def test_workspace_list_handles_codex_without_native_session(self):
        hermes = self.register('hermes', root=self.root / 'hermes', name='蛇蛇')
        codex = self.register()
        member = codex['member_profile']
        with patch.object(session_adapter, 'profile_state_db',
                          wraps=session_adapter.profile_state_db) as state:
            full = self.client.get('/projects/p/workspaces')
            partial = self.client.get(f'/projects/p/workspaces?profile={member}')
            other = self.client.get('/projects/q/workspaces')
        self.assertEqual(full.status_code, 200, full.text)
        self.assertEqual(partial.status_code, 200, partial.text)
        self.assertEqual(other.status_code, 200, other.text)
        self.assertEqual({m['profile'] for m in full.json()['members']},
                         {hermes['member_profile'], member})
        self.assertEqual([m['profile'] for m in partial.json()['members']], [member])
        self.assertTrue(all(m['work_session_exists'] is False for m in full.json()['members']))
        self.assertTrue(all(call.args[0] == hermes['member_profile'] for call in state.call_args_list))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM session_bindings').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM session_switch_ops').fetchone()[0], 0)

    def test_codex_workspace_session_status_scopes_binding_and_reset(self):
        reg = self.register()
        member = reg['member_profile']
        self.register(project='q')
        self.assertTrue(session_adapter.codex_save_thread(self.conn, 'p', member, 'chat', 23, 'chat-thread'))
        def exists(project='p'):
            response = self.client.get(f'/projects/{project}/workspaces?profile={member}')
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()['members'][0]['work_session_exists']
        with patch.object(session_adapter, 'profile_state_db',
                          side_effect=AssertionError('Codex queried Hermes state')):
            self.assertFalse(exists())
            self.assertTrue(session_adapter.codex_save_thread(self.conn, 'p', member, 'work', None, 'work-thread'))
            self.assertTrue(exists())
            self.assertFalse(exists('q'))
            self.conn.execute("UPDATE session_bindings SET runtime='hermes' WHERE kind='work'")
            self.conn.commit()
            self.assertFalse(exists())
            self.conn.execute("UPDATE session_bindings SET runtime='codex' WHERE kind='work'")
            self.conn.commit()
            with patch.object(wake, 'session_busy', return_value=False):
                reset = self.client.post(f'/projects/p/members/{member}/reset-session')
            self.assertTrue(reset.json()['ok'], reset.text)
            self.assertFalse(exists())

    def test_discovery_includes_root_native_and_independent_homes(self):
        (self.home / 'config.toml').write_text('[profiles.old]\nmodel="test"\n', encoding='utf-8')
        (self.home / 'dev.config.toml').write_text('', encoding='utf-8')
        independent = self.home / 'summer'
        independent.mkdir()
        (independent / 'auth.json').write_text('{}', encoding='utf-8')
        candidates = discovery._scan_codex()
        self.assertEqual({c['profile'] for c in candidates}, {'default', 'old', 'dev', 'summer'})
        chosen = next(c for c in candidates if c['profile'] == 'summer')
        self.assertEqual(chosen['config_root'], str(independent))
        self.assertEqual(bot_identity.codex_settings(chosen), (str(independent), None))
        self.assertEqual(bot_identity.codex_settings({'config_root': str(self.home), 'profile': 'dev'}), (str(self.home), 'dev'))

    def capture_spawn(self, member):
        proc = MagicMock()
        proc.pid = 54321
        proc.poll.return_value = None
        with patch.object(wake.subprocess, 'Popen', return_value=proc) as popen, \
             patch.object(wake, 'session_busy', return_value=False), \
             patch('engine.project_rules.sync'), patch('threading.Thread'):
            result = wake._spawn(member, 'work', 'review signal', None, 'p')
        self.assertTrue(result['ok'], result)
        return popen.call_args.args[0], popen.call_args.kwargs

    def test_selected_profile_home_and_mcp_execution_context_reach_child(self):
        (self.home / 'dev.config.toml').write_text('', encoding='utf-8')
        reg = self.register(profile='dev')
        cmd, kwargs = self.capture_spawn(reg['member_profile'])
        self.assertEqual(kwargs['env']['CODEX_HOME'], str(self.home))
        self.assertEqual(cmd[cmd.index('--profile') + 1], 'dev')
        self.assertNotIn('--ignore-user-config', cmd)
        self.assertIn('XDG_GROUP_ENGINE_EXECUTION', cmd[cmd.index('-c') + 1])
        self.assertNotIn('GROUP_DB', cmd[cmd.index('-c') + 1])
        self.assertIn('GROUP_ENGINE_URL', cmd[cmd.index('-c') + 1])
        self.assertIn(reg['member_profile'], cmd[cmd.index('-c') + 1])
        row = self.conn.execute('SELECT bot_id,runtime FROM wake_runs').fetchone()
        self.assertEqual(tuple(row), (reg['bot_id'], 'codex'))

    def test_independent_home_is_selected(self):
        isolated = self.home / 'summer'
        isolated.mkdir()
        (isolated / 'config.toml').write_text('', encoding='utf-8')
        reg = self.register(profile='summer', root=isolated)
        cmd, kwargs = self.capture_spawn(reg['member_profile'])
        self.assertEqual(kwargs['env']['CODEX_HOME'], str(isolated))
        self.assertNotIn('--profile', cmd)

    def test_expired_auth_is_synchronized_before_child_launch(self):
        from scripts.test_codex_auth import credential
        import time
        isolated = self.home / 'profiles' / 'dev'
        isolated.mkdir(parents=True)
        (isolated / 'config.toml').write_text('', encoding='utf-8')
        (isolated / 'auth.json').write_text(json.dumps(credential(time.time() - 60)), encoding='utf-8')
        latest = json.dumps(credential(time.time() + 3600, refresh='latest'))
        (self.home / 'auth.json').write_text(latest, encoding='utf-8')
        reg = self.register(profile='dev', root=isolated)
        self.capture_spawn(reg['member_profile'])
        self.assertEqual((isolated / 'auth.json').read_text(), latest)

    def test_401_rechecks_auth_once_without_replaying_execution(self):
        reg = self.register()
        member = reg['member_profile']
        proc = MagicMock(pid=54321, returncode=1)
        proc.poll.return_value = 1
        error = 'workspace routing discovery unauthorized (401)'
        stream = [{'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'message': error}},
                  {'type': 'error', 'message': error},
                  {'type': 'error', 'message': error},
                  {'type': 'turn.failed', 'error': {'message': error}}]
        proc.stdout = io.StringIO('\n'.join(json.dumps(e) for e in stream))

        def thread(*args, **kwargs):
            result = MagicMock()
            result.start.side_effect = kwargs['target']
            return result

        with patch.object(wake.subprocess, 'Popen', return_value=proc) as popen, \
             patch.object(wake, 'session_busy', return_value=False), \
             patch('engine.codex_auth.sync', return_value=False) as sync, \
             patch('engine.project_rules.sync'), patch('threading.Thread', side_effect=thread), \
             patch.object(wake, '_handle_task_exit'), patch('engine.heartbeat.on_exit'):
            result = wake._spawn(member, 'work', 'signal', None, 'p')
        self.assertTrue(result['ok'], result)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(sync.call_count, 2)
        self.assertEqual(sync.call_args_list[0].kwargs, {})
        self.assertEqual(sync.call_args_list[1].kwargs, {'unauthorized': True})

    def test_expired_primary_prevents_launch_with_actionable_error(self):
        from engine.codex_auth import AuthSyncError
        member = self.register()['member_profile']
        with patch('engine.codex_auth.sync', side_effect=AuthSyncError('主账户凭据已过期')), \
             patch('engine.project_rules.sync'), patch.object(wake.subprocess, 'Popen') as popen:
            result = wake._spawn(member, 'work', 'signal', None, 'p')
        self.assertFalse(result['ok'])
        self.assertIn('主账户', result['detail'])
        popen.assert_not_called()

    def test_busy_new_is_rejected_and_first_reset_rejects_late_thread(self):
        reg = self.register()
        member = reg['member_profile']
        with patch.object(wake, 'session_busy', return_value=True):
            result = commands._handle_new(self.conn, 'p', [member], '', None)
        self.assertFalse(result['ok'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM session_switch_ops').fetchone()[0], 0)
        with patch.object(wake, 'session_busy', return_value=False):
            result = commands._handle_new(self.conn, 'p', [member], '', None)
        self.assertTrue(result['ok'])
        self.assertFalse(session_adapter.codex_save_thread(self.conn, 'p', member, 'work', None,
                                                          'old-thread', bot_id=reg['bot_id'], spawn_gen=None))
        self.assertTrue(session_adapter.codex_save_thread(self.conn, 'p', member, 'work', None,
                                                         'new-thread', bot_id=reg['bot_id'], spawn_gen=0))

    def running_record(self, member, started=100.5, session=None):
        session = session or wake.session_name(member, 'work', None, 'p')
        self.conn.execute("INSERT INTO wake_runs(session,started_at,pid,profile,project,runtime) VALUES(?,?,54321,?,'p','codex')",
                          (session, started, member))
        self.conn.commit()
        return session

    def test_stop_kills_owned_native_process_and_skips_hermes_database(self):
        reg = self.register()
        member = reg['member_profile']
        session = self.running_record(member)
        proc = MagicMock(pid=54321)
        proc.poll.return_value = None
        wake._native_procs[session] = (proc, 100.5)
        def kill(pid):
            self.assertEqual(pid, 54321)
            proc.poll.return_value = 1
            return True
        with patch.object(stop, '_kill_process_tree', side_effect=kill), \
             patch.object(stop, '_soft_delete_tail') as hermes:
            result = stop.stop_profile(member, 'p')
        self.assertTrue(result['stopped'])
        self.assertEqual(result['killed'], 1)
        hermes.assert_not_called()

    def test_native_pid_reuse_and_unknown_query(self):
        member = self.register()['member_profile']
        session = self.running_record(member)
        with patch.object(wake.process_query, 'command_line', return_value=['codex.exe', 'exec', 'unrelated']):
            self.assertEqual(wake.find_bot_processes(member, session), [])
        with patch.object(wake.process_query, 'command_line', return_value=None):
            self.assertIsNone(wake.find_bot_processes(member, session))
        context = dict(profile=member, project='p', session=session, wake_started_at=100.5, thread_id=None)
        argv = ['codex.exe', 'exec'] + wake._codex_mcp_config(member, 'p', context=context)
        with patch.object(wake.process_query, 'command_line', return_value=argv):
            self.assertEqual(wake.find_bot_processes(member, session), [54321])
        for change in ({'wake_started_at': 100.51}, {'profile': 'other'}, {'session': session + '-other'}, {'project': 'q'}):
            argv = ['codex.exe', 'exec'] + wake._codex_mcp_config(change.get('profile', member), 'p', context={**context, **change})
            with patch.object(wake.process_query, 'command_line', return_value=argv):
                self.assertEqual(wake.find_bot_processes(member, session), [])

    def test_stream_reader_binds_and_persists_before_watcher_closes_run(self):
        reg = self.register()
        member = reg['member_profile']
        proc = MagicMock(pid=54321, returncode=0)
        proc.poll.return_value = 0
        events = [{'type': 'thread.started', 'thread_id': 'native-thread'},
                  {'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'native thought'}},
                  {'type': 'turn.completed'}]
        proc.stdout = io.StringIO('\n'.join(json.dumps(e) for e in events))
        def thread(*args, **kwargs):
            result = MagicMock()
            result.start.side_effect = kwargs['target']
            return result
        with patch.object(wake.subprocess, 'Popen', return_value=proc), \
             patch.object(wake, 'session_busy', return_value=False), \
             patch('engine.project_rules.sync'), patch('threading.Thread', side_effect=thread), \
             patch.object(wake, '_handle_task_exit'), patch('engine.heartbeat.on_exit'):
            result = wake._spawn(member, 'work', 'signal', None, 'p')
        self.assertTrue(result['ok'], result)
        run = self.conn.execute('SELECT * FROM wake_runs').fetchone()
        self.assertEqual(run['runtime_session_id'], 'native-thread')
        self.assertIsNotNone(run['ended_at'])
        self.assertEqual(session_adapter.codex_thread_id(self.conn, 'p', member, 'work', None), 'native-thread')
        data = codex_reasoning.read(run['session'], run['started_at'], ended_at=run['started_at'])
        self.assertEqual(data['entries'][0]['reasoning'], 'native thought')
        self.assertTrue(data['entries'][-1]['finished'])
        self.assertNotIn(run['session'], wake._native_procs)

    def test_runtime_snapshot_survives_later_bot_changes(self):
        reg = self.register()
        session = self.running_record(reg['member_profile'])
        self.conn.execute("UPDATE bots SET runtime='hermes' WHERE id=?", (reg['bot_id'],))
        self.conn.commit()
        db.init_schema(self.conn)
        self.assertEqual(self.conn.execute('SELECT runtime FROM wake_runs WHERE session=?', (session,)).fetchone()[0], 'codex')

    def test_member_handle_collision_preserves_legacy_member(self):
        # The next Bot ID happens to match a legitimate old physical profile.
        next_id = self.conn.execute('SELECT MAX(id)+1 FROM bots').fetchone()[0]
        reserved = f'bot-{next_id}'
        self.conn.execute("INSERT INTO members(profile,display_name,role) VALUES(?,'legacy','')", (reserved,))
        self.conn.commit()
        reg = discovery.register_bot(self.conn, 'codex', 'default', str(self.home), 'new')
        member = bot_identity.enroll(self.conn, reg['bot_id'], 'p')
        self.assertNotEqual(member, reserved)
        self.assertEqual(self.conn.execute('SELECT display_name FROM members WHERE profile=?', (reserved,)).fetchone()[0], 'legacy')

    def test_stop_failure_preserves_jobs(self):
        member = self.register()['member_profile']
        wake.enqueue(self.conn, member, 'work', 'test', project='p')
        with patch.object(wake, 'find_bot_processes', return_value=[54321]), \
             patch.object(stop, '_kill_process_tree', return_value=False), \
             patch.object(stop, '_wait_dead', return_value=False):
            result = stop.stop_profile(member, 'p')
        self.assertFalse(result['stopped'])
        self.assertEqual(self.conn.execute('SELECT status FROM wake_jobs').fetchone()[0], 'queued')

    def test_reasoning_events_are_persisted_scoped_and_bind_send_result(self):
        member = self.register()['member_profile']
        session = self.running_record(member)
        send = {'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'tool': 'group_send',
                'arguments': {'content': 'hello'}, 'result': {'content': [{'text': '{"message_id":42}'}]}}}
        codex_reasoning.record(session, 100.5, {'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'checking'}})
        codex_reasoning.record(session, 100.5, send)
        codex_reasoning.record(session, 200.5, {'type': 'turn.completed'})
        data = codex_reasoning.read(session, 100.5)
        self.assertEqual(len(data['entries']), 2)
        self.assertEqual(data['entries'][1]['reply_message_id'], 42)
        self.assertEqual(codex_reasoning.read(session, 100.5, data['latest_id'])['entries'], [])
        with patch.object(session_adapter, 'compression_tip', side_effect=AssertionError('Hermes called')):
            result = self.client.get('/reasoning', params={'profile': member, 'session': session, 'wake_started_at': 100.5})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['entries'][0]['reasoning'], 'checking')


if __name__ == '__main__':
    unittest.main()
