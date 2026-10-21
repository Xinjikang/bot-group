"""Soft removal with history retention; directory checks for archive recovery."""
from pathlib import Path
from fastapi import HTTPException
from . import db, project_workspaces as ws


def check_directory(conn, project):
    target = ws.key(project['root'])
    for row in conn.execute('SELECT name,root FROM projects WHERE archived=0 AND removed=0 AND name<>?', (project['name'],)):
        try:
            other = ws.key(row['root'])
        except (HTTPException, OSError, ValueError, RuntimeError):
            continue
        if other == target:
            raise HTTPException(409, f"目录已由项目 {row['name']} 使用，请先移除该项目后再恢复。")
    root = ws.canonical(project['root'])
    if not Path(root).is_dir():
        raise HTTPException(409, f'项目主目录不存在或不可访问：{root}。项目保持归档，请先恢复原项目文件，再重试恢复。')
    workspaces = list(conn.execute('SELECT profile,path FROM project_workspaces WHERE project=?', (project['name'],)))
    if project['directory_mode'] == 'worktree' or project['worktree_root'] or workspaces:
        try:
            ws.inspect(root, root)
        except HTTPException as exc:
            raise HTTPException(409, f'项目主仓库无效：{root}。{exc.detail}。项目保持归档，请先恢复原 Git 仓库，再重试恢复。') from exc
        for workspace in workspaces:
            try:
                ws.inspect(root, workspace['path'])
            except HTTPException as exc:
                raise HTTPException(409, f"成员 {workspace['profile']} 的 worktree 无效：{workspace['path']}。{exc.detail}。项目保持归档，请先恢复工作区文件及 Git 登记，再重试恢复。") from exc


def change(name, removed, actor='user'):
    if not removed:
        raise HTTPException(404, '已移除项目保留历史；重新使用目录请导入新项目')
    with ws.LOCK:
        conn = db.get_connection()
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                project = conn.execute('SELECT * FROM projects WHERE name=?', (name,)).fetchone()
                if project is None:
                    raise HTTPException(404, '项目不存在')
                if bool(project['removed']) == removed:
                    conn.commit()
                    from . import browser_resources
                    return {'project': name, 'removed': removed,
                            'browser_cleanup': browser_resources.release(project=name)}
                if removed:
                    if conn.execute("SELECT 1 FROM tasks WHERE project=? AND status IN ('pending','in_progress','blocked')", (name,)).fetchone():
                        raise HTTPException(409, '项目还有未结束的任务，请先处理后再移除')
                    if conn.execute("SELECT 1 FROM wake_jobs WHERE project=? AND status IN ('queued','running')", (name,)).fetchone():
                        raise HTTPException(409, '项目还有等待或运行中的 Bot 工作，请先处理后再移除')
                    ws.ensure_idle(name)
                    if conn.execute('SELECT 1 FROM project_workspaces WHERE project=?', (name,)).fetchone():
                        raise HTTPException(409, '项目仍绑定受管工作区，请先在成员与目录中释放工作区后再移除')
                conn.execute('UPDATE projects SET removed=?,removed_at=?,archived=? WHERE name=?',
                             (int(removed), db.now_iso() if removed else '', int(removed), name))
                ws.audit(conn, name, actor, 'project.remove',
                         {'root': project['root'], 'history': 'preserved'})
            from . import browser_resources
            return {'project': name, 'removed': removed,
                    'browser_cleanup': browser_resources.release(project=name)}
        finally:
            conn.close()
