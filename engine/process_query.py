"""Process command lines with explicit unknown results and bounded diagnostics."""
from __future__ import annotations

import logging
import os
import threading
import time

import psutil

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_failures: dict[tuple[str, str], tuple[tuple, float]] = {}


def diagnostic(scope: str, stage: str, error: Exception | None = None) -> None:
    """Rate-limit failures by caller scope; omit exception text and command lines."""
    key = (scope, stage)
    with _lock:
        if error is None:
            if _failures.pop(key, None) is not None:
                _log.info('Process query recovered: scope=%s stage=%s', scope, stage)
            return
        signature = (type(error).__name__, getattr(error, 'winerror', None),
                     getattr(error, 'errno', None))
        now = time.monotonic()
        previous = _failures.get(key)
        if previous is None or previous[0] != signature or now - previous[1] >= 60:
            _failures[key] = (signature, now)
            _log.warning('Process query failed: scope=%s stage=%s error=%s winerror=%s errno=%s',
                         scope, stage, *signature)


def _command(process) -> list[str]:
    argv = process.cmdline()
    if not process.is_running():
        raise psutil.NoSuchProcess(process.pid)
    if not argv:
        # An alive candidate with unreadable arguments cannot prove absence.
        raise psutil.AccessDenied(process.pid)
    return argv


def command_line(pid: int, *, scope: str) -> list[str] | None:
    """Return argv, [] for an exited PID, or None when identity is unknown."""
    stage = f'command_line:{pid}'
    try:
        argv = _command(psutil.Process(pid))
    except psutil.NoSuchProcess:
        argv = []
    except (psutil.Error, OSError) as exc:
        diagnostic(scope, stage, exc)
        return None
    diagnostic(scope, stage)
    return argv


def _candidate_pids(names: set[str]) -> list[int]:
    if os.name == 'nt':
        from .process_tree import snapshot
        # Toolhelp names are current and avoid opening every system process.
        return [pid for pid, (_, name) in snapshot().items() if name in names]
    return psutil.pids()


def list_processes(names: set[str], *, scope: str) -> list[tuple[int, list[str]]] | None:
    """Read named candidates; any incomplete candidate makes the query unknown."""
    found = []
    try:
        for pid in _candidate_pids(names):
            try:
                # Fresh instances avoid process_iter's cached Windows names after PID reuse.
                process = psutil.Process(pid)
                if process.name().lower() not in names:
                    continue
                found.append((process.pid, _command(process)))
            except psutil.NoSuchProcess:
                continue
    except (psutil.Error, OSError) as exc:
        diagnostic(scope, 'enumerate', exc)
        return None
    diagnostic(scope, 'enumerate')
    return found


def option(argv: list[str], *names: str) -> str | None:
    """Read a CLI option, excluding positional prompt text after its delimiter."""
    for index, arg in enumerate(argv):
        if arg in ('--', '-q', '--query'):
            break
        if arg in names:
            return argv[index + 1] if index + 1 < len(argv) else None
        for name in names:
            if arg.startswith(name + '='):
                return arg[len(name) + 1:]
    return None
