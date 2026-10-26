"""Windows shell-link integration uses an isolated desktop and inert launcher."""
import base64
import json
import os
import shutil
import struct
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from engine.maintenance import desktop, install, make_release

ROOT = Path(__file__).resolve().parents[1]


class IconAssetTest(unittest.TestCase):
    def test_icon_sizes_and_release_assets(self):
        raw = (ROOT / 'frontend/assets/bot-group.ico').read_bytes()
        reserved, kind, count = struct.unpack_from('<HHH', raw)
        self.assertEqual((reserved, kind, count), (0, 1, 7))
        dimensions = set()
        for i in range(count):
            width, height, _, _, _, _, length, offset = struct.unpack_from('<BBBBHHII', raw, 6 + i * 16)
            dimensions.add((width or 256, height or 256))
            self.assertLessEqual(offset + length, len(raw))
            self.assertGreater(length, 0)
        self.assertEqual(dimensions, {(s, s) for s in (16, 24, 32, 48, 64, 128, 256)})
        managed = make_release._managed_files(ROOT)
        self.assertIn('frontend/assets/bot-group.ico', managed)
        self.assertIn('frontend/assets/bot-group-icon.png', managed)


class InstallShortcutTest(unittest.TestCase):
    def test_successful_install_maintains_shortcut(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for name in ('_invalidate_state', 'check_python', 'ensure_venv', 'pip_install',
                         'first_config', 'selfcheck', 'write_state', 'venv_python_version'):
                stack.enter_context(patch.object(install, name))
            stack.enter_context(patch.object(install, 'detect_runtime', return_value={}))
            shortcut = stack.enter_context(patch.object(desktop, 'ensure_shortcut', return_value=False))
            self.assertEqual(install.do_install(), 0)
            shortcut.assert_called_once_with(install.PROGRAM_ROOT, install.venv_python(), install.config.data_dir())

    def test_failure_and_opt_out_preserve_desktop(self):
        with patch.object(install, '_invalidate_state'), \
             patch.object(install, 'check_python', side_effect=RuntimeError('invalid')), \
             patch.object(desktop, 'ensure_shortcut') as shortcut:
            self.assertEqual(install.do_install(), 1)
            shortcut.assert_not_called()
        with patch.dict(os.environ, {'GROUP_DESKTOP_SHORTCUT': '0'}), \
             patch.object(desktop.subprocess, 'run') as run:
            self.assertFalse(desktop.ensure_shortcut(Path('.'), Path(sys.executable), Path('.')))
            run.assert_not_called()


@unittest.skipUnless(sys.platform == 'win32', 'Windows shell links')
class DesktopShortcutTest(unittest.TestCase):
    def test_real_link_unicode_repeat_collision_and_launch_arguments(self):
        with tempfile.TemporaryDirectory(prefix='desktop-test-') as folder, \
             patch.dict(os.environ, {'GROUP_DESKTOP_SHORTCUT': '1'}):
            base = Path(folder)
            target = base / "程序 space ' $ & (test)"
            data = base / '数据 space'
            target.joinpath('tools').mkdir(parents=True)
            data.mkdir()
            screen = base / 'desktop'
            screen.mkdir()
            probe = target / 'tools/service.py'
            probe.write_text(
                'import argparse,json,os,sys,pathlib\n'
                'parser=argparse.ArgumentParser();parser.add_argument("command",choices=["start","stop"])\n'
                'parser.parse_args()\n'
                'pathlib.Path("probe.json").write_text(json.dumps({"argv":sys.argv[1:],"data":os.environ["GROUP_DATA_DIR"]}))\n',
                encoding='utf-8')
            self.assertTrue(desktop.ensure_shortcut(target, Path(sys.executable), data, desktop=screen))
            (screen / 'Bot Group.lnk').rename(screen / 'BOT GROUP.lnk')
            icon = target / 'frontend/assets/bot-group.ico'
            icon.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / 'frontend/assets/bot-group.ico', icon)
            self.assertTrue(desktop.ensure_shortcut(target, Path(sys.executable), data, desktop=screen))
            links = list(screen.glob('*.lnk'))
            self.assertEqual(len(links), 1)
            self.assertEqual(links[0].name, 'Bot Group.lnk')
            original = links[0].read_bytes()
            script = r"""
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($env:TEST_LINK)
if ($link.WorkingDirectory -ne $env:TEST_ROOT) { throw 'Working directory mismatch' }
if ($link.IconLocation -ne ($env:TEST_ICON + ',0')) { throw 'Icon mismatch' }
$p = Start-Process -FilePath $link.TargetPath -ArgumentList $link.Arguments -WorkingDirectory $link.WorkingDirectory -WindowStyle Hidden -Wait -PassThru
exit $p.ExitCode
"""
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                                     base64.b64encode(script.encode('utf-16-le')).decode()],
                                    env={**os.environ, 'TEST_LINK': str(links[0]), 'TEST_ROOT': str(target),
                                         'TEST_ICON': str(icon)},
                                    capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((target / 'probe.json').read_text()), {'argv': ['start'], 'data': str(data)})
            other = base / 'second-install'
            other.mkdir()
            self.assertTrue(desktop.ensure_shortcut(other, Path(sys.executable), data, desktop=screen))
            self.assertEqual(len(list(screen.glob('*.lnk'))), 2)
            self.assertEqual(links[0].read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
