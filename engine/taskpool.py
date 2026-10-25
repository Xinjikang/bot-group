"""单库任务池逻辑（方案B）：操作 group.db，带 project 过滤。

群聊与任务共用 group.db，各项目共用业务表并以 project 字段隔离。
所有函数签名带 project 参数，SQL 全部 WHERE project=? 过滤。

三层：
    requirement（需求）
      └── task（开发任务）
            └── node（流程节点日志）

Claim 语义（pull 模型）：
  * bot 只 claim next_bot 匹配自己的任务
  * 只派发前置任务全部 done 的任务；可领任务按 (requirement id, task id) 稳定排序
  * 一个 bot 同时最多持有一个 in_progress 任务
  * claim 原子：UPDATE ... WHERE status='pending' + rowcount 检查
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path

from . import db
from .task_access import begin_write

# 状态常量（与旧 taskqueue.db 一致）
PENDING = "pending"
IN_PROGRESS = "in_progress"
BLOCKED = "blocked"
DONE = "done"
CANCELLED = "cancelled"
REQ_OPEN = "open"
REQ_DONE = "done"
REQ_BLOCKED = "blocked"
REQ_CANCELLED = "cancelled"

# req#117 task#198：需求拆解状态机。
DECOMPOSE_PENDING = "pending"
DECOMPOSE_DECOMPOSING = "decomposing"
DECOMPOSE_DONE = "done"
DECOMPOSE_TTL_SECONDS = 30 * 60  # 占用有效期 30 分钟，每次成功写任务续期


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def _conn() -> sqlite3.Connection:
    c = db.get_connection()
    db.init_schema(c)
    return c


def _log_event(conn, actor, event_type, project, requirement_id=None,
               task_id=None, node_id=None, detail=""):
    conn.execute(
        "INSERT INTO events (project, requirement_id, task_id, node_id, ts, actor, event_type, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (project, requirement_id, task_id, node_id, db.now_iso(), actor, event_type, detail),
    )


def _set_requirement_updated(conn, requirement_id):
    conn.execute(
        "UPDATE requirements SET updated_at = ? WHERE id = ?",
        (db.now_iso(), requirement_id),
    )


# --------------------------------------------------------------------------
# 项目注册（方案B：projects 表替代 projects.yaml）
# --------------------------------------------------------------------------

def _save_project_owner(conn, project, owner):
    conn.execute("INSERT INTO settings(key,project,value,updated_at) VALUES('owner_profile',?,?,?) "
                 "ON CONFLICT(key,project) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                 (project, owner, db.now_iso()))


def project_add(name: str, root: str, description: str | None = None, owner_profile=None,
                display_name=None, directory_mode='shared', worktree_root='', actor='system') -> dict:
    conn = _conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        result = project_add_in_transaction(conn, name, root, description, owner_profile,
                                            display_name, directory_mode, worktree_root, actor)
        if 'error' not in result:
            conn.commit()
        return result
    finally:
        conn.close()


def project_add_in_transaction(conn, name, root, description=None, owner_profile=None,
                               display_name=None, directory_mode='shared', worktree_root='', actor='system'):
    """Pure registration on the caller's connection; no schema init or commit."""
    name = (name or "").strip()
    if not name and display_name and display_name.strip():
        from uuid import uuid4
        name = 'p_' + uuid4().hex
    root = (root or "").strip()
    if not name:
        return {"error": "project name is required"}
    if not root:
        return {"error": "root (project path) is required"}
    if directory_mode not in ('shared', 'worktree'):
        return {'error': 'directory_mode must be shared or worktree'}
    if conn.execute('SELECT 1 FROM projects WHERE name=?', (name,)).fetchone():
        return {'error': 'project ID already exists; update its settings instead'}
    if owner_profile is not None:
        if not isinstance(owner_profile, str):
            return {'error': 'owner_profile must be a profile string'}
        owner_profile = owner_profile.strip()
        if owner_profile and not conn.execute(
            "SELECT 1 FROM members WHERE profile=? AND profile<>'user'", (owner_profile,)
        ).fetchone():
            return {'error': '项目负责人必须是已注册的 bot'}
    conn.execute(
        "INSERT INTO projects (name, root, description, archived, created_at) VALUES (?, ?, ?, 0, ?)",
        (name, root, (description or "").strip(), db.now_iso()),
    )
    if owner_profile is not None:
        _save_project_owner(conn, name, owner_profile)
    conn.execute('UPDATE projects SET display_name=?,directory_mode=?,worktree_root=? WHERE name=?',
                 ((display_name or name).strip(), directory_mode, worktree_root, name))
    from .project_workspaces import audit
    audit(conn, name, actor, 'project.create', {'root': root, 'display_name': display_name or name,
          'directory_mode': directory_mode, 'worktree_root': worktree_root, 'owner_profile': owner_profile})
    return {"project": name, "root": root}


def project_remove(name: str) -> dict:
    from . import project_removal
    from fastapi import HTTPException
    try:
        project_removal.change((name or '').strip(), True)
        return {"removed": name}
    except HTTPException as exc:
        return {'error': exc.detail}


def project_list(include_archived: bool = False) -> dict:
    """项目列表。默认只返回活跃项目（需求2c：归档不在 UI 展示——chip/切换器用默认）。
    include_archived=True 返回全部，供项目管理弹窗展示归档项目及恢复入口。"""
    conn = _conn()
    try:
        where = "WHERE removed=0" if include_archived else "WHERE archived=0 AND removed=0"
        rows = conn.execute(
            f"SELECT *, COALESCE((SELECT value FROM settings WHERE key='owner_profile' "
            f"AND project=projects.name),'') AS owner_profile FROM projects {where} ORDER BY name"
        ).fetchall()
        return {"projects": [{**_row_to_dict(r), 'id': r['name']} for r in rows]}
    finally:
        conn.close()


def project_archive(name: str, archived: bool = True) -> dict:
    from .project_workspaces import LOCK
    with LOCK:
        return _project_archive(name, archived)


def _project_archive(name: str, archived: bool) -> dict:
    name = (name or "").strip()
    conn = _conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM projects WHERE name=?', (name,)).fetchone()
        if row and row['removed']:
            return {'error': '项目已移除，重新使用该目录请导入新项目'}
        if row and not archived:
            from .project_removal import check_directory
            from fastapi import HTTPException
            try:
                check_directory(conn, row)
            except HTTPException as exc:
                return {'error': exc.detail}
        if archived:
            tasks = [dict(r) for r in conn.execute(
                "SELECT id,status FROM tasks WHERE project=? AND status IN ('pending','in_progress','blocked') ORDER BY id", (name,))]
            jobs = [dict(r) for r in conn.execute(
                "SELECT id,profile,status FROM wake_jobs WHERE project=? AND status IN ('queued','running') ORDER BY id", (name,))]
            if tasks or jobs:
                labels = {'pending': '待处理', 'in_progress': '执行中', 'blocked': '阻塞待处理',
                          'queued': '等待唤醒', 'running': '正在唤醒'}
                return {'error': '项目还有未结束的工作，请先处理后再归档。'
                        + (' 任务：' + '、'.join(f"#{t['id']}（{labels[t['status']]}）" for t in tasks) if tasks else '')
                        + (' 唤醒：' + '、'.join(f"#{j['id']} {j['profile']}（{labels[j['status']]}）" for j in jobs) if jobs else ''),
                        'tasks': tasks, 'wake_jobs': jobs}
            from .project_workspaces import ensure_idle
            from fastapi import HTTPException
            try:
                ensure_idle(name)
            except HTTPException as exc:
                return {'error': f'请先处理项目中尚未结束的 bot，再归档。{exc.detail}'}
        cur = conn.execute(
            "UPDATE projects SET archived=? WHERE name=?",
            (1 if archived else 0, name),
        )
        conn.commit()
        if cur.rowcount == 0:
            return {"error": f"unknown project '{name}'"}
        result = {"project": name, "archived": bool(archived)}
        if archived:
            from . import browser_resources
            result['browser_cleanup'] = browser_resources.release(project=name)
        return result
    finally:
        conn.close()


# group.db 内带 project 列、改名需级联的表（顺序无关，都是 UPDATE project 列）
_PROJECT_COLUMN_TABLES = [
    "threads", "messages", "wake_jobs", "attachments",
    "requirements", "tasks", "nodes", "events", "settings",
    "browser_resources",
]


def _hermes_home() -> Path:
    """hermes 根目录（state.db 所在）。引擎环境 HERMES_HOME 已设。"""
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent.parent


def _profile_state_db(profile: str) -> Path:
    """该 profile 的 state.db 路径。default → <home>/state.db，其他 → <home>/profiles/<名>/state.db。"""
    home = _hermes_home()
    if profile == "default":
        return home / "state.db"
    return home / "profiles" / profile / "state.db"


def _sync_session_titles(conn: sqlite3.Connection, old_name: str, new_name: str) -> int:
    """改名级联第二维度：各 bot state.db 的 sessions.title 同步 replace。

    方案 B 会话名 {profile}-{project}-{type} 拼在 group.db 外面（state.db 的
    sessions.title）。改名后旧 title 成孤儿 → 断点续断裂（bot 失忆）。这里把
    title 里的「-{旧名}-」替换成「-{新名}-」，断点续延续、记忆不丢。

    返回改动的行数。state.db 不存在或 bot 无该会话时跳过（幂等）。
    """
    total = 0
    # 复用外层连接读 members（避免嵌套 init_schema 撞 DDL 锁）
    profiles = [r["profile"] for r in conn.execute("SELECT profile FROM members").fetchall()]
    # 补齐默认 profile 并去重。
    profiles = sorted(set(profiles) | {"default"})

    # SQL REPLACE 的 -旧名- needle 在项目名有包含关系时可能发生
    # 误伤（proj 改名会动 my-proj 的 title）。会话名结构 {profile}-{project}-{type}[-{tid}]，
    # project 固定在从左数第三段——LIKE 取候选后 Python 端按段精确匹配替换。
    for prof in profiles:
        dbp = _profile_state_db(prof)
        if not dbp.exists():
            continue
        try:
            c = sqlite3.connect(str(dbp), timeout=30)
            c.execute("PRAGMA busy_timeout=30000")
            rows = c.execute(
                "SELECT id, title FROM sessions WHERE title LIKE ?",
                (f"%-{old_name}-%",),
            ).fetchall()
            for sid, title in rows:
                # 🔴 二次修正（实测三连抓）：段位法防不住包含关系——
                # fe-dev-02-my-proj-work 的倒数第二段恰是 'proj'（my-proj 拆开后），
                # 从右往左照样误命中。唯一可靠锚定=profile 全名前缀枚举：
                # title 必须以 f"{profile}-{old_name}-" 开头（该 profile 的该项目会话），
                # 替换前缀为新名。包含关系（my-proj）的前缀是 fe-dev-02-my-proj-work
                # 不以 fe-dev-02-proj- 开头→不命中。
                for prof2 in profiles:
                    pref = f"{prof2}-{old_name}-"
                    if title.startswith(pref):
                        new_title = f"{prof2}-{new_name}-" + title[len(pref):]
                        c.execute("UPDATE sessions SET title=? WHERE id=?",
                                  (new_title, sid))
                        total += 1
                        break
            c.commit()
            c.close()
        except sqlite3.Error:
            # state.db 可能被 bot 进程占用（WAL 锁），单条失败不阻断整体改名
            continue
    return total


def project_update(name: str, new_name: str = "", root: str = "",
                   description: str = "", desc_present: bool = False, owner_profile=None,
                   display_name=None, directory_mode=None, worktree_root=None, actor='system') -> dict:
    """Update display/config fields; name remains the stable project ID, including for legacy callers."""
    name = (name or "").strip()
    new_name = (new_name or "").strip()
    root = (root or "").strip()
    description = (description or "").strip()

    conn = _conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
        if row is None:
            return {"error": f"unknown project '{name}'"}

        previous_owner = conn.execute("SELECT value FROM settings WHERE project=? AND key='owner_profile'", (name,)).fetchone()
        if owner_profile is not None:
            if not isinstance(owner_profile, str):
                return {'error': 'owner_profile must be a profile string'}
            owner_profile = owner_profile.strip()
            error = _validate_next_bot(conn, owner_profile, name) if owner_profile else None
            if error:
                return {'error': error}

        renamed = False
        cascaded = 0
        sessions_synced = 0

        # Legacy new_name edits the label; stable IDs and session names stay fixed.
        label = display_name if display_name is not None else new_name
        if label:
            conn.execute('UPDATE projects SET display_name=? WHERE name=?', (label.strip(), name))
        if directory_mode is not None:
            if directory_mode not in ('shared', 'worktree'):
                return {'error': 'directory_mode must be shared or worktree'}
            conn.execute('UPDATE projects SET directory_mode=? WHERE name=?', (directory_mode, name))
        if worktree_root is not None:
            conn.execute('UPDATE projects SET worktree_root=? WHERE name=?', (worktree_root, name))

        # 改路径 / 改描述（projects 表自身字段）
        if root:
            conn.execute("UPDATE projects SET root=? WHERE name=?", (root, name))
        # 区分「没传」（desc_present=False 跳过）和「传了空串」
        # （desc_present=True 更新=清空）。此前 if description: 把空串当"没传"，描述清不掉。
        if desc_present:
            conn.execute("UPDATE projects SET description=? WHERE name=?", (description, name))

        if owner_profile is not None:
            _save_project_owner(conn, name, owner_profile)

        from .project_workspaces import audit
        after = dict(conn.execute('SELECT * FROM projects WHERE name=?', (name,)).fetchone())
        audit(conn, name, actor, 'project.update', {'before': dict(row), 'after': after,
              'owner_before': previous_owner[0] if previous_owner else '',
              'owner_after': owner_profile if owner_profile is not None else (previous_owner[0] if previous_owner else '')})
        if any(row[field] != after[field] for field in ('root', 'directory_mode', 'worktree_root')):
            from .codex_project_access import commit_directory_access
            members = [r[0] for r in conn.execute('SELECT profile FROM project_members WHERE project=?', (name,))]
            commit_directory_access(conn, name, members)
        else:
            conn.commit()
        return {
            "project": name,
            "renamed": renamed,
            "display_name": label or row['display_name'],
            "root": root or row["root"],
            "description": after['description'],
            "cascaded": cascaded,
            "sessions_synced": sessions_synced,
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Requirements
# --------------------------------------------------------------------------

_REQUIREMENT_FIELDS = {
    "title": ("title", "标题：一句话说明做什么"),
    "background": ("background_goal", "背景与目标：填写现状问题、使用场景和预期结果"),
    "scope": ("scope", "范围：填写本次包含项和范围外事项"),
    "approach": ("approach_basis", "方案与依据：填写已确认方案、关键约束和设计依据"),
    "acceptance": ("acceptance", "验收标准：逐条填写可验证的正常行为和异常边界"),
    "undecided": ("undecided", "未决事项：无则填写“无”，有则逐条记录及解决状态"),
    "source_ref": ("source_ref", "来源关联：填写 thread_id 和 message_id（用户创建指令消息）"),
}


def _requirement_values(values):
    cleaned, errors = {}, []
    for field, value in values.items():
        column, hint = _REQUIREMENT_FIELDS[field]
        if not isinstance(value, str) or not value.strip():
            errors.append({"field": field, "message": f"{field} 必须是非空字符串；{hint}"})
        else:
            cleaned[column] = value.strip()
    if errors:
        return {}, {"error": "；".join(item["message"] for item in errors),
                    "code": "requirement_fields_invalid", "field_errors": errors}
    return cleaned, None


def req_create(project: str, role: str, title: str = "", version: str = "",
               description: str = "", priority: int = 0, *, background: str = "",
               scope: str = "", approach: str = "", acceptance: str = "",
               undecided: str = "", source_ref: str = "") -> dict:
    conn = _conn()
    try:
        from .project_permissions import check
        begin_write(conn)
        from . import project_readiness
        blocked = project_readiness.check(conn, project)
        if blocked:
            return blocked
        denied = check(conn, project, role, 'req.create')
        if denied:
            return denied
        values, invalid = _requirement_values(dict(title=title, background=background, scope=scope,
            approach=approach, acceptance=acceptance, undecided=undecided, source_ref=source_ref))
        if invalid:
            return invalid
        # req#111：入口 fail-fast——落库前即时校验门禁（undecided + source_ref 来源可追溯），
        # 不合规当场返回具体原因+格式示例，不再等 scanner 静默拦截、需求卡死。
        from . import requirement_gate
        candidate = {
            'id': 0, 'project': project, 'description': description,
            'background_goal': values['background_goal'], 'scope': values['scope'],
            'approach_basis': values['approach_basis'], 'acceptance': values['acceptance'],
            'undecided': values['undecided'], 'source_ref': values['source_ref'],
        }
        blocked = requirement_gate.check(conn, candidate)
        if blocked:
            return blocked
        # project_root 从 projects 表取
        root = conn.execute(
            "SELECT root FROM projects WHERE name=?", (project,)
        ).fetchone()
        project_root = root["root"] if root else ""
        ts = db.now_iso()
        cur = conn.execute(
            "INSERT INTO requirements (project, title, version, description, priority, status, project_root, created_at, updated_at, "
            "background_goal, scope, approach_basis, acceptance, undecided, source_ref) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project, values['title'], version, description, priority, REQ_OPEN, project_root, ts, ts,
             values['background_goal'], values['scope'], values['approach_basis'], values['acceptance'],
             values['undecided'], values['source_ref']),
        )
        rid = cur.lastrowid
        _log_event(conn, role, "dispatch", project, requirement_id=rid, detail=values['title'])
        conn.commit()
        return {"requirement_id": rid, **values, "priority": priority,
                "project": project, "project_root": project_root}
    finally:
        conn.close()


def req_update(project: str, role: str, requirement_id: int, *, title: str | None = None,
               background: str | None = None, scope: str | None = None,
               approach: str | None = None, acceptance: str | None = None,
               undecided: str | None = None, source_ref: str | None = None) -> dict:
    """Update supplied fields atomically; omitted fields preserve historical data."""
    conn = _conn()
    try:
        from .project_permissions import check
        begin_write(conn)
        denied = check(conn, project, role, 'req.update')
        if denied:
            return denied
        if not conn.execute('SELECT id FROM requirements WHERE id=? AND project=?',
                            (requirement_id, project)).fetchone():
            return {"error": f"requirement {requirement_id} not found"}
        supplied = dict(title=title, background=background, scope=scope, approach=approach,
                        acceptance=acceptance, undecided=undecided, source_ref=source_ref)
        values, invalid = _requirement_values({k: v for k, v in supplied.items() if v is not None})
        if invalid:
            return invalid
        if not values:
            return {"error": "请至少提供一个需求字段进行更新", "code": "requirement_fields_invalid"}
        assignments = ', '.join(f'{field}=?' for field in values)
        conn.execute(f'UPDATE requirements SET {assignments}, updated_at=? WHERE id=? AND project=?',
                     (*values.values(), db.now_iso(), requirement_id, project))
        _log_event(conn, role, 'req_update', project, requirement_id=requirement_id,
                   detail=json.dumps(values, ensure_ascii=False))
        conn.commit()
        return {"updated": True, "requirement_id": requirement_id, "project": project, **values}
    finally:
        conn.close()


def req_list(project: str, status: str = "") -> dict:
    conn = _conn()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM requirements WHERE project=? AND status=? ORDER BY priority ASC, id ASC",
                (project, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM requirements WHERE project=? ORDER BY priority ASC, id ASC",
                (project,),
            ).fetchall()
        return {"requirements": [_row_to_dict(r) for r in rows], "count": len(rows)}
    finally:
        conn.close()


def req_status(project: str, requirement_id: int) -> dict:
    conn = _conn()
    try:
        req = conn.execute(
            "SELECT * FROM requirements WHERE id=? AND project=?", (requirement_id, project)
        ).fetchone()
        if req is None:
            return {"error": f"requirement {requirement_id} not found"}
        tasks = conn.execute(
            "SELECT * FROM tasks WHERE requirement_id=? AND project=? ORDER BY priority ASC, id ASC",
            (requirement_id, project),
        ).fetchall()
        task_list = []
        for t in tasks:
            nodes = conn.execute(
                "SELECT * FROM nodes WHERE task_id=? ORDER BY ts ASC, id ASC", (t["id"],)
            ).fetchall()
            task_list.append({
                "task": _row_to_dict(t),
                "nodes": [_row_to_dict(n) for n in nodes],
            })
        return {"requirement": _row_to_dict(req), "tasks": task_list}
    finally:
        conn.close()


def req_complete(project: str, role: str, requirement_id: int) -> dict:
    return _complete_requirement(project, role, requirement_id, require_grant=True)


def admin_req_complete(project: str, requirement_id: int) -> dict:
    """Close a requirement as the human; admin identity is enforced by the API."""
    return _complete_requirement(project, 'user', requirement_id, require_grant=False)


def _complete_requirement(project, role, requirement_id, *, require_grant):
    conn = _conn()
    try:
        from .project_permissions import check
        begin_write(conn)
        if require_grant:
            denied = check(conn, project, role, 'req.complete')
            if denied:
                return denied
        req = conn.execute(
            "SELECT * FROM requirements WHERE id=? AND project=?", (requirement_id, project)
        ).fetchone()
        if req is None:
            return {"error": f"requirement {requirement_id} not found"}
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE requirement_id=? AND project=? "
            "AND status NOT IN (?, ?)",
            (requirement_id, project, DONE, CANCELLED),
        ).fetchone()
        if remaining["c"] > 0:
            return {"error": f"requirement {requirement_id} still has {remaining['c']} unfinished task(s)"}
        ts = db.now_iso()
        conn.execute(
            "UPDATE requirements SET status=?, updated_at=? WHERE id=?",
            (REQ_DONE, ts, requirement_id),
        )
        _log_event(conn, role, "req_complete", project, requirement_id=requirement_id)
        conn.commit()
        return {"completed": True, "requirement_id": requirement_id,
                "message": f"Requirement {requirement_id} closed."}
    finally:
        conn.close()


def req_cancel(project: str, role: str, requirement_id: int, reason: str = "") -> dict:
    """撤回需求并持久化取消状态。

    语义：需求不做了。把该需求下所有未完成 task 标 cancelled（清 next_bot/
    claimed_by，堵扫描器复活），requirement 标 cancelled。与 req_complete 的
    「做完了才关」区分开——撤回是「没做，不做了」。

    撤回必须同步需求和任务状态，避免扫描器继续派发仍处于 open/pending 的记录。
    """
    reason = (reason or "").strip()
    conn = _conn()
    try:
        from .project_permissions import check
        begin_write(conn)
        denied = check(conn, project, role, 'req.cancel')
        if denied:
            return denied
        req = conn.execute(
            "SELECT * FROM requirements WHERE id=? AND project=?", (requirement_id, project)
        ).fetchone()
        if req is None:
            return {"error": f"requirement {requirement_id} not found"}
        ts = db.now_iso()
        # 该需求下所有未完成 task 标 cancelled（清 next_bot/claimed_by，堵扫描器）
        from . import browser_resources
        task_ids = [r['id'] for r in conn.execute(
            'SELECT id FROM tasks WHERE requirement_id=? AND status NOT IN (?,?)',
            (requirement_id, DONE, CANCELLED))]
        for task_id in task_ids:
            browser_resources.mark_task(conn, project, task_id)
        cur = conn.execute(
            "UPDATE tasks SET status='cancelled', next_bot='', claimed_by='' "
            "WHERE requirement_id=? AND status != ?",
            (requirement_id, DONE),
        )
        cancelled_tasks = cur.rowcount
        conn.execute(
            "UPDATE requirements SET status=?, updated_at=? WHERE id=?",
            (REQ_CANCELLED, ts, requirement_id),
        )
        _log_event(conn, role, "req_cancel", project, requirement_id=requirement_id,
                   detail=reason or "cancelled")
        conn.commit()
        cleanup = [browser_resources.release(project=project, task_id=tid) for tid in task_ids]
        return {"cancelled": True, "requirement_id": requirement_id,
                "cancelled_tasks": cancelled_tasks,
                "browser_cleanup": cleanup,
                "message": f"Requirement {requirement_id} cancelled ({cancelled_tasks} task(s) cancelled)."}
    finally:
        conn.close()


def req_resume(project: str, role: str, requirement_id: int) -> dict:
    """Reopen a blocked requirement with req.resume; child tasks retain their states."""
    return _resume_requirement(project, requirement_id, role, require_grant=True)


def admin_req_resume(project: str, requirement_id: int) -> dict:
    """Human recovery; the API enforces administrator identity."""
    return _resume_requirement(project, requirement_id, 'user', require_grant=False)


def _resume_requirement(project, requirement_id, actor, *, require_grant):
    conn = _conn()
    try:
        begin_write(conn)
        if require_grant:
            from .project_permissions import check
            denied = check(conn, project, actor, 'req.resume')
            if denied:
                return denied
        req = conn.execute('SELECT * FROM requirements WHERE id=? AND project=?',
                           (requirement_id, project)).fetchone()
        if req is None:
            return {'error': f'requirement {requirement_id} not found'}
        if req['status'] != REQ_BLOCKED:
            return {'error': f"requirement {requirement_id} is {req['status']}, only blocked requirements can be resumed"}
        conn.execute('UPDATE requirements SET status=?,updated_at=? WHERE id=? AND project=?',
                     (REQ_OPEN, db.now_iso(), requirement_id, project))
        _log_event(conn, actor, 'req_resume', project, requirement_id=requirement_id)
        conn.commit()
        return {'resumed': True, 'requirement_id': requirement_id, 'status': REQ_OPEN}
    finally:
        conn.close()


def req_archive(project: str, requirement_id: int, archived: bool = True) -> dict:
    """req#76：归档/取消归档整个需求（仿 projects.archived，只允许 done/cancelled 归档）。

    归档 = 把整个需求从任务面板历史里收起（archived=1）；取消归档 = 恢复展示（archived=0）。
    只允许对 status ∈ {done, cancelled} 的需求归档——进行中的需求（open/blocked）不能归档，
    防止把还在流转的活藏起来。归档是整个需求级动作，不针对单个 task。
    """
    conn = _conn()
    try:
        req = conn.execute(
            "SELECT * FROM requirements WHERE id=? AND project=?", (requirement_id, project)
        ).fetchone()
        if req is None:
            return {"error": f"requirement {requirement_id} not found"}
        if archived and req["status"] not in (REQ_DONE, REQ_CANCELLED):
            return {"error": f"requirement {requirement_id} is '{req['status']}', only done/cancelled can be archived"}
        ts = db.now_iso()
        conn.execute(
            "UPDATE requirements SET archived=?, updated_at=? WHERE id=?",
            (1 if archived else 0, ts, requirement_id),
        )
        _log_event(conn, "user", "req_archive" if archived else "req_unarchive",
                   project, requirement_id=requirement_id)
        conn.commit()
        return {"requirement_id": requirement_id, "archived": bool(archived)}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------

def _validate_next_bot(conn, next_bot: str, project: str = "") -> str | None:
    """校验 next_bot 是当前项目启用的具体 Bot 成员标识。

    Bot 查询花名册的项目角色后选定接手人；领取和派发均精确匹配成员标识。

    合法值 = members 表里已注册、非 user，且在该项目下启用的 profile 名
    启停按项目维度；成员必须在 project_members 中显式启用。
    """
    from .router import _profile_active

    row = conn.execute(
        "SELECT 1 FROM members WHERE profile=? AND profile!='user'",
        (next_bot,),
    ).fetchone()
    if row is not None and _profile_active(conn, project, next_bot):
        return None
    valid = [
        r["profile"]
        for r in conn.execute("SELECT profile FROM members WHERE profile!='user' ORDER BY profile")
        if _profile_active(conn, project, r["profile"])
    ]
    scope = f"（项目 {project}）" if project else ""
    return (
        f"next_bot '{next_bot}' 不是合法 bot 名{scope}。合法值：{', '.join(valid)}。"
        f"请写具体 bot 的 profile 名（如 fe-dev-01），不要写角色统称（如 fe-dev）。"
    )


def _validate_expected_role(conn, next_bot: str, project: str, expected_role_id) -> str | None:
    """req#112：核对 next_bot 当前角色是否等于期望角色 expected_role_id。

    引擎只做「这个人当前是不是这个角色」的事实核对，不判「下一步该是什么角色」的流程语义。
    expected_role_id 是 project_roles.id；实时查 project_members.role_id（数据库最新值，
    不复用旧快照），不等则拒绝流转（任务状态与历史保持不变）。
    """
    try:
        expected_role_id = int(expected_role_id)
    except (TypeError, ValueError):
        return f"expected_role_id 必须是角色 id（整数），收到 {expected_role_id!r}"
    row = conn.execute(
        "SELECT role_id FROM project_members WHERE project=? AND profile=?",
        (project, next_bot),
    ).fetchone()
    current = row["role_id"] if row else None
    if current != expected_role_id:
        return (
            f"next_bot '{next_bot}' 当前角色 role_id={current} 与期望角色 "
            f"expected_role_id={expected_role_id} 不一致，拒绝流转。"
            f"角色认知可能过期，请重新 group_list_members 核对后选择。"
        )
    return None


def _tasks_fingerprint(tasks: list) -> str:
    """稳定内容指纹（幂等标识比对：同一 key 不同内容判冲突）。"""
    payload = json.dumps(tasks, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _occupied_result(conn, requirement_id: int) -> dict:
    """构造「已被占用」的明确占用结果（并发/非本人拆解时返回）。"""
    row = conn.execute(
        "SELECT decompose_owner, decompose_expires_at FROM requirements WHERE id=?", (requirement_id,)
    ).fetchone()
    owner = row["decompose_owner"] if row else ""
    expires = row["decompose_expires_at"] if row else ""
    return {"occupied": True, "code": "decompose_occupied", "owner": owner, "expires_at": expires,
            "message": f"需求 {requirement_id} 正在被 {owner or '其他执行者'} 拆解"
                       f"（占用有效期至 {expires or '未知'}），请勿并发重复拆解。"}


def _append_stale(existing: str, cred: str) -> str:
    """req#118 #203：追加被超时接管覆盖的旧凭据到 stale_credentials（JSON 数组，去重）。"""
    if not cred:
        return existing or '[]'
    stale = json.loads(existing) if existing else []
    if cred not in stale:
        stale.append(cred)
    return json.dumps(stale)


def _acquire_decompose(conn, req, role, credential, ts):
    """原子取得/续用/校验需求拆解权。返回 (credential, error)。

    credential 两种形态：
    - 显式执行凭证（req#117 #199：MCP 层按 session 生成的执行标识，区分同一 bot 的
      CHAT/WORK 不同执行）：pending 首次占用记录该凭证；同执行续用匹配放行；不同执行
      未超时且同成员（owner==role）返回明确占用结果、不同成员返回凭据失效；超时则 CAS 接管。
    - 无 credential（测试/直调/scanner 兜底）：沿用 #198 语义——pending 原子占用（uuid）、
      decomposing 超时原子接管、本人续用沿用原凭据、他人占用未超时返回占用结果。
    """
    state = req["decompose_state"]
    if state == DECOMPOSE_DONE:
        return None, {"error": "requirement already decomposed", "code": "decompose_done",
                      "message": f"需求 {req['id']} 已完成拆解，不再自动拆解。"}
    stored_cred = req["decompose_credential"]
    owner = req["decompose_owner"]
    expires = req["decompose_expires_at"]
    expired = not expires or expires < ts
    if credential == "":
        credential = None
    if credential is not None:
        # 显式执行凭证路径
        if state == DECOMPOSE_PENDING:
            # 首次占用：记录调用方提供的执行凭证（区分不同执行）
            cur = conn.execute(
                "UPDATE requirements SET decompose_state=?, decompose_owner=?, decompose_credential=?, decompose_expires_at=? "
                "WHERE id=? AND decompose_state=?",
                (DECOMPOSE_DECOMPOSING, role, credential, db.now_iso(DECOMPOSE_TTL_SECONDS), req["id"], DECOMPOSE_PENDING),
            )
            if cur.rowcount == 0:
                return None, _occupied_result(conn, req["id"])
            return credential, None
        if state == DECOMPOSE_DECOMPOSING:
            if stored_cred == credential:
                if owner != role:
                    return None, {"error": "decompose credential belongs to another member",
                                  "code": "decompose_credential_stale"}
                return credential, None  # 同执行续用（写任务时续期）
            if expired:
                # 超时接管：CAS 记录新执行凭证，并追加被覆盖旧 stored_cred 到 stale（req#118 #203）。
                cur = conn.execute(
                    "UPDATE requirements SET decompose_owner=?, decompose_credential=?, decompose_expires_at=?, "
                    "decompose_stale_credentials=? "
                    "WHERE id=? AND decompose_state=? AND decompose_credential=? AND decompose_expires_at=?",
                    (role, credential, db.now_iso(DECOMPOSE_TTL_SECONDS),
                     _append_stale(req["decompose_stale_credentials"], stored_cred),
                     req["id"], DECOMPOSE_DECOMPOSING, stored_cred, expires),
                )
                if cur.rowcount == 0:
                    return None, _occupied_result(conn, req["id"])
                return credential, None
            if owner == role:
                # 同成员不同执行（同一 bot 的 CHAT/WORK 并发）→ 明确占用结果
                return None, _occupied_result(conn, req["id"])
            # 不同成员旧凭据迟到 → 凭据失效
            return None, {"error": "decompose credential invalid or expired", "code": "decompose_credential_stale",
                          "message": "执行凭据已失效（超时被接管或已完成），请重新取得拆解权。"}
        return None, {"error": f"unknown decompose_state {state}", "code": "decompose_state_invalid"}
    if state == DECOMPOSE_PENDING:
        new_cred = uuid.uuid4().hex
        cur = conn.execute(
            "UPDATE requirements SET decompose_state=?, decompose_owner=?, decompose_credential=?, decompose_expires_at=? "
            "WHERE id=? AND decompose_state=?",
            (DECOMPOSE_DECOMPOSING, role, new_cred, db.now_iso(DECOMPOSE_TTL_SECONDS), req["id"], DECOMPOSE_PENDING),
        )
        if cur.rowcount == 0:
            return None, _occupied_result(conn, req["id"])
        return new_cred, None
    if state == DECOMPOSE_DECOMPOSING:
        if expired:
            new_cred = uuid.uuid4().hex
            cur = conn.execute(
                "UPDATE requirements SET decompose_owner=?, decompose_credential=?, decompose_expires_at=?, "
                "decompose_stale_credentials=? "
                "WHERE id=? AND decompose_state=? AND decompose_credential=? AND decompose_expires_at=?",
                (role, new_cred, db.now_iso(DECOMPOSE_TTL_SECONDS),
                 _append_stale(req["decompose_stale_credentials"], stored_cred),
                 req["id"], DECOMPOSE_DECOMPOSING, stored_cred, expires),
            )
            if cur.rowcount == 0:
                return None, _occupied_result(conn, req["id"])
            return new_cred, None
        if owner == role:
            if not stored_cred:
                stored_cred = uuid.uuid4().hex
                conn.execute("UPDATE requirements SET decompose_credential=? WHERE id=?",
                             (stored_cred, req["id"]))
            return stored_cred, None
        return None, _occupied_result(conn, req["id"])
    return None, {"error": f"unknown decompose_state {state}", "code": "decompose_state_invalid"}


def _finalize_decompose(conn, project, requirement_id):
    """显式提交拆解完成：校验本轮至少一个任务且依赖全部存在，然后标已拆完。"""
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE requirement_id=? AND project=?", (requirement_id, project)
    ).fetchone()["c"]
    if count == 0:
        return {"error": "cannot finalize decompose: no task created yet", "code": "decompose_incomplete",
                "message": "本轮未创建任何任务/草稿，不能提交拆解完成。"}
    from . import task_brief
    for row in conn.execute(
        "SELECT * FROM tasks WHERE requirement_id=? AND project=?", (requirement_id, project)
    ):
        if row["status"] != CANCELLED and not task_brief.ready(row):
            return {"error": f"Task {row['id']} requires a published, complete brief",
                    "code": "decompose_incomplete", "task_id": row["id"]}
        for dep in json.loads(row["prerequisites"]):
            if not conn.execute("SELECT 1 FROM tasks WHERE id=? AND project=?", (dep, project)).fetchone():
                return {"error": f"cannot finalize decompose: prerequisite task {dep} missing",
                        "code": "decompose_incomplete",
                        "message": f"前置任务 {dep} 不存在，依赖未补齐，不能提交拆解完成。"}
    # req#118 #203：done 后保留完成者 decompose_credential（供旧凭据迟到写入拒），只清空 expires_at。
    conn.execute("UPDATE requirements SET decompose_state=?, decompose_expires_at='' WHERE id=?",
                 (DECOMPOSE_DONE, requirement_id))
    return None


def task_create(project: str, role: str, requirement_id: int, tasks: list, *,
                credential: str | None = None, idempotency_key: str = None, complete: bool = False) -> dict:
    if not isinstance(tasks, list):
        return {"error": "tasks must be a list"}
    # req#118 #201：complete=true 且 tasks 空 = 独立提交拆解完成（分批建任务补依赖后零新增提交），
    # 不再一律拒空 tasks。
    if not tasks and not complete:
        return {"error": "a non-empty tasks list is required"}
    conn = _conn()
    try:
        from .project_permissions import check
        begin_write(conn)
        from . import project_readiness
        blocked = project_readiness.check(conn, project)
        if blocked:
            return blocked
        denied = check(conn, project, role, 'task.create')
        if denied:
            return denied
        req = conn.execute(
            "SELECT * FROM requirements WHERE id=? AND project=?", (requirement_id, project)
        ).fetchone()
        if req is None:
            return {"error": f"requirement {requirement_id} not found"}
        from .requirement_lifecycle import check as check_lifecycle
        inactive = check_lifecycle(req)
        if inactive:
            return inactive
        ts = db.now_iso()
        from . import requirement_gate
        blocked = requirement_gate.check(conn, req)
        if blocked:
            return blocked

        # 幂等：同一 idempotency_key 重复提交返回同一结果、不同内容明确报冲突。
        # req#118 #201：幂等分支仅 tasks 非空时检查——独立 finalize（complete=true 且 tasks 空）
        # 不建任务，跳过幂等记录，避免原幂等键提前重放而静默遗漏完成动作。
        fingerprint = _tasks_fingerprint(tasks) if tasks else None
        if idempotency_key and tasks:
            existing = conn.execute(
                "SELECT content_hash, task_ids FROM decompose_requests "
                "WHERE project=? AND requirement_id=? AND idempotency_key=?",
                (project, requirement_id, idempotency_key)).fetchone()
            if existing:
                if existing["content_hash"] != fingerprint:
                    return {"error": "idempotency key reused with different content",
                            "code": "idempotency_conflict"}
                replayed_ids = json.loads(existing["task_ids"])
                # req#118 #201 退修（node 944）：幂等命中且 complete=true 时不再提前返回 replayed，
                # 改为校验凭据（_acquire_decompose）后执行 _finalize_decompose，返回 replayed+done，
                # 不静默遗漏 complete 动作；已 done 返回 already_done；complete=false 保持原 replayed 语义。
                if complete:
                    _, occupy_error = _acquire_decompose(conn, req, role, credential, ts)
                    if occupy_error:
                        if occupy_error.get("code") == "decompose_done":
                            # req#118 #203：done 后重复 complete 区分旧凭据迟到完成提交 vs 合法完成者幂等重试。
                            # credential 落在 stale 列表（被接管覆盖的旧凭据）→ 拒，不得伪装完成。
                            _stale = json.loads(req["decompose_stale_credentials"]) if req["decompose_stale_credentials"] else []
                            if credential and credential in _stale:
                                return {"error": "decompose credential invalid or expired",
                                        "code": "decompose_credential_stale",
                                        "message": "执行凭据已失效（超时被接管或已完成），不能提交本轮拆解完成。"}
                            return {"requirement_id": requirement_id, "task_ids": replayed_ids,
                                    "count": len(replayed_ids), "replayed": True,
                                    "credential": "", "decompose_state": DECOMPOSE_DONE,
                                    "already_done": True}
                        return occupy_error
                    finalize_error = _finalize_decompose(conn, project, requirement_id)
                    if finalize_error:
                        return finalize_error
                    conn.commit()
                    return {"requirement_id": requirement_id, "task_ids": replayed_ids,
                            "count": len(replayed_ids), "replayed": True,
                            "credential": credential, "decompose_state": DECOMPOSE_DONE}
                return {"requirement_id": requirement_id, "task_ids": replayed_ids,
                        "count": len(replayed_ids), "replayed": True,
                        "credential": req["decompose_credential"] if req["decompose_owner"] == role else "",
                        "decompose_state": req["decompose_state"]}

        # 原子占用/续用/校验凭据。
        orig_credential = credential
        credential, occupy_error = _acquire_decompose(conn, req, role, credential, ts)
        if occupy_error:
            if complete and occupy_error.get("code") == "decompose_done":
                # req#118 #201/#203：done 后重复 complete 区分旧凭据迟到完成提交 vs 合法完成者幂等重试。
                # 调用者原凭据落在 stale 列表（被接管覆盖的旧凭据）→ 拒，不得伪装完成；否则 already_done 幂等安全。
                _stale = json.loads(req["decompose_stale_credentials"]) if req["decompose_stale_credentials"] else []
                if orig_credential and orig_credential in _stale:
                    return {"error": "decompose credential invalid or expired",
                            "code": "decompose_credential_stale",
                            "message": "执行凭据已失效（超时被接管或已完成），不能提交本轮拆解完成。"}
                return {"requirement_id": requirement_id, "task_ids": [], "count": 0,
                        "credential": "", "decompose_state": DECOMPOSE_DONE, "already_done": True}
            return occupy_error

        created = []
        if tasks:
            for i, t in enumerate(tasks):
                if not isinstance(t, dict):
                    return {"error": f"task #{i} must be an object"}
                title = (t.get("title") or "").strip()
                if not title:
                    return {"error": f"task #{i} missing title"}
                from . import task_brief
                draft = t.get("draft", False)
                if type(draft) is not bool:
                    return {"error": "draft must be boolean"}
                brief = {field: t.get(field, "") for field in task_brief.FIELDS}
                brief["prerequisites"] = t.get("prerequisites", [] if draft else None)
                invalid = task_brief.validate(conn, project, brief, partial=draft)
                if invalid:
                    return {**invalid, "task_index": i}
                priority = t.get("priority") or 0
                try:
                    priority = int(priority)
                except (TypeError, ValueError):
                    priority = 0
                if 'next_role' in t:
                    return {"error": "Use next_bot with a concrete member profile; next_role has been renamed."}
                next_bot = (t.get("next_bot") or "").strip()
                if next_bot:
                    err = _validate_next_bot(conn, next_bot, project)
                    if err:
                        return {"error": f"task #{i}: {err}"}
                cur = conn.execute(
                    "INSERT INTO tasks (project, requirement_id, title, priority, status, next_bot, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (project, requirement_id, title, priority, PENDING, next_bot, ts),
                )
                task_id = cur.lastrowid
                conn.execute(
                    "UPDATE tasks SET description=?,design_basis=?,acceptance_criteria=?,prerequisites=?,briefing_status=? WHERE id=?",
                    (*(brief[f].strip() for f in task_brief.FIELDS), json.dumps(brief["prerequisites"]),
                     "draft" if draft else "ready", task_id))
                conn.execute(
                    "INSERT INTO nodes (project, task_id, actor, type, meta, result, comment, ts) "
                    "VALUES (?, ?, ?, 'decompose', '{}', '', '', ?)",
                    (project, task_id, role, ts),
                )
                created.append(task_id)

            # 续期占用（每次成功写任务续期）。
            conn.execute("UPDATE requirements SET decompose_expires_at=? WHERE id=?",
                         (db.now_iso(DECOMPOSE_TTL_SECONDS), requirement_id))

            # 幂等记录。
            if idempotency_key:
                conn.execute(
                    "INSERT INTO decompose_requests (project, requirement_id, idempotency_key, content_hash, task_ids, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (project, requirement_id, idempotency_key, fingerprint, json.dumps(created), ts))

        # 显式提交拆解完成（req#118 #201：complete=true 且 tasks 空时跳过创建循环直接 finalize）。
        if complete:
            finalize_error = _finalize_decompose(conn, project, requirement_id)
            if finalize_error:
                return finalize_error

        _log_event(conn, role, "task_create", project, requirement_id=requirement_id,
                   detail=f"created {len(created)} tasks")
        conn.commit()
        state = conn.execute("SELECT decompose_state FROM requirements WHERE id=?", (requirement_id,)).fetchone()
        return {"requirement_id": requirement_id, "task_ids": created, "count": len(created),
                "credential": credential, "decompose_state": state["decompose_state"] if state else ""}
    finally:
        conn.close()


def _claimable_tasks(conn, project: str, role: str) -> list:
    from .task_brief import eligible
    rows = conn.execute(
        """
        SELECT t.*, r.priority AS req_priority
        FROM tasks t
        JOIN requirements r ON r.id = t.requirement_id
        WHERE t.project=? AND t.next_bot=? AND t.status=?
          AND r.status IN ('open','blocked') AND r.archived=0
        ORDER BY r.id ASC, t.id ASC
        """,
        (project, role, PENDING),
    ).fetchall()
    return [row for row in rows if eligible(conn, row)]


def task_claim(project: str, role: str) -> dict:
    from .task_brief import context
    conn = _conn()
    try:
        begin_write(conn)
        from . import project_readiness
        blocked = project_readiness.check(conn, project)
        if blocked:
            return {**blocked, 'claimed': False, 'reason': blocked['code']}
        held = conn.execute(
            "SELECT t.id,t.requirement_id,t.title FROM tasks t JOIN requirements r ON r.id=t.requirement_id "
            "WHERE t.claimed_by=? AND t.status=? AND t.project=? "
            "AND r.status IN ('open','blocked') AND r.archived=0",
            (role, IN_PROGRESS, project),
        ).fetchall()
        if held:
            return {
                "claimed": False,
                "reason": "already_in_progress",
                "message": f"You already hold {len(held)} in_progress task(s). Finish or block them before claiming more.",
                "in_progress": [_row_to_dict(h) for h in held],
                "contexts": [context(
                    conn, conn.execute('SELECT * FROM tasks WHERE id=? AND project=?', (h['id'], project)).fetchone()) for h in held],
            }
        claimable = _claimable_tasks(conn, project, role)
        if not claimable:
            return {"claimed": False, "reason": "nothing_claimable",
                    "message": "No task is eligible. Use task_get to inspect briefing_status and dependency_blockers; publish a complete brief and wait for prerequisites to be done."}
        target = claimable[0]
        ts = db.now_iso()
        cur = conn.execute(
            "UPDATE tasks SET status=?, claimed_by=?, claimed_at=? WHERE id=? AND status=?",
            (IN_PROGRESS, role, ts, target["id"], PENDING),
        )
        if cur.rowcount == 0:
            return {"claimed": False, "reason": "race",
                    "message": "Task was claimed by someone else; retry."}
        _log_event(conn, role, "claim", project, requirement_id=target["requirement_id"],
                   task_id=target["id"])
        _set_requirement_updated(conn, target["requirement_id"])
        from . import browser_resources
        browser_resources.bind_task(conn, project, role, target['id'])
        conn.commit()
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (target["id"],)).fetchone()
        return {"claimed": True, **context(conn, task)}
    finally:
        conn.close()


def task_get(project: str, task_id: int) -> dict:
    from .task_brief import context
    conn = _conn()
    try:
        row = conn.execute('SELECT * FROM tasks WHERE id=? AND project=?', (task_id, project)).fetchone()
        return context(conn, row) if row else {"error": "Task not found in this project"}
    finally:
        conn.close()


def _meta_to_json(meta) -> str:
    if meta is None:
        return "{}"
    if isinstance(meta, str):
        return meta
    return json.dumps(meta, ensure_ascii=False)


def task_log(project: str, role: str, task_id: int, type: str, meta=None,
             result: str = "", comment: str = "", next_bot: str = "",
             expected_role_id=None) -> dict:
    type = (type or "").strip()
    if not type:
        return {"error": "type is required (the workflow stage)"}
    next_bot = (next_bot or "").strip()
    if not next_bot:
        return {"error": "next_bot is required — select a teammate from group_list_members according to the project workflow"}
    if expected_role_id is None:
        return {"error": "expected_role_id is required — declare the role id the next_bot is expected to hold (from group_list_members)"}
    if not isinstance(result, str) or not result.strip():
        return {"error": "result must be a non-empty string describing the actual outcome and verification; fill it before handing off. Task ownership is unchanged.",
                "code": "task_result_required"}
    meta = _meta_to_json(meta)
    conn = _conn()
    try:
        # Serialize ownership validation, node creation and handoff with cancellation.
        begin_write(conn)
        err = _validate_next_bot(conn, next_bot, project)
        if err:
            return {"error": err}
        err = _validate_expected_role(conn, next_bot, project, expected_role_id)
        if err:
            return {"error": err}
        task = conn.execute(
            "SELECT * FROM tasks WHERE id=? AND project=?", (task_id, project)
        ).fetchone()
        if task is None:
            return {"error": f"task {task_id} not found"}
        from .requirement_lifecycle import check_task
        inactive = check_task(conn, task)
        if inactive:
            return inactive
        if task["claimed_by"] != role:
            return {"error": f"task {task_id} is held by {task['claimed_by'] or '(nobody)'}, not you ({role})"}
        if task["status"] != IN_PROGRESS:
            return {"error": f"task {task_id} is not in_progress (status={task['status']})"}
        ts = db.now_iso()
        cur = conn.execute(
            "INSERT INTO nodes (project, task_id, actor, type, meta, result, comment, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (project, task_id, role, type, meta, result, comment, ts),
        )
        node_id = cur.lastrowid
        conn.execute(
            "UPDATE tasks SET status=?, next_bot=?, claimed_by='' WHERE id=?",
            (PENDING, next_bot, task_id),
        )
        _log_event(conn, role, "node", project, requirement_id=task["requirement_id"],
                   task_id=task_id, node_id=node_id, detail=type)
        _set_requirement_updated(conn, task["requirement_id"])
        from . import browser_resources
        browser_resources.mark_task(conn, project, task_id, role)
        conn.commit()
        cleanup = browser_resources.release(project=project, task_id=task_id, profile=role)
        claimable = _claimable_tasks(conn, project, role)
        return {
            "logged": True, "node_id": node_id, "task_id": task_id,
            "browser_cleanup": cleanup,
            "task_status": PENDING, "next_bot": next_bot,
            "next_claimable": len(claimable),
            "message": f"Node logged, task handed to {next_bot}. {len(claimable)} more claimable task(s) for you.",
        }
    finally:
        conn.close()


def task_complete(project: str, role: str, task_id: int, type: str = "accept",
                  meta=None, result: str = "", comment: str = "") -> dict:
    type = (type or "accept").strip()
    meta = _meta_to_json(meta)
    conn = _conn()
    try:
        from .project_permissions import check
        begin_write(conn)
        denied = check(conn, project, role, 'task.complete')
        if denied:
            return denied
        task = conn.execute(
            "SELECT * FROM tasks WHERE id=? AND project=?", (task_id, project)
        ).fetchone()
        if task is None:
            return {"error": f"task {task_id} not found"}
        from .requirement_lifecycle import check_task
        inactive = check_task(conn, task)
        if inactive:
            return inactive
        if task["next_bot"] != role:
            return {"error": f"task {task_id} is waiting on {task['next_bot'] or '(nobody)'}, not you ({role})"}
        if task["status"] != PENDING:
            return {"error": f"task {task_id} is not pending (status={task['status']})"}
        from .task_brief import ready, dependency_blockers
        if not ready(task):
            return {"error": "Publish a complete task brief before completion",
                    "code": "task_brief_incomplete"}
        blockers = dependency_blockers(conn, task)
        if blockers:
            return {"error": "All prerequisites must be done before completion",
                    "code": "task_dependencies_incomplete", "dependency_blockers": blockers}
        ts = db.now_iso()
        cur = conn.execute(
            "INSERT INTO nodes (project, task_id, actor, type, meta, result, comment, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (project, task_id, role, type, meta, result, comment, ts),
        )
        node_id = cur.lastrowid
        conn.execute(
            "UPDATE tasks SET status=?, done_at=?, claimed_by='' WHERE id=?",
            (DONE, ts, task_id),
        )
        _log_event(conn, role, "complete", project, requirement_id=task["requirement_id"],
                   task_id=task_id, node_id=node_id, detail=type)
        _set_requirement_updated(conn, task["requirement_id"])
        from . import browser_resources
        browser_resources.mark_task(conn, project, task_id)
        conn.commit()
        return {"completed": True, "node_id": node_id, "task_id": task_id,
                "browser_cleanup": browser_resources.release(project=project, task_id=task_id),
                "requirement_id": task["requirement_id"], "message": f"Task {task_id} closed."}
    finally:
        conn.close()


def task_block(project: str, role: str, task_id: int, reason: str,
               kind: str = "technical") -> dict:
    reason = (reason or "").strip()
    if not reason:
        return {"error": "reason is required"}
    kind = (kind or "technical").strip()
    if kind not in ("technical", "decision"):
        return {"error": "kind must be 'technical' or 'decision'"}
    conn = _conn()
    try:
        begin_write(conn)
        task = conn.execute(
            "SELECT * FROM tasks WHERE id=? AND project=?", (task_id, project)
        ).fetchone()
        if task is None:
            return {"error": f"task {task_id} not found"}
        from .requirement_lifecycle import check_task
        inactive = check_task(conn, task)
        if inactive:
            return inactive
        if task["claimed_by"] != role:
            return {"error": f"task {task_id} is held by {task['claimed_by'] or '(nobody)'}, not you ({role})"}
        if task["status"] != IN_PROGRESS:
            return {"error": f"task {task_id} is not in_progress (status={task['status']})"}
        ts = db.now_iso()
        conn.execute("UPDATE tasks SET status=?, block_kind=? WHERE id=?",
                     (BLOCKED, kind, task_id))
        _log_event(conn, role, "block", project, requirement_id=task["requirement_id"],
                   task_id=task_id, detail=f"{kind}: {reason}")
        conn.execute(
            "UPDATE requirements SET status=?, updated_at=? WHERE id=?",
            (REQ_BLOCKED, ts, task["requirement_id"]),
        )
        conn.commit()
        return {"blocked": True, "task_id": task_id,
                "requirement_id": task["requirement_id"], "kind": kind,
                "message": "Task blocked. Explain the blockage and follow the project's escalation rules: technical issues go to the configured project owner (user if unavailable); decisions go to user."}
    finally:
        conn.close()


def task_list(project: str, role: str, status: str = "") -> dict:
    conn = _conn()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE project=? AND next_bot=? AND status=? ORDER BY id ASC",
                (project, role, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE project=? AND next_bot=? ORDER BY id ASC",
                (project, role),
            ).fetchall()
        return {"tasks": [_row_to_dict(r) for r in rows], "count": len(rows), "role": role}
    finally:
        conn.close()


def task_update(project: str, role: str, task_id: int, title: str = "",
                priority: int | None = None, next_bot: str = "", description=None,
                design_basis=None, acceptance_criteria=None, prerequisites=None,
                publish: bool | None = None, credential: str | None = None) -> dict:
    conn = _conn()
    try:
        from .project_permissions import check
        begin_write(conn)
        denied = check(conn, project, role, 'task.update')
        if denied:
            return denied
        task = conn.execute(
            "SELECT * FROM tasks WHERE id=? AND project=?", (task_id, project)
        ).fetchone()
        if task is None:
            return {"error": f"task {task_id} not found"}
        from .requirement_lifecycle import check_task
        inactive = check_task(conn, task)
        if inactive:
            return inactive
        # req#117 #199 + req#118 #202/#203：旧凭据校验。
        # 拆解中（decomposing）：修改草稿需有效执行凭证（超时接管后旧凭据被拒），credential 空（无执行上下文）同样拒；
        # 已完成（done）：credential 空（无执行上下文）或落在被接管覆盖的 stale 列表中被拒，防止旧执行完成后迟到写入。
        req_row = conn.execute(
            "SELECT decompose_state, decompose_owner, decompose_credential, decompose_stale_credentials "
            "FROM requirements WHERE id=? AND project=?",
            (task["requirement_id"], project)).fetchone()
        if req_row:
            if (req_row["decompose_state"] == DECOMPOSE_DECOMPOSING
                    and (not credential or req_row["decompose_credential"] != credential
                         or req_row["decompose_owner"] != role)):
                return {"error": "decompose credential invalid or expired", "code": "decompose_credential_stale",
                        "message": "执行凭据已失效（超时被接管或已完成），不能修改本轮草稿。"}
            if req_row["decompose_state"] == DECOMPOSE_DONE:
                # req#118 #203：done 后修订仍需执行凭据——空凭据（无执行上下文，验收5）或落在 stale 的旧凭据均拒。
                stale = json.loads(req_row["decompose_stale_credentials"]) if req_row["decompose_stale_credentials"] else []
                if not credential or credential in stale:
                    return {"error": "decompose credential invalid or expired", "code": "decompose_credential_stale",
                            "message": "执行凭据已失效（超时被接管或已完成），不能修改本轮草稿。"}
        updates = []
        if prerequisites is not None and task['status'] not in (PENDING, BLOCKED):
            return {"error": "Dependencies can only be edited on pending or blocked tasks"}
        params = []
        from . import task_brief
        supplied = dict(description=description, design_basis=design_basis,
                        acceptance_criteria=acceptance_criteria, prerequisites=prerequisites)
        if publish is not None and type(publish) is not bool:
            return {"error": "publish must be boolean"}
        if publish is not None or any(v is not None for v in supplied.values()):
            brief = {f: task[f] for f in task_brief.FIELDS}
            brief['prerequisites'] = json.loads(task['prerequisites'])
            brief.update({k: v for k, v in supplied.items() if v is not None})
            publishing = publish is True or (publish is None and task['briefing_status'] == 'ready')
            invalid = task_brief.validate(conn, project, brief, task_id, partial=not publishing)
            if invalid:
                return invalid
            for field in task_brief.FIELDS:
                updates.append(f'{field}=?')
                params.append(brief[field].strip())
            updates.extend(['prerequisites=?', 'briefing_status=?'])
            params.extend([json.dumps(brief['prerequisites']), 'ready' if publishing else 'draft'])
        title = (title or "").strip()
        if title:
            updates.append("title = ?")
            params.append(title)
        if priority is not None:
            try:
                priority = int(priority)
            except (TypeError, ValueError):
                priority = 0
            updates.append("priority = ?")
            params.append(priority)
        next_bot = (next_bot or "").strip()
        if next_bot:
            if task["status"] != PENDING:
                return {"error": f"task {task_id} is {task['status']}, cannot reassign (only pending tasks can be reassigned)"}
            err = _validate_next_bot(conn, next_bot, project)
            if err:
                return {"error": err}
            updates.append("next_bot = ?")
            params.append(next_bot)
        if not updates:
            return {"error": "nothing to update (title, priority, or next_bot required)"}
        params.append(task_id)
        conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id=?", params)
        _log_event(conn, role, "task_update", project, requirement_id=task["requirement_id"],
                   task_id=task_id, detail=", ".join(updates))
        _set_requirement_updated(conn, task["requirement_id"])
        conn.commit()
        updated = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return {"updated": True, "task": _row_to_dict(updated)}
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 管理员手动操作：侧栏支持 block、恢复和 cancel。
# task_block 由 Bot 阻塞自己领取的任务；此处提供管理员对任意任务的
# 管理动作，权限在 API 层 _admin_only 把守；block/ cancel 同时把正在干活的
# bot 硬停下来（复用 engine/stop.py 的 stop_profile）。
# --------------------------------------------------------------------------

def admin_task_block(project: str, task_id: int, reason: str = "") -> dict:
    """管理员手动 block 一个任务（kind='manual'）。若任务被领取中，返回领用者 profile
    供 API 层停止该 bot（taskpool 层不做进程操作，职责分离）。"""
    reason = (reason or "").strip() or "manual block by user"
    conn = _conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        task = conn.execute(
            "SELECT * FROM tasks WHERE id=? AND project=?", (task_id, project)
        ).fetchone()
        if task is None:
            return {"error": f"task {task_id} not found"}
        from .requirement_lifecycle import check_task
        inactive = check_task(conn, task)
        if inactive:
            return inactive
        if task["status"] in (DONE, CANCELLED):
            return {"error": f"task {task_id} is {task['status']}, cannot block"}
        ts = db.now_iso()
        was_in_progress = task["status"] == IN_PROGRESS and task["claimed_by"]
        conn.execute("UPDATE tasks SET status=?, block_kind=? WHERE id=?",
                     (BLOCKED, "manual", task_id))
        _log_event(conn, "user", "block", project, requirement_id=task["requirement_id"],
                   task_id=task_id, detail=f"manual: {reason}")
        _set_requirement_updated(conn, task["requirement_id"])
        conn.commit()
        result = {"blocked": True, "task_id": task_id,
                  "requirement_id": task["requirement_id"]}
        if was_in_progress:
            # claimed_by 存的是 role 名（be-dev-01 等 profile 名，成员表同名）
            result["stop_profile"] = task["claimed_by"]
        return result
    finally:
        conn.close()


def admin_task_resume(project: str, task_id: int, next_bot: str = "") -> dict:
    """管理员手动恢复 blocked 任务 → pending，可指定 next_bot（默认原 next_bot）。"""
    return _resume_task(project, task_id, next_bot, 'user', require_grant=False)


def task_resume(project: str, role: str, task_id: int, next_bot: str = "") -> dict:
    """Restore a blocked task to pending with task.resume project permission."""
    return _resume_task(project, task_id, next_bot, role, require_grant=True)


def _resume_task(project, task_id, next_bot, actor, *, require_grant):
    conn = _conn()
    try:
        begin_write(conn)
        if require_grant:
            from .project_permissions import check
            denied = check(conn, project, actor, 'task.resume')
            if denied:
                return denied
        task = conn.execute(
            "SELECT * FROM tasks WHERE id=? AND project=?", (task_id, project)
        ).fetchone()
        if task is None:
            return {"error": f"task {task_id} not found"}
        from .requirement_lifecycle import check_task
        inactive = check_task(conn, task)
        if inactive:
            return inactive
        if task["status"] != BLOCKED:
            return {"error": f"task {task_id} is {task['status']}, only blocked tasks can be resumed"}
        target = (next_bot or "").strip() or task["next_bot"] or ""
        if target:
            err = _validate_next_bot(conn, target, project)
            if err:
                return {"error": err}
        ts = db.now_iso()
        conn.execute(
            "UPDATE tasks SET status=?, block_kind='', next_bot=?, claimed_by='', claimed_at=NULL WHERE id=?",
            (PENDING, target, task_id),
        )
        _log_event(conn, actor, "resume", project, requirement_id=task["requirement_id"],
                   task_id=task_id, detail=f"resume → {target or '(nobody)'}")
        _set_requirement_updated(conn, task["requirement_id"])
        conn.commit()
        return {"resumed": True, "task_id": task_id, "next_bot": target}
    finally:
        conn.close()


def task_cancel(project: str, role: str, task_id: int, reason: str = "") -> dict:
    """Revoke one task with a project grant; running processes are not terminated."""
    conn = _conn()
    try:
        from .project_permissions import check
        begin_write(conn)
        denied = check(conn, project, role, 'task.cancel')
        if denied:
            return denied
        task = conn.execute('SELECT * FROM tasks WHERE project=? AND id=?', (project, task_id)).fetchone()
        if task is None:
            return {'error': f'task {task_id} not found'}
        if task['status'] in (DONE, CANCELLED):
            return {'error': f"task {task_id} is already {task['status']}"}
        conn.execute("UPDATE tasks SET status=?,next_bot='',claimed_by='',block_kind='' WHERE id=?", (CANCELLED, task_id))
        _log_event(conn, role, 'cancel', project, requirement_id=task['requirement_id'],
                   task_id=task_id, detail=(reason or '').strip() or 'task revoked')
        _set_requirement_updated(conn, task['requirement_id'])
        from . import browser_resources
        browser_resources.mark_task(conn, project, task_id)
        conn.commit()
        return {'cancelled': True, 'task_id': task_id, 'requirement_id': task['requirement_id'],
                'browser_cleanup': browser_resources.release(project=project, task_id=task_id),
                'previous_assignee': task['claimed_by'] or task['next_bot'],
                'message': '任务已撤回并请求释放浏览器资源。请通知原接手人停止该任务，正在运行的 Bot 进程需用户另行叫停。'}
    finally:
        conn.close()


def admin_task_cancel(project: str, task_id: int, reason: str = "") -> dict:
    """管理员手动 cancel 单个任务。若任务在领取中，返回领用者供 API 层停 bot。"""
    reason = (reason or "").strip() or "manual cancel by user"
    conn = _conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        task = conn.execute(
            "SELECT * FROM tasks WHERE id=? AND project=?", (task_id, project)
        ).fetchone()
        if task is None:
            return {"error": f"task {task_id} not found"}
        if task["status"] in (DONE, CANCELLED):
            return {"error": f"task {task_id} is already {task['status']}"}
        ts = db.now_iso()
        was_in_progress = task["status"] == IN_PROGRESS and task["claimed_by"]
        conn.execute(
            "UPDATE tasks SET status=?, next_bot='', claimed_by='', block_kind='' WHERE id=?",
            (CANCELLED, task_id),
        )
        _log_event(conn, "user", "cancel", project, requirement_id=task["requirement_id"],
                   task_id=task_id, detail=f"manual: {reason}")
        _set_requirement_updated(conn, task["requirement_id"])
        from . import browser_resources
        browser_resources.mark_task(conn, project, task_id)
        conn.commit()
        result = {"cancelled": True, "task_id": task_id,
                  "browser_cleanup": browser_resources.release(project=project, task_id=task_id),
                  "requirement_id": task["requirement_id"]}
        if was_in_progress:
            result["stop_profile"] = task["claimed_by"]
        return result
    finally:
        conn.close()


# --------------------------------------------------------------------------

