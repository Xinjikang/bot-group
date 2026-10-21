"""Verify the running loopback engine's actual picker; cancel without saving a path."""
import concurrent.futures
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

def main(port=8180):
    import psutil
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetForegroundWindow.restype = wintypes.HWND
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    listeners = [c.pid for c in psutil.net_connections('tcp')
                 if c.status == 'LISTEN' and c.laddr.port == port]
    assert len(set(listeners)) == 1, listeners
    engine = psutil.Process(listeners[0])
    before = {p.pid for p in engine.children()}

    def request():
        req = urllib.request.Request(f'http://127.0.0.1:{port}/local/directory-picker',
            data=b'{}', headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=190) as response:
            return json.load(response)

    with concurrent.futures.ThreadPoolExecutor() as pool:
        result = pool.submit(request)
        seen = []
        handles = set()
        try:
            start = time.monotonic()
            while time.monotonic() - start < 12:
                pids = {p.pid for p in engine.children()
                        if p.pid not in before and p.name().lower() == 'powershell.exe'}
                windows = []

                @callback_type
                def collect(hwnd, data):
                    pid = wintypes.DWORD()
                    user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value in pids:
                        name = ctypes.create_unicode_buffer(256)
                        user.GetClassNameW(hwnd, name, 256)
                        if name.value == '#32770':
                            handles.add(hwnd)
                            windows.append({'hwnd': hwnd, 'visible': bool(user.IsWindowVisible(hwnd)),
                                'topmost': bool(user.GetWindowLongW(hwnd, -20) & 8),
                                'foreground': user.GetForegroundWindow() == hwnd})
                    return True

                user.EnumWindows(collect, 0)
                if windows:
                    sample = {'seconds': round(time.monotonic() - start, 1), 'windows': windows}
                    seen.append(sample)
                    if len(seen) == 1 or windows != seen[-2]['windows']:
                        print(json.dumps(sample), flush=True)
                if result.done():
                    print('API:', result.result(), flush=True)
                    break
                time.sleep(.2)
        finally:
            for hwnd in handles:
                user.PostMessageW(hwnd, 0x111, 2, 0)
        response = result.result(timeout=190)
        assert response == {'path': None}, response
        assert seen, 'No native dialog observed'
        stable = [s for s in seen if s['seconds'] >= 3]
        assert stable and all(any(w['visible'] and w['topmost'] for w in s['windows'])
                              for s in stable), 'Native dialog lost topmost during sustained observation'
        assert any(w['foreground'] for s in seen for w in s['windows']), 'Native dialog never gained foreground'
        print('PASS: live engine picker remains visible/topmost for 9 seconds; foreground observed; cancelled')


if __name__ == '__main__':
    if '--serve' in sys.argv:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from fastapi import FastAPI
        import uvicorn
        from engine.directory_picker import choose_directory
        app = FastAPI()

        @app.post('/local/directory-picker')
        def pick():
            return {'path': choose_directory('')}

        uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[-1]), log_config=None)
    elif '--isolated' in sys.argv:
        # Match start.sh: use the venv launcher, which forwards to the base
        # pythonw executable while retaining the project's site-packages.
        runtime = str(Path(__file__).resolve().parents[1] / '.venv/Scripts/pythonw.exe')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        server = subprocess.Popen([runtime, '-B', __file__, '--serve', str(port)],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            for _ in range(50):
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.2):
                        break
                except OSError:
                    if server.poll() is not None:
                        raise RuntimeError(server.communicate()[1].decode('utf-8', errors='replace'))
                    time.sleep(.1)
            print('Isolated API using production runtime:', runtime, flush=True)
            main(port)
        finally:
            server.terminate()
            server.wait(timeout=10)
    else:
        main()
