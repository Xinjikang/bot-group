"""Global ACP file-edit policy shared by all Hermes gateway executions."""
from . import db

DEFAULT = 'workspace_session'
POLICIES = ('ask', DEFAULT, 'session')
KEY = 'hermes_edit_approval_policy'


def read(conn):
    row = conn.execute("SELECT value FROM settings WHERE project='' AND key=?", (KEY,)).fetchone()
    value = row['value'] if row else DEFAULT
    return value if value in POLICIES else DEFAULT


def write(conn, policy):
    if not isinstance(policy, str) or policy not in POLICIES:
        raise ValueError('请选择 ask、workspace_session 或 session')
    conn.execute("INSERT INTO settings(project,key,value,updated_at) VALUES('',?,?,?) "
                 'ON CONFLICT(key,project) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                 (KEY, policy, db.now_iso()))
