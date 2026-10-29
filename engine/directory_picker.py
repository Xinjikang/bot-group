"""Platform folder selection for the local administrator UI."""
import os
import subprocess
import sys
import threading

from fastapi import HTTPException

_lock = threading.Lock()
_SCRIPT = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -ReferencedAssemblies System.Windows.Forms -TypeDefinition @'
using System;
using System.Text;
using System.Threading;
using System.Runtime.InteropServices;
using System.Windows.Forms;
public static class FolderPickerWindow {
    private delegate bool WindowCallback(IntPtr hwnd, IntPtr data);
    [DllImport("user32.dll")] private static extern bool EnumWindows(WindowCallback callback, IntPtr data);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint process);
    [DllImport("kernel32.dll")] private static extern uint GetCurrentProcessId();
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetClassName(IntPtr hwnd, StringBuilder name, int size);
    [DllImport("user32.dll")] private static extern bool SetWindowPos(IntPtr hwnd, IntPtr after, int x, int y, int width, int height, uint flags);
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr hwnd, int command);
    [DllImport("user32.dll")] private static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
    [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
    [DllImport("kernel32.dll")] private static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] private static extern int GetWindowLong(IntPtr hwnd, int index);
    public static DialogResult Show(FolderBrowserDialog dialog, Form owner) {
        uint process = GetCurrentProcessId();
        using (var stop = new ManualResetEvent(false)) {
            // The shell can create its dialog on another thread. Inspect this
            // process from a worker independent of the modal UI message pump.
            var watcher = new Thread(delegate() {
                IntPtr activated = IntPtr.Zero;
                while (!stop.WaitOne(100)) {
                    EnumWindows(delegate(IntPtr hwnd, IntPtr data) {
                        uint ownerProcess;
                        GetWindowThreadProcessId(hwnd, out ownerProcess);
                        if (ownerProcess != process || !IsWindowVisible(hwnd)) return true;
                        var name = new StringBuilder(256);
                        GetClassName(hwnd, name, name.Capacity);
                        if (name.ToString() == "#32770") {
                            if ((GetWindowLong(hwnd, -20) & 8) == 0) {
                                // ASYNCWINDOWPOS avoids cross-thread modal deadlocks;
                                // SHOWWINDOW (no NOACTIVATE) makes the dialog both
                                // topmost and active, so a fullscreen/maximized
                                // Chrome no longer hides it behind itself.
                                SetWindowPos(hwnd, new IntPtr(-1), 0, 0, 0, 0, 0x4043);
                            }
                            if (activated != hwnd) {
                                // Windows blocks SetForegroundWindow from background
                                // threads (foreground lock). Attach our input queue to
                                // the current foreground thread, restore the dialog,
                                // then activate; detach in all cases. SW_RESTORE first
                                // so a minimized/obscured dialog is brought to front
                                // and given keyboard focus. Retry next cycle until it
                                // actually takes foreground, then leave the user's
                                // subsequent app switches alone.
                                uint currentThread = GetCurrentThreadId();
                                IntPtr foreground = GetForegroundWindow();
                                uint foregroundThread = 0;
                                if (foreground != IntPtr.Zero) {
                                    uint foregroundProcess;
                                    foregroundThread = GetWindowThreadProcessId(foreground, out foregroundProcess);
                                }
                                bool attached = foregroundThread != 0
                                    && foregroundThread != currentThread
                                    && AttachThreadInput(currentThread, foregroundThread, true);
                                try {
                                    ShowWindow(hwnd, 9); // SW_RESTORE
                                    if (SetForegroundWindow(hwnd)) activated = hwnd;
                                } finally {
                                    if (attached) AttachThreadInput(currentThread, foregroundThread, false);
                                }
                            }
                        }
                        return true;
                    }, IntPtr.Zero);
                }
            });
            watcher.IsBackground = true;
            watcher.Start();
            try {
                owner.Show();
                owner.Activate();
                return dialog.ShowDialog(owner);
            } finally {
                stop.Set();
                watcher.Join();
            }
        }
    }
}
'@
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Select project folder'
$dialog.ShowNewFolderButton = $false
$dialog.SelectedPath = $env:GROUP_PICKER_INITIAL
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.Opacity = 0
try {
    if ([FolderPickerWindow]::Show($dialog, $owner) -eq [System.Windows.Forms.DialogResult]::OK) {
        [Console]::Write($dialog.SelectedPath)
    }
} finally {
    $dialog.Dispose()
    $owner.Dispose()
}
"""


_MAC_SCRIPT = '''
try
    set initialPath to system attribute "GROUP_PICKER_INITIAL"
    if initialPath is "" then
        set chosen to choose folder with prompt "Select project folder"
    else
        set chosen to choose folder with prompt "Select project folder" default location (POSIX file initialPath)
    end if
    return POSIX path of chosen
on error number -128
    return ""
end try
'''
_TK_SCRIPT = '''
import os
import tkinter as tk
from tkinter.filedialog import askdirectory
root = tk.Tk()
root.withdraw()
try:
    print(askdirectory(parent=root, title="Select project folder",
                       initialdir=os.environ.get("GROUP_PICKER_INITIAL") or None, mustexist=True))
finally:
    root.destroy()
'''


def choose_directory(initial: str) -> str | None:
    if not _lock.acquire(blocking=False):
        raise HTTPException(409, '目录选择器已打开，请先完成当前选择')
    try:
        env = os.environ.copy()
        env['GROUP_PICKER_INITIAL'] = initial
        flags = 0
        if sys.platform == 'win32':
            powershell = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'),
                                      'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
            command = [powershell, '-NoProfile', '-STA', '-Command', _SCRIPT]
            flags = subprocess.CREATE_NO_WINDOW
        elif sys.platform == 'darwin':
            command = ['/usr/bin/osascript', '-e', _MAC_SCRIPT]
        else:
            command = [sys.executable, '-X', 'utf8', '-c', _TK_SCRIPT]
        result = subprocess.run(
            command,
            env=env, capture_output=True, encoding='utf-8', timeout=180,
            creationflags=flags,
        )
        if result.returncode:
            raise HTTPException(503, '无法打开目录选择器，请确认本机图形桌面可用（Linux 还需安装 tkinter），或直接输入路径')
        return result.stdout.strip() or None
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(408, '目录选择已超时，请重试或直接输入路径') from exc
    except OSError as exc:
        raise HTTPException(503, '无法打开目录选择器，请直接输入路径') from exc
    finally:
        _lock.release()
