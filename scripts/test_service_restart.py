"""Restart authorization, handoff receipts and an isolated Windows restart loop."""
import io
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import site
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import auth, config, restart
from engine.maintenance import restart_engine

ROOT = Path(__file__).resolve().parents[1]


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='restart-receipt-')
        self.addCleanup(self.tmp.cleanup)
        self.environment = patch.dict(os.environ, {'GROUP_DATA_DIR': self.tmp.name,
            'GROUP_DB': str(Path(self.tmp.name) / 'isolated.db'), 'GROUP_ADMIN_PASSWORD': 'test'})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_request_is_deduplicated_and_pins_effective_configuration(self):
        controller = restart.Restart('old')
        with patch.object(config, 'host', return_value='127.0.0.1'), \
             patch.object(config, 'port', return_value=54321), \
             patch.object(restart.subprocess, 'Popen') as spawn:
            first = controller.begin()
            second = controller.begin()
        self.assertEqual(first['id'], second['id'])
        spawn.assert_called_once()
        env = spawn.call_args.kwargs['env']
        self.assertEqual(env['GROUP_ENGINE_URL'], 'http://127.0.0.1:54321')
        self.assertEqual(env['GROUP_RESTART_INSTANCE'], 'old')
        self.assertEqual(env['GROUP_DB'], str(Path(self.tmp.name) / 'isolated.db'))
        self.assertIn('restart_engine.py', spawn.call_args.args[0][-2])
        self.assertNotIn('GROUP_ADMIN_PASSWORD', first)

    def test_spawn_failure_is_persisted(self):
        with patch.object(restart.subprocess, 'Popen', side_effect=OSError('test spawn failed')):
            with self.assertRaises(OSError):
                restart.Restart('old').begin()
        self.assertEqual(restart.status()['state'], 'failed')

    def test_dead_helper_is_reported_without_restarting_again(self):
        restart.write_receipt({'id': 'test', 'state': 'restarting', 'created_at': 1, 'helper_pid': 12345})
        with patch.object(restart, '_pid_alive', return_value=False):
            self.assertEqual(restart.status()['state'], 'failed')

    def test_script_error_and_unchanged_instance_never_report_success(self):
        receipt = {'id': 'test', 'instance_id': 'old', 'program_root': 'test-root', 'db_path': 'test-db'}
        restart.write_receipt(receipt)
        with patch.object(restart_engine.subprocess, 'run', return_value=Mock(returncode=9)):
            self.assertEqual(restart_engine.run('test'), 1)
        self.assertEqual(restart.status()['state'], 'failed')
        restart.write_receipt(receipt)
        response = io.StringIO(json.dumps({**receipt, 'status': 'ok'}))
        opener = Mock()
        opener.open.return_value = response
        with patch.object(restart_engine.subprocess, 'run', return_value=Mock(returncode=0)), \
             patch.object(restart_engine.urllib.request, 'build_opener', return_value=opener):
            self.assertEqual(restart_engine.run('test'), 1)
        self.assertEqual(restart.status()['state'], 'failed')

    def test_http_authorization_and_deduplication(self):
        from fastapi.testclient import TestClient
        from engine.main import app
        controller = restart.Restart('old')
        with patch.object(app.state, 'server', Mock(), create=True), \
             patch.object(app.state, 'restart', controller, create=True), \
             patch.object(app.state, 'shutdown', Mock(snapshot=lambda: {'state': 'running'}), create=True), \
             patch.object(restart.subprocess, 'Popen') as spawn:
            client = TestClient(app)
            for path in ('/admin/restart', '/admin/shutdown'):
                self.assertEqual(client.post(path).status_code, 401)
                self.assertEqual(client.post(path, headers={'X-Group-Profile': 'bot-test',
                                 'X-Group-Key': auth.bot_token('bot-test')}).status_code, 403)
            headers = {'Authorization': 'Bearer ' + auth.login('test')['token']}
            first = client.post('/admin/restart', headers=headers)
            self.assertEqual(first.status_code, 202)
            self.assertEqual(client.post('/admin/restart', headers=headers).json()['id'], first.json()['id'])
            self.assertEqual(client.get('/admin/restart', headers=headers).json()['shutdown']['state'], 'running')
            self.assertEqual(client.get('/admin/restart').status_code, 401)
            spawn.assert_called_once()
            client.close()

    def test_shutdown_heartbeat_keeps_topic_turn_for_restart(self):
        from engine import heartbeat, wake
        wake._shutting_down.set()
        try:
            with patch.object(heartbeat, '_advance_turn_on_exit') as advance:
                heartbeat.on_exit('test', pid=12345, thread_id=99)
                advance.assert_not_called()
        finally:
            wake._shutting_down.clear()

    def test_wrong_instance_never_receives_stop_request(self):
        from engine.maintenance import stop_engine
        health = {'status': 'ok', 'instance_id': 'different', 'graceful_shutdown': 1, 'pid': 12345}
        opener = Mock()
        opener.open.return_value = io.StringIO(json.dumps(health))
        with patch.dict(os.environ, {'GROUP_RESTART_INSTANCE': 'expected'}), \
             patch.object(stop_engine.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(RuntimeError, '实例已变更'):
                stop_engine.stop('http://127.0.0.1:1')
        self.assertEqual(opener.open.call_count, 1)


@unittest.skipUnless(os.name == 'nt', 'Windows start.bat integration')
class RestartIntegration(unittest.TestCase):
    def test_two_real_restarts_keep_database_and_release_old_process(self):
        # Dedicated copied program/data/port. Only install.check is substituted;
        # the BAT, helper, stop/start clients, server and HTTP paths are real.
        with tempfile.TemporaryDirectory(prefix='restart space ') as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / 'engine', root / 'engine', ignore=shutil.ignore_patterns('__pycache__', '.hermes-tmp*'))
            (root / 'tools').mkdir()
            for name in ('service.py', 'start_engine.py', 'stop_engine.py', 'restart_engine.py',
                         'bootstrap.py', 'upgrade.py'):
                shutil.copy2(ROOT / 'tools' / name, root / 'tools' / name)
            (root / 'tools' / 'install.py').write_text('import sys\nsys.exit(0)\n', encoding='utf-8')
            shutil.copy2(ROOT / 'start.bat', root / 'start.bat')
            venv = root / '.venv'
            (venv / 'Scripts').mkdir(parents=True)
            source = Path(sys.executable).parent
            for name in ('python.exe', 'pythonw.exe'):
                shutil.copy2(source / name, venv / 'Scripts' / name)
            shutil.copy2(source.parent / 'pyvenv.cfg', venv / 'pyvenv.cfg')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            url = f'http://127.0.0.1:{port}'
            data = root / 'data'
            data.mkdir()
            env = {**os.environ, 'GROUP_DATA_DIR': str(data), 'GROUP_DB': str(data / 'group.db'),
                   'GROUP_ENGINE_HOST': '127.0.0.1', 'GROUP_ENGINE_PORT': str(port),
                   'GROUP_ENGINE_URL': url, 'XDG_GROUP_ENGINE_URL': url,
                   'GROUP_ATTACHMENTS_DIR': str(data / 'attachments'),
                   'GROUP_ADMIN_PASSWORD': 'test', 'GROUP_SECRET': 'test-only-secret',
                   'HERMES_HOME': str(data / 'hermes'),
                   'PYTHONPATH': os.pathsep.join(site.getsitepackages()),
                   'PATH': str(venv / 'Scripts') + os.pathsep + os.environ['PATH']}
            env.pop('GROUP_RESTART_INSTANCE', None)
            env.pop('GROUP_SERVICE_MAINTENANCE', None)
            browser_probe = root / 'browser_probe.py'
            browser_calls = root / 'browser_calls.txt'
            browser_probe.write_text(
                "import pathlib,sys\nwith pathlib.Path(__file__).with_name('browser_calls.txt').open('a') as f:\n"
                "    f.write(sys.argv[1] + '\\n')\n", encoding='utf-8')
            env['BROWSER'] = f'"{Path(sys.executable).as_posix()}" "{browser_probe.as_posix()}" %s'
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def request(path, method='GET'):
                headers = {}
                if method == 'POST' or path.startswith('/admin/') or path == '/health':
                    headers['Authorization'] = 'Bearer ' + token
                with opener.open(urllib.request.Request(url + path, headers=headers, method=method), timeout=5) as response:
                    return json.load(response)
            def wait_for(operation, timeout=35):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        value = operation()
                        if value:
                            return value
                    except OSError:
                        pass
                    time.sleep(.1)
                logs = list((data / 'logs').glob('*.log')) + [root / 'initial.log']
                self.fail('\n'.join(p.read_text(errors='replace')[-4000:] for p in logs if p.exists()))
            with patch.dict(os.environ, env):
                token = auth.login('test')['token']
                with (root / 'initial.log').open('wb') as log:
                    initial = subprocess.Popen([os.environ['COMSPEC'], '/d', '/c', str(root / 'start.bat')],
                                               cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT,
                                               creationflags=subprocess.CREATE_NO_WINDOW)
                    try:
                        health = wait_for(lambda: request('/health'))
                        self.assertEqual(Path(health['db_path']), data / 'group.db')
                        self.assertEqual(initial.wait(timeout=10), 0)
                        self.assertFalse(browser_calls.exists())
                        for _ in range(2):
                            launch = subprocess.run([str(venv / 'Scripts' / 'python.exe'),
                                                     str(root / 'tools' / 'service.py'), 'start'],
                                                    cwd=root, env=env, capture_output=True, timeout=15,
                                                    creationflags=subprocess.CREATE_NO_WINDOW)
                            self.assertEqual(launch.returncode, 0, (launch.stdout, launch.stderr))
                            self.assertEqual(request('/health')['instance_id'], health['instance_id'])
                        self.assertEqual(browser_calls.read_text().splitlines(), [url + '/app/'])
                        old_id, old_pid = health['instance_id'], health['pid']
                        second = subprocess.run([os.environ['COMSPEC'], '/d', '/c', str(root / 'start.bat')],
                                                cwd=root, env=env, input=b'\r\n', capture_output=True, timeout=45,
                                                creationflags=subprocess.CREATE_NO_WINDOW)
                        self.assertEqual(second.returncode, 0, (second.stdout, second.stderr))
                        health = request('/health')
                        self.assertNotEqual(health['instance_id'], old_id)
                        self.assertFalse(restart._pid_alive(old_pid))
                        self.assertEqual(browser_calls.read_text().splitlines(), [url + '/app/'])
                        import sqlite3
                        with closing(sqlite3.connect(data / 'group.db')) as conn, conn:
                            conn.execute('CREATE TABLE restart_probe(value TEXT)')
                            conn.execute("INSERT INTO restart_probe VALUES('preserved')")
                        (root / 'tools' / 'install.py').write_text(
                            "import sys\nprint('install-check-stdout', flush=True)\n"
                            "print('dependency-readiness-failed', file=sys.stderr, flush=True)\n"
                            "sys.exit(1)\n", encoding='utf-8')
                        failed = request('/admin/restart', 'POST')
                        def check_failed():
                            receipt = request('/admin/restart')
                            return receipt if receipt.get('id') == failed['id'] and receipt.get('state') == 'failed' else None
                        receipt = wait_for(check_failed)
                        wait_for(lambda: not restart._pid_alive(receipt['helper_pid']))
                        self.assertEqual(request('/health')['instance_id'], health['instance_id'])
                        log_text = (data / 'logs' / 'service-restart.log').read_text(encoding='utf-8', errors='replace')
                        self.assertIn('install-check-stdout', log_text)
                        self.assertIn('dependency-readiness-failed', log_text)
                        (root / 'tools' / 'install.py').write_text('import sys\nsys.exit(0)\n', encoding='utf-8')
                        for _ in range(2):
                            old_id, old_pid = health['instance_id'], health['pid']
                            operation = request('/admin/restart', 'POST')
                            def completed():
                                receipt = request('/admin/restart')
                                if receipt.get('state') == 'failed':
                                    self.fail(receipt['error'])
                                return receipt if receipt.get('id') == operation['id'] and receipt.get('state') == 'succeeded' else None
                            receipt = wait_for(completed)
                            health = request('/health')
                            self.assertNotEqual(old_id, health['instance_id'])
                            self.assertEqual(health['instance_id'], receipt['new_instance_id'])
                            self.assertFalse(restart._pid_alive(old_pid))
                            wait_for(lambda: not restart._pid_alive(receipt['helper_pid']))
                        with closing(sqlite3.connect(data / 'group.db')) as conn:
                            self.assertEqual(conn.execute('SELECT value FROM restart_probe').fetchone()[0], 'preserved')
                        self.assertEqual(browser_calls.read_text().splitlines(), [url + '/app/'])
                    finally:
                        # Only this fixture's port and copied instance are targeted.
                        try:
                            health = request('/health')
                            self.assertEqual(Path(health['program_root']), root)
                            request('/admin/shutdown', 'POST')
                            wait_for(lambda: not restart._pid_alive(health['pid']))
                        finally:
                            initial.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
