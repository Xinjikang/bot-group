"""Public installation contract: real PowerShell, isolated files and stub downloads."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tools import make_release, releases, upgrade

ROOT = Path(__file__).resolve().parents[1]
PS = shutil.which('powershell')


class PublicReleaseTests(unittest.TestCase):
    def test_release_contains_installer_and_fixed_bootstrap_addresses(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'tools').mkdir()
            (root / 'tools/bootstrap.py').write_text('pass\n')
            (root / 'install.ps1').write_text('Write-Host done\n')
            (root / 'README.zh-CN.md').write_text('Chinese\n')
            base = 'https://github.com/Xinjikang/bot-group/releases/download/v1.2.3/'
            make_release.make_release(root, root / 'out', 'bot-group', 'v1.2.3', base)
            entry = json.loads((root / 'out/releases.json').read_text())['releases'][0]
            self.assertEqual(entry['bootstrap'], base + 'bootstrap.py')
            self.assertEqual(entry['source'], base + 'bot-group-v1.2.3.zip')
            self.assertEqual(entry['bootstrap_sha256'], hashlib.sha256((root / 'tools/bootstrap.py').read_bytes()).hexdigest())
            self.assertEqual((root / 'out/install.ps1').read_bytes(), (root / 'install.ps1').read_bytes())
            manifest = json.loads((root / 'out/release_manifest.json').read_text())
            self.assertIn('README.zh-CN.md', manifest['program_files'])

    def test_upgrade_defaults_to_public_index_and_preserves_override(self):
        with mock.patch.object(upgrade.config, '_pick', side_effect=lambda explicit, names, default: explicit or default), \
             mock.patch.object(releases, 'fetch_index', return_value=[]) as fetch:
            upgrade._fetch_release_index()
            fetch.assert_called_once_with(releases.DEFAULT_INDEX_URL)
            upgrade._fetch_release_index('https://example.org/releases.json')
            self.assertEqual(fetch.call_args.args[0], 'https://example.org/releases.json')


@unittest.skipUnless(os.name == 'nt' and PS, 'Windows PowerShell required')
class PowerShellInstallerTests(unittest.TestCase):
    def run_script(self, body, *, expected=0, env=None):
        with tempfile.TemporaryDirectory(prefix='public-installer-') as folder:
            script = Path(folder) / 'check.ps1'
            script.write_text("$ErrorActionPreference = 'Stop'\n. '" + str(ROOT / 'install.ps1') + "'\n" + body,
                              encoding='utf-8-sig')
            result = subprocess.run([PS, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)],
                                    capture_output=True, timeout=60, env=dict(env or os.environ, TEMP=folder, TMP=folder))
            self.assertEqual(result.returncode, expected, (result.stdout, result.stderr))
            return result

    def test_real_python_probe_and_invalid_runtime(self):
        self.run_script("""
$found = Get-CompatiblePython 'PYTHON'
if ($found -ne 'PYTHON') { throw "Wrong interpreter: $found" }
if (Get-CompatiblePython 'does-not-exist-bot-group.exe') { throw 'Invalid interpreter accepted' }
""".replace('PYTHON', sys.executable))

    def test_download_checks_hash_and_https_before_execution(self):
        self.run_script("""
function Invoke-WebRequest { param($Uri, $OutFile, $TimeoutSec, [switch]$UseBasicParsing)
    [IO.File]::WriteAllText($OutFile, 'fixture')
}
$file = Join-Path $env:TEMP ([guid]::NewGuid().ToString() + '.txt')
try {
    $rejected = $false
    try { Get-Download 'https://example.org/install.py' $file ('0' * 64) } catch { $rejected = $true }
    if (-not $rejected) { throw 'Bad hash accepted' }
    $rejected = $false
    try { Get-Download 'http://example.org/install.py' $file } catch { $rejected = $true }
    if (-not $rejected) { throw 'HTTP accepted' }
} finally { if (Test-Path $file) { Remove-Item -LiteralPath $file } }
""")

    def test_missing_python_uses_verified_per_user_install_without_path_changes(self):
        self.run_script("""
$script:probes = 0
function Get-CompatiblePython {
    $script:probes++
    if ($script:probes -eq 2) { return 'verified-python.exe' }
}
function Get-Download { param($Url, $Destination, $Sha256)
    if ($Url -ne 'https://www.python.org/ftp/python/3.13.16/python-3.13.16-amd64.exe') { throw 'Unexpected Python source' }
    if ($Sha256 -ne 'fb4f9f5d438b2396da0086dc70b935c530cb578e37adc6d354f7ad2037fee83b') { throw 'Missing official hash' }
}
function Start-Process { param($FilePath, $ArgumentList, [switch]$Wait, [switch]$PassThru, $WindowStyle)
    foreach ($required in @('InstallAllUsers=0', 'PrependPath=0', 'Include_launcher=0')) {
        if ($ArgumentList -notcontains $required) { throw "Missing option: $required" }
    }
    if (-not $Wait -or $WindowStyle -ne 'Hidden') { throw 'Invalid process handling' }
    return [pscustomobject]@{ExitCode=0}
}
if ((Install-PrivatePython $env:TEMP) -ne 'verified-python.exe') { throw 'Installed Python was not selected' }
""")

    def test_upgrade_bat_uses_base_python_and_propagates_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'installed with spaces'
            subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(root / '.venv')], check=True)
            (root / 'tools').mkdir()
            (root / 'tools/upgrade.py').write_text(
                "import sys\nassert sys.prefix == sys.base_prefix, 'upgrade must run outside venv'\n"
                "assert sys.argv[1:] == ['upgrade', '--index-url', 'https://example.org/index.json']\nsys.exit(37)\n")
            shutil.copyfile(ROOT / 'upgrade.bat', root / 'upgrade.bat')
            result = subprocess.run([os.environ['COMSPEC'], '/d', '/c', 'upgrade.bat', '--index-url',
                                     'https://example.org/index.json'], cwd=root, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 37, (result.stdout, result.stderr))

    def test_real_powershell_installs_pinned_release_and_restores_environment(self):
        for failure in (False, True):
            with self.subTest(bootstrap_failure=failure), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                bootstrap = root / 'bootstrap.py'
                bootstrap.write_text("import json,os,pathlib,sys\np=pathlib.Path(os.environ['INSTALL_DIR']);p.mkdir(parents=True,exist_ok=True)\n(p/'receipt.json').write_text(json.dumps({k:os.environ[k] for k in ['BOOTSTRAP_URL','BOOTSTRAP_SHA256','INSTALL_DIR']}))\nsys.exit(" + ('7' if failure else '0') + ')\n')
                entry = dict(version='v1.2.3', source='https://example.org/v1.2.3/source.zip', sha256='a' * 64,
                             bootstrap='https://example.org/v1.2.3/bootstrap.py',
                             bootstrap_sha256=hashlib.sha256(bootstrap.read_bytes()).hexdigest())
                (root / 'releases.json').write_text(json.dumps({'releases': [entry, dict(entry, version='v9.0.0', prerelease=True)]}))
                environment = dict(os.environ, INSTALLER_FIXTURE=str(root), BOOTSTRAP_URL='original',
                                   BOOTSTRAP_SHA256='original', INSTALL_DIR='original')
                self.run_script("""
$InstallDir = Join-Path $env:INSTALLER_FIXTURE 'Chinese space install'
$IndexUrl = 'https://example.org/releases.json'
$PythonPath = 'PYTHON'
function Invoke-WebRequest { param($Uri, $OutFile, $TimeoutSec, [switch]$UseBasicParsing)
    Copy-Item -LiteralPath (Join-Path $env:INSTALLER_FIXTURE ([IO.Path]::GetFileName(([uri]$Uri).AbsolutePath))) -Destination $OutFile
}
function Install-PrivatePython { throw 'Existing Python should be reused' }
$failed = $false
try { Install-BotGroup } catch { $failed = $true; Write-Host $_ }
if ($failed -ne EXPECT_FAILURE) { throw 'Unexpected install result' }
if ($env:BOOTSTRAP_URL -ne 'original' -or $env:INSTALL_DIR -ne 'original') { throw 'Environment leaked' }
""".replace('PYTHON', sys.executable).replace('EXPECT_FAILURE', '$true' if failure else '$false'), env=environment)
                receipt = json.loads((root / 'Chinese space install/receipt.json').read_text())
                self.assertEqual(receipt['BOOTSTRAP_URL'], entry['source'])
                self.assertEqual(receipt['BOOTSTRAP_SHA256'], entry['sha256'])


if __name__ == '__main__':
    unittest.main()
