"""Hermes installation identity and legacy session-ID compatibility."""
import base64
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from engine import hermes_runtime as runtime, session_adapter
from engine.maintenance import install


class HermesCompatibilityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / 'home'
        self.source = self.home / 'hermes-agent'
        self.source.mkdir(parents=True)
        self.native = self.source / 'venv/Scripts/hermes.exe'
        self.native.parent.mkdir(parents=True)
        self.native.touch()
        self.write_source()
        environment = patch.dict(os.environ, {'HERMES_HOME': str(self.home), 'HERMES_BIN': str(self.native)})
        environment.start()
        self.addCleanup(environment.stop)
        old_path = sys.path[:]
        saved = {n: m for n, m in sys.modules.items() if n == 'hermes_state' or n.startswith('hermes_state_')}
        for name in saved:
            del sys.modules[name]
        def restore():
            for name in list(sys.modules):
                if name == 'hermes_state' or name.startswith('hermes_state_'):
                    del sys.modules[name]
            sys.modules.update(saved)
            sys.path[:] = old_path
        self.addCleanup(restore)

    def write_source(self, missing=()):
        methods = ''.join(f'    def {name}(self, *args): pass\n' for name in runtime.SESSION_METHODS if name not in missing)
        (self.source / 'hermes_state.py').write_text('class SessionDB:\n' + methods, encoding='utf-8')
        (self.source / 'hermes_state_registry.py').write_text('def acquire(path): pass\n', encoding='utf-8')

    def test_venv_layout(self):
        self.assertEqual(session_adapter._hermes_src(), self.source)
        self.assertEqual(install.detect_runtime()['hermes']['source'], str(self.source))

    def test_home_bin_layout(self):
        entry = self.home / 'bin/hermes.exe'
        entry.parent.mkdir()
        entry.touch()
        with patch.dict(os.environ, {'HERMES_BIN': str(entry)}):
            self.assertEqual(session_adapter._hermes_src(), self.source)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows forwarding wrapper')
    def test_wrapper_uses_actual_target(self):
        wrapper = self.home / 'bin/hermes.cmd'
        wrapper.parent.mkdir()
        wrapper.write_text('@echo off\n"%~dp0../hermes-agent/venv/Scripts/hermes.exe" %*\n')
        with patch.dict(os.environ, {'HERMES_BIN': str(wrapper)}):
            self.assertEqual(Path(runtime.executable()), self.native)
            self.assertEqual(session_adapter._hermes_src(), self.source)

    def test_configured_home_precedes_other_path_install(self):
        entry = self.home / 'bin/hermes.exe'
        entry.parent.mkdir()
        entry.touch()
        with patch.dict(os.environ, {'HERMES_BIN': ''}), patch.object(runtime.shutil, 'which', return_value='other.exe'):
            self.assertEqual(Path(runtime.executable()), entry)

    def write_launcher(self, script='raise RuntimeError("static check executed launcher")'):
        wrapper = self.home / 'bin/hermes.cmd'
        wrapper.parent.mkdir(exist_ok=True)
        payload = base64.b64encode(script.encode('utf-8')).decode('ascii')
        code = f"import base64; exec(base64.b64decode('{payload}'))"
        wrapper.write_text(f'@echo off\r\n"{sys.executable}" -I -c "{code}" %*\r\n',
                           encoding='utf-8-sig', newline='')
        return wrapper, code

    @unittest.skipUnless(sys.platform == 'win32', 'Windows base64 launcher')
    def test_base64_launcher_auto_detection_preserves_argv_and_source(self):
        wrapper, code = self.write_launcher()
        with patch.dict(os.environ, {'HERMES_BIN': ''}), \
             patch('subprocess.run', side_effect=AssertionError('static check launched process')):
            self.assertEqual(runtime.native_entry(str(wrapper)), str(wrapper))
            self.assertEqual(runtime.command(), [sys.executable, '-I', '-c', code])
            self.assertEqual(session_adapter._hermes_src(), self.source)
            result = install.detect_runtime()['hermes']
        self.assertEqual(result['status'], 'compatible')
        self.assertEqual(result['bin'], str(wrapper))
        self.assertEqual(result['source'], str(self.source))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows base64 launcher')
    def test_gateway_pool_receives_complete_launcher_command(self):
        from engine import hermes_gateway as gateway
        from engine import hermes_connections
        wrapper, code = self.write_launcher()
        with patch.dict(os.environ, {'HERMES_BIN': str(wrapper)}), \
             patch.object(hermes_connections, 'acquire') as acquire:
            _, home, argv = gateway.connection('default')
        self.assertEqual(argv, [sys.executable, '-I', '-c', code])
        self.assertEqual(home, self.home)
        acquire.assert_called_once_with(self.home, argv, callback=None, business=True)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows launcher grammar')
    def test_launcher_rejects_extra_commands_arguments_and_invalid_payload(self):
        wrapper, code = self.write_launcher()
        valid = f'"{sys.executable}" -I -c "{code}" %*'
        invalid = [valid + ' & echo extra', valid + ' | more', 'call ' + valid,
                   'set X=1\n' + valid, 'if exist x ' + valid, 'goto x\n' + valid,
                   valid.replace('-I', '-i'), valid.replace('-I', '-I --extra'),
                   valid.replace(str(sys.executable), '%UNRESOLVED%/python.exe'),
                   valid.replace(str(sys.executable), '../python.exe'),
                   valid.replace(str(sys.executable), str(self.native)),
                   valid.replace(code, "import base64; exec(base64.b64decode('abc'))"),
                   valid.replace(code, 'print(123)'), valid.replace(' %*', ' %* > output')]
        for script in invalid:
            with self.subTest(script=script):
                wrapper.write_text('@echo off\n' + script + '\n', encoding='utf-8')
                with self.assertRaises(runtime.HermesConfigurationError):
                    runtime.command(str(wrapper))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows installation layout')
    def test_configured_home_venv_precedes_path(self):
        with patch.dict(os.environ, {'HERMES_BIN': ''}), \
             patch.object(runtime.shutil, 'which', return_value='other.exe'):
            self.assertEqual(runtime.executable(), str(self.native))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows installation layout')
    def test_localappdata_launcher_and_native_fallback(self):
        local = self.root / 'local'
        installed = local / 'hermes/hermes-agent/venv/Scripts/hermes.exe'
        installed.parent.mkdir(parents=True)
        installed.touch()
        wrapper = local / 'hermes/bin/hermes.cmd'
        wrapper.parent.mkdir()
        wrapper.write_text(f'@echo off\n"{installed}" %*\n', encoding='utf-8')
        with patch.dict(os.environ, {'HERMES_BIN': '', 'HERMES_HOME': '', 'LOCALAPPDATA': str(local)}), \
             patch.object(runtime.shutil, 'which', return_value=None):
            self.assertEqual(runtime.command(), [str(installed)])
            wrapper.unlink()
            self.assertEqual(runtime.command(), [str(installed)])

    def test_conflicting_home_and_entry_fail(self):
        other = self.root / 'other'
        other.mkdir()
        for name in ('hermes_state.py', 'hermes_state_registry.py'):
            (other / name).write_text('')
        entry = other / 'bin/hermes.exe'
        entry.parent.mkdir()
        entry.touch()
        with self.assertRaisesRegex(runtime.HermesConfigurationError, '冲突'):
            runtime.source_directory(self.home, str(entry))

    def test_unknown_layout_fails(self):
        entry = self.root / 'unknown/hermes.exe'
        entry.parent.mkdir()
        entry.touch()
        with self.assertRaisesRegex(runtime.HermesConfigurationError, '无法.*定位'):
            runtime.source_directory(self.home, str(entry))

    def test_explicit_missing_entry_does_not_choose_other_install(self):
        with patch.dict(os.environ, {'HERMES_BIN': str(self.root / 'absent.exe')}):
            self.assertEqual(runtime.diagnostic(self.home)['status'], 'unavailable')

    def test_profile_home_stays_cli_managed(self):
        with patch.dict(os.environ, {'HERMES_HOME': str(self.home / 'profiles/demo')}):
            self.assertFalse(session_adapter.available())

    def test_explicit_entry_without_home_does_not_invent_profile_root(self):
        with patch.dict(os.environ, {'HERMES_HOME': ''}), patch.object(session_adapter, '_DEFAULT_HOME_FALLBACK', self.root):
            self.assertFalse(session_adapter.available())

    def test_error_identity_survives_string_only_queue_reporting(self):
        from engine.execution_errors import classify
        for cls in (runtime.HermesCompatibilityError, runtime.HermesConfigurationError):
            self.assertEqual(classify(str(cls('test'))), 'configuration')

    def test_static_check_legacy_does_not_execute_source(self):
        with (self.source / 'hermes_state.py').open('a') as stream:
            stream.write("\nraise RuntimeError('must not execute during install')\n")
        self.assertEqual(runtime.inspect_source(self.source)['session_ids'], 'legacy')
        self.assertNotIn('hermes_state', sys.modules)

    def test_static_check_inherited_methods(self):
        (self.source / 'hermes_state_mixins.py').write_bytes((self.source / 'hermes_state.py').read_bytes())
        (self.source / 'hermes_state.py').write_text('from hermes_state_mixins import SessionDB as Base\nclass SessionDB(Base): pass\n')
        self.assertEqual(runtime.inspect_source(self.source)['status'], 'compatible')

    def test_missing_required_method_fails_static_and_runtime(self):
        self.write_source(missing=('get_compression_tip',))
        for check in (runtime.inspect_source, runtime.prepare_source):
            with self.subTest(check=check), self.assertRaisesRegex(runtime.HermesCompatibilityError, 'get_compression_tip'):
                check(self.source)

    def test_runtime_import_dependency_is_reported(self):
        (self.source / 'hermes_state_registry.py').write_text('import nonexistent_hermes_dependency\n')
        with self.assertRaisesRegex(runtime.HermesCompatibilityError, 'nonexistent_hermes_dependency'):
            runtime.prepare_source(self.source)

    def test_loaded_other_install_is_rejected(self):
        import types
        sys.modules['hermes_state'] = types.SimpleNamespace(__file__=str(self.root / 'other/hermes_state.py'))
        with self.assertRaises(runtime.HermesConfigurationError):
            runtime.prepare_source(self.source)

    def test_selected_source_wins_over_other_sys_path(self):
        other = self.root / 'other'
        other.mkdir()
        (other / 'hermes_state.py').write_text("raise RuntimeError('wrong source')")
        sys.path.append(str(self.source))
        sys.path.insert(0, str(other))
        runtime.prepare_source(self.source)
        self.assertEqual(Path(sys.modules['hermes_state'].__file__).parent, self.source)

    def test_legacy_id_shape_and_uniqueness(self):
        runtime.prepare_source(self.source)
        ids = {runtime.new_session_id(self.source) for _ in range(100)}
        self.assertEqual(len(ids), 100)
        for value in ids:
            self.assertRegex(value, r'^\d{8}_\d{6}_[0-9a-f]{6}$')

    def test_native_id_function_is_used(self):
        (self.source / 'hermes_state_ids.py').write_text("def new_session_id(): return 'native-id'\n")
        runtime.prepare_source(self.source)
        self.assertEqual(runtime.inspect_source(self.source)['session_ids'], 'native')
        self.assertEqual(runtime.new_session_id(self.source), 'native-id')

    def test_existing_ids_module_dependency_error_never_falls_back(self):
        (self.source / 'hermes_state_ids.py').write_text('import nonexistent_hermes_dependency\n')
        runtime.prepare_source(self.source)
        with self.assertRaisesRegex(runtime.HermesCompatibilityError, 'nonexistent_hermes_dependency'):
            runtime.new_session_id(self.source)

    def test_native_module_missing_function_is_reported(self):
        (self.source / 'hermes_state_ids.py').write_text('x = 1\n')
        runtime.prepare_source(self.source)
        with self.assertRaises(runtime.HermesCompatibilityError):
            runtime.new_session_id(self.source)

    def test_install_diagnostic_retains_path_and_reason(self):
        self.write_source(missing=('create_session',))
        result = install.detect_runtime()['hermes']
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('create_session', result['detail'])
        self.assertEqual(result['bin'], str(self.native))

    def test_native_id_syntax_error_is_actionable(self):
        (self.source / 'hermes_state_ids.py').write_text('def broken(\n')
        runtime.prepare_source(self.source)
        with self.assertRaises(runtime.HermesCompatibilityError):
            runtime.new_session_id(self.source)


if __name__ == '__main__':
    unittest.main()
