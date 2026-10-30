"""Carry HTTP task authorization into the task pool's write transaction."""

from contextlib import contextmanager
from contextvars import ContextVar

from fastapi import HTTPException

from . import auth, message_execution
from .access import require_project, require_thread

WORK_ONLY = {"task_claim", "task_log", "task_complete", "task_block"}
_guard = ContextVar("task_write_access", default=None)


def execution_for(request, supplied):
    scope = getattr(request.state, "execution_scope", None) if request is not None else None
    if scope is not None and scope.get('credential_type') == 'session':
        if supplied is not None and (any(supplied.get(key) != scope[key] for key in auth.SESSION_FIELDS)
                                     or 'wake_started_at' in supplied):
            raise HTTPException(403, 'Context does not match the session credential')
        return scope
    if supplied and supplied.get('credential_type') == 'session':
        raise HTTPException(403, 'Session credential required')
    if scope is None:
        return supplied
    if supplied is not None and any(supplied.get(key) != scope[key] for key in auth.EXECUTION_FIELDS):
        raise HTTPException(403, "Execution does not match the member credential")
    return scope


def credential_for(execution, supplied):
    if execution is None:
        return None if supplied == "" else supplied
    expected = f"exec:{execution['session']}"
    if supplied and supplied != expected:
        raise HTTPException(403, "Decomposition credential does not match the execution")
    return expected


def validate(conn, actor, project, operation, execution, thread_id=None):
    require_project(conn, actor, project)
    if execution is not None:
        if execution.get("profile") != actor or execution.get("project") != project:
            raise HTTPException(403, "Execution identity or project mismatch")
        if execution.get('credential_type') == 'session' and operation == 'operation_receipt':
            from . import hermes_session_auth
            hermes_session_auth.validate(conn, execution)
            return
        message_execution.validate(conn, actor, {
            "project": project, "thread_id": execution.get("thread_id"),
            "text": None, "need_reply": False, "execution": execution,
        })
        if operation in WORK_ONLY and execution.get("thread_id") is not None:
            raise HTTPException(403, "This operation requires a work execution")
    if thread_id is not None:
        require_thread(conn, actor, thread_id)
        if conn.execute("SELECT status FROM threads WHERE id=?", (thread_id,)).fetchone()[0] == "closed":
            raise HTTPException(403, "Topic is closed")


@contextmanager
def scope(actor, project, operation, execution, thread_id=None):
    token = _guard.set((actor, project, operation, execution, thread_id))
    try:
        yield
    finally:
        _guard.reset(token)


def begin_write(conn):
    conn.execute("BEGIN IMMEDIATE")
    guard = _guard.get()
    if guard is not None:
        validate(conn, *guard)


def public_result(value):
    """Keep decomposition lease secrets out of nested read/brief responses."""
    if isinstance(value, dict):
        return {key: public_result(item) for key, item in value.items()
                if key not in ("decompose_credential", "decompose_stale_credentials")}
    if isinstance(value, list):
        return [public_result(item) for item in value]
    return value
