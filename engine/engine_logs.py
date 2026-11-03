"""Bounded snapshots of the active engine log in the configured data directory."""
from . import config

MAX_BYTES = 256 * 1024
MAX_LINES = 1000


def snapshot() -> dict:
    path = config.logs_dir() / 'engine.log'
    try:
        with path.open('rb') as stream:
            size = stream.seek(0, 2)
            start = max(0, size - MAX_BYTES)
            stream.seek(start)
            raw = stream.read(min(size, MAX_BYTES))
    except FileNotFoundError:
        return {'text': '', 'lines': 0, 'truncated': False, 'available': False}
    # Drop the partial first line when the byte boundary falls inside a record.
    if start and b'\n' in raw:
        raw = raw.split(b'\n', 1)[1]
    lines = raw.decode('utf-8', errors='replace').splitlines()
    return {'text': '\n'.join(lines[-MAX_LINES:]), 'lines': min(len(lines), MAX_LINES),
            'truncated': bool(start or len(lines) > MAX_LINES), 'available': True}
