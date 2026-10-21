"""Open only our picker, verify native visibility/topmost/foreground, then cancel it."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.directory_picker import _SCRIPT


def main():
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user.GetForegroundWindow.restype = wintypes.HWND
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    env = os.environ.copy()
    env['GROUP_PICKER_INITIAL'] = str(Path(__file__).resolve().parents[1])
    binary = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    process = subprocess.Popen([str(binary), '-NoProfile', '-STA', '-Command', _SCRIPT],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, encoding='utf-8',
        creationflags=subprocess.CREATE_NO_WINDOW)
    hwnd = None
    previous = None
    first_seen = None
    foreground_seen = False
    stable_samples = []
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and process.poll() is None:
            windows = []

            @callback_type
            def collect(window, data):
                pid = wintypes.DWORD()
                user.GetWindowThreadProcessId(window, ctypes.byref(pid))
                if pid.value == process.pid and user.IsWindowVisible(window):
                    name = ctypes.create_unicode_buffer(256)
                    user.GetClassNameW(window, name, 256)
                    if name.value == '#32770':
                        windows.append(window)
                return True

            user.EnumWindows(collect, 0)
            if windows:
                hwnd = windows[0]
                topmost = bool(user.GetWindowLongW(hwnd, -20) & 0x8)
                foreground = user.GetForegroundWindow() == hwnd
                if first_seen is None:
                    first_seen = time.monotonic()
                foreground_seen = foreground_seen or foreground
                if time.monotonic() - first_seen >= 2:
                    stable_samples.append(topmost)
                sample = (hwnd, topmost, foreground)
                if sample != previous:
                    print('Native window:', sample, flush=True)
                    previous = sample
                if time.monotonic() - first_seen >= 5:
                    assert stable_samples and all(stable_samples), 'Picker lost topmost'
                    assert foreground_seen, 'Picker never gained foreground'
                    break
            time.sleep(.1)
        else:
            raise AssertionError('Picker did not become visible, topmost and foreground')
        user.PostMessageW(hwnd, 0x111, 2, 0)  # IDCANCEL, only our process window.
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert stdout == '', repr(stdout)
        print('PASS: sustained native visibility/topmost, foreground observed, cancel and process exit')
    finally:
        if process.poll() is None:
            process.kill()
            stdout, stderr = process.communicate()
            if stderr:
                print(stderr, file=sys.stderr)


if __name__ == '__main__':
    main()
