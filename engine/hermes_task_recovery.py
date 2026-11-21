"""Resume a stalled task through its current Hermes execution and existing wake job."""
from __future__ import annotations

import hashlib
import json
import logging
import time

logger = logging.getLogger(__name__)
CHECK_INTERVAL = 15
START_CHECK_INTERVAL = 0.2
CHECK_LEASE = 60
SCHEMA = """
CREATE TABLE IF NOT EXISTS hermes_task_recoveries (
    request_id TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL,
    task_id INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    binding_generation INTEGER NOT NULL,
    text TEXT NOT NULL,
    next_check_at REAL NOT NULL,
    admission_id TEXT,
    notice_message_id INTEGER,
    state TEXT NOT NULL DEFAULT 'pending'
);
CREATE UNIQUE INDEX IF NOT EXISTS hermes_task_recovery_pending
ON hermes_task_recoveries(execution_id) WHERE state='pending';
CREATE INDEX IF NOT EXISTS hermes_task_recovery_admission
ON hermes_task_recoveries(execution_id,admission_id) WHERE state IN ('started','accepted','unaccepted');
CREATE INDEX IF NOT EXISTS hermes_task_recovery_held
ON hermes_task_recoveries(job_id) WHERE state='unaccepted';
CREATE INDEX IF NOT EXISTS hermes_task_recovery_notice
ON hermes_task_recoveries(notice_message_id) WHERE notice_message_id IS NOT NULL;
"""


def _current_binding(conn, row):
    return conn.execute("SELECT b.generation FROM session_bindings b JOIN hermes_session_grants g "
                        'ON g.project=b.project AND g.bot_id=b.bot_id AND g.kind=b.kind '
                        'AND g.thread_id=b.thread_id AND g.generation=b.generation AND g.session_id=b.session_id '
                        'JOIN project_members m ON m.project=b.project AND m.profile=b.profile '
                        'JOIN projects p ON p.name=b.project '
                        "WHERE b.project=? AND b.profile=? AND b.session_id=? AND b.kind='work' "
                        "AND b.thread_id=0 AND b.runtime='hermes' AND b.switching=0 AND g.session=? "
                        "AND g.profile=b.profile AND g.revoked=0 AND m.active=1 "
                        "AND (m.bot_id IS NULL OR m.bot_id=b.bot_id) "
                        "AND p.archived=0 AND p.removed=0 AND p.readiness_status='ready'",
                        (row['project'], row['profile'], row['session_id'], row['session'])).fetchone()


def _waiting_is_stale(conn, row, now):
    return (row['state'] == 'waiting_delegation' and row['execution_deadline'] is not None
            and row['execution_deadline'] <= now and conn.execute(
                'SELECT 1 FROM hermes_delegations WHERE execution_id=? AND consumed=0 LIMIT 1',
                (row['execution_id'],)).fetchone())


def candidate(conn, job, *, now=None):
    """Select from the task queue, not from the old execution's optional task_id."""
    from . import scanner, task_retry, wake
    if job['kind'] != 'work' or job['thread_id'] is not None:
        return None
    now = time.time() if now is None else now
    session = wake.session_name(job['profile'], 'work', None, job['project'])
    row = conn.execute("SELECT * FROM hermes_executions WHERE session=? AND profile=? AND project=? "
                       "AND kind='work' AND thread_id IS NULL AND cancel_reason IS NULL "
                       "AND state IN ('manual','waiting_delegation')",
                       (session, job['profile'], job['project'])).fetchone()
    if row is None or (row['state'] != 'manual' and not _waiting_is_stale(conn, row, now)):
        return None
    binding = _current_binding(conn, row)
    if binding is None:
        return None
    explicit = job['trigger'].removeprefix('retry:task:') if job['trigger'].startswith('retry:task:') else ''
    if explicit and not explicit.isdecimal():
        return None
    preferred = int(explicit) if explicit else row['task_id']
    if row['task_id'] is not None and preferred != row['task_id']:
        return None
    tid = task_retry.select_task(conn, row['profile'], row['project'], preferred)
    if tid is None:
        return None
    task = conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone()
    node = conn.execute('SELECT id,ts FROM nodes WHERE task_id=? AND project=? ORDER BY id DESC LIMIT 1',
                        (tid, row['project'])).fetchone()
    claim = scanner._parse_ts(task['claimed_at'] or '')
    last_node = scanner._parse_ts(node['ts']) if node else None
    if not claim or now - max(claim, last_node or 0) <= scanner.CLAIM_STALE_SECONDS:
        return None
    # A new admission, claim, node or binding is a new stall, not a retransmit.
    anchor = (row['execution_id'], row['admission_id'], row['generation'], tid,
              task['claimed_at'], task['status'], node['id'] if node else None, binding['generation'])
    request = 'task-recovery-' + hashlib.sha256(json.dumps(anchor).encode()).hexdigest()
    return row, tid, binding['generation'], request


def _text(conn, row, tid):
    waits = conn.execute('SELECT delegation_id,consumed FROM hermes_delegations WHERE execution_id=? '
                         'ORDER BY delegation_id', (row['execution_id'],)).fetchall()
    # Only tool-owned handles and recorded states belong in the resume signal.
    processes = [{'id': wait['delegation_id'][8:], 'result_pending': not bool(wait['consumed'])}
                 for wait in waits if wait['delegation_id'].startswith('process:')]
    return (f'[系统唤醒信号] 接续未完成任务 #{tid}。先调用 task_get 读取原任务与最新节点，'
            '按当前归属继续并在原任务追加节点。不要重启已经发出的命令。'
            f'已有后台进程句柄与待核对结果：{json.dumps(processes, ensure_ascii=False)}。'
            '有句柄时先查询结果，无结果则保留待核对。')


def reserve(conn, job, selected):
    """Transactionally reserve the same wake and logical execution; no RPC under the write lock."""
    now = time.time()
    conn.execute('BEGIN IMMEDIATE')
    try:
        current = conn.execute("SELECT * FROM wake_jobs WHERE id=? AND status='queued'",
                               (job['id'],)).fetchone()
        choice = candidate(conn, current, now=now) if current and not holds_wake(conn, current) else None
        if choice is None or choice[3] != selected[3]:
            return False
        row, tid, generation, request = choice
        old = pending(conn, row)
        if old and old['request_id'] != request:
            conn.execute("UPDATE hermes_task_recoveries SET state='abandoned' WHERE request_id=?",
                         (old['request_id'],))
        # Reuse the original payload exactly, including after a lost submit response.
        changed = conn.execute('INSERT INTO hermes_task_recoveries '
            '(request_id,execution_id,task_id,job_id,binding_generation,text,next_check_at) VALUES(?,?,?,?,?,?,?) '
            'ON CONFLICT(request_id) DO UPDATE SET next_check_at=excluded.next_check_at '
            "WHERE state='pending' AND next_check_at<=?",
            (request, row['execution_id'], tid, job['id'], generation, _text(conn, row, tid),
             now + CHECK_LEASE, now)).rowcount
        conn.commit()
        return bool(changed)
    finally:
        if conn.in_transaction:
            conn.rollback()


def pending(conn, row):
    return conn.execute("SELECT * FROM hermes_task_recoveries WHERE execution_id=? AND state='pending'",
                        (row['execution_id'],)).fetchone()


def active_cue(conn, row):
    return conn.execute("SELECT * FROM hermes_task_recoveries WHERE execution_id=? "
                        "AND admission_id=? AND state IN ('started','accepted','unaccepted')",
                        (row['execution_id'], row['admission_id'])).fetchone()


def current_admission(conn, row):
    return active_cue(conn, row) is not None


def mark_accepted(conn, cue):
    """A successful task node and the wake acknowledgement commit together."""
    from . import db
    if conn.execute("UPDATE hermes_task_recoveries SET state='accepted' WHERE request_id=? AND state='started'",
                    (cue['request_id'],)).rowcount:
        conn.execute("UPDATE wake_jobs SET status='done',ended_at=?,detail='Hermes task recovery accepted' "
                     "WHERE id=? AND status='queued'", (db.now_iso(), cue['job_id']))


def _notice(conn, row, cue, *, ended):
    """Append a notice once; subsequent actions belong to the recovery state."""
    from . import db
    mid = cue['notice_message_id']
    if mid is not None:
        return (mid, False) if ended else None
    reason = ('本轮已结束，但未在原任务追加节点。' if ended
              else 'Hermes 无法确认旧执行的结果（unknown）。')
    text = (f"任务 #{cue['task_id']} 自动续办受阻：{reason}"
            '请先在 Hermes 中核对旧执行结果和后台进程；不要重跑原命令。')
    mid = conn.execute("INSERT INTO messages(project,ts,sender,sender_type,content,mentions,need_reply) "
                       "VALUES(?,?,'engine','engine',?,'[\"user\"]',0)",
                       (row['project'], db.now_iso(), text)).lastrowid
    conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(?,'user','pending')", (mid,))
    conn.execute('UPDATE hermes_task_recoveries SET notice_message_id=? WHERE request_id=?',
                 (mid, cue['request_id']))
    return mid, True


def publish_notice(project, notice):
    if notice is None:
        return
    from . import events
    mid, added = notice
    data = {'project': project, 'message_id': mid}
    if added:
        events.bus.publish('message.new', {**data, 'sender': 'engine', 'thread_id': None})
    else:
        # Invalidate only: a delayed event must never carry stale state into the UI.
        events.bus.publish('recovery.notice.changed', data)


def _release_ready(cue):
    return (cue['execution_state'] == 'terminal' and not cue['cleanup_pending']
            and cue['admission_id'] and cue['admission_id'] == cue['latest_admission'])


def notice_states(conn, project):
    """Current state of notified rounds, keyed by immutable message ID, not the timeline cursor."""
    rows = conn.execute("SELECT r.notice_message_id, r.state, r.admission_id, e.state AS execution_state, "
                        "e.cleanup_pending, e.admission_id AS latest_admission FROM hermes_task_recoveries r "
                        "JOIN hermes_executions e ON e.execution_id=r.execution_id "
                        "JOIN wake_jobs j ON j.id=r.job_id "
                        "WHERE r.notice_message_id IS NOT NULL AND e.kind='work' AND e.thread_id IS NULL "
                        "AND (r.state='released' OR (r.state='unaccepted' AND j.status='queued')) "
                        "AND (? IS NULL OR e.project=?)", (project, project))
    return {r['notice_message_id']: ('released' if r['state'] == 'released' else
                                     'releasable' if _release_ready(r) else 'cleaning') for r in rows}


def mark_unaccepted(conn, row, cue):
    from . import db, task_retry
    if not conn.execute("UPDATE hermes_task_recoveries SET state='unaccepted' WHERE request_id=? "
                        "AND state IN ('pending','started')", (cue['request_id'],)).rowcount:
        return None
    if task_retry.select_task(conn, row['profile'], row['project'], cue['task_id']) is None:
        conn.execute("UPDATE wake_jobs SET status='cancelled',ended_at=?,detail='Unaccepted recovery no longer owns task' "
                     "WHERE id=? AND status='queued'", (db.now_iso(), cue['job_id']))
        return None
    if conn.execute("UPDATE wake_jobs SET detail='Hermes recovery ended without a task node; review before retry' "
                    "WHERE id=? AND status='queued'", (cue['job_id'],)).rowcount:
        return _notice(conn, row, cue, ended=True)
    return None


def holds_wake(conn, job):
    """Only an explicit release retires a notified recovery's exact queue record."""
    return conn.execute("SELECT 1 FROM hermes_task_recoveries WHERE job_id=? AND state='unaccepted' LIMIT 1",
                        (job['id'],)).fetchone() is not None


def release(conn, project, notice_id):
    """A user acknowledges exactly one confirmed exit, not a session or all its jobs."""
    from . import db, task_retry, taskpool
    conn.execute('BEGIN IMMEDIATE')
    try:
        cue = conn.execute('SELECT r.*, e.profile, e.state AS execution_state, e.cleanup_pending, '
                           'e.admission_id AS latest_admission, e.event_floor FROM hermes_task_recoveries r '
                           'JOIN hermes_executions e ON e.execution_id=r.execution_id '
                           'WHERE r.notice_message_id=? AND e.project=? AND e.kind=\'work\' AND e.thread_id IS NULL',
                           (notice_id, project)).fetchone()
        if cue is None or cue['state'] not in ('unaccepted', 'released'):
            raise ValueError('本项目不存在此待解除的续办提醒')
        tid = cue['task_id']
        if cue['state'] == 'released':
            return {'task_id': tid, 'notice_message_id': notice_id, 'detail': f'任务 #{tid} 的待核对催办已解除。'}
        if not _release_ready(cue):
            raise ValueError('本次执行结束或清理尚未确认，暂不能解除；请核对 Hermes 后重试')
        changed = conn.execute("UPDATE wake_jobs SET status='cancelled',ended_at=?,detail='Unaccepted recovery released by user' "
                               "WHERE id=? AND profile=? AND project=? AND kind='work' AND status='queued'",
                               (db.now_iso(), cue['job_id'], cue['profile'], project)).rowcount
        if not changed:
            raise ValueError('原催办已不处于待核对状态')
        task = conn.execute('SELECT * FROM tasks WHERE id=? AND project=?', (tid, project)).fetchone()
        progressed = conn.execute("SELECT 1 FROM events WHERE project=? AND task_id=? AND id>? "
                                  "AND event_type IN ('node','resume') LIMIT 1",
                                  (project, tid, cue['event_floor'] or 0)).fetchone()
        paused = not progressed and task_retry.select_task(conn, cue['profile'], project, tid) is not None
        if paused:
            next_bot = cue['profile'] if task['status'] == 'in_progress' else task['next_bot']
            conn.execute("UPDATE tasks SET status='blocked',block_kind='manual',next_bot=?,claimed_by='' "
                         "WHERE id=? AND project=?", (next_bot, tid, project))
            taskpool._log_event(conn, 'user', 'block', project, requirement_id=task['requirement_id'],
                                task_id=tid, detail=f'manual: release unaccepted recovery notice {notice_id}')
            taskpool._set_requirement_updated(conn, task['requirement_id'])
        conn.execute("UPDATE hermes_task_recoveries SET state='released' WHERE request_id=?", (cue['request_id'],))
        conn.execute("UPDATE inbox SET status='seen',seen_at=? WHERE message_id=? AND profile='user' AND status='pending'",
                     (db.now_iso(), notice_id))
        conn.commit()
        publish_notice(project, (notice_id, False))
        detail = f'任务 #{tid} 的待核对催办已解除，未重投。'
        if paused:
            detail += '原任务已暂挂；需要续办时，可在任务侧栏恢复。'
        return {'task_id': tid, 'notice_message_id': notice_id, 'job_id': cue['job_id'], 'detail': detail}
    finally:
        if conn.in_transaction:
            conn.rollback()


def _owned(conn, row, cue):
    job = conn.execute("SELECT * FROM wake_jobs WHERE id=? AND status='queued'", (cue['job_id'],)).fetchone()
    current = candidate(conn, job) if job else None
    return current is not None and current[3] == cue['request_id']


def _record(conn, row, cue, *, unknown=False, receipt=None, adopt=False):
    """Fence ownership and persist the notice or admission in one SQLite write transaction."""
    from . import hermes_gateway_ledger as ledger
    mid = None
    conn.execute('BEGIN IMMEDIATE')
    try:
        current = pending(conn, row)
        if not current or current['request_id'] != cue['request_id']:
            return False
        request = cue['request_id']
        if not _owned(conn, row, current):
            conn.execute("UPDATE hermes_task_recoveries SET state='abandoned' WHERE request_id=?", (request,))
            conn.commit()
            return False
        if receipt is not None:
            if not _matches(row, current, receipt):
                return False
            conn.execute('UPDATE hermes_task_recoveries SET admission_id=? WHERE request_id=?',
                         (receipt['admission_id'], request))
        ended = receipt is not None and receipt['status'] == 'terminal' and type(receipt.get('execution_generation')) is int
        if ended:
            conn.execute('INSERT OR IGNORE INTO hermes_native_turns(execution_id,admission_id,request_id) VALUES(?,?,?)',
                         (row['execution_id'], receipt['admission_id'], request))
            floor = conn.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
            conn.execute('UPDATE hermes_executions SET event_floor=? WHERE execution_id=?',
                         (floor, row['execution_id']))
            ledger.record_terminal(conn, row, receipt, time.time())
            mid = mark_unaccepted(conn, row, current)
        elif unknown:
            mid = _notice(conn, row, current, ended=False)
            conn.execute('UPDATE hermes_task_recoveries SET next_check_at=? WHERE request_id=?',
                         (time.time() + CHECK_INTERVAL, request))
        elif adopt:
            conn.execute('INSERT OR IGNORE INTO hermes_delegation_cycles VALUES(?,?)',
                         (row['execution_id'], cue['binding_generation']))
            conn.execute('INSERT OR IGNORE INTO hermes_native_turns(execution_id,admission_id,request_id) VALUES(?,?,?)',
                         (row['execution_id'], receipt['admission_id'], request))
            floor = conn.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
            conn.execute("UPDATE hermes_executions SET state='running',admission_id=?,generation=?,authority_epoch=?,"
                         "task_id=?,job_id=?,event_floor=?,execution_deadline=?,detail='' WHERE execution_id=?",
                         (receipt['admission_id'], receipt['execution_generation'], receipt['authority_epoch'],
                          cue['task_id'], cue['job_id'], floor, time.time() + 3600, row['execution_id']))
            conn.execute("UPDATE hermes_task_recoveries SET state='started' WHERE request_id=?", (request,))
        else:
            delay = (START_CHECK_INTERVAL if receipt and receipt['status'] == 'queued' else CHECK_INTERVAL)
            conn.execute('UPDATE hermes_task_recoveries SET next_check_at=? WHERE request_id=?',
                         (time.time() + delay, request))
        conn.commit()
    finally:
        if conn.in_transaction:
            conn.rollback()
    publish_notice(row['project'], mid)
    return ended or (adopt and receipt['status'] == 'started')


def _matches(row, cue, receipt):
    return (receipt.get('ref') == {'session_id': row['session_id'], 'profile_id': row['profile_id']}
            and isinstance(receipt.get('admission_id'), str) and bool(receipt['admission_id'])
            and receipt['admission_id'] != row['admission_id']
            and (not cue['admission_id'] or cue['admission_id'] == receipt['admission_id'])
            and receipt.get('input_id') in (None, cue['request_id'])
            and type(receipt.get('authority_epoch')) is int
            and (row['authority_epoch'] is None or receipt['authority_epoch'] >= row['authority_epoch']))


def _native_snapshot(client, sid):
    snapshot = client.rpc('session.resume', session_id=sid, omit_messages=True, timeout=5)
    if snapshot['session_id'] != sid or not isinstance(snapshot['pending'], list):
        raise ValueError('Recovery snapshot does not match the current session')
    return snapshot


def ensure_ready(scope, task_id):
    """Let the first read confirm this B admission without waiting for a worker tick."""
    from contextlib import closing
    from . import db, hermes_gateway, hermes_session_auth
    with closing(db.get_connection()) as conn:
        row = conn.execute("SELECT * FROM hermes_executions WHERE session=? AND session_id=? "
                           "AND project=? AND profile=? AND kind='work' AND thread_id IS NULL "
                           "AND state IN ('manual','waiting_delegation') AND cancel_reason IS NULL",
                           (scope['session'], scope['session_id'], scope['project'], scope['profile'])).fetchone()
        cue = pending(conn, row) if row else None
        if not cue or cue['task_id'] != task_id or cue['binding_generation'] != scope['generation']:
            return
        hermes_session_auth.validate(conn, scope)
        if not _owned(conn, row, cue):
            return
        # No write lock, no submission and no gateway startup on the HTTP path.
        try:
            sid = row['session_id']
            client, _, _ = hermes_gateway.connection(row['profile'], home=row['home'], read_only=True)
            with closing(client):
                snapshot = _native_snapshot(client, sid)
        except (KeyError, ValueError, OSError, TimeoutError, hermes_gateway.GatewayTransportError,
                hermes_gateway.GatewayUnavailable, hermes_gateway.GatewayRefusal):
            logger.warning('Could not verify Hermes recovery readiness execution=%s', row['execution_id'])
            return  # The normal authorization still fails closed.
        active = [r for r in snapshot['pending'] if r.get('status') in ('started', 'unknown')]
        if snapshot['running'] and len(active) == 1:
            receipt = active[0]
            if (receipt.get('status') == 'started' and _matches(row, cue, receipt)
                    and (cue['admission_id'] or receipt.get('input_id') == cue['request_id'])
                    and type(receipt.get('execution_generation')) is int):
                if _record(conn, row, cue, receipt=receipt, adopt=True):
                    hermes_gateway._start_worker(row['execution_id'])


def poll(conn, row, client, cue):
    """Ask only the current native session. An unknown old admission must not be discarded."""
    if not _owned(conn, row, cue):
        return _record(conn, row, cue)
    sid = row['session_id']
    snapshot = _native_snapshot(client, sid)
    items = snapshot['pending']
    own = next((item for item in items if item.get('input_id') == cue['request_id']
                or (cue['admission_id'] and cue['admission_id'] == item['admission_id'])), None)
    if any(item['status'] == 'unknown' for item in items):
        return _record(conn, row, cue, unknown=True)
    if own is not None:
        receipt = own
    elif cue['admission_id']:
        receipt = client.rpc('prompt.receipt', session_id=sid, admission_id=cue['admission_id'], timeout=5)
    elif snapshot['running'] or items:
        # A callback or other current turn is active: preserve it and the original wake.
        return _record(conn, row, cue)
    elif _owned(conn, row, cue):
        receipt = client.rpc('prompt.submit', session_id=sid, submission_id=cue['request_id'],
                             text=cue['text'], timeout=5)
    else:
        return _record(conn, row, cue)
    if (receipt.get('ref') != {'session_id': sid, 'profile_id': row['profile_id']}
            or not isinstance(receipt.get('admission_id'), str) or not receipt['admission_id']
            or type(receipt.get('authority_epoch')) is not int):
        raise ValueError('Recovery receipt does not match the current session')
    status = receipt['status']
    if status == 'unknown':
        return _record(conn, row, cue, unknown=True, receipt=receipt)
    if status == 'queued':
        return _record(conn, row, cue, receipt=receipt)
    # A terminal receipt with no execution generation can be a cancelled queue item.
    if status not in ('started', 'terminal') or type(receipt.get('execution_generation')) is not int:
        logger.warning('Hermes recovery did not start request=%s status=%s', cue['request_id'], status)
        return _record(conn, row, cue, unknown=True, receipt=receipt)
    return _record(conn, row, cue, receipt=receipt, adopt=True)
