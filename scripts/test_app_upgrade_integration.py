"""Isolated real API -> detached upgrade -> restart/rollback integration."""
from contextlib import closing
import io
import json
import os
from pathlib import Path
import shutil
import site
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import uuid
import zipfile
from unittest.mock import patch

from engine import auth, restart
from engine.maintenance import make_release

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'nt', 'Windows installation lifecycle')
class AppUpgradeIntegration(unittest.TestCase):
    def test_real_api_upgrade_and_failed_upgrade_restore_running_service(self):
        self.run_upgrade()

    def test_previous_tools_package_upgrades_and_rolls_back_through_original_helper(self):
        self.run_upgrade('1ac85a2d4fccc751343c940c3312f660314bd67d')

    def run_upgrade(self, previous_revision=None):
        with tempfile.TemporaryDirectory(prefix='ui upgrade ') as tmp:
            workspace = Path(tmp)
            source, root, data = (workspace / name for name in ('source', 'installed', 'data'))
            root.mkdir(); data.mkdir()
            for name in ('engine', 'tools'):
                shutil.copytree(ROOT / name, source / name,
                    ignore=shutil.ignore_patterns('__pycache__', '.hermes-tmp*'))
            (source / 'tools/install.py').write_text('import sys\nsys.exit(0)\n')
            index = workspace / 'releases.json'
            def build(version, previous=False):
                out = workspace / version
                if previous:
                    old_source = workspace / 'previous-source'
                    raw = subprocess.check_output(['git', 'archive', '--format=zip', previous_revision,
                                                   'engine', 'tools'], cwd=ROOT)
                    with zipfile.ZipFile(io.BytesIO(raw)) as old:
                        old.extractall(old_source)
                    (old_source / 'tools/install.py').write_text('import sys\nsys.exit(0)\n')
                    subprocess.run([sys.executable, '-X', 'utf8', '-B', str(old_source / 'tools/make_release.py'),
                                    '--version', version, '--out', str(out)], check=True, cwd=old_source)
                else:
                    self.assertEqual(make_release.make_release(source, out, 'bot-group', version), 0)
                release = json.loads((out / 'releases.json').read_text())
                for key in ('source', 'manifest'):
                    release['releases'][0][key] = (out / release['releases'][0][key]).as_uri()
                index.write_text(json.dumps(release))
                return out / f'bot-group-{version}.zip'
            with zipfile.ZipFile(build('v1.0.0', previous=bool(previous_revision))) as archive:
                archive.extractall(root)
            venv = root / '.venv'
            (venv / 'Scripts').mkdir(parents=True)
            for name in ('python.exe', 'pythonw.exe'):
                shutil.copy2(Path(sys.executable).parent / name, venv / 'Scripts' / name)
            shutil.copy2(Path(sys.executable).parent.parent / 'pyvenv.cfg', venv / 'pyvenv.cfg')
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            url = f'http://127.0.0.1:{port}'
            env = {**os.environ, 'GROUP_DATA_DIR': str(data), 'GROUP_DB': str(data / 'group.db'),
                   'GROUP_ENGINE_HOST': '127.0.0.1', 'GROUP_ENGINE_PORT': str(port),
                   'GROUP_ENGINE_URL': url, 'XDG_GROUP_ENGINE_URL': url,
                   'GROUP_ATTACHMENTS_DIR': str(data / 'attachments'),
                   'GROUP_ADMIN_PASSWORD': 'test', 'GROUP_SECRET': 'test-only-secret',
                   'HERMES_HOME': str(data / 'hermes'), 'RELEASE_INDEX_URL': index.as_uri(),
                   'PYTHONPATH': os.pathsep.join(site.getsitepackages()), 'PYTHONDONTWRITEBYTECODE': '1'}
            env.pop('GROUP_RESTART_INSTANCE', None)
            env.pop('GROUP_SERVICE_MAINTENANCE', None)
            browser_probe = workspace / 'browser_probe.py'
            browser_calls = workspace / 'browser_calls.txt'
            browser_probe.write_text(
                "import pathlib,sys\npathlib.Path(__file__).with_name('browser_calls.txt').write_text(sys.argv[1])\n",
                encoding='utf-8')
            env['BROWSER'] = f'"{Path(sys.executable).as_posix()}" "{browser_probe.as_posix()}" %s'
            env.pop('UPGRADE_URL', None); env.pop('UPGRADE_SHA256', None)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with patch.dict(os.environ, env):
                token = auth.login('test')['token']
            def request(path, body=None):
                headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
                payload = json.dumps(body).encode() if body is not None else None
                with opener.open(urllib.request.Request(url + path, headers=headers, data=payload), timeout=5) as response:
                    return json.load(response)
            def wait_for(operation, timeout=50):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        result = operation()
                        if result:
                            return result
                    except OSError:
                        pass
                    time.sleep(.15)
                logs = list((data / 'logs').glob('*.log')) + [workspace / 'initial.log']
                self.fail('\n'.join(p.read_text(errors='replace')[-5000:] for p in logs if p.exists()))
            with (workspace / 'initial.log').open('wb') as log:
                initial = subprocess.Popen([str(venv / 'Scripts/python.exe'), '-B', '-m', 'engine.main'],
                    cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
                helper_pid = None
                try:
                    health = wait_for(lambda: request('/health'))
                    with closing(sqlite3.connect(data / 'group.db')) as conn, conn:
                        conn.execute('CREATE TABLE upgrade_probe(value TEXT)')
                        conn.execute("INSERT INTO upgrade_probe VALUES('preserved')")
                    (data / 'attachments').mkdir(exist_ok=True)
                    (data / 'attachments/probe.txt').write_text('preserved')
                    attempts = [('v1.1.0', ''), ('v1.2.0', 'ready'), ('v1.3.0', 'start')]
                    if previous_revision:
                        attempts.insert(0, ('v1.0.1', 'ready'))
                    current_version = 'v1.0.0'
                    for target, fail in attempts:
                        (source / 'tools/install.py').write_text('import sys\nsys.exit(0)\n')
                        if fail == 'ready':
                            (source / 'tools/install.py').write_text("import sys\nsys.exit(9 if sys.argv[1] == 'check' else 0)\n")
                        elif fail == 'start':
                            (source / 'tools/install.py').write_text('import sys\nsys.exit(0)\n')
                            (source / 'tools/service.py').write_text('import sys\nsys.exit(9)\n')
                        build(target)
                        check = request('/system/updates')
                        self.assertTrue(check['can_upgrade'], check)
                        self.assertTrue(check['update_available'], check)
                        old_pid, old_id = health['pid'], health['instance_id']
                        operation = request('/system/upgrade', {'request_id': uuid.uuid4().hex, 'version': target})
                        def completed():
                            receipt = request('/system/upgrade')
                            return receipt if receipt.get('id') == operation['id'] and receipt.get('state') in ('succeeded', 'failed') else None
                        receipt = wait_for(completed)
                        helper_pid = receipt['helper_pid']
                        self.assertEqual(receipt['state'], 'failed' if fail else 'succeeded', receipt)
                        if fail:
                            self.assertIn('已恢复原版本并启动', receipt['error'])
                        health = request('/health')
                        self.assertNotEqual(health['instance_id'], old_id)
                        self.assertFalse(restart._pid_alive(old_pid))
                        if not fail:
                            current_version = target
                        self.assertEqual(request('/system/version')['current_version'], current_version)
                        self.assertEqual((root / 'tools/__init__.py').exists(),
                                         bool(previous_revision and current_version == 'v1.0.0'))
                        wait_for(lambda: not restart._pid_alive(helper_pid))
                        self.assertFalse(browser_calls.exists(), 'Maintenance opened a browser')
                    with closing(sqlite3.connect(data / 'group.db')) as conn:
                        self.assertEqual(conn.execute('SELECT value FROM upgrade_probe').fetchone()[0], 'preserved')
                    self.assertEqual((data / 'attachments/probe.txt').read_text(), 'preserved')
                finally:
                    try:
                        health = request('/health')
                        self.assertEqual(Path(health['program_root']), root)
                        request('/admin/shutdown', {})
                        wait_for(lambda: not restart._pid_alive(health['pid']))
                    finally:
                        initial.wait(timeout=15)
                        if helper_pid:
                            wait_for(lambda: not restart._pid_alive(helper_pid))


if __name__ == '__main__':
    unittest.main()
