"""HTTP authentication and the default-deny member resource contract."""

import json
from typing import Optional

from fastapi import Depends, Header, HTTPException, Request

from . import auth, db

PUBLIC = {("POST", "/auth/login"), ("GET", "/auth/status")}
BOT_ROUTES = {
    ("GET", "/mcp-operations/{operation_id}"),
    ("GET", "/commands"), ("POST", "/task-tools/{operation}"),
    ("GET", "/members"), ("GET", "/threads"), ("GET", "/thread"),
    ("GET", "/inbox"), ("GET", "/wake/check"), ("GET", "/attachments/{attachment_id}"),
    ("POST", "/msg"), ("POST", "/thread"), ("POST", "/thread/task-update"),
    ("POST", "/inbox/seen"), ("POST", "/heartbeat"),
    ("POST", "/message/{message_id}/retract"),
}

# 停派发期间明确未受理的写操作（副作用路由），须回 503 + X-Group-Not-Accepted:1；
# 管理查询（GET）与心跳（非 Hermes 生命周期）保留。
WRITE_ROUTES = {
    ("POST", "/msg"), ("POST", "/thread"), ("POST", "/thread/task-update"),
    ("POST", "/inbox/seen"), ("POST", "/task-tools/{operation}"),
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
        from .signing_keys import RecoveryError
        try:
            ring = auth.signing_keys()
        except RecoveryError as exc:
            raise HTTPException(503, str(exc)) from None
        if not auth.verify_bot(profile, key, ring=ring) or db.is_builtin_user(profile):
            raise HTTPException(401, "Invalid member credential")
        if request is not None:
            request.state.execution_scope = auth.execution_scope(profile, key, ring[1])
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
    from . import wake
    if wake._shutting_down.is_set() and route in WRITE_ROUTES:
        # 停派发：新的写工具请求明确未受理（客户端按 X-Group-Not-Accepted 不重放，设计 4.4/7.2）。
        raise HTTPException(503, "Engine is shutting down; request not accepted",
                            headers={"X-Group-Not-Accepted": "1"})
    conn = db.get_connection()
    try:
        if not conn.execute("SELECT 1 FROM members WHERE profile=?", (profile,)).fetchone():
            raise HTTPException(403, "Unknown member")
        path = route[1]
        scope = getattr(request.state, 'execution_scope', None)
        gateway_run = None
        if scope is not None:
            gateway_run = conn.execute('SELECT * FROM hermes_executions WHERE session=? AND started_at=?',
                                       (scope['session'], scope['wake_started_at'])).fetchone()
            if gateway_run is not None:
                if (gateway_run['signing_generation'] != scope['signing_generation']
                        or any(gateway_run[k] != scope[k] for k in ('profile', 'project', 'thread_id'))
                        or gateway_run['kind'] != ('work' if scope['thread_id'] is None else 'chat')):
                    raise HTTPException(401, 'Execution credential identity mismatch')
                if gateway_run['state'] == 'terminal':
                    raise HTTPException(401, 'Execution credential expired')
                require_project(conn, profile, scope['project'])
                requested_project = request.query_params.get('project')
                if requested_project and requested_project != scope['project']:
                    raise HTTPException(403, 'Execution project mismatch')
                requested_thread = request.query_params.get('id' if path == '/thread' else 'thread_id')
                if path in ('/thread', '/inbox') and requested_thread is not None:
                    if str(scope['thread_id']) != requested_thread:
                        raise HTTPException(403, 'Execution topic mismatch')
                if path == '/inbox':
                    expected_kind = 'chat' if scope['thread_id'] is not None else 'work'
                    if request.query_params.get('kind') != expected_kind:
                        raise HTTPException(403, 'Execution inbox kind mismatch')
                    if expected_kind == 'chat' and requested_thread is None:
                        raise HTTPException(403, 'Execution topic required')
                    if expected_kind == 'work' and requested_project != scope['project']:
                        raise HTTPException(403, 'Execution project required')
            elif scope['signing_generation'] != 0:
                # Previous generations require a registered Hermes execution.
                native = conn.execute('SELECT runtime,ended_at,profile,project,thread_id FROM wake_runs '
                                      'WHERE session=? AND started_at=?',
                                      (scope['session'], scope['wake_started_at'])).fetchone()
                if (scope['signing_generation'] != auth.signing_keys()[0]
                        or native is None or native['runtime'] != 'codex' or native['ended_at'] is not None
                        or any(native[k] != scope[k] for k in ('profile', 'project', 'thread_id'))):
                    raise HTTPException(401, 'Execution credential expired or unregistered')
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
            if gateway_run is not None and path == '/heartbeat':
                if body.get('thread_id') != scope['thread_id']:
                    raise HTTPException(403, 'Heartbeat topic mismatch')
                pid = body.get('pid')
                if type(pid) is not int or pid <= 0:
                    raise HTTPException(422, 'Heartbeat process required')
                conn.execute('UPDATE wake_runs SET pid=? WHERE session=? AND started_at=? AND ended_at IS NULL',
                             (pid, scope['session'], scope['wake_started_at']))
                conn.commit()
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
