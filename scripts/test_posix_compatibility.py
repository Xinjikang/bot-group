"""Cross-platform install/runtime contracts with isolated processes and fixtures."""
import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

from engine import directory_picker, hud_runtime, process_query, process_tree, stop, wake
from engine.maintenance import bootstrap, install, make_release

ROOT = Path(__file__).resolve().parents[1]


class CompatibilityTests(unittest.TestCase):
    def test_lock_markers_are_evaluated_inside_target_interpreter(self):
        from pip._vendor.packaging.markers import default_environment
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / 'requirements.lock'
            lock.write_text('common==1.0\npywin32==312; sys_platform == "win32"\n', encoding='utf-8')
            with patch.object(install, 'LOCKFILE', lock), patch.object(install.subprocess, 'run') as run:
                run.return_value.returncode = 0
                self.assertTrue(install.verify_lock_versions())
                code = run.call_args.args[0][2]
            for platform in ('darwin', 'linux', 'win32'):
                environment = dict(default_environment(), sys_platform=platform)
                with self.subTest(platform=platform), patch.object(sys, 'argv', ['check', str(lock)]), \
                     patch('pip._vendor.packaging.markers.default_environment', return_value=environment), \
                     patch('importlib.metadata.version', side_effect=lambda name: {'common': '1.0', 'pywin32': '312'}[name]) as version, \
                     contextlib.redirect_stdout(io.StringIO()):
                    exec(code, {})
                    self.assertEqual(version.call_count, 2 if platform == 'win32' else 1)
            with patch.object(sys, 'argv', ['check', str(lock)]), \
                 patch('importlib.metadata.version', return_value='0.0'), \
                 contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                exec(code, {})

    def test_install_refuses_to_stamp_dependency_drift(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            for name in ('_invalidate_state', 'check_python', 'ensure_venv', 'pip_install', 'selfcheck'):
                stack.enter_context(patch.object(install, name))
            stack.enter_context(patch.object(install, 'detect_runtime', return_value={}))
            stack.enter_context(patch.object(install, 'first_config', return_value=root / '.env'))
            stack.enter_context(patch.object(install, 'verify_lock_versions', return_value=False))
            stamp = stack.enter_context(patch.object(install, 'write_state'))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            self.assertEqual(install.do_install(), 1)
            stamp.assert_not_called()

    def test_posix_names_preserve_profile_and_session_scope(self):
        for name in ('python', 'Python', 'python3', 'python3.11', 'hermes', 'python.exe'):
            proc = Mock(pid=123)
            proc.name.return_value = name
            proc.is_running.return_value = True
            proc.cmdline.return_value = [name, 'hermes', '-p', 'dev', 'chat', '-c', 'topic']
            with self.subTest(name=name), patch.object(wake, '_find_codex_processes', return_value=False), \
                 patch('engine.hermes_gateway.process_state', return_value=False), \
                 patch.object(process_query, '_candidate_pids', return_value=[123]), \
                 patch.object(process_query.psutil, 'Process', return_value=proc):
                self.assertEqual(wake.find_bot_processes('dev', 'topic'), [123])
                self.assertEqual(wake.find_bot_processes('other', 'topic'), [])
        self.assertFalse(process_query.matches_name('python-malware', {'python.exe'}))

    def test_posix_snapshot_and_launcher_scope(self):
        import psutil
        proc = Mock(pid=30)
        proc.ppid.return_value = 20
        proc.name.return_value = 'python3.11'
        with patch.object(process_tree, 'os', SimpleNamespace(name='posix')), \
             patch.object(psutil, 'pids', return_value=[30, 99]), \
             patch.object(psutil, 'Process', side_effect=[proc, psutil.NoSuchProcess(99)]):
            self.assertEqual(process_tree.snapshot(), {30: (20, 'python3.11')})
        graph = {10: (1, 'python3'), 20: (10, 'nohup'), 30: (20, 'python3.11'),
                 40: (30, 'hermes'), 50: (40, 'node'), 60: (10, 'python')}
        self.assertEqual(process_tree.plan({40}, graph, 10), ({20}, {20, 30, 40, 50}))
        self.assertEqual(process_tree.plan({40}, graph, 10, {40: 30})[0], {30})

    def test_posix_kill_only_selected_tree(self):
        import psutil
        owner, root, child = Mock(pid=10), Mock(pid=30), Mock(pid=40)
        owner.parents.return_value = [Mock(pid=1)]
        root.children.return_value = [child]
        with patch.object(stop, 'os', SimpleNamespace(name='posix')), \
             patch.object(psutil, 'Process', side_effect=lambda pid=None: owner if pid is None else root), \
             patch.object(psutil, 'wait_procs', return_value=([], [])) as wait:
            self.assertFalse(stop._kill_process_tree(10))
            self.assertTrue(stop._kill_process_tree(30))
            root.kill.assert_called_once()
            child.kill.assert_called_once()
            owner.kill.assert_not_called()
            self.assertEqual(wait.call_args.args[0], [child, root])

    def test_picker_paths_cancellation_and_failure(self):
        for platform in ('darwin', 'linux'):
            for selected in ('/tmp/项目 "quoted"', ''):
                with self.subTest(platform=platform, selected=selected), \
                     patch.object(directory_picker.sys, 'platform', platform), \
                     patch.object(directory_picker.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, selected, '')) as run:
                    initial = '/tmp/项目 "quoted"; $command'
                    self.assertEqual(directory_picker.choose_directory(initial), selected or None)
                    argv = run.call_args.args[0]
                    self.assertNotIn(initial, argv)
                    self.assertEqual(run.call_args.kwargs['env']['GROUP_PICKER_INITIAL'], initial)
                    self.assertEqual(argv[0], '/usr/bin/osascript' if platform == 'darwin' else sys.executable)
                    self.assertEqual(run.call_args.kwargs['creationflags'], 0)
            with patch.object(directory_picker.sys, 'platform', platform), \
                 patch.object(directory_picker.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', '')), \
                 self.assertRaises(directory_picker.HTTPException) as error:
                directory_picker.choose_directory('')
            self.assertEqual(error.exception.status_code, 503)
            self.assertFalse(directory_picker._lock.locked())

    def test_hud_import_failure_is_environment_error_without_secret_output(self):
        script = 'import sys; print("private-token", file=sys.stderr); raise ModuleNotFoundError("private-module")'
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(RuntimeError) as error:
            hud_runtime._run([sys.executable, '-c', script], '', hud_runtime._env(), tmp)
        self.assertIn('运行环境', str(error.exception))
        self.assertNotIn('模型配置', str(error.exception))
        self.assertNotIn('private', str(error.exception))

    def test_release_sh_permissions_and_extract_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'source'; root.mkdir()
            (root / 'upgrade.sh').write_bytes(b'#!/usr/bin/env bash\r\nexit 0\r\n')
            out = Path(tmp) / 'release'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(make_release.make_release(root, out, 'fixture', 'v1.0.0'), 0)
            archive = out / 'fixture-v1.0.0.zip'
            with zipfile.ZipFile(archive) as z:
                self.assertEqual(z.getinfo('upgrade.sh').external_attr >> 16, 0o100755)
                self.assertNotIn(b'\r', z.read('upgrade.sh'))
            extracted = Path(tmp) / 'extracted'
            with patch.object(bootstrap, 'os', SimpleNamespace(name='posix')), patch.object(Path, 'chmod') as chmod:
                bootstrap._extract(archive, extracted, 'zip')
                chmod.assert_called_once_with(0o755)

    @unittest.skipUnless(os.environ.get('BASH_TEST_BIN'), 'set BASH_TEST_BIN to a native Bash executable')
    def test_upgrade_shell_forwards_arguments_and_exit_status(self):
        with tempfile.TemporaryDirectory(prefix='upgrade shell ') as tmp:
            root = Path(tmp)
            shutil.copyfile(ROOT / 'upgrade.sh', root / 'upgrade.sh')
            venv = root / '.venv' / 'bin'; venv.mkdir(parents=True)
            python = venv / 'python'
            python.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$FIXTURE_BASE"\n', encoding='utf-8')
            base = root / 'base python'
            base.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$FIXTURE_OUTPUT"\nexit 7\n', encoding='utf-8')
            python.chmod(0o755); base.chmod(0o755)
            output = root / 'args.txt'
            env = dict(os.environ, FIXTURE_BASE=base.as_posix(), FIXTURE_OUTPUT=output.as_posix())
            result = subprocess.run([os.environ['BASH_TEST_BIN'], str(root / 'upgrade.sh'), '--index-url', 'https://example.invalid/a b'],
                                    env=env, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertEqual(output.read_text().splitlines(), ['-X', 'utf8', 'tools/upgrade.py', 'upgrade', '--index-url', 'https://example.invalid/a b'])


if __name__ == '__main__':
    unittest.main()
