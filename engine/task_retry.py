"""Task binding and bounded retry decisions for engine-owned executions."""
MAX_ATTEMPTS = 3


def project_owner(conn, project):
    """Resolve the configured, enabled owner to a profile or the human fallback."""
    from .router import _profile_active
    from .db import BUILTIN_USER_PROFILE
    row = conn.execute("SELECT m.profile FROM settings s JOIN members m ON m.profile=s.value "
                       "WHERE s.key='owner_profile' AND s.project=? AND m.profile<>?",
                       (project, BUILTIN_USER_PROFILE)).fetchone()
    return row['profile'] if row and _profile_active(conn, project, row['profile']) else BUILTIN_USER_PROFILE


def notification_targets(conn, project, sender):
    """Use the project owner, escalating self-notifications to the human."""
    from .db import BUILTIN_USER_PROFILE
    owner = project_owner(conn, project)
    return [owner if owner != sender else BUILTIN_USER_PROFILE]


def select_task(conn, profile, project, preferred=None):
    """Resolve one exact task before starting the process; never mutate state."""
    rows = conn.execute(
        "SELECT t.* FROM tasks t JOIN requirements r ON r.id=t.requirement_id "
        "WHERE t.project=? AND r.status IN ('open','blocked') AND r.archived=0 "
        "AND ((t.status='in_progress' AND t.claimed_by=?) "
        "OR (t.status='pending' AND t.next_bot=?)) "
        "ORDER BY (t.status='in_progress') DESC,r.id,t.id",
        (project, profile, profile)).fetchall()
    from .task_brief import ready, dependency_blockers
    rows = [r for r in rows if (r['status'] == 'in_progress' or ready(r))
            and not dependency_blockers(conn, r)]
    if preferred is not None:
        return next((r['id'] for r in rows if r['id'] == preferred), None)
    return rows[0]['id'] if rows else None


def retry_action(task, profile, failures, interrupted=False):
    """Manual/agent decisions and successful handoff take precedence."""
    if interrupted or task is None:
        return 'preserve'
    owned = (task['status'] == 'in_progress' and task['claimed_by'] == profile
             or task['status'] == 'pending' and task['next_bot'] == profile)
    if not owned:
        return 'preserve'
    return 'block' if failures >= MAX_ATTEMPTS else 'retry'


def finish(profile, project, task_id, attempt, reason, job_id=None, event_floor=None):
    """Persist a failure for one bound task; retry only its work session."""
    import json
    from . import db, taskpool, wake, events, router
    if task_id is None:
        return {'blocked': [], 'retry': []}
    conn = db.get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        task = conn.execute('SELECT * FROM tasks WHERE id=? AND project=?', (task_id, project)).fetchone()
        from .requirement_lifecycle import check_task
        if task is not None and check_task(conn, task):
            conn.rollback()
            return {'blocked': [], 'retry': []}
        if retry_action(task, profile, 1) == 'preserve':
            conn.rollback()
            return {'blocked': [], 'retry': []}
        if event_floor is not None and conn.execute(
            "SELECT 1 FROM events WHERE task_id=? AND event_type IN ('node','resume') AND id>?",
            (task_id, event_floor)).fetchone():
            conn.rollback()
            return {'blocked': [], 'retry': []}
        floor = conn.execute("SELECT COALESCE(MAX(id),0) FROM events WHERE task_id=? "
                             "AND event_type IN ('node','resume')", (task_id,)).fetchone()[0]
        failures = conn.execute("SELECT detail FROM events WHERE task_id=? AND actor=? "
                                "AND event_type='execution.failed' AND id>?", (task_id, profile, floor)).fetchall()
        if any(json.loads(r['detail'])['attempt'] == attempt for r in failures):
            conn.rollback()
            return {'blocked': [], 'retry': []}
        count = len(failures) + 1
        action = retry_action(task, profile, count)
        detail = json.dumps({'attempt': attempt, 'failure': count, 'error': reason[-2000:]}, ensure_ascii=False)
        taskpool._log_event(conn, profile, 'execution.failed', project,
                            requirement_id=task['requirement_id'], task_id=task_id, detail=detail)
        if action == 'block':
            conn.execute("UPDATE tasks SET status='blocked',block_kind='technical' WHERE id=?", (task_id,))
            conn.execute("UPDATE requirements SET status='blocked',updated_at=? WHERE id=?",
                         (db.now_iso(), task['requirement_id']))
            taskpool._log_event(conn, profile, 'block', project,
                                requirement_id=task['requirement_id'], task_id=task_id, detail=detail)
        # Close only this attempt's queue record. A fast-exiting child may finish
        # before the queue worker has recorded Popen success.
        if job_id is not None:
            conn.execute("UPDATE wake_jobs SET status='failed',detail=?,ended_at=? "
                         "WHERE id=? AND profile=? AND project=? AND status IN ('running','done')",
                         (reason[-2000:], db.now_iso(), job_id, profile, project))
        if action == 'retry':
            wake.enqueue(conn, profile, 'work', f'retry:task:{task_id}', project=project)
        conn.commit()
        payload = {'project': project, 'profile': profile, 'task_id': task_id,
                   'attempt': count, 'error': reason[-2000:], 'blocked': action == 'block'}
        events.bus.publish('task.updated', payload)
        events.bus.publish('execution.failed', payload)
        if action == 'block':
            errors = [json.loads(r['detail'])['error'] for r in failures] + [reason]
            summary = '\n'.join(f"第 {i} 次：{error[-500:].replace('@', '＠')}"
                                for i, error in enumerate(errors, 1))
            targets = notification_targets(conn, project, profile)
            mentions = ' '.join('@' + target for target in targets)
            report = router.route_message(conn, 'engine',
                f"{mentions} {profile} 执行任务 #{task_id} 已连续失败 {count}/{MAX_ATTEMPTS} 次，转为 technical blocked。\n{summary}",
                'engine', None, project=project, need_reply=None)
            events.bus.publish('message.new', {'message_id': report['message_id'], 'sender': 'engine',
                                               'thread_id': None, 'project': project})
        return {'blocked': [task_id] if action == 'block' else [],
                'retry': [task_id] if action == 'retry' else []}
    finally:
        conn.close()
