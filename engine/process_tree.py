"""Terminate execution trees while protecting the engine and its host ancestors."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import time

from .browser_resources import _pid_alive

_WRAPPERS = {'python.exe', 'pythonw.exe', 'nohup.exe', 'hermes.exe', 'codex.exe'}


def snapshot():
    """Read native PID/parent/name metadata without parsing localized shell output."""
    if os.name != 'nt':
        import psutil
        result = {}
        for pid in psutil.pids():
            try:
                process = psutil.Process(pid)
                result[pid] = (process.ppid(), process.name().lower())
            except psutil.NoSuchProcess:
                continue
        return result
    class Entry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('pid', wintypes.DWORD), ('heap', ctypes.c_size_t),
                    ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                    ('parent', wintypes.DWORD), ('priority', wintypes.LONG),
                    ('flags', wintypes.DWORD), ('name', wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = Entry()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel.Process32FirstW(handle, ctypes.byref(entry)):
            raise ctypes.WinError(ctypes.get_last_error())
        result = {}
        while True:
            result[entry.pid] = (entry.parent, entry.name.lower())
            if not kernel.Process32NextW(handle, ctypes.byref(entry)):
                if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                    raise ctypes.WinError(ctypes.get_last_error())
                return result
    finally:
        kernel.CloseHandle(handle)


def plan(pids, processes, owner, launchers=None):
    """Mirror the old start.sh parent walk, stopping before the engine/its ancestors."""
    from .process_query import matches_name
    protected, cursor = {0, 1, owner}, owner
    while cursor in processes:
        cursor = processes[cursor][0]
        if cursor in protected:
            break
        protected.add(cursor)
    roots = set()
    for pid in pids:
        if type(pid) is not int or pid <= 1 or pid in protected:
            raise RuntimeError('拒绝终止引擎或其宿主进程')
        if pid not in processes:
            continue
        root, seen = pid, {pid}
        ceiling = launchers.get(pid, pid) if launchers is not None else None
        while root in processes:
            if root == ceiling:
                break
            parent = processes[root][0]
            if parent in protected or parent in seen or parent not in processes:
                break
            if not matches_name(processes[parent][1], _WRAPPERS):
                break
            seen.add(parent)
            root = parent
        if ceiling is not None and root != ceiling:
            root = pid  # A stale/reparented launcher cannot authorize a wider kill.
        roots.add(root)
    children = set(roots)
    while True:
        found = {pid for pid, (parent, _) in processes.items() if parent in children}
        if found <= children:
            break
        children.update(found)
    if children & protected:
        raise RuntimeError('进程树包含受保护的引擎宿主')
    # One tree kill per outermost root, including MCP/tool descendants.
    roots = {pid for pid in roots if processes[pid][0] not in children}
    return roots, children


def terminate(pids, timeout=15, *, launchers=None):
    from .stop import _kill_process_tree
    roots, descendants = plan(pids, snapshot(), os.getpid(), launchers)
    for root in roots:
        if _pid_alive(root) and not _kill_process_tree(root) and _pid_alive(root):
            raise RuntimeError(f'停止 Bot 进程树失败：PID {root}')
    deadline = time.monotonic() + timeout
    while True:
        alive = [pid for pid in descendants if _pid_alive(pid)]
        if os.name != 'nt':
            import psutil
            live = []
            for pid in alive:
                try:
                    if psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
                        live.append(pid)
                except psutil.NoSuchProcess:
                    pass
            alive = live
        if not alive:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f'Bot 子进程仍在运行：{alive}')
        time.sleep(0.1)
