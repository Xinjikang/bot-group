"""Probe a generated Windows profile using an isolated home and local Git repository."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tomlkit
from engine import codex_project_access, profile_create, wake


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True, help='New, unused evidence directory')
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('This probe requires Windows')
    codex, git = wake._codex_bin(), shutil.which('git')
    if not git:
        parser.error('Git must be installed')
    root = args.evidence.resolve()
    root.mkdir(parents=True, exist_ok=False)
    home, workspace = root / 'home', root / 'workspace'
    home.mkdir()
    workspace.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env['CODEX_HOME'] = str(home)
    runs = []

    def run(command):
        result = subprocess.run(command, cwd=workspace, env=env, capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=45)
        runs.append({'command': command, 'exit_code': result.returncode,
                     'stdout': result.stdout, 'stderr': result.stderr})
        (root / 'commands.json').write_text(json.dumps(runs, ensure_ascii=False, indent=2), encoding='utf-8')
        if result.returncode:
            raise RuntimeError(f'Command failed ({result.returncode}); see {root / "commands.json"}')
        return result.stdout

    body = {'model': '', 'instructions': '', 'display_name': 'Sandbox probe', 'profile': 'sandbox-probe'}
    with patch('engine.codex_auth.read_auth', return_value=(None, None)):
        content, _ = profile_create._codex(None, None, body)
    config = tomlkit.parse(content['config.toml'].decode())
    codex_project_access.grant(config, config, str(workspace))
    content['config.toml'] = tomlkit.dumps(config).encode()
    for name, data in content.items():
        (home / name).write_bytes(data)
    run([git, '-c', 'init.templateDir=', 'init', str(workspace)])
    (workspace / 'probe.txt').write_text('sandbox-ready\n', encoding='utf-8')
    run([codex, '--version'])
    shell = shutil.which('powershell.exe')
    if not shell:
        raise RuntimeError('Windows PowerShell is required for the command-startup probe')
    run([codex, 'sandbox', '--', shell, '-NoProfile', '-Command', 'Get-Location'])
    read = run([codex, 'sandbox', '--', shell, '-NoProfile', '-Command', 'Get-Content -LiteralPath probe.txt'])
    status = run([codex, 'sandbox', '--', git, 'status', '--short'])
    if 'sandbox-ready' not in read or 'probe.txt' not in status:
        raise RuntimeError('Sandbox commands did not return the fixture contents')
    print('PASS: generated profile starts sandboxed PowerShell, reads the workspace and runs Git status')


if __name__ == '__main__':
    main()
