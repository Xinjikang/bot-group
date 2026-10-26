"""Real Chrome presence, BAT and Windows shortcut against an isolated server."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PYTHON = ROOT / '.venv/Scripts/python.exe'


def server():
    import uvicorn
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles
    from engine.api import router_api
    from engine.main import health, app as engine_app
    from engine.ui_presence import Presence, router
    from engine.maintenance.desktop import ensure_shortcut

    app = FastAPI()
    app.include_router(router_api)
    app.include_router(router)
    app.get('/health')(health)
    app.state.ui_presence = Presence()
    app.state.stop_event = threading.Event()
    engine_app.state.instance_id = 'isolated-presence-fixture'
    app.mount('/app', StaticFiles(directory=ROOT / 'frontend', html=True))

    @app.get('/test/presence')
    async def presence():
        return {'count': len(app.state.ui_presence.clients)}

    assert ensure_shortcut(ROOT, PYTHON, Path(os.environ['GROUP_DATA_DIR']),
                           desktop=Path(os.environ['UI_TEST_DESKTOP']))
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        service = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        thread = threading.Thread(target=service.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not service.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(.01)
        print('URL=http://127.0.0.1:' + str(sock.getsockname()[1]), flush=True)
        try:
            sys.stdin.readline()
        finally:
            app.state.stop_event.set()
            service.should_exit = True
            thread.join(10)
            assert not thread.is_alive()


def main():
    from playwright.sync_api import sync_playwright
    assert os.name == 'nt', 'Windows BAT and shortcut integration'
    with tempfile.TemporaryDirectory(prefix='ui-presence-') as tmp:
        folder = Path(tmp)
        desktop = folder / 'desktop'
        desktop.mkdir()
        calls = folder / 'browser-calls.txt'
        env = {**os.environ, 'GROUP_DATA_DIR': tmp, 'GROUP_DB': str(folder / 'isolated.db'),
               'GROUP_ADMIN_PASSWORD': 'presence-test', 'GROUP_SECRET': 'presence-test',
               'GROUP_ENGINE_HOST': '127.0.0.1', 'GROUP_DESKTOP_SHORTCUT': '1',
               'UI_TEST_DESKTOP': str(desktop), 'UI_TEST_SHORTCUT': str(desktop / 'Bot Group.lnk'),
               'UI_BROWSER_CALLS': str(calls)}
        env.pop('GROUP_RESTART_INSTANCE', None)
        env.pop('GROUP_SERVICE_MAINTENANCE', None)
        env['BROWSER'] = f'"{PYTHON.as_posix()}" "{Path(__file__).as_posix()}" --record-browser %s'
        child = subprocess.Popen([str(PYTHON), '-X', 'utf8', '-B', __file__, '--server'],
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding='utf-8',
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            for _ in range(5):
                line = child.stdout.readline().strip()
                if line.startswith('URL='):
                    break
            assert line.startswith('URL='), line
            url = line[4:]
            env.update(GROUP_ENGINE_PORT=url.rsplit(':', 1)[1], GROUP_ENGINE_URL=url, XDG_GROUP_ENGINE_URL=url)

            def launch(entry):
                if entry == 'bat':
                    command = [os.environ['COMSPEC'], '/d', '/c', str(ROOT / 'start.bat')]
                else:
                    command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                        '$p = Start-Process -FilePath $env:UI_TEST_SHORTCUT -WindowStyle Hidden -Wait -PassThru; exit $p.ExitCode']
                result = subprocess.run(command, env=env, cwd=ROOT, capture_output=True,
                    timeout=25, creationflags=subprocess.CREATE_NO_WINDOW)
                assert result.returncode == 0, (entry, result.stdout, result.stderr)

            def browser_calls(expected):
                actual = calls.read_text().splitlines() if calls.exists() else []
                assert actual == [url + '/app/'] * expected, actual

            with sync_playwright() as p:
                browser = p.chromium.launch(channel='chrome', headless=True)
                context = browser.new_context()
                context.add_init_script('window.__focusCalls = 0; window.focus = () => window.__focusCalls++;')
                errors = []

                def wait_count(count):
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        if context.request.get(url + '/test/presence').json()['count'] == count:
                            return
                        time.sleep(.05)
                    raise AssertionError(f'presence count did not reach {count}')

                def page_open():
                    page = context.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(url + '/app/')
                    page.locator('#login').wait_for(state='visible')
                    return page

                initial = context.request.get(url + '/health').json()
                launch('bat')
                browser_calls(1)
                launch('shortcut')
                browser_calls(1)
                page = page_open()
                wait_count(1)
                background = context.new_page()
                background.goto('about:blank')
                launch('bat')
                launch('shortcut')
                page.wait_for_function('window.__focusCalls === 2')
                browser_calls(1)
                page.reload()
                wait_count(1)
                launch('bat')
                page.wait_for_function('window.__focusCalls === 1')
                second = page_open()
                wait_count(2)
                launch('shortcut')
                second.wait_for_function('window.__focusCalls === 1')
                second.close()
                wait_count(1)
                launch('bat')
                page.wait_for_function('window.__focusCalls === 2')
                browser_calls(1)
                page.close()
                wait_count(0)
                launch('shortcut')
                browser_calls(2)
                page = page_open()
                wait_count(1)
                page.close()
                wait_count(0)
                launch('bat')
                browser_calls(3)
                assert context.request.get(url + '/health').json() == initial
                assert not errors, errors
                browser.close()
            print(json.dumps({'result': 'PASS', 'entries': ['start.bat', 'Bot Group.lnk'],
                'cases': ['initial open', 'rapid repeat', 'login page', 'background page',
                          'reload', 'multiple pages', 'close all then reopen'],
                'browser_calls': 3, 'engine_instance_unchanged': True}))
        finally:
            child.communicate('\n', timeout=15)
            assert child.returncode == 0, child.returncode


if __name__ == '__main__':
    if '--record-browser' in sys.argv:
        with Path(os.environ['UI_BROWSER_CALLS']).open('a', encoding='utf-8') as output:
            output.write(sys.argv[-1] + '\n')
    elif '--server' in sys.argv:
        server()
    else:
        main()
