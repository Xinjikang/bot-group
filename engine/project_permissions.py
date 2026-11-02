"""Explicit project grants. Profile names carry identity, never implicit authority.

req#96 task#164：授权对象从「只能选 bot」扩展为「可勾角色 + 可勾 bot」多选，
判定取并集（bot 直接授权 ∪ 角色授权，任一命中即放行）。给角色授权 = 给该角色下
所有 active 成员 bot 授权。

角色授权存储：复用 project_permissions 表（架构师口径：绝不建第二张表），
profile 列以 `role:` + project_roles.id 前缀区分——role_id 是稳定主键，改名后
授权不断（需求第四点），删除角色由 api 删除端点级联清理对应授权行。
"""
import hashlib
import json

from fastapi import HTTPException
from . import db

PERMISSIONS = {
    'req.create': '创建需求',
    'req.update': '更新需求',
    'req.resume': '恢复需求',
    'req.complete': '关闭需求',
    'req.cancel': '撤回需求',
    'task.create': '创建任务',
    'task.update': '更新任务',
    'task.resume': '恢复任务',
    'task.complete': '关闭任务',
    'task.cancel': '撤回任务',
}

# 角色授权在 project_permissions.profile 列的前缀（req#96 task#164）。
ROLE_GRANT_PREFIX = 'role:'


def _is_role_grant(target) -> bool:
    return isinstance(target, str) and target.startswith(ROLE_GRANT_PREFIX)


def _role_id_from_grant(target):
    """'role:3' -> 3；非法/非数字返回 None。"""
    raw = target[len(ROLE_GRANT_PREFIX):]
    return int(raw) if raw.isdigit() else None


def _role_grant(role_id) -> str:
    return f'{ROLE_GRANT_PREFIX}{role_id}'


def _member_ok(conn, project, profile) -> bool:
    """目标 profile 是否为该项目的已启用、非内置 user 成员。"""
    return conn.execute(
        'SELECT 1 FROM members m '
        'JOIN project_members pm ON pm.project=? AND pm.profile=m.profile '
        'WHERE m.profile=? AND m.profile<>? AND COALESCE(pm.active,0)=1',
        (project, profile, db.BUILTIN_USER_PROFILE)).fetchone() is not None


def _denied(project, permission) -> dict:
    return {'error': f'缺少项目权限：{PERMISSIONS[permission]}（{project}）。'
                     '请联系用户在项目权限中授权。',
            'code': 'permission_denied'}


def check(conn, project, profile, permission):
    """Caller must be a registered, enabled project member AND hold the grant
    either directly (bot) or via role membership (union)."""
    if permission not in PERMISSIONS:
        return {'error': 'Unknown project permission', 'code': 'permission_denied'}
    if not _member_ok(conn, project, profile):
        return _denied(project, permission)
    # 直接授权（bot）
    if conn.execute('SELECT 1 FROM project_permissions WHERE project=? AND profile=? AND permission=?',
                    (project, profile, permission)).fetchone():
        return None
    # 角色授权（成员 role_id 对应角色被授该权限）
    row = conn.execute('SELECT role_id FROM project_members WHERE project=? AND profile=?',
                       (project, profile)).fetchone()
    if row and row['role_id'] is not None:
        if conn.execute('SELECT 1 FROM project_permissions WHERE project=? AND profile=? AND permission=?',
                        (project, _role_grant(row['role_id']), permission)).fetchone():
            return None
    return _denied(project, permission)


def _snapshot(conn, project):
    if not conn.execute('SELECT 1 FROM projects WHERE name=?', (project,)).fetchone():
        raise HTTPException(404, '项目不存在')
    grants = {key: [] for key in PERMISSIONS}
    # bot 直接授权
    for row in conn.execute(
            'SELECT g.permission,g.profile FROM project_permissions g '
            'JOIN project_members pm ON pm.project=g.project AND pm.profile=g.profile '
            'JOIN members m ON m.profile=g.profile '
            "WHERE g.project=? AND pm.active=1 AND m.profile<>? AND g.profile NOT LIKE ?",
            (project, db.BUILTIN_USER_PROFILE, ROLE_GRANT_PREFIX + '%')):
        if row['permission'] in grants:
            grants[row['permission']].append(row['profile'])
    # 角色授权（防御性：仅保留 role_id 仍存在的角色，残留孤儿行不暴露）
    valid_role_ids = {str(r['id']) for r in conn.execute(
        'SELECT id FROM project_roles WHERE project=?', (project,))}
    for row in conn.execute(
            'SELECT g.permission,g.profile FROM project_permissions g '
            "WHERE g.project=? AND g.profile LIKE ?",
            (project, ROLE_GRANT_PREFIX + '%')):
        if row['profile'][len(ROLE_GRANT_PREFIX):] in valid_role_ids and row['permission'] in grants:
            grants[row['permission']].append(row['profile'])
    # 统一字典序，保证 save 里 before['grants'] 与 after 排序口径一致（避免误判变更）
    grants = {k: sorted(v) for k, v in grants.items()}
    members = [dict(r) for r in conn.execute(
        'SELECT m.profile,m.display_name,b.profile AS host_profile,COALESCE(pm.active,0) AS active '
        'FROM members m '
        'LEFT JOIN bots b ON b.id=m.bot_id '
        'JOIN project_members pm ON pm.profile=m.profile AND pm.project=? '
        "WHERE m.profile<>? AND pm.active=1 ORDER BY m.profile",
        (project, db.BUILTIN_USER_PROFILE))]
    roles = [dict(r) for r in conn.execute(
        'SELECT pr.id, pr.project, pr.name, pr.created_at, '
        '(SELECT COUNT(*) FROM project_members pm WHERE pm.project=pr.project '
        ' AND pm.role_id=pr.id) AS member_count '
        'FROM project_roles pr WHERE pr.project=? ORDER BY pr.id',
        (project,))]
    from .project_team import mark_owner_role
    mark_owner_role(conn, project, roles)
    revision = hashlib.sha256(json.dumps(
        {'grants': grants, 'members': members, 'roles': roles}, sort_keys=True).encode()).hexdigest()
    return {'project': project,
            'permissions': [{'key': k, 'label': v} for k, v in PERMISSIONS.items()],
            'members': members, 'roles': roles, 'grants': grants, 'revision': revision}


def view(project):
    conn = db.get_connection()
    try:
        return _snapshot(conn, project)
    finally:
        conn.close()


def save_in_transaction(conn, project, body, actor):
    """Validate revision, members and roles, write grants/audit; caller commits."""
    grants = body.get('grants')
    if not isinstance(grants, dict) or set(grants) != set(PERMISSIONS):
        raise HTTPException(400, '请提交完整的项目权限列表')
    if any(not isinstance(v, list) or any(not isinstance(p, str) for p in v) for v in grants.values()):
        raise HTTPException(400, '每项权限的授权对象必须为字符串数组（bot profile 或 role:<id>）')
    before = _snapshot(conn, project)
    if body.get('revision') != before['revision']:
        raise HTTPException(409, '权限已被修改，请重新加载后保存')
    members = {m['profile'] for m in before['members']}
    valid_role_ids = {str(r['id']) for r in before['roles']}
    for targets in grants.values():
        for target in targets:
            if _is_role_grant(target):
                rid = _role_id_from_grant(target)
                if rid is None or str(rid) not in valid_role_ids:
                    raise HTTPException(400, '授权角色不存在')
            elif target not in members:
                raise HTTPException(400, '授权成员不存在')
    after = {k: sorted(set(v)) for k, v in grants.items()}
    if after != before['grants']:
        revoked = {target for permission, targets in before['grants'].items()
                   for target in targets if target not in after[permission]}
        for target in revoked:
            if _is_role_grant(target):
                conn.execute('UPDATE hermes_session_grants SET revoked=1 WHERE project=? AND profile IN '
                             '(SELECT profile FROM project_members WHERE project=? AND role_id=?)',
                             (project, project, _role_id_from_grant(target)))
            else:
                conn.execute('UPDATE hermes_session_grants SET revoked=1 WHERE project=? AND profile=?',
                             (project, target))
        conn.execute('DELETE FROM project_permissions WHERE project=?', (project,))
        conn.executemany('INSERT INTO project_permissions(project,profile,permission) VALUES(?,?,?)',
                         [(project, p, k) for k, targets in after.items() for p in targets])
        from .project_workspaces import audit
        audit(conn, project, actor, 'permissions.update', {'before': before['grants'], 'after': after})
    return _snapshot(conn, project)


def save(project, body, actor):
    conn = db.get_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        result = save_in_transaction(conn, project, body, actor)
        conn.commit()
        return result
    finally:
        conn.close()


def granted_profiles(conn, project, permission) -> list:
    """持有某权限的全部 active 成员 profile（直接授权 ∪ 角色授权展开），有序去重。

    供 scanner 枚举「该唤醒谁」使用——角色授权需展开为该角色下所有 active 成员，
    否则角色授 task.create 不会被需求扫描唤醒（check 只判定，不枚举成员）。
    """
    profiles = set()
    for row in conn.execute(
            'SELECT profile FROM project_permissions WHERE project=? AND permission=?',
            (project, permission)):
        target = row['profile']
        if _is_role_grant(target):
            rid = _role_id_from_grant(target)
            if rid is None:
                continue
            for m in conn.execute(
                    'SELECT pm.profile FROM project_members pm '
                    'JOIN members mm ON mm.profile=pm.profile '
                    'WHERE pm.project=? AND pm.role_id=? AND COALESCE(pm.active,0)=1 '
                    'AND mm.profile<>?',
                    (project, rid, db.BUILTIN_USER_PROFILE)):
                profiles.add(m['profile'])
        else:
            profiles.add(target)
    return sorted(profiles)
