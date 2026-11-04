"""Persist deferred session-query failures without consuming queued work."""
import json
import time

from . import hermes_gateway, wake

PREFIX = 'session_state_query: '
ALERT_AFTER_SECONDS = 60


def record(conn, job):
    session = wake.session_name(job['profile'], job['kind'], job['thread_id'], job['project'])
    binding = hermes_gateway._binding_for_engine_session(conn, job['profile'], session)
    failure = None
    if binding:
        key = (binding['session_id'], binding['generation'])
        with hermes_gateway._native_cache_lock:
            cached = hermes_gateway._native_cache.get(key)
        if cached and cached[0] == 'error':
            failure = cached
    if failure is None:
        if (job['detail'] or '').startswith(PREFIX):
            conn.execute("UPDATE wake_jobs SET detail=NULL WHERE id=? AND status='queued' AND detail=?",
                         (job['id'], job['detail']))
            conn.commit()
        return
    _, _, fails, reason, first = failure
    detail = PREFIX + json.dumps({'state': 'error', 'reason': reason,
                                 'session_id': binding['session_id'],
                                 'generation': binding['generation']}, ensure_ascii=False)
    if job['detail'] != detail:
        conn.execute("UPDATE wake_jobs SET detail=? WHERE id=? AND status='queued'",
                     (detail, job['id']))
        conn.commit()
    if fails >= 3 and time.monotonic() - first >= ALERT_AFTER_SECONDS:
        from . import execution_errors
        attempt = f"preflight:{job['id']}:{binding['session_id']}:{binding['generation']}"
        if conn.execute('SELECT 1 FROM execution_errors WHERE project=? AND profile=? AND attempt=?',
                        (job['project'], job['profile'], attempt)).fetchone():
            return
        # The notification has its own durable deduplication key; the queued job stays retryable.
        execution_errors.report(job['profile'], job['project'], job['thread_id'], attempt,
                                f'session state query failed: {reason}')
