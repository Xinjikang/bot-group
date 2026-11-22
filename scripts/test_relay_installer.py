"""Exercise the standalone distribution using temporary profile directories."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

from engine.maintenance import make_release
from tools import install_relay


ROOT = Path(__file__).resolve().parents[1]


class RelayInstallerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.version = 'v1.2.3'
        self.sums = make_release._relay_checksums(ROOT, self.root, self.version)
        self.installer = self.root / 'install-relay.pyz'
        self.home = self.root / 'named profile'
        self.home.mkdir()
        self.plugin = self.home / 'plugins/bot-group-relay'

    def test_artifacts_match_and_help_runs_without_hermes(self):
        help_text = subprocess.check_output([sys.executable, str(self.installer), '--help'], text=True)
        self.assertIn('--profile', help_text)
        tar = self.root / f'bot-group-relay-{self.version}.tar.gz'
        for artifact in (tar, self.installer):
            self.assertIn(hashlib.sha256(artifact.read_bytes()).hexdigest() + '  ' + artifact.name, self.sums)
        with zipfile.ZipFile(self.installer) as bundle:
            self.assertEqual(bundle.read('relay.tar.gz'), tar.read_bytes())
        stage = self.root / 'contents'
        stage.mkdir()
        version, paths = install_relay.unpack(self.installer, stage)
        self.assertEqual(version, self.version)
        manifest = json.loads((stage / 'manifest.json').read_text())
        self.assertEqual(set(manifest['files']), {p.as_posix() for p in paths} - {'manifest.json'})
        for path, info in manifest['files'].items():
            self.assertEqual(hashlib.sha256((stage / path).read_bytes()).hexdigest(), info['sha256'])

    def test_tampered_payload_rejected_before_unpack(self):
        bad = self.root / 'bad.pyz'
        with zipfile.ZipFile(bad, 'w') as out:
            out.writestr('relay.tar.gz', b'incomplete')
            out.writestr('relay.sha256', '0' * 64)
        with self.assertRaisesRegex(ValueError, 'checksum'):
            install_relay.unpack(bad, self.home)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_install_upgrade_failure_and_retry(self):
        cli = ['hermes', '-p', 'luna']
        self.enterContext(mock.patch.object(sys, 'argv', [str(self.installer), '--profile', 'luna']))
        config = self.enterContext(mock.patch.object(subprocess, 'check_output', return_value=str(self.home / 'config.yaml')))
        calls = []
        def run(command, *, check):
            calls.append(command)
            if command[1:3] == ['-m', 'venv']:
                executable = Path(command[-1]) / 'bin/python'
                executable.parent.mkdir(parents=True)
                executable.touch()
        runner = self.enterContext(mock.patch.object(subprocess, 'run', side_effect=run))
        if os.name == 'nt':
            # Windows test hosts lack Linux flock and unprivileged POSIX symlinks.
            self.enterContext(mock.patch.dict(sys.modules, fcntl=mock.Mock()))
            self.enterContext(mock.patch.object(Path, 'symlink_to', lambda path, target, **kw: path.write_text(str(target))))
        install_relay.main()
        config.assert_called_with([*cli, 'config', 'path'], text=True)
        self.assertEqual(calls[-1], [*cli, 'plugins', 'enable', 'bot-group-relay'])
        self.assertTrue((self.plugin / 'mcp/server.py').is_file())
        state = self.plugin / 'state/pairing.json'
        state.parent.mkdir()
        state.write_bytes(b'fixture pairing')
        previous = self.plugin / 'VERSION'
        previous.write_text('previous')
        runner.side_effect = subprocess.CalledProcessError(1, 'hermes')
        with self.assertRaises(subprocess.CalledProcessError):
            install_relay.main()
        self.assertEqual(previous.read_text(), 'previous')
        self.assertEqual(state.read_bytes(), b'fixture pairing')
        runner.side_effect = run
        install_relay.main()
        self.assertEqual(previous.read_text().strip(), self.version)
        self.assertEqual(state.read_bytes(), b'fixture pairing')
        self.assertEqual(sum(command[1:3] == ['-m', 'venv'] for command in calls), 1)

    def test_dependency_failure_preserves_existing_installation(self):
        stage = self.root / 'stage'
        stage.mkdir()
        install_relay.unpack(self.installer, stage)
        self.plugin.mkdir(parents=True)
        (self.plugin / 'VERSION').write_text('previous')
        with mock.patch.object(subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'pip')):
            with self.assertRaises(subprocess.CalledProcessError):
                install_relay.prepare_runtime(stage, self.home / '.bot-group-relay')
        self.assertEqual((self.plugin / 'VERSION').read_text(), 'previous')
        self.assertFalse(list((self.home / '.bot-group-relay').rglob('ready')))
