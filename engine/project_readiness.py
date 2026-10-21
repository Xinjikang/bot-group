"""One readiness decision for execution and business creation (req#103)."""


def is_ready(conn, project):
    return conn.execute(
        "SELECT 1 FROM projects WHERE name=? AND archived=0 AND readiness_status='ready'",
        (project,),
    ).fetchone() is not None


def check(conn, project):
    row = conn.execute('SELECT readiness_status FROM projects WHERE name=?', (project,)).fetchone()
    if row and row['readiness_status'] != 'ready':
        return {'error': '项目文件尚未就绪，请查看初始化进度并恢复。',
                'code': 'project_not_ready', 'readiness_status': row['readiness_status']}
    return None
