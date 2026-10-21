"""#218: real CMD parsing with native interpreter probes, plus Git/ZIP bytes.

The probe records argv and exits; it never opens or executes a service target.
Set BAT_ENTRY_EVIDENCE_DIR to preserve synthetic subprocess receipts.
"""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

from scripts import make_release


ROOT = Path(__file__).resolve().parents[1]
ENTRIES = ('start.bat', 'stop.bat', 'install.bat', 'bootstrap.bat')
CASES = (
    ('start.bat', '', ['scripts\\service.py', 'start']),
    ('stop.bat', '', ['scripts\\service.py', 'stop']),
    ('install.bat', '', ['scripts\\install.py', 'install']),
    ('install.bat', 'install', ['scripts\\install.py', 'install']),
    ('install.bat', 'repair', ['scripts\\install.py', 'repair']),
    ('bootstrap.bat', '', ['scripts\\bootstrap.py']),
)
# Exact pre-fix UTF-8/LF entry bodies (2d8da3f); no production target is copied.
LEGACY_START = '''@echo off
setlocal
rem 原生 Windows 启停：探测 Python 调 scripts/service.py（不依赖 Git Bash）。
cd /d "%~dp0"
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  echo [start-engine] 未找到 Python。请安装 Python 3.11+ 后重试。
  exit /b 1
)
%PY% scripts\\service.py start
exit /b %errorlevel%
'''
LEGACY_INSTALL = '''@echo off
setlocal
rem 安装入口（req#122 task#213）：install 安装 / repair 迁移旧环境（验证 venv 依赖 + 记录版本，不重装）。
rem 幂等可重跑；失败保持未就绪。无需预装 Git。
cd /d "%~dp0"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  echo [install] 未找到 Python，请安装 Python 3.11+ 后重试。
  exit /b 1
)

set "CMD=%~1"
if "%CMD%"=="" set "CMD=install"
%PY% scripts\\install.py %CMD%
exit /b %errorlevel%
'''


def assert_cmd_bytes(test, raw):
    raw.decode('ascii')
    test.assertTrue(raw.endswith(b'\r\n'))
    test.assertNotIn(b'\n', raw.replace(b'\r\n', b''))
    test.assertNotIn(b'\r', raw.replace(b'\r\n', b''))


def copy_entries(destination):
    destination.mkdir(parents=True, exist_ok=True)
    for name in ENTRIES:
        shutil.copyfile(ROOT / name, destination / name)
    scripts = destination / 'scripts'
    scripts.mkdir(exist_ok=True)
    for name in ('service.py', 'install.py', 'bootstrap.py'):
        (scripts / name).write_text('raise AssertionError("probe target must stay inert")\n',
                                    encoding='ascii')


def build_archive(source, output):
    with redirect_stdout(io.StringIO()):
        result = make_release.make_release(source, output, 'bat-fixture', 'v1.0.0')
    if result != 0:
        raise AssertionError(f'fixture release failed: {result}')
    return output / 'bat-fixture-v1.0.0.zip'


class BatBytesTests(unittest.TestCase):
    def test_worktree_ascii_crlf(self):
        for name in ENTRIES:
            with self.subTest(entry=name):
                assert_cmd_bytes(self, (ROOT / name).read_bytes())

    def test_release_zip_preserves_entry_bytes_and_manifest(self):
        with tempfile.TemporaryDirectory(prefix='bat-release-') as directory:
            root = Path(directory)
            copy_entries(root / 'source space')
            archive = build_archive(root / 'source space', root / 'output')
            with zipfile.ZipFile(archive) as z:
                manifest = json.loads(z.read('release_manifest.json'))
                for name in ENTRIES:
                    self.assertIn(name, manifest['program_files'])
                    self.assertEqual(z.read(name), (ROOT / name).read_bytes())
                    assert_cmd_bytes(self, z.read(name))

    @unittest.skipUnless(shutil.which('git'), 'Git checkout/archive contract')
    def test_git_checkout_blob_and_archive_keep_crlf(self):
        with tempfile.TemporaryDirectory(prefix='bat-git-') as directory:
            root = Path(directory)
            copy_entries(root)
            shutil.copyfile(ROOT / '.gitattributes', root / '.gitattributes')
            env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
            env['GIT_CONFIG_NOSYSTEM'] = '1'
            env['GIT_CONFIG_GLOBAL'] = os.devnull
            git = shutil.which('git')

            def run(*args):
                result = subprocess.run([git, '-C', str(root), *args], env=env,
                                        capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout

            run('init', '-q')
            run('-c', 'core.autocrlf=true', 'add', '.gitattributes', *ENTRIES)
            run('-c', 'user.name=Bat Fixture', '-c', 'user.email=bat@example.invalid',
                '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')
            attrs = run('check-attr', 'text', '--', *ENTRIES).decode('ascii')
            for name in ENTRIES:
                self.assertIn(f'{name}: text: unset', attrs)
                self.assertEqual(run('show', f'HEAD:{name}'), (ROOT / name).read_bytes())
            for mode in ('true', 'false', 'input'):
                for name in ENTRIES:
                    (root / name).unlink()
                run('-c', f'core.autocrlf={mode}', 'checkout', '--', *ENTRIES)
                for name in ENTRIES:
                    self.assertEqual((root / name).read_bytes(), (ROOT / name).read_bytes())
            archive = run('archive', '--format=zip', 'HEAD')
            with zipfile.ZipFile(io.BytesIO(archive)) as z:
                for name in ENTRIES:
                    self.assertEqual(z.read(name), (ROOT / name).read_bytes())
                    assert_cmd_bytes(self, z.read(name))


@unittest.skipUnless(sys.platform == 'win32', 'real Windows CMD')
class BatCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='bat-cmd-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.work = self.root / 'program with spaces'
        copy_entries(self.work)
        self.bin = self.root / 'probe bin'
        self.bin.mkdir()
        self.cmd = Path(os.environ['SystemRoot']) / 'System32' / 'cmd.exe'
        shutil.copyfile(self.cmd.with_name('where.exe'), self.bin / 'where.exe')
        self.capture = self.root / 'received.json'
        self.counter = 0
        self.env = os.environ.copy()
        for key in ('PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP', 'PYTHONINSPECT'):
            self.env.pop(key, None)
        self.env.update({
            'PATH': str(self.bin), 'PATHEXT': '.COM;.EXE;.BAT;.CMD',
            'COMSPEC': str(self.cmd), 'NoDefaultCurrentDirectoryInExePath': '1',
            'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1',
            'BAT_PROBE_REPORT': str(self.capture), 'BAT_PROBE_EXIT': '0',
            'GROUP_DATA_DIR': str(self.root / 'data'), 'GROUP_DB': str(self.root / 'data/group.db'),
            'GROUP_ATTACHMENTS_DIR': str(self.root / 'attachments'),
            'GROUP_ENGINE_HOST': '127.0.0.1', 'GROUP_ENGINE_PORT': '18042',
            'GROUP_ENGINE_URL': 'http://127.0.0.1:18042',
            'XDG_GROUP_ENGINE_URL': 'http://127.0.0.1:18042',
            'HERMES_HOME': str(self.root / 'hermes'), 'CODEX_HOME': str(self.root / 'codex'),
        })
        # A native PE probe has no CMD wrapper and never evaluates script targets.
        import pip
        stub = Path(pip.__file__).parent / '_vendor/distlib/t64.exe'
        script = '''import json,os,sys
from pathlib import Path
Path(os.environ['BAT_PROBE_REPORT']).write_text(json.dumps({
    'executable':Path(sys.argv[0]).name,'argv':sys.argv[1:],'cwd':os.getcwd(),
    'exit_code':int(os.environ['BAT_PROBE_EXIT'])}),encoding='utf-8')
sys.exit(int(os.environ['BAT_PROBE_EXIT']))
'''
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, 'w') as z:
            z.writestr('__main__.py', script)
        self.native = stub.read_bytes() + f'#!"{sys.executable}"\n'.encode('utf-8') + payload.getvalue()

    def interpreters(self, *names):
        for name in ('python', 'py'):
            path = self.bin / f'{name}.exe'
            if name in names:
                path.write_bytes(self.native)
            elif path.exists():
                path.unlink()

    def run_entry(self, name, args='', codepage=None, exit_code=0, work=None,
                  expected_parse_error=False):
        work = work or self.work
        if self.capture.exists():
            self.capture.unlink()
        cp_file = self.root / 'codepage.txt'
        driver = self.root / 'driver space.cmd'
        command = '@echo off\r\n'
        chcp = self.cmd.with_name('chcp.com')
        if codepage is not None:
            command += f'"{chcp}" {codepage} >nul\r\n'
        command += f'"{chcp}" > "{cp_file}"\r\ncall "{work / name}" {args}\r\nexit /b %errorlevel%\r\n'
        driver.write_bytes(command.encode('ascii'))
        env = dict(self.env, BAT_PROBE_EXIT=str(exit_code))
        startup = subprocess.STARTUPINFO()
        startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        # chcp needs a real console; keep its window hidden while capturing output.
        proc = subprocess.run(f'"{self.cmd}" /d /q /s /c ""{driver}""',
                              cwd=self.root, env=env, capture_output=True, timeout=15,
                              creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
        received = json.loads(self.capture.read_text(encoding='utf-8')) if self.capture.exists() else None
        match = re.search(rb'(\d+)', cp_file.read_bytes())
        self.assertIsNotNone(match, (cp_file.read_bytes(), proc.stdout, proc.stderr))
        actual_cp = int(match.group(1))
        if codepage is not None:
            self.assertEqual(actual_cp, codepage)
        directory = os.environ.get('BAT_ENTRY_EVIDENCE_DIR')
        if directory:
            self.counter += 1
            folder = Path(directory)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f'{self._testMethodName}-{self.counter}.json').write_text(json.dumps({
                'entry': name, 'arguments': args, 'codepage': actual_cp,
                'expected_exit': None if expected_parse_error else exit_code,
                'expected_parse_error': expected_parse_error, 'probe_exit': exit_code,
                'exit_code': proc.returncode,
                'received': received, 'stdout_hex': proc.stdout.hex(), 'stderr_hex': proc.stderr.hex(),
                'entry_sha256': hashlib.sha256((work / name).read_bytes()).hexdigest(),
            }, indent=2), encoding='utf-8')
        return proc, received

    def assert_invocation(self, case, codepage, interpreter='python', exit_code=0, work=None):
        name, args, expected = case
        proc, received = self.run_entry(name, args, codepage, exit_code, work)
        self.assertEqual(proc.returncode, exit_code, (proc.stdout, proc.stderr))
        self.assertEqual(proc.stderr, b'', (proc.stdout, proc.stderr))
        self.assertEqual(proc.stdout, b'')
        self.assertIsNotNone(received)
        self.assertEqual(received['argv'], (['-3'] if interpreter == 'py' else []) + expected)
        self.assertEqual(received['executable'], interpreter + '.exe')
        self.assertEqual(received['cwd'], str(work or self.work))

    def test_python_preferred_for_all_commands_and_codepages(self):
        self.interpreters('python', 'py')
        for cp in (None, 65001):
            for case in CASES:
                with self.subTest(codepage=cp, case=case):
                    self.assert_invocation(case, cp)

    def test_py_launcher_fallback_for_all_commands_and_codepages(self):
        self.interpreters('py')
        for cp in (None, 65001):
            for case in CASES:
                with self.subTest(codepage=cp, case=case):
                    self.assert_invocation(case, cp, 'py')

    def test_nonzero_exit_propagates_for_both_interpreters(self):
        for interpreter in ('python', 'py'):
            self.interpreters(interpreter)
            for cp in (None, 65001):
                for case in CASES:
                    with self.subTest(interpreter=interpreter, codepage=cp, case=case):
                        self.assert_invocation(case, cp, interpreter, 37)

    def test_missing_interpreter_is_actionable_and_returns_one(self):
        self.interpreters()
        for cp in (None, 65001):
            for name in ENTRIES:
                with self.subTest(codepage=cp, entry=name):
                    proc, received = self.run_entry(name, codepage=cp, exit_code=1)
                    self.assertEqual(proc.returncode, 1, (proc.stdout, proc.stderr))
                    self.assertIsNone(received)
                    self.assertEqual(proc.stderr, b'')
                    self.assertIn(b'Python 3.11+', proc.stdout)
                    self.assertIn(b'not found', proc.stdout)

    def test_old_utf8_lf_and_crlf_chinese_reproduce_parse_errors(self):
        self.interpreters('python')
        for label, text in (('start-lf', LEGACY_START), ('install-lf', LEGACY_INSTALL),
                            ('install-crlf', LEGACY_INSTALL.replace('\n', '\r\n'))):
            with self.subTest(legacy=label):
                name = f'legacy-{label}.bat'
                (self.work / name).write_bytes(text.encode('utf-8'))
                proc, _ = self.run_entry(name, codepage=936, expected_parse_error=True)
                self.assertTrue(proc.stderr, (label, proc.returncode, proc.stdout))
                if label == 'start-lf':
                    self.assertIn(b"'ine]'", proc.stderr)

    def test_released_entries_execute_from_space_path(self):
        archive = build_archive(self.work, self.root / 'release')
        extracted = self.root / 'extracted release space'
        with zipfile.ZipFile(archive) as z:
            z.extractall(extracted)
        for interpreter in ('python', 'py'):
            self.interpreters(interpreter)
            for cp in (None, 65001):
                for case in CASES:
                    with self.subTest(interpreter=interpreter, codepage=cp, case=case):
                        self.assert_invocation(case, cp, interpreter, work=extracted)


if __name__ == '__main__':
    unittest.main()
