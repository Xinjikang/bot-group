"""DSH 0.2.2 startup selection and private credential regressions."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from engine import dsh_auth, dsh_auto, native_runtime


class CompatibilityTests(unittest.TestCase):
    def test_package_version_and_native_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = root / 'bin' / 'dsh'
            entry.parent.mkdir()
            entry.touch()
            bot = {'runtime_entry': str(entry), 'config_root': tmp, 'profile': 'member'}
            for version, native in [('0.2.1-rc.5', False), ('0.2.2-rc.2', True),
                                    ('0.2.2', True), ('0.3.0', True), ('unknown', False)]:
                with self.subTest(version=version):
                    (root / 'package.json').write_text(json.dumps(
                        {'name': '@deepseek-ai/dsh', 'version': version}))
                    self.assertEqual(dsh_auto.native_selection(bot), native)
                    command, _ = native_runtime.dsh(bot, native_auto=native, resume='saved')
                    self.assertEqual('--permission-mode' in command, native)
                    self.assertIn('saved', command)
                    self.assertEqual(command[-1], '-')
                    if native:
                        self.assertEqual(command[command.index('--approval-policy') + 1], 'never')

    def test_npm_wrapper_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'node_modules'
            entry = root / '.bin' / 'dsh.cmd'
            entry.parent.mkdir(parents=True)
            entry.touch()
            package = root / '@deepseek-ai' / 'dsh'
            package.mkdir(parents=True)
            (package / 'package.json').write_text(json.dumps(
                {'name': '@deepseek-ai/dsh', 'version': '0.2.2-rc.2'}))
            self.assertEqual(native_runtime.dsh_version(str(entry)), '0.2.2-rc.2')

    def test_posix_copy_private_creation_and_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.credentials.yaml').write_bytes(b'synthetic-credential')
            private = root / 'private'
            original_open = os.open
            with patch.object(dsh_auth.sys, 'platform', 'linux'), \
                 patch.object(dsh_auth.os, 'chmod', wraps=os.chmod) as chmod, \
                 patch.object(dsh_auth.os, 'open', wraps=original_open) as opened:
                dest = dsh_auth.copy_credentials(tmp, str(private))
                chmod.assert_called_once_with(private, 0o700)
                self.assertEqual(opened.call_args.args[2], 0o600)
                self.assertTrue(opened.call_args.args[1] & os.O_EXCL)
                self.assertEqual(dest.read_bytes(), b'synthetic-credential')
                if os.name == 'posix':
                    self.assertEqual(private.stat().st_mode & 0o777, 0o700)
                    self.assertEqual(dest.stat().st_mode & 0o777, 0o600)
            with patch.object(dsh_auth.sys, 'platform', 'linux'), \
                 patch.object(dsh_auth.os, 'chmod', side_effect=OSError('denied')):
                with self.assertRaises(dsh_auth.AuthSyncError):
                    dsh_auth.copy_credentials(tmp, str(root / 'failed'))
                self.assertFalse((root / 'failed' / 'credentials.yaml').exists())

    def test_exclusive_creation_preserves_competing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.credentials.yaml').write_bytes(b'synthetic')
            dest = root / 'credentials.yaml'
            def compete(_):
                dest.write_bytes(b'other-writer')
            with patch.object(dsh_auth, 'restrict_dir', side_effect=compete):
                with self.assertRaises(dsh_auth.AuthSyncError):
                    dsh_auth.copy_credentials(tmp, tmp)
            self.assertEqual(dest.read_bytes(), b'other-writer')

    @unittest.skipUnless(shutil.which('node'), 'Node.js required')
    def test_native_adapter_creation_resume_no_tools_and_block(self):
        source = dsh_auto.adapter_source(native_selection=True)
        source = 'const isAgentLoopRequest = () => false;\n' + source[source.index('export const name'):]
        harness = r'''
const fs = require('node:fs');
const assert = require('node:assert/strict');
(async () => {
    const source = fs.readFileSync(0, 'utf8');
    const adapter = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
    const handlers = {};
    let preset = 'workspace-write';
    let executed = 0;
    adapter.apply({permissionPresets: {current: () => preset,
        set: () => { throw Error('session/created cannot append'); }},
        on: (name, fn) => { handlers[name] = fn; }});
    assert.equal(handlers['session/created'], undefined);
    for (const id of ['new', 'resumed']) {
        preset = 'auto';
        handlers['session/event']({id}, {type: 'turn/start'});
        assert.equal(handlers['tools/pre-execute']({agent:{session:{id}}}, () => ++executed), executed);
    }
    preset = 'workspace-write';
    const denied = handlers['tools/pre-execute']({agent:{session:{}}}, () => ++executed);
    assert.equal(denied.kind, 'deny');
    assert.equal(executed, 2);
})().catch(e => {console.error(e); process.exitCode = 1;});
'''
        result = subprocess.run(['node', '-e', harness], input=source,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stderr.splitlines()
        self.assertIn('preset=auto preexec=0', lines[0])
        self.assertIn('preset=n/a', lines[-1])


if __name__ == '__main__':
    unittest.main()
