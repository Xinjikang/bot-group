"""Read-only version discovery using the installer's formal release index."""
from __future__ import annotations

from datetime import datetime, timezone
import threading
import time

from . import config
from scripts import releases

_LOCK = threading.Lock()
_cached = None
_cache_key = None
_checked = 0.0


def version():
    try:
        value = (config.program_root() / 'VERSION').read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError):
        value = ''
    return {'current_version': value or None, 'version_known': releases.version_key(value) is not None}


def check():
    """Coalesce simultaneous page loads without downloading or installing assets."""
    global _cached, _cache_key, _checked
    current = version()
    url = config._pick(None, ('RELEASE_INDEX_URL',), releases.DEFAULT_INDEX_URL)
    key = (url, current['current_version'])
    with _LOCK:
        if (_cached is not None and _cached['status'] != 'unavailable'
                and _cache_key == key and time.monotonic() - _checked < 60):
            return dict(_cached)
        result = {**current, 'latest_version': None, 'update_available': False,
                  'checked_at': datetime.now(timezone.utc).isoformat(), 'status': 'unavailable'}
        try:
            target = releases.resolve(releases.pick_formal(releases.fetch_index(url)))
            current_key = releases.version_key(current['current_version'] or '')
            result.update(latest_version=target.version,
                          update_available=bool(current_key and releases.version_key(target.version) > current_key),
                          status='ok' if current_key else 'unknown_version')
        except Exception:
            # Provider errors may include private index URLs or credentials.
            result['message'] = '暂时无法检查更新，请稍后重试。'
        _cached, _cache_key, _checked = result, key, time.monotonic()
        return dict(result)
