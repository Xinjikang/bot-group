"""Shared eligibility and recipients for automatic requirement recovery."""

from . import db, project_permissions, requirement_gate


NO_CANDIDATE_REASON = (
    "无 task.create 授权成员：请在项目设置中为至少一名有效成员授予 task.create 权限"
    "（或为该成员配置持有该权限的角色）后再拆解。"
)


def candidates(conn, project=None):
    """Read pending/expired requirements whose child tasks allow recovery."""
    project_filter = " AND r.project=?" if project is not None else ""
    params = [db.now_iso()]
    if project is not None:
        params.append(project)
    return conn.execute(
        "SELECT r.* FROM requirements r JOIN projects p ON p.name=r.project "
        "WHERE p.archived=0 AND p.readiness_status='ready' "
        "AND r.status='open' AND r.archived=0 "
        "AND (r.decompose_state='pending' OR (r.decompose_state='decomposing' "
        "AND (r.decompose_expires_at='' OR r.decompose_expires_at<?))) "
        "AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.requirement_id=r.id AND t.status='blocked')"
        + project_filter + " ORDER BY r.priority,r.id", params,
    ).fetchall()


def pick_candidate(conn, project):
    """Choose one enabled, authorized member in stable role/profile order."""
    granted = set(project_permissions.granted_profiles(conn, project, "task.create"))
    for row in conn.execute(
        "SELECT pm.profile FROM project_members pm JOIN members m ON m.profile=pm.profile "
        "WHERE pm.project=? AND pm.active=1 AND m.profile<>? "
        "ORDER BY COALESCE(pm.role_id,0),pm.profile", (project, db.BUILTIN_USER_PROFILE),
    ):
        if row['profile'] in granted:
            return row['profile']
    return None


def coordination_owner(conn, project):
    """Resolve an enabled Bot owner for missing-grant coordination."""
    row = conn.execute(
        "SELECT s.value FROM settings s JOIN project_members pm "
        "ON pm.project=s.project AND pm.profile=s.value "
        "JOIN members m ON m.profile=pm.profile "
        "WHERE s.project=? AND s.key='owner_profile' AND pm.active=1 AND m.profile<>?",
        (project, db.BUILTIN_USER_PROFILE),
    ).fetchone()
    return row['value'] if row else None


def context(conn, profile, project):
    """Return actionable rows and whether this member must coordinate grants."""
    coordinate = False
    if project_permissions.check(conn, project, profile, 'task.create'):
        if pick_candidate(conn, project) is not None or coordination_owner(conn, project) != profile:
            return [], False
        coordinate = True
    rows = [row for row in candidates(conn, project) if not requirement_gate.check(conn, row)]
    return rows, coordinate
