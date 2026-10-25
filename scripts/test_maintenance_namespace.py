"""Maintenance entry points and real Hermes imports coexist in one interpreter."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from engine.maintenance import make_release

ROOT = Path(__file__).resolve().parents[1]
SOURCE = os.environ.get('HERMES_TEST_SOURCE', '')
COEXIST = r'''
import os, sys, tempfile
from pathlib import Path
from unittest.mock import patch

with tempfile.TemporaryDirectory() as folder:
    home = Path(folder)
    os.environ.update(HERMES_HOME=str(home), GROUP_DATA_DIR=str(home), GROUP_DB=str(home / 'group.db'))
    from engine import session_adapter
    with patch.object(session_adapter, '_hermes_src', return_value=Path(sys.argv[1])), \
         patch.object(session_adapter, 'profile_state_db', return_value=home / 'state.db'):
        def maintenance():
            from engine import app_updates, app_upgrade, restart
            from engine.maintenance import releases, upgrade, update_engine, restart_engine, install
            target = releases.Release('v99.0.0', 'isolated', 'a' * 64)
            with patch.object(upgrade, 'discover_release', return_value=target):
                assert app_updates.check_update()['latest_version'] == 'v99.0.0'
            assert app_upgrade.status() == {}
            assert restart.status() == {}
            assert callable(restart_engine.run) and callable(update_engine.run)
            assert install.PROGRAM_ROOT == Path.cwd()

        if sys.argv[2] == 'maintenance-first':
            maintenance()
            assert 'tools' not in sys.modules
        sid = session_adapter.create_session('review', 'namespace-session')
        tools_package = sys.modules['tools']
        maintenance()
        assert session_adapter.session_exists('review', sid)
        assert session_adapter.resolve_session_by_title('review', 'namespace-session') == sid
        assert session_adapter.compression_tip('review', sid) == sid
        second = session_adapter.create_session('review', 'namespace-new-session')
        assert second != sid and session_adapter.session_exists('review', second)
        assert sys.modules['tools'] is tools_package
        assert Path(tools_package.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())
        print('PASS', sys.argv[2], 'update-check/create/restore/new/restart-imports')
'''


class MaintenanceNamespaceTests(unittest.TestCase):
    def run_python(self, args, *, cwd=ROOT, env=None, expected=0):
        result = subprocess.run([sys.executable, '-X', 'utf8', '-B', *args], cwd=cwd,
                                env=env, capture_output=True, text=True, encoding='utf-8',
                                timeout=45, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    @unittest.skipUnless(SOURCE, 'set HERMES_TEST_SOURCE for real Hermes coexistence')
    def test_maintenance_then_hermes_in_same_process(self):
        self.run_python(['-c', COEXIST, SOURCE, 'maintenance-first'])

    @unittest.skipUnless(SOURCE, 'set HERMES_TEST_SOURCE for real Hermes coexistence')
    def test_hermes_then_maintenance_in_same_process(self):
        self.run_python(['-c', COEXIST, SOURCE, 'hermes-first'])

    def test_cli_entries_from_another_working_directory_without_site_packages(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ('install', 'upgrade', 'make_release', 'service', 'stop_engine', 'repair_hermes_identity'):
                with self.subTest(entry=name):
                    self.run_python(['-S', str(ROOT / 'tools' / (name + '.py')), '--help'], cwd=folder)

    def test_exported_bootstrap_runs_alone_and_release_contains_implementation(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'release'
            self.assertEqual(make_release.make_release(ROOT, output, 'bot-group', 'v99.0.0'), 0)
            manifest = json.loads((output / 'release_manifest.json').read_text())
            self.assertIn('engine/maintenance/upgrade.py', manifest['program_files'])
            self.assertIn('engine/maintenance/install.py', manifest['program_files'])
            self.assertNotIn('tools/__init__.py', manifest['program_files'])
            with zipfile.ZipFile(output / 'bot-group-v99.0.0.zip') as archive:
                self.assertIn(b'engine.maintenance', archive.read('scripts/service.py'))
            standalone = output / 'bootstrap.py'
            self.assertEqual(standalone.read_bytes(), (ROOT / 'engine/maintenance/bootstrap.py').read_bytes())
            env = {key: value for key, value in os.environ.items()
                   if key not in ('BOOTSTRAP_URL', 'BOOTSTRAP_SHA256', 'PYTHONPATH')}
            result = self.run_python(['-S', str(standalone)], cwd=folder, env=env, expected=2)
            self.assertIn('BOOTSTRAP_URL', result.stderr)


if __name__ == '__main__':
    unittest.main()
