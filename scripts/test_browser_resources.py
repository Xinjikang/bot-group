"""Isolated browser ownership, task-event cleanup, and authenticated IPC tests."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import browser_resources as br, db, taskpool
from scripts.unified_test_support import isolated_database, confirmed_requirement_fields, role_id_of
from scripts.test_task_brief import BRIEF


def fake_harness(root):
    root = Path(root)
    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen()
    token = 'isolated-test-token'
    (root / 'bu-test.pid').write_text(str(os.getpid()))
    endpoint = root / 'bu-test.port'
    endpoint.write_text(json.dumps({'port': server.getsockname()[1], 'token': token}))
    try:
        while True:
            with server.accept()[0] as client:
                req = json.loads(client.makefile('rb').readline())
                response = {'error': 'unauthorized'}
                if req.get('token') == token:
                    response = {'pong': True, 'pid': os.getpid()} if req['meta'] == 'ping' else {'ok': True}
                client.sendall((json.dumps(response) + '\n').encode())
                if req.get('meta') == 'shutdown' and response.get('ok'):
                    break
    finally:
        server.close()
        endpoint.unlink()


def scope(project='alpha', profile='dev', task_id=None, thread_id=None, active=False):
    env = {}
    started = time.time_ns() / 1e9
    session = f'{profile}-{project}-' + ('work' if thread_id is None else f'chat-{thread_id}')
    conn = db.get_connection()
    try:
        conn.execute('INSERT INTO wake_runs(session,started_at,project,profile,thread_id,ended_at) VALUES(?,?,?,?,?,?)',
                     (session, started, project, profile, thread_id, None if active else started + 1))
        br.register(conn, env, project=project, profile=profile, session=session,
                    started_at=started, thread_id=thread_id, task_id=task_id)
        conn.commit()
    finally:
        conn.close()
    return env, session, started


def launch_harness(test, env):
    root = Path(env['BH_RUNTIME_DIR'])
    endpoint = root / 'bu-test.port'
    proc = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', __file__, '--fake-harness', str(root)],
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    def cleanup():
        if proc.poll() is None:
            if endpoint.exists():
                br._request(endpoint, json.loads(endpoint.read_text()), 'shutdown')
            proc.wait(timeout=10)
    test.addCleanup(cleanup)
    deadline = time.monotonic() + 10
    while not endpoint.exists():
        if proc.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError('isolated harness did not start')
        time.sleep(0.02)
    return proc, endpoint


def create_task():
    rid = taskpool.req_create('alpha', 'default', 'browser', **confirmed_requirement_fields())['requirement_id']
    tid = taskpool.task_create('alpha', 'architect', rid, [{'title': 'browser', 'next_bot': 'dev', **BRIEF}])['task_ids'][0]
    return rid, tid


class BrowserResourcesTests(unittest.TestCase):
    def setUp(self):
        self.fixture = isolated_database()
        self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)

    def test_namespace_isolated_even_for_same_explicit_browser_name(self):
        first, _, _ = scope()
        second, _, _ = scope(project='beta')
        self.assertNotEqual(first['BH_RUNTIME_DIR'], second['BH_RUNTIME_DIR'])
        self.assertNotEqual(first['BU_NAME'], 'default')
        self.assertEqual(first['BH_RUNTIME_DIR_SHARED'], '1')

    @unittest.skipUnless(os.name == 'nt', 'Windows TCP harness protocol')
    def test_shutdown_exits_only_selected_process_and_keeps_artifacts(self):
        first, _, _ = scope(task_id=1)
        second, _, _ = scope(project='beta', task_id=1)
        proc, endpoint = launch_harness(self, first)
        other, other_endpoint = launch_harness(self, second)
        artifact = Path(first['BH_TMP_DIR'])
        artifact.mkdir()
        (artifact / 'evidence.png').write_bytes(b'evidence')
        self.assertTrue(br.release(project='alpha', task_id=1)['ok'])
        self.assertEqual(proc.wait(timeout=10), 0)
        self.assertFalse(endpoint.exists())
        self.assertIsNone(other.poll())
        self.assertTrue(other_endpoint.exists())
        self.assertTrue((artifact / 'evidence.png').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows TCP harness protocol')
    def test_wrong_pid_does_not_shutdown_endpoint_and_failure_is_retryable(self):
        env, _, _ = scope(task_id=1)
        proc, endpoint = launch_harness(self, env)
        pid_file = endpoint.with_suffix('.pid')
        original = pid_file.read_text()
        pid_file.write_text(str(os.getpid()))
        result = br.release(project='alpha')
        self.assertFalse(result['ok'])
        self.assertIsNone(proc.poll())
        pid_file.write_text(original)
        self.assertTrue(br.release(project='alpha')['ok'])
        self.assertEqual(proc.wait(timeout=10), 0)

    def test_handoff_releases_sender_completion_releases_task(self):
        _, tid = create_task()
        taskpool.task_claim('alpha', 'dev')
        own, _, _ = scope(task_id=tid)
        other, _, _ = scope(profile='reviewer', task_id=tid)
        chat, _, _ = scope(thread_id=22)
        with patch.object(br, '_close_scope') as close:
            rejected = taskpool.task_log('alpha', 'dev', tid, 'dev', result='', next_bot='default',
                                        expected_role_id=role_id_of('alpha', 'default'))
            self.assertIn('error', rejected)
            close.assert_not_called()
            result = taskpool.task_log('alpha', 'dev', tid, 'dev', result='verified', next_bot='default',
                                      expected_role_id=role_id_of('alpha', 'default'))
            self.assertTrue(result['browser_cleanup']['ok'])
            close.assert_called_once_with(Path(own['BH_RUNTIME_DIR']).name)
            close.reset_mock()
            self.assertTrue(taskpool.task_complete('alpha', 'default', tid)['completed'])
            close.assert_called_once_with(Path(other['BH_RUNTIME_DIR']).name)
        self.assertTrue(Path(chat['BH_RUNTIME_DIR']).exists())

    def test_cancel_paths_and_archive_use_committed_events(self):
        for action in ('bot', 'admin', 'requirement'):
            with self.subTest(action=action):
                rid, tid = create_task()
                env, _, _ = scope(task_id=tid)
                with patch.object(br, '_close_scope') as close:
                    if action == 'bot':
                        result = taskpool.task_cancel('alpha', 'architect', tid)
                    elif action == 'admin':
                        result = taskpool.admin_task_cancel('alpha', tid)
                    else:
                        result = taskpool.req_cancel('alpha', 'architect', rid)
                    self.assertTrue(result['cancelled'])
                    close.assert_called_once_with(Path(env['BH_RUNTIME_DIR']).name)
        env, _, _ = scope(thread_id=4)
        with patch('engine.project_workspaces.ensure_idle'), patch.object(br, '_close_scope') as close:
            self.assertTrue(taskpool.project_archive('alpha')['archived'])
            close.assert_called_once_with(Path(env['BH_RUNTIME_DIR']).name)

    def test_claim_binds_only_active_work_and_run_exit_only_retries_marked(self):
        _, tid = create_task()
        env, session, started = scope(active=True)
        scope(thread_id=4, active=True)
        with patch.object(br, '_close_scope') as close:
            taskpool.task_claim('alpha', 'dev')
            br.finish_run(session, started)
            close.assert_not_called()
            taskpool.task_cancel('alpha', 'architect', tid)
            close.assert_called_once_with(Path(env['BH_RUNTIME_DIR']).name)
            conn = db.get_connection()
            conn.execute('UPDATE wake_runs SET ended_at=? WHERE session=? AND started_at=?',
                         (time.time(), session, started))
            conn.commit()
            conn.close()
            br.finish_run(session, started)
            self.assertEqual(close.call_count, 2)

    def test_failed_cleanup_keeps_task_result_and_records_error(self):
        _, tid = create_task()
        scope(task_id=tid)
        with patch.object(br, '_close_scope', side_effect=TimeoutError('test timeout')):
            result = taskpool.task_cancel('alpha', 'architect', tid)
        self.assertTrue(result['cancelled'])
        self.assertFalse(result['browser_cleanup']['ok'])
        conn = db.get_connection()
        try:
            self.assertEqual(conn.execute('SELECT last_error FROM browser_resources').fetchone()[0], 'test timeout')
        finally:
            conn.close()

    def test_project_removal_releases_its_resources_and_retry_is_scoped(self):
        from engine import project_removal
        env, _, _ = scope()
        other, _, _ = scope(project='beta')
        with patch('engine.project_workspaces.ensure_idle'), patch.object(br, '_close_scope') as close:
            self.assertTrue(project_removal.change('alpha', True)['browser_cleanup']['ok'])
            close.assert_called_once_with(Path(env['BH_RUNTIME_DIR']).name)
            close.reset_mock()
            self.assertTrue(project_removal.change('alpha', True)['browser_cleanup']['ok'])
            close.assert_not_called()
        conn = db.get_connection()
        try:
            self.assertEqual(conn.execute('SELECT id FROM browser_resources').fetchone()[0],
                             Path(other['BH_RUNTIME_DIR']).name)
        finally:
            conn.close()

    def test_release_excludes_managed_execution_resources(self):
        from engine import hermes_gateway_ledger as ledger
        _, session_local, _ = scope(profile='dev')
        _, session_gw, started_gw = scope(profile='bot')
        conn = db.get_connection()
        try:
            # gateway 托管未终态执行（hermes_executions 非 terminal）对应 bot 资源应保留。
            ledger.prepare(conn, session=session_gw, project='alpha', profile='bot', kind='work',
                           thread_id=None, task_id=None, job_id=None, event_floor=0, source='',
                           python='["hermes"]', home='/isolated', workdir='/isolated/work',
                           session_id='sid', profile_id='home', started_at=started_gw)
        finally:
            conn.close()
        result = br.release(exclude_managed=True)
        self.assertTrue(result['ok'])
        conn = db.get_connection()
        try:
            rows = {r['session']: r for r in conn.execute('SELECT * FROM browser_resources')}
        finally:
            conn.close()
        # 本地执行资源已释放（删除）；gateway 托管未终态执行资源保留且不标记释放（设计 4.4）。
        self.assertNotIn(session_local, rows)
        self.assertIn(session_gw, rows)
        self.assertEqual(rows[session_gw]['release_requested'], 0)


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--fake-harness':
        fake_harness(sys.argv[2])
    else:
        unittest.main()
