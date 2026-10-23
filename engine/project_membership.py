"""Project-scoped membership removal with transactional handoff checks."""
import json

from fastapi import HTTPException

from . import db, project_workspaces as ws, router


def _preview(conn, project, profile):
    if db.is_builtin_user(profile):
        raise HTTPException(400, '用户身份保留在项目中')
    project_row = ws.project(conn, project)
    if project_row.get('removed'):
        raise HTTPException(409, '项目已移除')
    member = conn.execute('SELECT * FROM project_members WHERE project=? AND profile=?',
                          (project, profile)).fetchone()
    if member is None:
        raise HTTPException(404, '该成员已移出项目')
    blockers = []
    owner = conn.execute("SELECT value FROM settings WHERE project=? AND key='owner_profile'",
                         (project,)).fetchone()
    if owner and owner['value'] == profile:
        blockers.append({'kind': 'owner', 'title': '当前项目负责人：请先在基本信息中更换负责人'})
    for row in conn.execute(
            "SELECT id,title FROM tasks WHERE project=? AND status NOT IN ('done','cancelled') "
            "AND (next_bot=? OR claimed_by=?) ORDER BY id", (project, profile, profile)):
        blockers.append({'kind': 'task', 'id': row['id'], 'title': f"任务 #{row['id']}：{row['title']}"})
    for row in conn.execute(
            "SELECT id,title FROM requirements WHERE project=? AND status NOT IN ('done','cancelled') "
            "AND decompose_owner=? AND decompose_state='decomposing' ORDER BY id", (project, profile)):
        blockers.append({'kind': 'requirement', 'id': row['id'],
                         'title': f"正在拆解需求 #{row['id']}：{row['title']}"})
    for row in conn.execute("SELECT id,title FROM threads WHERE project=? AND creator=? AND status='open'",
                            (project, profile)):
        blockers.append({'kind': 'thread', 'id': row['id'],
                         'title': f"负责收口的话题 #{row['id']}：{row['title']}"})
    try:
        ws.ensure_idle(project, profile)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        blockers.append({'kind': 'runtime', 'title': f'请先停止本项目中的运行并核实退出：{exc.detail}'})
    if conn.execute('SELECT 1 FROM session_bindings WHERE project=? AND profile=? AND switching=1',
                    (project, profile)).fetchone():
        blockers.append({'kind': 'session', 'title': '会话正在切换，请等待切换完成后重试'})
    return {'project': project, 'profile': profile, 'can_remove': not blockers, 'blockers': blockers}


def preview(project, profile):
    conn = db.get_connection()
    try:
        return _preview(conn, project, profile)
    finally:
        conn.close()


def remove(project, profile, actor):
    from .session_adapter import _SWITCH_LOCK
    # Match process launch lock order, then serialize task handoffs and queue claims.
    with ws.LOCK, _SWITCH_LOCK:
        conn = db.get_connection()
        try:
            conn.execute('BEGIN IMMEDIATE')
            result = _preview(conn, project, profile)
            if result['blockers']:
                raise HTTPException(409, {**result, 'message': '请先处理成员的职责或运行状态，再移出项目'})
            conn.execute('DELETE FROM project_members WHERE project=? AND profile=?', (project, profile))
            conn.execute('DELETE FROM project_permissions WHERE project=? AND profile=?', (project, profile))
            for row in conn.execute('SELECT id,members FROM threads WHERE project=?', (project,)).fetchall():
                members = json.loads(row['members'])
                if profile in members:
                    conn.execute('UPDATE threads SET members=? WHERE id=?',
                                 (json.dumps([p for p in members if p != profile], ensure_ascii=False), row['id']))
            expired = conn.execute(
                "UPDATE inbox SET status='expired' WHERE profile=? AND status='pending' AND "
                'message_id IN (SELECT id FROM messages WHERE project=?)', (profile, project)).rowcount
            cancelled = conn.execute(
                "UPDATE wake_jobs SET status='killed',detail='member removed from project',ended_at=? "
                "WHERE project=? AND profile=? AND status='queued'", (db.now_iso(), project, profile)).rowcount
            conn.execute('UPDATE wake_jobs SET chat_context_until=NULL WHERE project=? AND profile=?',
                         (project, profile))
            advanced = router.advance_past_profile(conn, profile, project, commit=False)
            ws.audit(conn, project, actor, 'member.remove', {'profile': profile,
                     'expired_inbox': expired, 'cancelled_jobs': cancelled, 'advanced_topics': advanced})
            conn.commit()
            return {'project': project, 'profile': profile, 'removed': True,
                    'expired_inbox': expired, 'cancelled_jobs': cancelled, 'advanced_topics': advanced}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
