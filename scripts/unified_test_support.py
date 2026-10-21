"""Temporary shared database and local API fixtures for unified-service tests."""
import os
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from engine import db

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def isolated_database():
    with tempfile.TemporaryDirectory(prefix='unified_mcp_') as directory:
        path = str(Path(directory) / 'group.db')
        with patch.dict(os.environ, {'GROUP_DB': path}):
            conn = db.get_connection()
            try:
                db.init_schema(conn)
                for profile in ('default', 'architect', 'dev', 'reviewer', 'user'):
                    conn.execute('INSERT INTO members(profile,display_name,role) VALUES(?,?,?)',
                                 (profile, profile, profile))
                for project in ('alpha', 'beta'):
                    conn.execute('INSERT INTO projects(name,root,created_at) VALUES(?,?,?)',
                                 (project, str(Path(directory) / project), db.now_iso()))
                    # Explicit grants for the historical workflow fixtures; production has no defaults.
                    from scripts.project_test_support import enable_members
                    enable_members(conn, (project,))
                    # req#112：预插角色实体 + 回填 project_members.role_id（task_log 交接需 expected_role_id 核对）
                    role_ids = {}
                    for name in ('default', 'architect', 'dev', 'reviewer'):
                        role_ids[name] = conn.execute(
                            'INSERT INTO project_roles(project,name,created_at) VALUES(?,?,?)',
                            (project, name, db.now_iso())).lastrowid
                    for profile in ('default', 'architect', 'dev', 'reviewer'):
                        conn.execute('UPDATE project_members SET role_id=? WHERE project=? AND profile=?',
                                     (role_ids[profile], project, profile))
                    from engine.project_permissions import PERMISSIONS
                    conn.executemany('INSERT INTO project_permissions(project,profile,permission) VALUES(?,?,?)',
                                     [(project, profile, permission) for profile in ('default', 'architect') for permission in PERMISSIONS])
                # 预插一条用户消息（id=1），供 REQUIREMENT_FIELDS 的 source_ref 引用。
                conn.execute("INSERT INTO messages(project,ts,sender,sender_type,content,mentions) "
                             "VALUES('alpha',?,'user','user','Isolated fixture source message','[]')",
                             (db.now_iso(),))
                conn.commit()
            finally:
                conn.close()
            yield path


def server_parameters(path, url):
    from engine.auth import bot_token
    from mcp import StdioServerParameters
    env = {key: value for key, value in os.environ.items()
           if key not in {"GROUP_DB", "GROUP_DATA_DIR", "DATA_DIR", "GROUP_ATTACHMENTS_DIR",
                          "XDG_GROUP_ENGINE_EXECUTION"}}
    return StdioServerParameters(
        command=os.sys.executable,
        args=[str(ROOT / 'mcp' / 'server.py')],
        env={**env, 'GROUP_ENGINE_URL': url,
             'GROUP_ENGINE_TOKEN': bot_token('default'),
             'TASK_QUEUE_ROLE': 'default', 'HERMES_PROFILE': 'default', 'HERMES_CHAT_MODE': '0'},
    )


@contextmanager
def isolated_api(*, frontend=False):
    """Only API routes: no queue/scanner workers and no real bot spawning."""
    from fastapi import FastAPI
    import uvicorn
    from engine import api
    app = FastAPI()
    app.include_router(api.router_api)
    if frontend:
        from fastapi.staticfiles import StaticFiles
        app.mount("/app", StaticFiles(directory=str(ROOT / "frontend"), html=True))
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError('isolated API failed to start')
            time.sleep(0.01)
        yield f'http://127.0.0.1:{sock.getsockname()[1]}'
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        if thread.is_alive():
            raise RuntimeError('isolated API failed to stop')

REQUIREMENT_FIELDS = dict(background='Isolated regression scenario', scope='Test fixture only', approach='Approved test contract', acceptance='Assertions pass', undecided='\u65e0', source_ref='thread_id=0; message_id=1')


def confirmed_requirement_fields(project='alpha'):
    """Create a real source row only for tests that decompose a requirement."""
    conn = db.get_connection()
    try:
        mid = conn.execute("INSERT INTO messages(project,ts,sender,sender_type,content,mentions) "
                           "VALUES(?,?,'user','user','Confirm decomposition of test scope','[]')",
                           (project, db.now_iso())).lastrowid
        conn.commit()
        return dict(REQUIREMENT_FIELDS, source_ref=f'thread_id=0; message_id={mid}')
    finally:
        conn.close()


def role_id_of(project, name):
    """req#112：查项目角色的稳定 id（project_roles.id），供 task_log 的 expected_role_id 用。"""
    conn = db.get_connection()
    try:
        return conn.execute(
            "SELECT id FROM project_roles WHERE project=? AND name=?",
            (project, name),
        ).fetchone()["id"]
    finally:
        conn.close()
