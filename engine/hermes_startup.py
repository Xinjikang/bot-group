"""Gateway startup diagnostics and one replay of a confirmed unexecuted admission."""
from contextlib import contextmanager
import json
import logging
import time

LOG = logging.getLogger(__name__)
MCP_FAILURE = 'RuntimeStoreError: acp_mcp_discovery_failed'


def failure_detail(conn, row, receipt):
    if receipt.get('outcome') != 'failed':
        return ''
    result = receipt.get('result')
    if not isinstance(result, dict):
        return ''
    error = result.get('error')
    if isinstance(error, dict):
        error = error.get('message')
    if not isinstance(error, str) or not error.strip():
        return ''
    from .execution_errors import redact, _secrets
    safe = redact(error, _secrets(conn, row['profile'], row['project'], None))
    LOG.warning('Gateway execution failure execution_id=%s detail=%s', row['execution_id'], safe)
    unexecuted = (error.strip() == MCP_FAILURE and result.get('failed') is True
                  and result.get('completed') is False and result.get('messages') == []
                  and result.get('final_response') == ''
                  and result.get('_messages_are_turn_suffix') is True)
    return json.dumps({'startup_retryable': unexecuted, 'error': safe})


def retryable(detail):
    try:
        value = json.loads(detail)
    except (ValueError, TypeError):
        return False
    return (isinstance(value, dict) and value.get('startup_retryable') is True
            and value.get('error') == MCP_FAILURE)


def force_refresh(conn, job_id):
    if job_id is None:
        return False
    rows = conn.execute('SELECT outcome,detail FROM hermes_executions WHERE job_id=?', (job_id,)).fetchall()
    return len(rows) == 1 and rows[0]['outcome'] == 'failed' and retryable(rows[0]['detail'])


def same_turn(conn, row):
    if row['kind'] != 'chat' or row['thread_id'] is None:
        return True
    topic = conn.execute('SELECT * FROM threads WHERE id=? AND project=?',
                         (row['thread_id'], row['project'])).fetchone()
    return bool(topic is not None and topic['status'] == 'open' and topic['turn_profile'] == row['profile']
                and topic['round'] == row['round'] and topic['turn_parent_id'] == row['parent_id']
                and row['profile'] in json.loads(topic['members'] or '[]'))


def eligible(conn, row):
    from . import wake, router
    if (row['outcome'] != 'failed' or row['cancel_reason'] or row['job_id'] is None
            or not retryable(row['detail']) or wake._shutting_down.is_set()
            or row['session'] in wake._intentional_stops):
        return False
    if not force_refresh(conn, row['job_id']) or not router._profile_active(conn, row['project'], row['profile']):
        return False
    job = conn.execute('SELECT * FROM wake_jobs WHERE id=?', (row['job_id'],)).fetchone()
    if (job is None or job['status'] not in ('done', 'running')
            or any(job[k] != row[k] for k in ('profile', 'project', 'kind', 'thread_id'))):
        return False
    project = conn.execute('SELECT archived,readiness_status FROM projects WHERE name=?', (row['project'],)).fetchone()
    if not project or project['archived'] or project['readiness_status'] != 'ready':
        return False
    if conn.execute("SELECT 1 FROM hermes_executions WHERE session=? AND execution_id<>? "
                    "AND (state<>'terminal' OR started_at>?)",
                    (row['session'], row['execution_id'], row['started_at'])).fetchone():
        return False
    conflict = ('thread_id IS ?' if row['kind'] == 'chat' else 'project=?')
    scope = row['thread_id'] if row['kind'] == 'chat' else row['project']
    if conn.execute("SELECT 1 FROM wake_jobs WHERE id<>? AND profile=? AND kind=? "
                    "AND status IN ('queued','running') AND " + conflict,
                    (row['job_id'], row['profile'], row['kind'], scope)).fetchone():
        return False
    return same_turn(conn, row)


def requeue(conn, row):
    from . import wake
    with wake._session_lock:
        conn.execute('BEGIN IMMEDIATE')
        try:
            if not eligible(conn, row):
                conn.rollback()
                return False
            conn.execute("UPDATE wake_jobs SET status='queued',started_at=NULL,ended_at=NULL,exit_code=NULL,"
                         "chat_context_until=NULL,detail='MCP initialization recovery (1/1)' WHERE id=?",
                         (row['job_id'],))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    LOG.warning('Gateway MCP startup recovery queued job_id=%s execution_id=%s retry=1 limit=1',
                row['job_id'], row['execution_id'])
    return True


@contextmanager
def phase(profile, project, session, name):
    started = time.monotonic()
    LOG.info('Gateway startup phase=%s profile=%r project=%r session=%r status=started',
             name, profile, project, session)
    try:
        yield
    except BaseException as exc:
        LOG.warning('Gateway startup phase=%s profile=%r project=%r session=%r status=failed '
                    'elapsed=%.3fs error_type=%s', name, profile, project, session,
                    time.monotonic() - started, type(exc).__name__)
        raise
    else:
        LOG.info('Gateway startup phase=%s profile=%r project=%r session=%r status=completed elapsed=%.3fs',
                 name, profile, project, session, time.monotonic() - started)
