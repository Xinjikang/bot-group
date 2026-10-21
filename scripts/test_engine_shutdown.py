"""Graceful engine exit: drain, cleanup, authorization, SSE, process and port."""
import json
import asyncio
from http.client import RemoteDisconnected
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import browser_resources as br, db, wake
from engine.lifecycle import Shutdown
from scripts.stop_engine import stop
from scripts.test_browser_resources import scope, launch_harness
from scripts.unified_test_support import isolated_database


class ShutdownTests(unittest.TestCase):
    def tearDown(self):
        wake._shutting_down.clear()

    def test_waits_for_execution_and_closes_gate_before_releasing(self):
        with isolated_database():
            exited = Mock()
            gate = threading.Event()
            controller = Shutdown(gate, exited, timeout=3)
            busy = threading.Event()
            busy.set()
            with patch.object(controller, '_busy', side_effect=busy.is_set), \
                 patch.object(br, 'release', return_value={'ok': True}) as release:
                controller.begin()
                self.assertTrue(wake._shutting_down.wait(2))
                self.assertFalse(gate.is_set())
                release.assert_not_called()
                self.assertFalse(wake._spawn('dev', 'work', None, None, 'alpha')['ok'])
                busy.clear()
                controller.worker.join(5)
                self.assertFalse(controller.worker.is_alive())
                release.assert_called_once_with()
                exited.assert_called_once_with()
                self.assertEqual(controller.snapshot()['state'], 'stopping')
                self.assertTrue(gate.is_set())

    def test_timeout_and_cleanup_failure_leave_engine_available_for_retry(self):
        exited = Mock()
        controller = Shutdown(threading.Event(), exited, timeout=0)
        with patch.object(controller, '_busy', return_value=True), \
             patch.object(controller, '_terminate', side_effect=RuntimeError('tree termination failed')), \
             patch.object(br, 'release') as release:
            controller.begin()
            controller.worker.join(3)
            self.assertEqual(controller.snapshot()['state'], 'failed')
            self.assertFalse(wake._shutting_down.is_set())
            self.assertFalse(controller.stop_event.is_set())
            release.assert_not_called()
            exited.assert_not_called()
        with patch.object(controller, '_busy', return_value=False), \
             patch.object(br, 'release', return_value={'ok': False}):
            controller.begin()
            controller.worker.join(3)
            self.assertEqual(controller.snapshot()['state'], 'failed')
            self.assertFalse(wake._shutting_down.is_set())
            self.assertFalse(controller.stop_event.is_set())
            exited.assert_not_called()
        with patch.object(controller, '_busy', return_value=False), \
             patch.object(br, 'release', return_value={'ok': True}):
            controller.begin()
            controller.worker.join(3)
            exited.assert_called_once_with()

    def test_workers_pause_during_drain_and_resume_after_timeout(self):
        from engine import queue, scanner
        for module, operation in ((queue, 'process_one'), (scanner, 'run_once')):
            with self.subTest(worker=module.__name__):
                stopped = threading.Event()
                processed = threading.Event()
                wake._shutting_down.set()
                controller = Shutdown(stopped, Mock(), timeout=0)
                with patch.object(module.db, 'get_connection', return_value=Mock()) as connect, \
                     patch.object(module.db, 'init_schema'), \
                     patch.object(module, operation, side_effect=lambda conn: processed.set()), \
                     patch.object(queue, 'watchdog'), \
                     patch.object(controller, '_busy', return_value=True), \
                     patch.object(controller, '_terminate', side_effect=RuntimeError('tree termination failed')):
                    worker = threading.Thread(target=module.run_forever, args=(stopped,), daemon=True)
                    worker.start()
                    try:
                        self.assertFalse(processed.wait(0.2))
                        connect.assert_not_called()
                        controller.begin()
                        controller.worker.join(3)
                        self.assertTrue(processed.wait(2))
                        self.assertTrue(worker.is_alive())
                    finally:
                        stopped.set()
                        worker.join(3)
                    self.assertFalse(worker.is_alive())

    def test_sse_remains_live_during_drain_and_failed_shutdown(self):
        from fastapi import FastAPI, Request
        from engine import api, events

        async def check():
            application = FastAPI()
            application.state.stop_event = threading.Event()
            request = Request({'type': 'http', 'app': application})
            channel = asyncio.Queue()
            with patch.object(events.bus, 'subscribe', return_value=channel):
                response = await api.get_events(request)
            stream = response.body_iterator
            try:
                self.assertEqual(await anext(stream), ': ok\n\n')
                wake._shutting_down.set()
                channel.put_nowait('during drain')
                self.assertIn('during drain', await asyncio.wait_for(anext(stream), 2))
                controller = Shutdown(application.state.stop_event, Mock(), timeout=0)
                with patch.object(controller, '_busy', return_value=True), \
                     patch.object(controller, '_terminate', side_effect=RuntimeError('tree termination failed')):
                    controller.begin()
                    await asyncio.to_thread(controller.worker.join, 3)
                channel.put_nowait('after failure')
                self.assertIn('after failure', await asyncio.wait_for(anext(stream), 2))
                application.state.stop_event.set()
                with self.assertRaises(StopAsyncIteration):
                    await anext(stream)
            finally:
                await stream.aclose()
        asyncio.run(check())

    def test_timeout_terminates_then_releases_then_exits(self):
        order = []
        controller = Shutdown(threading.Event(), lambda: order.append('exit'), timeout=0)
        with patch.object(controller, '_busy', return_value=True), \
             patch.object(controller, '_terminate', side_effect=lambda: order.append('terminate')), \
             patch.object(br, 'release', side_effect=lambda: order.append('release') or {'ok': True}):
            controller.begin()
            controller.worker.join(3)
        self.assertEqual(order, ['terminate', 'release', 'exit'])
        self.assertEqual(controller.snapshot()['state'], 'stopping')

    def test_terminating_state_deduplicates_shutdown_requests(self):
        controller = Shutdown(threading.Event(), Mock(), timeout=0)
        controller.state = 'terminating'
        self.assertEqual(controller.begin()['state'], 'terminating')
        self.assertIsNone(controller.worker)

    def test_recovered_run_termination_is_scoped_and_records_exit(self):
        from engine import process_tree
        with isolated_database():
            conn = db.get_connection()
            conn.execute("INSERT INTO wake_runs(session,started_at,profile,project,pid) VALUES('test-session',1,'test-bot','test',654321)")
            conn.commit()
            conn.close()
            controller = Shutdown(threading.Event(), Mock(), timeout=0)
            with patch.object(wake, 'find_bot_processes', return_value=[654322]), \
                 patch.object(controller, '_busy', return_value=False), \
                 patch.object(process_tree, 'terminate') as terminate, \
                 patch('engine.heartbeat.on_exit'):
                controller._terminate()
            terminate.assert_called_once_with({654322}, launchers={654322: 654321})
            conn = db.get_connection()
            row = conn.execute("SELECT ended_at,exit_code FROM wake_runs WHERE session='test-session'").fetchone()
            conn.close()
            self.assertIsNotNone(row['ended_at'])
            self.assertEqual(row['exit_code'], 1)

    def test_unknown_execution_blocks_tree_kill(self):
        from engine import process_tree
        controller = Shutdown(threading.Event(), Mock(), timeout=0)
        with patch.object(controller, '_runs', return_value=[{'profile': 'test', 'session': 'test'}]), \
             patch.object(wake, 'find_bot_processes', return_value=None), \
             patch.object(process_tree, 'terminate') as terminate:
            with self.assertRaisesRegex(RuntimeError, '无法核实'):
                controller._terminate()
            terminate.assert_not_called()

    def test_stop_client_accepts_disconnect_only_after_pid_and_port_exit(self):
        from engine import config
        health = {'pid': 12345, 'instance_id': 'isolated', 'graceful_shutdown': 1,
                  'program_root': str(config.program_root()), 'db_path': config.db_path()}
        def response(payload):
            from unittest.mock import MagicMock
            import io
            result = MagicMock()
            result.__enter__.return_value = io.StringIO(json.dumps(payload))
            return result
        opener = Mock()
        opener.open.side_effect = [response(health), response(health), RemoteDisconnected()]
        with patch('scripts.stop_engine.urllib.request.build_opener', return_value=opener), \
             patch('scripts.stop_engine.auth._password', return_value=''), \
             patch('scripts.stop_engine._pid_alive', return_value=False), \
             patch('scripts.stop_engine.socket.create_connection', side_effect=ConnectionRefusedError):
            self.assertIn('优雅退出', stop('http://127.0.0.1:1', timeout=1))

    def test_stop_client_preserves_old_server_without_shutdown_support(self):
        posts = []
        class OldEngine(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

            def do_POST(self):
                posts.append(self.path)
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), OldEngine)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaisesRegex(RuntimeError, '旧版引擎'):
                stop(f'http://127.0.0.1:{server.server_port}')
            self.assertEqual(posts, [])
            self.assertTrue(thread.is_alive())
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()

    def test_upgrade_refuses_foreign_engine_before_posting(self):
        from scripts import upgrade
        requests = []
        class ForeignEngine(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(('GET', self.path))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({
                    'status': 'ok', 'graceful_shutdown': 1, 'pid': os.getpid(),
                    'instance_id': 'foreign', 'program_root': str(Path(__file__).parent),
                    'db_path': str(Path(__file__).parent / 'unused.db'),
                }).encode())

            def do_POST(self):
                requests.append(('POST', self.path))
                self.send_response(500)
                self.end_headers()

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), ForeignEngine)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix='upgrade-target-') as tmp, \
                 patch.object(upgrade.config, 'host', return_value='127.0.0.1'), \
                 patch.object(upgrade.config, 'port', return_value=server.server_port), \
                 patch.object(upgrade.config, 'program_root', return_value=Path(tmp)), \
                 patch.object(upgrade, '_db_file', return_value=Path(tmp) / 'group.db'):
                self.assertEqual(upgrade._stop_independent(), 1)
            self.assertEqual(requests, [('GET', '/health')])
            self.assertTrue(thread.is_alive())
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()

    @unittest.skipUnless(os.name == 'nt', 'Windows engine process/IPC integration')
    def test_real_engine_exit_releases_harness_and_sse(self):
        with tempfile.TemporaryDirectory(prefix='engine-exit-') as tmp:
            database = str(Path(tmp) / 'group.db')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            url = f'http://127.0.0.1:{port}'
            env = {**os.environ, 'GROUP_DB': database, 'GROUP_ENGINE_PORT': str(port),
                   'GROUP_DATA_DIR': tmp, 'GROUP_ATTACHMENTS_DIR': str(Path(tmp) / 'attachments'),
                   'GROUP_ADMIN_PASSWORD': 'isolated-password', 'GROUP_SECRET': 'isolated-secret',
                   'HERMES_HOME': tmp}
            with open(Path(tmp) / 'engine.log', 'wb') as log:
                proc = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', '-m', 'engine.main'],
                                        cwd=Path(__file__).resolve().parents[1], env=env,
                                        stdout=log, stderr=subprocess.STDOUT,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                from engine import auth
                with patch.dict(os.environ, env):
                    headers = {'Authorization': 'Bearer ' + auth.login('isolated-password')['token']}
                stream = None
                harness = None
                try:
                    deadline = time.monotonic() + 20
                    while True:
                        try:
                            with opener.open(urllib.request.Request(url + '/health', headers=headers), timeout=1) as response:
                                health = json.load(response)
                            break
                        except OSError:
                            if proc.poll() is not None or time.monotonic() > deadline:
                                self.fail(Path(tmp, 'engine.log').read_text(errors='replace'))
                            time.sleep(0.1)
                    self.assertEqual(health['graceful_shutdown'], 1)
                    self.assertEqual(Path(health['db_path']), Path(database))
                    self.assertEqual(Path(health['program_root']), Path(__file__).resolve().parents[1])
                    for denied_headers in ({}, {'X-Group-Profile': 'dev'}):
                        with self.assertRaises(urllib.error.HTTPError) as rejected:
                            opener.open(urllib.request.Request(url + '/admin/shutdown', method='POST', headers=denied_headers))
                        self.assertIn(rejected.exception.code, (401, 403))
                    stream = opener.open(urllib.request.Request(url + '/events', headers=headers), timeout=10)
                    self.assertEqual(stream.readline(), b': ok\n')
                    with patch.dict(os.environ, env):
                        browser_env, _, _ = scope()
                        harness, endpoint = launch_harness(self, browser_env)
                        result = stop(url, timeout=20)
                        self.assertIn('优雅退出', result)
                        self.assertEqual(proc.wait(timeout=5), 0)
                        self.assertEqual(harness.wait(timeout=5), 0)
                        self.assertFalse(endpoint.exists())
                        conn = db.get_connection()
                        self.assertEqual(conn.execute('SELECT count(*) FROM browser_resources').fetchone()[0], 0)
                        conn.close()
                    self.assertEqual(stream.read().strip(), b'')
                finally:
                    if stream:
                        stream.close()
                    if proc.poll() is None:
                        # Test-only child; cleanup via the same authenticated API.
                        with patch.dict(os.environ, env):
                            stop(url, timeout=20)
                        proc.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
