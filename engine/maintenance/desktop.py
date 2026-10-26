"""Maintain a current-user Windows shortcut for one installed instance."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


# Environment-based data selection also works after rollback to older service CLIs.
_LAUNCH = (
    "import os,runpy,sys;os.environ['GROUP_DATA_DIR']=sys.argv[1];"
    "entry=sys.argv[2];sys.argv=[entry,'start'];runpy.run_path(entry,run_name='__main__')"
)


_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$spec = $env:BOT_GROUP_SHORTCUT_SPEC | ConvertFrom-Json
$shell = New-Object -ComObject WScript.Shell
$desktop = $spec.desktop
if (-not $desktop) { $desktop = $shell.SpecialFolders.Item('Desktop') }
if (-not $desktop -or -not (Test-Path -LiteralPath $desktop -PathType Container)) {
    throw 'Desktop directory is unavailable'
}
$path = Join-Path $desktop 'BOT GROUP.lnk'
if (Test-Path -LiteralPath $path) {
    $existing = $shell.CreateShortcut($path)
    if ($existing.TargetPath -ne $spec.target -or $existing.Arguments -ne $spec.arguments) {
        $path = Join-Path $desktop ('BOT GROUP (' + $spec.id + ').lnk')
    }
}
if (Test-Path -LiteralPath $path) {
    $existing = $shell.CreateShortcut($path)
    if ($existing.TargetPath -ne $spec.target -or $existing.Arguments -ne $spec.arguments) {
        throw 'Shortcut name is occupied by another target'
    }
}
$link = $shell.CreateShortcut($path)
$link.TargetPath = $spec.target
$link.Arguments = $spec.arguments
$link.WorkingDirectory = $spec.root
$link.Description = 'Start BOT GROUP and open its web interface'
$link.WindowStyle = 7
$link.Save()
"""


def ensure_shortcut(root: Path, python: Path, data_dir: Path, *, desktop: Path | None = None) -> bool:
    if sys.platform != 'win32' or os.environ.get('GROUP_DESKTOP_SHORTCUT') == '0':
        return False
    root, python, data_dir = root.resolve(), python.resolve(), data_dir.resolve()
    arguments = subprocess.list2cmdline([
        '-X', 'utf8', '-c', _LAUNCH, str(data_dir), str(root / 'tools' / 'service.py'),
    ])
    identity = hashlib.sha256((str(root).casefold() + '\n' + str(data_dir).casefold()).encode()).hexdigest()[:8]
    spec = {'root': str(root), 'target': str(python), 'arguments': arguments, 'id': identity,
            'desktop': str(desktop) if desktop is not None else ''}
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    encoded = base64.b64encode(_SCRIPT.encode('utf-16-le')).decode('ascii')
    try:
        subprocess.run([str(executable), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                       env={**os.environ, 'BOT_GROUP_SHORTCUT_SPEC': json.dumps(spec)},
                       capture_output=True, check=True, timeout=30,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f'[install] 桌面快捷方式创建失败，可重新安装以补建：{exc}', file=sys.stderr)
        return False
    print('[install] 桌面快捷方式已就绪：BOT GROUP（启动服务并打开界面）')
    return True
