"""HTTP authentication and the default-deny member resource contract."""

import json
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request

from . import auth, db

PUBLIC = {("POST", "/auth/login"), ("GET", "/auth/status")}
BOT_ROUTES = {
    ("GET", "/commands"), ("POST", "/task-tools/{operation}"),
    ("GET", "/members"), ("GET", "/threads"), ("GET", "/thread"),
    ("GET", "/inbox"), ("GET", "/wake/check"), ("GET", "/attachments/{attachment_id}"),
    ("POST", "/msg"), ("POST", "/thread"), ("POST", "/thread/task-update"),
    ("POST", "/inbox/seen"), ("POST", "/heartbeat"),
    ("POST", "/message/{message_id}/retract"),
}


def resolve_identity(
    x_group_profile: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    x_group_key: Optional[str] = Header(default=None),
    request: Request = None,
) -> tuple[str, str]:
    if request is not None and (request.method, request.scope["route"].path) in PUBLIC:
        return "", "public"
    if isinstance(x_group_profile, str):
        profile = x_group_profile.strip()
        key = x_group_key if isinstance(x_group_key, str) else ""
        if not auth.verify_bot(profile, key) or db.is_builtin_user(profile):
            raise HTTPException(401, "Invalid member credential")
        if request is not None:
            request.state.execution_scope = auth.execution_scope(profile, key)
        return profile, "bot"
    if isinstance(authorization, str):
        if authorization.startswith("Bearer ") and auth.verify_token(authorization[7:].strip()):
            return "user", "user"
        raise HTTPException(401, "Invalid administrator credential")
    # EventSource uses an HttpOnly cookie restricted to the read-only SSE path.
    if request is not None and request.url.path == "/events":
        if auth.verify_token(request.cookies.get("group_events", "")):
            return "user", "user"
    if not auth.configured():
        if request is None or (request.client and request.client.host in ("127.0.0.1", "::1")):
            if request is not None:
                origin = request.headers.get("origin")
                if origin and origin != str(request.base_url).rstrip("/"):
                    raise HTTPException(403, "Cross-origin local access is forbidden")
                if request.headers.get("sec-fetch-site") == "cross-site":
                    raise HTTPException(403, "Cross-site local access is forbidden")
            return "", "dev"
    raise HTTPException(401, "unauthorized")


def require_project(conn, profile, project):
    if not isinstance(project, str) or not project.strip():
        raise HTTPException(403, "An accessible project is required")
    if not conn.execute(
        "SELECT 1 FROM project_members pm JOIN projects p ON p.name=pm.project "
        "JOIN members m ON m.profile=pm.profile "
        "WHERE pm.project=? AND pm.profile=? AND pm.active=1 "
        "AND p.archived=0 AND p.removed=0", (project, profile),
    ).fetchone():
        raise HTTPException(403, "Member is not active in this project")


def require_thread(conn, profile, thread_id):
    if isinstance(thread_id, bool) or not isinstance(thread_id, (str, int)):
        raise HTTPException(422, "Valid topic id required")
    try:
        thread_id = int(thread_id)
    except ValueError:
        raise HTTPException(422, "Valid topic id required")
    if not 0 < thread_id < 2**63:
        raise HTTPException(422, "Valid topic id required")
    row = conn.execute("SELECT project,members FROM threads WHERE id=?", (thread_id,)).fetchone()
    if row is None:
        raise HTTPException(403, "Topic is unavailable to this member")
    require_project(conn, profile, row["project"])
    if profile not in json.loads(row["members"]):
        raise HTTPException(403, "Topic membership required")
    return row["project"]


async def authorize(request: Request, identity=Depends(resolve_identity)):
    route = (request.method, request.scope["route"].path)
    if route in PUBLIC:
        return
    request.state.identity = identity
    profile, kind = identity
    if kind != "bot":
        return
    if route not in BOT_ROUTES:
        raise HTTPException(403, "Administrator access required")
    conn = db.get_connection()
    try:
        if not conn.execute("SELECT 1 FROM members WHERE profile=?", (profile,)).fetchone():
            raise HTTPException(403, "Unknown member")
        path = route[1]
        # Task operations and attachments enforce their own project/resource scope.
        if path == "/members":
            require_project(conn, profile, request.query_params.get("project", ""))
        if path in ("/inbox", "/wake/check"):
            target = request.query_params.get("profile", "")
            if target and target != profile:
                raise HTTPException(403, "Member may access only its own runtime and inbox")
            project = request.query_params.get("project")
            if project:
                require_project(conn, profile, project)
            if path == "/inbox" and request.query_params.get("thread_id"):
                require_thread(conn, profile, request.query_params["thread_id"])
        if path == "/thread" and request.method == "GET":
            require_thread(conn, profile, request.query_params.get("id", ""))
        if request.method == "POST" and path in (
            "/msg", "/thread", "/thread/task-update", "/heartbeat",
        ):
            try:
                body = await request.json()
            except (ValueError, UnicodeError):
                raise HTTPException(422, "JSON object required")
            if not isinstance(body, dict):
                raise HTTPException(422, "JSON object required")
            if path == "/msg":
                from .message_execution import validate_shape
                validate_shape(profile, body)
                scope = getattr(request.state, "execution_scope", None)
                if scope is not None and any(
                    scope[key] != (body[key] if key in ("project", "thread_id") else body["execution"][key])
                    for key in ("project", "thread_id", "session", "wake_started_at")
                ):
                    raise HTTPException(403, "Message does not match the credential execution")
            if path == "/heartbeat" and body.get("profile", profile) != profile:
                raise HTTPException(403, "Heartbeat identity mismatch")
            if path == "/heartbeat" and not conn.execute(
                "SELECT 1 FROM project_members pm JOIN projects p ON p.name=pm.project "
                "WHERE pm.profile=? AND pm.active=1 AND p.archived=0 AND p.removed=0",
                (profile,),
            ).fetchone():
                raise HTTPException(403, "Active project membership required")
            thread_id = body.get("thread_id")
            if thread_id is not None:
                project = require_thread(conn, profile, thread_id)
                if body.get("project") and body["project"] != project:
                    raise HTTPException(403, "Topic project mismatch")
            elif path in ("/msg", "/thread"):
                require_project(conn, profile, body.get("project", ""))
    finally:
        conn.close()
