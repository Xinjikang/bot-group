"""Project team mutations shared by HTTP and atomic import; caller owns transaction."""
from fastapi import HTTPException
from . import db
from . import project_workspaces as ws


def save_owner_role(conn, project, role_id):
    conn.execute("INSERT INTO settings(key,project,value,updated_at) VALUES('owner_role_id',?,?,?) "
                 "ON CONFLICT(key,project) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                 (project, str(role_id), db.now_iso()))


def owner_role_id(conn, project):
    row = conn.execute("SELECT pr.id FROM project_roles pr JOIN settings s ON s.project=pr.project "
                       "AND s.key='owner_role_id' AND s.value=CAST(pr.id AS TEXT) WHERE pr.project=?", (project,)).fetchone()
    return row['id'] if row else None


def mark_owner_role(conn, project, roles):
    owner_id = owner_role_id(conn, project)
    for role in roles:
        role['is_owner'] = role['id'] == owner_id if owner_id is not None else role['name'] == '项目负责人'


def create_role(conn, project, name, actor):
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100 or any(ord(c) < 32 for c in name):
        raise HTTPException(400, 'name 必须是长度不超过 100 的单行文本')
    name = name.strip()
    if not conn.execute('SELECT 1 FROM projects WHERE name=?', (project,)).fetchone():
        raise HTTPException(404, '项目不存在')
    if conn.execute('SELECT 1 FROM project_roles WHERE project=? AND name=?', (project, name)).fetchone():
        raise HTTPException(409, '该项目已存在同名角色')
    cur = conn.execute('INSERT INTO project_roles(project,name,created_at) VALUES(?,?,?)',
                       (project, name, db.now_iso()))
    result = {'id': cur.lastrowid, 'project': project, 'name': name}
    ws.audit(conn, project, actor, 'role.create', result)
    return result


def assign_role(conn, project, profile, role, actor):
    if not isinstance(role, str) or len(role) > 200 or any(c in role for c in '\r\n\0'):
        raise HTTPException(400, 'role 必须是长度不超过 200 的单行文本，可留空')
    role = role.strip()
    row = conn.execute('SELECT role,role_id FROM project_members WHERE project=? AND profile=?',
                       (project, profile)).fetchone()
    if not row or db.is_builtin_user(profile):
        raise HTTPException(404, '本项目中未找到该 Bot 成员')
    target = conn.execute('SELECT id FROM project_roles WHERE project=? AND name=?',
                          (project, role)).fetchone() if role else None
    role_id = target['id'] if target else None
    conn.execute('UPDATE project_members SET role=?,role_id=? WHERE project=? AND profile=?',
                 (role, role_id, project, profile))
    ws.audit(conn, project, actor, 'member.role', {'profile': profile, 'before': row['role'],
          'after': role, 'before_role_id': row['role_id'], 'after_role_id': role_id})
    return {'project': project, 'profile': profile, 'role': role, 'role_id': role_id}
