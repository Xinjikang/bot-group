"""Persistent two-generation credentials; key bytes never enter the ledger."""
from __future__ import annotations

import csv
import ctypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import threading
import uuid
from contextlib import closing, contextmanager

from . import config

_lock = threading.RLock()
_active_path: Path | None = None
_trusted_keys: dict[int, bytes] = {}


class RecoveryError(RuntimeError):
    """Dispatch must remain stopped until authentication can be recovered."""


def path():
    return config.data_dir() / 'secrets' / 'bot-signing.key'


def _protect(target):
    if os.name != 'nt':
        target.chmod(0o700 if target.is_dir() else 0o600)
        return
    result = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'],
                            capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    sid = next(csv.reader([result.stdout.decode(errors='replace').strip()]))[-1]
    if not sid.startswith('S-1-') or any(c not in 'S-0123456789' for c in sid):
        raise RecoveryError('Authentication recovery failed: account SID unavailable')
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    descriptor = ctypes.c_void_p()
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = ctypes.c_int
    flags = 'OICI' if target.is_dir() else ''
    if not convert(f'D:P(A;{flags};FA;;;{sid})', 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        setter = advapi.SetFileSecurityW
        setter.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p]
        setter.restype = ctypes.c_int
        if not setter(str(target), 0x80000004, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(descriptor)


def read(target=None):
    target = target or path()
    try:
        value = json.loads(target.read_text(encoding='utf-8'))
        generation, encoded = value['generation'], value['keys']
        if type(generation) is not int or generation < 1 or not isinstance(encoded, dict):
            raise ValueError()
        if set(encoded) not in ({str(generation)}, {str(generation), str(generation - 1)}):
            raise ValueError()
        keys = {int(g): bytes.fromhex(k) for g, k in encoded.items()}
        if any(not k for k in keys.values()):
            raise ValueError()
        return generation, keys
    except (OSError, ValueError, KeyError, TypeError):
        raise RecoveryError('Authentication recovery failed: signing key file missing or damaged') from None


def _write(target, generation, keys):
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            _protect(temporary)
            json.dump({'generation': generation, 'keys': {str(g): k.hex() for g, k in keys.items()}}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        if os.name != 'nt':
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _serialized(target):
    with _lock:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _protect(target.parent)
        from .maintenance.database_layout import _lock as file_lock
        with file_lock(target.parent / 'bot-signing.lock'):
            yield


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _state(conn):
    tables = _tables(conn)
    initialized = ('bot_signing_state' in tables and
                   conn.execute('SELECT 1 FROM bot_signing_state').fetchone() is not None)
    rows = []
    if 'hermes_executions' in tables:
        columns = {r[1] for r in conn.execute('PRAGMA table_info(hermes_executions)')}
        field = 'signing_generation' if 'signing_generation' in columns else 'NULL'
        rows = conn.execute(f"SELECT {field} FROM hermes_executions WHERE state!='terminal'").fetchall()
    return initialized, [r[0] for r in rows]


def _validate(conn, target):
    initialized, references = _state(conn)
    if target.exists():
        generation, keys = read(target)
        if initialized:
            saved = conn.execute('SELECT generation,digests FROM bot_signing_state').fetchone()
            digests = json.loads(saved[1])
            if (saved[0] not in keys or generation not in (saved[0], saved[0] + 1)
                    or any(hashlib.sha256(keys[int(g)]).hexdigest() != digest
                           for g, digest in digests.items() if int(g) in keys)):
                raise RecoveryError('Authentication recovery failed: signing key fingerprint mismatch')
        if any(g is not None and g not in keys for g in references):
            raise RecoveryError('Authentication recovery failed: referenced signing generation unavailable')
        if initialized and any(g is None for g in references):
            raise RecoveryError('Authentication recovery failed: execution signing generation missing')
        if not initialized and references and generation != 1:
            raise RecoveryError('Authentication recovery failed: legacy generation unavailable')
        return generation, keys
    if initialized or any(g is not None for g in references):
        raise RecoveryError('Authentication recovery failed: initialized signing key file missing')
    if references and not os.environ.get('GROUP_ENGINE_KEY', '').strip():
        raise RecoveryError('Authentication recovery deferred: legacy executions active without GROUP_ENGINE_KEY; keep old service running')
    return None


def preflight():
    """Read-only check, called before the old service is asked to exit."""
    database = Path(config.db_path()).resolve()
    if not database.exists():
        if path().exists():
            read()
        return
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
        _validate(conn, path())


def initialize(conn):
    """Run once at service startup, before recovery and worker admission."""
    global _active_path, _trusted_keys
    target = path()
    with _serialized(target):
        conn.execute('BEGIN IMMEDIATE')
        try:
            loaded = _validate(conn, target)
            initialized, references = _state(conn)
            if loaded is None:
                generation = 1
                keys = {1: os.environ.get('GROUP_ENGINE_KEY', '').strip().encode() or secrets.token_bytes(32)}
                _write(target, generation, keys)
            else:
                generation, keys = loaded
                _protect(target)
                keys = {g: k for g, k in keys.items() if g == generation or g in references}
                # An unmarked file is the recoverable first-create crash window.
                if initialized and len(keys) == 1:
                    generation += 1
                    keys[generation] = secrets.token_bytes(32)
                _write(target, generation, keys)
            conn.execute('CREATE TABLE IF NOT EXISTS bot_signing_state '
                         '(initialized INTEGER PRIMARY KEY CHECK(initialized=1), generation INTEGER NOT NULL, digests TEXT NOT NULL)')
            conn.execute('INSERT OR REPLACE INTO bot_signing_state VALUES(1,?,?)',
                         (generation, json.dumps({str(g): hashlib.sha256(k).hexdigest() for g, k in keys.items()})))
            conn.execute('UPDATE hermes_executions SET signing_generation=1 WHERE signing_generation IS NULL')
            conn.commit()
            _active_path = target
            _trusted_keys = dict(keys)
        except BaseException:
            conn.rollback()
            raise


def current():
    """Initialized services always re-read the atomic file and fail closed."""
    if _active_path is None:
        return None
    generation, keys = read(_active_path)
    if any(keys[g] != key for g, key in _trusted_keys.items() if g in keys):
        raise RecoveryError('Authentication recovery failed: signing key changed during service lifetime')
    return generation, keys


def retire(conn):
    """A terminal ledger commit precedes retirement; current generation stays."""
    if _active_path is None:
        return
    with _serialized(_active_path):
        conn.execute('BEGIN IMMEDIATE')
        try:
            generation, keys = _validate(conn, _active_path)
            _, references = _state(conn)
            retained = {g: k for g, k in keys.items() if g == generation or g in references}
            if retained != keys:
                _write(_active_path, generation, retained)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
