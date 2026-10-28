"""Run the documented public install command in an empty, isolated Windows directory."""
import argparse
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import urllib.request
from probe_config import port_number

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--shell', choices=['powershell', 'cmd'], required=True)
    parser.add_argument('--port', type=port_number, required=True)
    args = parser.parse_args()
    base = args.evidence.resolve()
    if base.exists():
        parser.error('Use a new evidence directory for each run')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', args.port))
    base.mkdir(parents=True)
    program = base / 'Chinese space install'
    temporary = base / 'temp'
    temporary.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(
        ('GROUP_', 'XDG_GROUP_', 'HERMES_', 'CODEX_', 'BOOTSTRAP_', 'RELEASE_INDEX_URL'))}
    env.update(INSTALL_DIR=str(program), TEMP=str(temporary), TMP=str(temporary), PYTHONUTF8='1',
               GROUP_ENGINE_HOST='127.0.0.1', GROUP_ENGINE_PORT=str(args.port),
               GROUP_DATA_DIR=str(program), GROUP_DB=str(program / 'group.db'),
               GROUP_ATTACHMENTS_DIR=str(program / 'attachments'),
               GROUP_ENGINE_URL=f'http://127.0.0.1:{args.port}',
               HERMES_HOME=str(base / 'empty-hermes'), CODEX_HOME=str(base / 'empty-codex'),
               GROUP_ADMIN_PASSWORD='', GROUP_SECRET='isolated-public-install',
               GROUP_DESKTOP_SHORTCUT='0')
    browser_probe = base / 'browser_probe.py'
    browser_probe.write_text(
        "import pathlib,sys\nwith pathlib.Path(__file__).with_name('browser_calls.txt').open('a') as f:\n"
        "    f.write(sys.argv[1] + '\\n')\n", encoding='utf-8')
    env['BROWSER'] = f'"{Path(sys.executable).as_posix()}" "{browser_probe.as_posix()}" %s'
    command = re.search(r'```' + args.shell + r'\n([^\n]+)',
                        (ROOT / 'README.md').read_text(encoding='utf-8')).group(1)
    steps = []

    def run(label, argv, cwd=base):
        print(label, flush=True)
        with (base / (label + '.log')).open('wb') as log:
            result = subprocess.run(argv, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    timeout=600, creationflags=subprocess.CREATE_NO_WINDOW)
        steps.append({'step': label, 'exit_code': result.returncode})
        if result.returncode:
            raise RuntimeError(f'{label}: exit {result.returncode}; see {base / (label + ".log")}')

    def bat(name):
        return [os.environ['COMSPEC'], '/d', '/c', name]

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path):
        with opener.open(f'http://127.0.0.1:{args.port}' + path, timeout=15) as response:
            return response.read()

    started = False
    try:
        argv = (['powershell', '-NoProfile', '-Command', command] if args.shell == 'powershell'
                else os.environ['COMSPEC'] + ' /d /s /c "' + command + '"')
        run('01-public-install', argv)
        assert (program / '.venv/Scripts/python.exe').is_file()
        assert (program / 'install.json').is_file()
        assert (program / 'README.zh-CN.md').is_file()
        version = (program / 'VERSION').read_text().strip()
        (program / 'user-notes.txt').write_text('KEEP USER DATA', encoding='utf-8')
        original_config = (program / '.env').read_bytes()
        run('02-repeat-install', argv)
        assert (program / '.env').read_bytes() == original_config
        assert (program / 'user-notes.txt').read_text() == 'KEEP USER DATA'
        # Leave only Windows tools on PATH: BAT must use the installed environment.
        env['PATH'] = str(Path(os.environ['SystemRoot']) / 'System32')
        started = True
        run('03-start', bat('start.bat'), program)
        assert (base / 'browser_calls.txt').read_text().splitlines() == [f'http://127.0.0.1:{args.port}/app/']
        health = json.loads(get('/health'))
        assert health['status'] == 'ok', health
        assert Path(health['program_root']).resolve() == program
        assert Path(health['db_path']).resolve() == program / 'group.db'
        assert json.loads(get('/auth/status')) == {'configured': False}
        html = get('/app/')
        assert b'<html' in html.lower()
        (base / 'app.html').write_bytes(html)
        (base / 'health.json').write_text(json.dumps(health, indent=2), encoding='utf-8')
        run('04-public-upgrade-check', bat('upgrade.bat'), program)
        assert json.loads(get('/health'))['instance_id'] == health['instance_id']
        assert (program / 'VERSION').read_text().strip() == version
        assert (program / 'user-notes.txt').read_text() == 'KEEP USER DATA'
        run('05-stop', bat('stop.bat'), program)
        started = False
        with socket.socket() as sock:
            assert sock.connect_ex(('127.0.0.1', args.port)) != 0
        print('PASS public installation, repeat install, BAT start/stop, HTTP, default release lookup', flush=True)
    finally:
        if started:
            try:
                health = json.loads(get('/health'))
                if Path(health.get('program_root', '')).resolve() == program:
                    run('cleanup-stop', bat('stop.bat'), program)
            except OSError:
                pass
        (base / 'result.json').write_text(json.dumps({'command': command, 'shell': args.shell,
            'program_root': str(program), 'port': args.port, 'steps': steps}, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
