"""API routes protected by the authentication and resource contract in access.py."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.responses import JSONResponse, FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from . import appearance, attachments, auth, commands, db, discovery, events, mcp_operations, profile_config, router, taskqueue_proxy, wake
from .delivery import effective_need_reply
from .task_api import create_router as _task_router
from .access import authorize, resolve_identity as _resolve_identity

router_api = APIRouter(dependencies=[Depends(authorize)])


def _utf16_len(text: str) -> int:
    """字符串的 UTF-16 码元数（emoji/代理对算 2，与前端 maxlength 同口径）。"""
    return len(text.encode("utf-16-le")) // 2


# --------------------------------------------------------------------------
# 身份解析
# --------------------------------------------------------------------------

@router_api.get("/commands")
def get_commands():
    return {"commands": commands.command_menu()}


router_api.include_router(_task_router(_resolve_identity))


@router_api.get('/mcp-operations/{operation_id}')
def get_mcp_operation(operation_id: str, request: Request):
    return mcp_operations.receipt(request, operation_id)


def _conn() -> sqlite3.Connection:
    c = db.get_connection()
    db.init_schema(c)
    return c


def _ensure_bot(conn: sqlite3.Connection, profile: str, display_name: str, role: str) -> int:
    """反推/复用该 profile 的 hermes Bot，返回 bot_id（req#86 task#147）。

    核心逻辑在 db._ensure_hermes_bot（与 session_adapter 共用），这里只包
    HTTP 错误语义：落库失败（极端并发下重查仍无行）按 500 返回。
    """
    try:
        return db._ensure_hermes_bot(conn, profile, display_name, role)
    except RuntimeError as exc:
        raise HTTPException(500, "Bot 身份落库失败，请重试") from exc


def _admin_only(identity: tuple[str, str], message: str) -> None:
    """管理员鉴权：身份 kind ∈ {user(带 token), dev(未设管理员密码的本机开发)}。

    bot 身份（X-Group-Profile header）一律 403——管理员编辑口不对 bot 开放。
    """
    if identity[1] not in ("user", "dev"):
        raise HTTPException(403, message)


from .hud_api import router as _hud_router
router_api.include_router(_hud_router(_resolve_identity, _admin_only))


# --------------------------------------------------------------------------
# 写
# --------------------------------------------------------------------------

@router_api.get("/system/version")
def get_app_version(identity=Depends(_resolve_identity)):
    _admin_only(identity, '版本信息仅限管理员使用')
    from .app_updates import version_info
    return JSONResponse(version_info(), headers={"Cache-Control": "no-store"})


@router_api.get("/system/updates")
def get_app_updates(identity=Depends(_resolve_identity)):
    _admin_only(identity, '更新检查仅限管理员使用')
    from .app_updates import check_update
    return JSONResponse(check_update(), headers={"Cache-Control": "no-store"})


class AppUpgradeBody(BaseModel):
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    version: str = Field(min_length=1, max_length=40)


@router_api.get('/system/upgrade')
def get_app_upgrade(identity=Depends(_resolve_identity)):
    _admin_only(identity, '升级状态仅限管理员使用')
    from .app_upgrade import status
    return JSONResponse(status(), headers={'Cache-Control': 'no-store'})


@router_api.post('/system/upgrade', status_code=202)
def start_app_upgrade(body: AppUpgradeBody, request: Request, identity=Depends(_resolve_identity)):
    _admin_only(identity, '升级应用仅限管理员使用')
    if getattr(request.app.state, 'server', None) is None:
        raise HTTPException(503, '请通过 python -m engine.main 启动服务')
    from .app_upgrade import begin
    try:
        return JSONResponse(begin(body.request_id, body.version, request.app.state.instance_id),
                            status_code=202, headers={'Cache-Control': 'no-store'})
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(503, str(exc)) from exc


class DirectoryPickerBody(BaseModel):
    initial: str = Field(default='', max_length=32767)


@router_api.post('/local/directory-picker')
def pick_local_directory(body: DirectoryPickerBody, request: Request,
                         identity: tuple = Depends(_resolve_identity)):
    _admin_only(identity, '目录选择仅限管理员使用')
    if not request.client or request.client.host not in ('127.0.0.1', '::1'):
        raise HTTPException(403, '请在运行引擎的电脑上选择目录，或直接输入路径')
    origin = request.headers.get('origin')
    if origin and origin != str(request.base_url).rstrip('/'):
        raise HTTPException(403, '目录选择须从本机页面发起')
    from .directory_picker import choose_directory
    return {'path': choose_directory(body.initial)}


@router_api.get('/local/directory-list')
def list_local_directory(request: Request, path: str = Query(default=''),
                         offset: int = Query(default=0, ge=0),
                         limit: int = Query(default=200, ge=1),
                         identity: tuple = Depends(_resolve_identity)):
    """网页内目录选择器导航引擎所在机器的目录树（req#110 方案A）。

    返回当前目录路径 + 子目录列表（含可导航性标识），不列文件、不暴露文件内容。
    无权限/空目录/大目录分页均正常处理不报错。管理员（带 token 的 user）可远程
    复用；未设管理员密码的本机开发（dev）仍限回环，避免目录树暴露给任意远端。
    """
    _admin_only(identity, '目录列表仅限管理员使用')
    if identity[1] == 'dev' and (not request.client or request.client.host not in ('127.0.0.1', '::1')):
        raise HTTPException(403, '请在运行引擎的电脑上选择目录，或直接输入路径')
    origin = request.headers.get('origin')
    if origin and origin != str(request.base_url).rstrip('/'):
        raise HTTPException(403, '目录列表须从本机页面发起')
    from .directory_list import list_directory
    return list_directory(path, offset, limit)

@router_api.get('/appearance/background')
def read_background(identity: tuple = Depends(_resolve_identity)):
    _admin_only(identity, '背景设置仅限管理员使用')
    data = appearance.read()
    headers = {'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
               'X-Background-Configured': 'true' if data is not None else 'false'}
    if not data:
        return Response(status_code=204, headers=headers)
    return Response(data, media_type=appearance.image_type(data), headers=headers)


@router_api.put('/appearance/background')
@router_api.delete('/appearance/background')
async def write_background(request: Request, identity: tuple = Depends(_resolve_identity)):
    _admin_only(identity, '背景设置仅限管理员使用')
    origin = request.headers.get('origin')
    if origin and origin != str(request.base_url).rstrip('/'):
        raise HTTPException(403, '背景设置须从本站页面发起')
    data = bytearray()
    if request.method == 'PUT':
        async for chunk in request.stream():
            if len(data) + len(chunk) > appearance.MAX_BYTES:
                raise HTTPException(413, '图片最大为 10 MiB。')
            data.extend(chunk)
        if not data:
            raise HTTPException(400, '请选择图片。')
    try:
        await asyncio.to_thread(appearance.save, bytes(data),
                                migrate=request.headers.get('if-none-match') == '*')
    except OSError as exc:
        raise HTTPException(507, '服务端图片保存失败，请检查磁盘空间和数据目录权限。') from exc
    return {'ok': True}


@router_api.post("/attachments")
async def upload_attachment(request: Request, project: str, filename: str = "image",
                            identity: tuple = Depends(_resolve_identity)):
    owner, kind = identity
    if kind == "bot":
        raise HTTPException(403, "Image uploads currently require the user")
    conn = _conn()
    try:
        if not conn.execute("SELECT 1 FROM projects WHERE name=? AND archived=0", (project,)).fetchone():
            raise HTTPException(400, "Select an active project")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > attachments.MAX_BYTES:
                raise HTTPException(413, "Image exceeds 10 MiB")
        return attachments.store(conn, bytes(data), filename, project, owner or "user")
    finally:
        conn.close()


@router_api.get("/attachments/{attachment_id}")
def read_attachment(attachment_id: str, identity: tuple = Depends(_resolve_identity)):
    conn = _conn()
    try:
        row, path = attachments.accessible(conn, attachment_id, *identity)
        return FileResponse(path, media_type=row["mime"], filename=row["filename"],
                            content_disposition_type="inline",
                            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
    finally:
        conn.close()


@router_api.post("/msg")
@mcp_operations.recorded
def post_msg(
    body: dict[str, Any],
    x_group_profile: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    x_group_key: Optional[str] = Header(default=None),
    request: Request = None,
):
    identity, kind = _resolve_identity(x_group_profile, authorization, x_group_key, request)
    sender = body.get("sender", "")
    if kind == "bot":
        sender = identity  # header 权威，防冒名
    elif kind == "user":
        sender = "user"
    # dev 模式：用 body 的 sender
    # 开发模式下 HTTP 调用可能缺少 X-Group-Profile；最终 sender 必须非空，
    # 否则返回 400，避免保存缺少发送者的消息。
    if not isinstance(sender, str) or not sender.strip():
        raise HTTPException(status_code=400,
                            detail="sender is required (missing X-Group-Profile identity header or sender field)")
    if kind == "dev" and sender != "user":
        kind = "bot"
    if kind == "bot":
        from .message_execution import validate_shape
        validate_shape(sender, body)
    raw_text = body.get("text")
    silent = "text" in body and raw_text is None and kind == "bot"
    if raw_text is not None and not isinstance(raw_text, str):
        raise HTTPException(status_code=400, detail="text must be a string or null")
    text = None if silent else (raw_text or "").strip()
    # 单条消息长度上限为 2000 UTF-16 码元，前后端使用相同计数规则。
    # 前端 composer-text maxlength + 引擎 post_msg 双保险，超限返回明确错误。
    if text is not None and _utf16_len(text) > 2000:
        raise HTTPException(status_code=400,
                            detail="消息超过 2000 字符上限（UTF-16 码元口径）")
    thread_id = body.get("thread_id")
    if thread_id is None and not (body.get("project") or "").strip():
        raise HTTPException(status_code=400, detail="project is required for point-to-point messages; use the source message project")
    project = (body.get("project") or "xjk").strip()

    attachment_ids = body.get("attachment_ids", [])
    if (not isinstance(attachment_ids, list) or len(attachment_ids) > attachments.MAX_IMAGES
            or any(not isinstance(aid, str) for aid in attachment_ids)
            or len(set(attachment_ids)) != len(attachment_ids)):
        raise HTTPException(400, "Use at most four distinct attachment IDs")
    if attachment_ids and (silent or kind == "bot"):
        raise HTTPException(400, "Image sending currently requires a user message")
    if not text and not silent and not attachment_ids:
        raise HTTPException(status_code=400, detail="text is required")

    conn = _conn()
    try:
        # 收口检测：text 以 [收口] 开头且 thread_id 非空
        execution = None
        if kind == "bot":
            from .message_execution import validate
            execution = validate(conn, sender, body)
        if text is not None and text.startswith("[收口]") and thread_id is not None:
            return _settle_thread(conn, sender, text, thread_id, attachment_ids, execution)

        # 话题消息：project 从 thread 取（权威），忽略 body 传的
        if thread_id is not None:
            thr = conn.execute("SELECT project FROM threads WHERE id=?", (thread_id,)).fetchone()
            if thr is not None:
                project = thr["project"]

        # req#83 task#146：斜杠命令分发（落到引擎 API 层，只对 sender=user，bot 的 /xxx 走普通消息）。
        # 命令处理有独立事务（commands 留痕），先分发再走普通路由。命令分支返回即结束，不落普通消息。
        if kind in ("user", "dev") and db.is_builtin_user(sender):
            cmd_result = commands.dispatch(conn, project, sender, "user", text, thread_id)
            if cmd_result is not None:
                events.bus.publish("command.executed", {
                    "command": cmd_result.get("command"),
                    "ok": cmd_result.get("ok"),
                    "project": project, "sender": sender, "thread_id": thread_id,
                })
                return cmd_result

        result = router.route_message(conn, sender, text, "bot" if kind == "bot" else "user", thread_id, project,
                                      attachment_ids=attachment_ids, need_reply=body.get("need_reply", 1), execution=execution)
        if result["message_id"] is not None:
            events.bus.publish("message.new", {"message_id": result["message_id"], "thread_id": thread_id,
                                               "sender": sender, "project": project, "execution": execution})
        if thread_id is not None:
            events.bus.publish("thread.updated", {"thread_id": thread_id})
        return result
    except commands.CommandError as e:
        raise HTTPException(status_code=400, detail=e.message) from e
    except router.RouteError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e
    finally:
        conn.close()


def _settle_thread(conn: sqlite3.Connection, sender: str, text: str, thread_id: int,
                   attachment_ids: list[str] | None = None, execution: dict | None = None) -> dict:
    """收口暂停自动讨论；关闭及结论填写由独立接口处理。"""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    thread = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
    if thread is None:
        raise HTTPException(status_code=404, detail=f"thread {thread_id} not found")
    if thread["status"] == "closed":
        raise HTTPException(status_code=400, detail="话题已关闭")
    if thread["creator"] != sender and not db.is_builtin_user(sender):
        raise HTTPException(status_code=403, detail="只有发起者能收口")

    ts = db.now_iso()
    router.validate_mention_scope(conn, text, thread["project"], json.loads(thread["members"]))
    # 收口消息落库（带 thread_id）
    cur = conn.execute(
        "INSERT INTO messages (project,ts,sender,sender_type,content,mentions,thread_id,round) "
        "VALUES (?, ?, ?, ?, ?, '[]', ?, ?)",
        (thread["project"], ts, sender, "user" if db.is_builtin_user(sender) else "bot", text, thread_id, thread["round"]),
    )
    close_msg_id = cur.lastrowid
    from .message_execution import record
    record(conn, close_msg_id, execution)
    if attachment_ids:
        attachments.bind(conn, attachment_ids, close_msg_id, thread["project"], sender)

    # 暂停自动讨论，结论留到关闭时填写。
    router.settle_thread(conn, thread_id)

    # 该话题 pending 全部 expired（closed_by 指向收口消息）
    conn.execute(
        """
        UPDATE inbox SET status='expired', closed_by=?
        WHERE message_id IN (SELECT id FROM messages WHERE thread_id=?)
          AND status='pending'
        """,
        (close_msg_id, thread_id),
    )
    conn.commit()

    events.bus.publish("thread.updated", {"thread_id": thread_id, "by": sender})
    events.bus.publish("message.new", {"thread_id": thread_id, "sender": sender, "message_id": close_msg_id,
                                       "project": thread["project"], "execution": execution})
    return {"settled": True, "closed": False, "thread_id": thread_id, "message_id": close_msg_id, "close_message_id": close_msg_id}


@router_api.post("/thread")
@mcp_operations.recorded
def post_thread(
    body: dict[str, Any],
    x_group_profile: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    x_group_key: Optional[str] = Header(default=None),
    request: Request = None,
):
    identity, kind = _resolve_identity(x_group_profile, authorization, x_group_key, request)
    creator = body.get("creator", "")
    if kind == "bot":
        creator = identity
    elif kind == "user":
        creator = "user"

    title = (body.get("title") or "").strip()
    members = body.get("members") or []
    opening = (body.get("opening") or "").strip()
    attachment_ids = body.get("attachment_ids", [])
    if (not isinstance(attachment_ids, list) or len(attachment_ids) > attachments.MAX_IMAGES
            or any(not isinstance(aid, str) for aid in attachment_ids)
            or len(set(attachment_ids)) != len(attachment_ids)):
        raise HTTPException(400, "Invalid attachment_ids")
    if attachment_ids:
        if kind not in ("user", "dev"):
            raise HTTPException(403, "Only users can attach opening images")
        creator = "user"
    task_id = body.get("task_id")
    project = (body.get("project") or "xjk").strip()

    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    if not opening and not attachment_ids:
        raise HTTPException(status_code=400, detail="opening or images are required")

    # creator 自动含
    member_list = [str(m) for m in members]
    if creator and creator not in member_list:
        member_list.append(creator)
    request_id = body.get("request_id")
    if request_id is not None and (
        not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128
    ):
        raise HTTPException(400, "request_id must be a non-empty string of at most 128 characters")
    payload = json.dumps([title, opening, member_list, task_id], ensure_ascii=False)
    if attachment_ids:
        payload = json.dumps([title, opening, member_list, task_id, attachment_ids], ensure_ascii=False)

    # 成员必须是注册在 members 表的 bot，且在该项目下启用（req#27 按项目维度）
    # 未知成员直接返回 400，防止写入无法唤醒的成员，
    # 明确报错比静默落库后无法唤醒好。
    conn = _conn()
    try:
        if request_id is not None:
            # Serialize lookup + creation across workers, including concurrent retries.
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute(
                "SELECT payload, thread_id FROM thread_create_requests "
                "WHERE project=? AND creator=? AND request_id=?",
                (project, creator, request_id),
            ).fetchone()
            if prior:
                if prior["payload"] != payload:
                    raise HTTPException(409, "request_id was already used with different topic content")
                return {"thread_id": prior["thread_id"], "members": member_list, "wake": [], "replayed": True}
        registered = {
            r["profile"]
            for r in conn.execute("SELECT profile FROM members")
        }
        registered.add("user")
        unknown = [m for m in member_list if m not in registered]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown members: {unknown}（先在 members 表注册）",
            )
        inactive = [
            m for m in member_list
            if not db.is_builtin_user(m) and not router._profile_active(conn, project, m)
        ]
        if inactive:
            raise HTTPException(
                status_code=400,
                detail=f"members disabled in this project: {inactive}（先在成员管理里为该项目启用）",
            )
        ts = db.now_iso()
        try:
            router.validate_mention_scope(conn, opening, project, member_list)
        except router.RouteError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        cur = conn.execute(
            "INSERT INTO threads (project, title, creator, task_id, members, status, round, created_at, last_active_at) "
            "VALUES (?, ?, ?, ?, ?, 'open', 0, ?, ?)",
            (project, title, creator, task_id, json.dumps(member_list, ensure_ascii=False), ts, ts),
        )
        thread_id = cur.lastrowid
        if request_id is not None:
            conn.execute(
                "INSERT INTO thread_create_requests VALUES (?, ?, ?, ?, ?)",
                (project, creator, request_id, payload, thread_id),
            )

        # 开场消息（thread_id 自动，parent_id=NULL，round=0，depth=0）
        cur2 = conn.execute(
            "INSERT INTO messages (project, ts, sender, sender_type, content, mentions, thread_id, parent_id, round, depth) "
            "VALUES (?, ?, ?, ?, ?, '[]', ?, NULL, 0, 0)",
            (project, ts, creator, "bot" if kind == "bot" else "user", opening, thread_id),
        )
        opening_msg_id = cur2.lastrowid
        if attachment_ids:
            attachments.bind(conn, attachment_ids, opening_msg_id, project, creator)

        # inbox 分发：给 creator 之外的每个成员插行
        for m in member_list:
            if m != creator:
                conn.execute(
                    "INSERT INTO inbox (message_id, profile, status) VALUES (?, ?, 'pending')",
                    (opening_msg_id, m),
                )
        conn.commit()

        # 树模型：开场白后 DFS 找第一个发言者（Round 1 第一个成员），
        # 只唤醒一个，不是全体。
        from . import router as _router
        enqueued = []
        nxt = _router._next_speaker(conn, thread_id)
        if nxt is not None:
            _router._set_turn(conn, thread_id, nxt["profile"], nxt["parent_id"], nxt["round"])
            if wake.enqueue(conn, nxt["profile"], "chat", "thread-open", opening_msg_id, thread_id=thread_id, project=project):
                enqueued.append(nxt["profile"])

        events.bus.publish("thread.created", {"thread_id": thread_id, "creator": creator})
        return {"thread_id": thread_id, "members": member_list, "wake": enqueued}
    finally:
        conn.close()


@router_api.post("/thread/task-update")
@mcp_operations.recorded
def post_thread_task_update(
    body: dict[str, Any],
    x_group_profile: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    x_group_key: Optional[str] = Header(default=None),
    request: Request = None,
):
    identity, kind = _resolve_identity(x_group_profile, authorization, x_group_key, request)
    profile = body.get("profile", "")
    if kind == "bot":
        profile = identity
    thread_id = body.get("thread_id")
    task_id = body.get("task_id")
    fields = body.get("fields") or {}
    credential = body.get("credential")

    if thread_id is None or task_id is None:
        raise HTTPException(status_code=400, detail="thread_id and task_id required")

    conn = _conn()
    try:
        from contextlib import nullcontext
        from . import task_access
        scope = nullcontext()
        if kind == "bot":
            from .access import require_thread
            project = require_thread(conn, profile, thread_id)
            execution = task_access.execution_for(request, None)
            credential = task_access.credential_for(execution, credential)
            scope = task_access.scope(profile, project, "task_update", execution, int(thread_id))
        with scope:
            result = taskqueue_proxy.task_update(conn, profile, int(thread_id), int(task_id), fields,
                                                 credential=credential)
        events.bus.publish("task.updated", result.get("event", {}))
        return result
    except taskqueue_proxy.ProxyError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from e
    finally:
        conn.close()


@router_api.post("/inbox/seen")
@mcp_operations.recorded
def post_inbox_seen(
    body: dict[str, Any],
    x_group_profile: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    x_group_key: Optional[str] = Header(default=None),
    request: Request = None,
):
    identity, kind = _resolve_identity(x_group_profile, authorization, x_group_key, request)
    profile = body.get("profile", "")
    if kind == "bot":
        profile = identity
    message_ids = body.get("message_ids") or []
    thread_id = body.get("thread_id")

    if thread_id is not None:
        _admin_only((identity, kind), "Only the user can mark an opened topic as read")
        if profile not in ("", "user"):
            raise HTTPException(403, "Opening a topic only marks the user's inbox")
        if isinstance(thread_id, bool) or not isinstance(thread_id, int) or thread_id <= 0:
            raise HTTPException(400, "thread_id must be a positive integer")
        through_id = body.get("through_id")
        if through_id is not None and (isinstance(through_id, bool) or
                                      not isinstance(through_id, int) or through_id < 0):
            raise HTTPException(400, "through_id must be a non-negative integer")
        conn = _conn()
        try:
            if not conn.execute("SELECT 1 FROM threads WHERE id=?", (thread_id,)).fetchone():
                raise HTTPException(404, "thread not found")
            cur = conn.execute(
                "UPDATE inbox SET status='seen', seen_at=? WHERE profile=? AND status='pending' "
                "AND message_id IN (SELECT id FROM messages WHERE thread_id=? AND (? IS NULL OR id<=?))",
                (db.now_iso(), db.BUILTIN_USER_PROFILE, thread_id, through_id, through_id),
            )
            conn.commit()
            return {"seen": cur.rowcount}
        finally:
            conn.close()

    if not profile:
        raise HTTPException(status_code=400, detail="profile required")
    if not message_ids:
        raise HTTPException(status_code=400, detail="message_ids required")

    conn = _conn()
    try:
        ts = db.now_iso()
        # ---- req#37 窄闸门：被 @ 的点对点消息，未实质回复前禁止标已读 ----
        # 只拦「thread_id IS NULL 且该 profile 在消息 mentions 里」的场景。
        # 放行条件：该 profile 在 @ 消息之后（ts 严格大于）发过
        #   thread_id IS NULL + sender=profile + mentions 含原发送者 的落库消息。
        # 话题消息不进闸（router 发言即已读兜底，走不到手动 mark_seen）。
        allowed: list[int] = []
        blocked: list[int] = []
        for mid in message_ids:
            msg = conn.execute(
                "SELECT id, project, thread_id, sender, mentions, need_reply FROM messages WHERE id=?",
                (mid,),
            ).fetchone()
            if msg is None:
                continue  # 不存在的消息不拦（照旧行为，不影响其它行）
            # req#63：bot 话题 pending 只由实际发言清理。
            if not db.is_builtin_user(profile) and msg["thread_id"] is not None:
                blocked.append(mid)
                continue
            if db.is_builtin_user(profile) or effective_need_reply(msg, profile) in (0, None):
                allowed.append(mid)
                continue
            original_sender = msg["sender"]
            # 同项目中消息 ID 更大的本人回复，且精确 @ 原发送者。
            replied = conn.execute(
                """
                SELECT 1 FROM messages m, json_each(m.mentions) je
                WHERE m.thread_id IS NULL
                  AND m.sender = ?
                  AND m.id > ?
                  AND m.project = ?
                  AND je.value = ?
                LIMIT 1
                """,
                (profile, msg["id"], msg["project"], original_sender),
            ).fetchone()
            if replied is not None:
                allowed.append(mid)
            else:
                blocked.append(mid)

        # 只标放行的；被拦的保持 pending，扫描器继续唤醒
        marked = 0
        if allowed:
            placeholders = ",".join("?" for _ in allowed)
            cur = conn.execute(
                f"UPDATE inbox SET status='seen', seen_at=? "
                f"WHERE profile=? AND message_id IN ({placeholders}) AND status='pending'",
                (ts, profile, *allowed),
            )
            marked = cur.rowcount
        conn.commit()
        result = {"seen": marked}
        if blocked:
            result["blocked"] = blocked
            result["reason"] = (
                "话题消息须由 bot 发言清理；要求回复的点对点消息须先实质回复。"
                "这些消息保持 pending。"
            )
        return result
    finally:
        conn.close()


@router_api.post("/auth/login")
def post_login(body: dict[str, Any], request: Request, response: Response):
    response.headers["Cache-Control"] = "no-store"
    client = request.client.host if request.client else "unknown"
    if not auth.allow_login(client):
        raise HTTPException(429, "Too many login attempts", headers={"Retry-After": str(auth.LOGIN_WINDOW)})
    result = auth.login(body.get("password", ""))
    if "error" in result:
        raise HTTPException(status_code=401, detail=result["error"])
    response.set_cookie("group_events", result["token"], httponly=True, samesite="strict",
                        secure=request.url.scheme == "https", path="/events",
                        max_age=auth._TOKEN_TTL_SECONDS)
    return result


@router_api.post("/auth/events")
def authorize_events(request: Request, response: Response, identity=Depends(_resolve_identity)):
    response.headers["Cache-Control"] = "no-store"
    _admin_only(identity, "Administrator access required")
    token = request.headers.get("authorization", "")[7:].strip()
    if identity[1] == "user":
        response.set_cookie("group_events", token, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", path="/events")
    return {"ok": True}


@router_api.post("/auth/logout")
def post_logout(response: Response):
    response.delete_cookie("group_events", path="/events")
    return {"ok": True}


@router_api.post("/auth/member-token")
def issue_member_token(body: dict[str, Any], identity=Depends(_resolve_identity)):
    _admin_only(identity, "Administrator access required")
    profile = body.get("profile")
    if not isinstance(profile, str) or not profile.strip():
        raise HTTPException(422, "Registered member profile required")
    profile = profile.strip()
    if db.is_builtin_user(profile):
        raise HTTPException(422, "Registered member profile required")
    conn = _conn()
    try:
        if not conn.execute("SELECT 1 FROM members WHERE profile=?", (profile,)).fetchone():
            raise HTTPException(404, "Unknown member")
        return JSONResponse({"profile": profile, "token": auth.bot_token(profile)},
                            headers={"Cache-Control": "no-store"})
    finally:
        conn.close()


@router_api.get("/auth/status")
def get_auth_status():
    """鉴权模式探测（req#122 task#212）：返回是否配置管理员密码，前端据此初始直进、不先弹登录。

    不泄露密码本身；默认免密（未设 GROUP_ADMIN_PASSWORD）返回 configured=false。
    """
    return {"configured": auth.configured()}


@router_api.get("/discovery/scan")
def get_discovery_scan(project: str = ''):
    """宿主 profile 自动发现（req#87 task#150，设计稿 2.4）。

    扫描 Hermes profiles/ 与 Codex 配置根（CODEX_HOME，默认 ~/.codex），
    产出可发现候选 [{host_type, profile, config_root, display_name, bot_id,
    bound, already_member}]：
    - bound=true：bots 表已绑定该 (host_type, config_root, profile)，添加流程
      直接复用 Bot；false：走注册新 Bot。
    - already_member=true：members 花名册已有该 profile（提示重复添加）。
    - 同名 profile 双宿主靠 host_type+config_root 区分（2.5 第 2 点）。
    扫描不到任何候选时返回空列表（冷启动「未发现可用宿主 profile」文案由
    前端按空列表渲染，不在此报错）。
    """
    conn = _conn()
    try:
        return {"candidates": discovery.scan_candidates(conn, project.strip()), "count": None}
    finally:
        conn.close()


@router_api.post("/bots/register")
def post_register_bot(body: dict[str, Any], identity=Depends(_resolve_identity)):
    """Bot 注册/复用（req#87 task#150：已绑定复用，未绑定注册新 Bot）。

    body：host_type（hermes/codex，必填）、profile（必填）、config_root
    （必填，来自 /discovery/scan 候选）、display_name（可选，仅新注册
    时落库；复用不覆盖——Bot 身份全局统一）、project（可选，带则顺带加入
    当前项目：members 花名册 upsert + project_members 兼容期口径）。
    """
    _admin_only(identity, '仅管理员可注册 Bot 或将 Bot 加入项目')
    host_type = (body.get("host_type") or "").strip()
    profile = (body.get("profile") or "").strip()
    config_root = (body.get("config_root") or "").strip()
    display_name = (body.get("display_name") or "").strip()
    if 'role' in body or 'grants' in body or 'permissions' in body:
        raise HTTPException(400, '角色与权限请在加入后通过项目成员设置配置')
    project = (body.get("project") or "").strip()
    conn = _conn()
    try:
        try:
            reg = discovery.register_bot(conn, host_type, profile, config_root, display_name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if project:
            from . import bot_identity
            if not conn.execute('SELECT 1 FROM projects WHERE name=?', (project,)).fetchone():
                raise HTTPException(404, '项目不存在')
            member = bot_identity.enroll(conn, reg['bot_id'], project)
        else:
            member = None
        if project:
            from .codex_project_access import commit_enrollment
            commit_enrollment(conn, project, [member])
        else:
            conn.commit()
        return {"bot_id": reg["bot_id"], "created": reg["created"],
                "profile": profile, "member_profile": member, "project": project or None}
    finally:
        conn.close()


class ProfileSource(BaseModel):
    model_config = ConfigDict(extra='forbid')
    profile: str = Field(min_length=1, max_length=64)
    config_root: str = Field(min_length=1, max_length=32767)


class ProfileCreateBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    project: str | None = Field(default=None, max_length=200)
    host_type: str = Field(pattern='^(codex|hermes)$')
    profile: str = Field(min_length=1, max_length=64)
    display_name: str = Field(default='', max_length=100)
    model: str = Field(default='', max_length=200, pattern=r'^[^\r\n]*$')
    instructions: str = Field(default='', max_length=32000)
    source: ProfileSource | None = None
    reuse_auth: bool = False
    request_id: str = Field(min_length=1, max_length=64)


@router_api.post('/profiles/create')
def post_create_profile(body: ProfileCreateBody, identity: tuple = Depends(_resolve_identity)):
    _admin_only(identity, '创建 profile 仅限管理员操作')
    from . import profile_create
    conn = _conn()
    try:
        return profile_create.create(conn, body.model_dump())
    finally:
        conn.close()


@router_api.get('/profiles/candidates')
def get_profile_candidates(project: str = '', identity=Depends(_resolve_identity)):
    _admin_only(identity, '成员候选仅限管理员使用')
    conn = _conn()
    try:
        candidates = discovery.scan_candidates(conn, project)
        found = {candidate['bot_id'] for candidate in candidates if candidate['bot_id'] is not None}
        enrolled = {row['bot_id'] for row in conn.execute('SELECT bot_id FROM project_members WHERE project=?', (project,))}
        for row in conn.execute("SELECT b.*, m.profile AS member_profile FROM bots b LEFT JOIN members m ON m.bot_id=b.id WHERE b.profile!='user' ORDER BY b.id"):
            if row['id'] in found or not discovery.candidate_available(row):
                continue
            candidates.append({'bot_id': row['id'], 'host_type': row['host_type'], 'profile': row['profile'],
                               'config_root': row['config_root'], 'display_name': row['display_name'],
                               'member_profile': row['member_profile'], 'bound': True,
                               'already_project_member': row['id'] in enrolled})
        return {'candidates': candidates}
    finally:
        conn.close()


@router_api.post("/members")
def post_member(body: dict[str, Any]):
    """注册/更新一个成员（bot 花名册）。profile 是唯一键。

    req#27：active 字段废弃（启停按项目维度走 project_members）；body 里的
    active 若带 project 则写入 project_members，否则忽略（纯花名册注册）。
    """
    profile = (body.get("profile") or "").strip()
    display_name = (body.get("display_name") or profile).strip()
    if 'role' in body or 'grants' in body or 'permissions' in body:
        raise HTTPException(400, '角色与权限请通过项目成员设置配置')
    project = (body.get("project") or "").strip()
    has_active = "active" in body
    active = 1 if body.get("active", True) else 0

    if not profile:
        raise HTTPException(status_code=400, detail="profile is required")

    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO members (profile, display_name) VALUES (?, ?) "
            "ON CONFLICT(profile) DO UPDATE SET display_name=excluded.display_name",
            (profile, display_name),
        )
        if has_active and project:
            # req#86 task#147 兼容期：项目已有该 profile 行（含 NULL bot_id 兼容行）
            # 直接 UPDATE，避免 (project,bot_id) 主键与既有行双行；无行才反推 Bot 插入。
            existing = conn.execute(
                "SELECT 1 FROM project_members WHERE project=? AND profile=?", (project, profile)
            ).fetchone()
            if existing is not None:
                conn.execute(
                    "UPDATE project_members SET active=? WHERE project=? AND profile=?",
                    (active, project, profile),
                )
            else:
                bot_id = _ensure_bot(conn, profile, display_name, '')
                conn.execute(
                    "INSERT INTO project_members (project, bot_id, profile, active) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(project, bot_id) DO UPDATE SET active=excluded.active, "
                    "profile=excluded.profile",
                    (project, bot_id, profile, active),
                )
        conn.commit()
        return {"profile": profile, "display_name": display_name,
                "active": active if (has_active and project) else None}
    finally:
        conn.close()


@router_api.get("/members")
def get_members(project: str = "", active_only: bool = False, role: str | None = None,
                request: Request = None):
    """花名册（纯身份）。带 project 时每个成员附该项目的启停状态（req#27）。"""
    project = project.strip()
    if active_only and not project:
        raise HTTPException(400, "project is required when active_only=true")
    if role is not None and not project:
        raise HTTPException(400, '角色查询需要指定项目')
    conn = _conn()
    try:
        from .bot_identity import member_views
        rows = member_views(conn)
        members = []
        pm = {}
        if project:
            pm = {
                (r["project"], r["profile"]): dict(r)
                for r in conn.execute("SELECT project, profile, active, role, role_id FROM project_members WHERE project=?", (project,))
            }
        for r in rows:
            if request is not None and request.state.identity[1] == "bot":
                if (project, r["profile"]) not in pm:
                    continue
            d = {"profile": r["profile"], "display_name": r["display_name"], "role": ''}
            d.update(bot_id=r['bot_id'], host_profile=r['host_profile'], runtime=r['runtime'])
            if project:
                # 新成员默认未加入；显式启用后才参与项目。
                key = (project, r["profile"])
                membership = pm.get(key, {})
                d['role'] = membership.get('role', '')
                d['role_id'] = membership.get('role_id')
                d["active"] = membership.get('active', 0) if not db.is_builtin_user(r["profile"]) else 1
            if role is not None and d['role'] != role:
                continue
            if not active_only or d.get("active"):
                members.append(d)
        return {"members": members, "count": len(members)}
    finally:
        conn.close()


@router_api.get('/user/display-name')
def get_user_display_name(identity=Depends(_resolve_identity)):
    _admin_only(identity, '仅用户可读取自己的称呼')
    conn = _conn()
    try:
        row = conn.execute('SELECT display_name FROM bots WHERE profile=?',
                           (db.BUILTIN_USER_PROFILE,)).fetchone()
        return JSONResponse({'profile': db.BUILTIN_USER_PROFILE, 'display_name': row['display_name']},
                            headers={'Cache-Control': 'private, no-store'})
    finally:
        conn.close()


@router_api.patch('/user/display-name')
def set_user_display_name(body: dict[str, Any], identity=Depends(_resolve_identity)):
    _admin_only(identity, '仅用户可修改自己的称呼')
    name = body.get('display_name')
    try:
        valid = (set(body) == {'display_name'} and isinstance(name, str) and bool(name.strip())
                 and _utf16_len(name) <= 200
                 and not any(ord(c) < 32 or 127 <= ord(c) <= 159 or c in '\u2028\u2029' for c in name))
    except UnicodeEncodeError:
        valid = False
    if not valid:
        raise HTTPException(400, '称呼须为 1–200 个字符的单行文字，不能包含控制字符')
    name = name.strip()
    conn = _conn()
    try:
        with conn:
            conn.execute('UPDATE bots SET display_name=? WHERE profile=?', (name, db.BUILTIN_USER_PROFILE))
            conn.execute('UPDATE members SET display_name=? WHERE profile=?', (name, db.BUILTIN_USER_PROFILE))
    finally:
        conn.close()
    events.bus.publish('user.display_name.changed', {'profile': db.BUILTIN_USER_PROFILE})
    return JSONResponse({'profile': db.BUILTIN_USER_PROFILE, 'display_name': name},
                        headers={'Cache-Control': 'private, no-store'})


@router_api.patch('/members/{profile}/display-name')
def set_member_display_name(profile: str, body: dict[str, Any], identity=Depends(_resolve_identity)):
    _admin_only(identity, 'Only administrators can rename a Bot')
    name = body.get('display_name')
    if (set(body) != {'display_name'} or not isinstance(name, str)
            or not name.strip() or _utf16_len(name) > 200
            or any(c in name for c in '\r\n\0')):
        raise HTTPException(400, 'display_name must be non-empty single-line text (up to 200 characters)')
    name = name.strip()
    conn = _conn()
    try:
        with conn:
            row = conn.execute('SELECT b.id,b.display_name,b.profile AS host_profile FROM members m '
                               'JOIN bots b ON b.id=m.bot_id WHERE m.profile=?', (profile,)).fetchone()
            if not row or db.is_builtin_user(profile):
                raise HTTPException(404, 'Bot member not found')
            conn.execute('UPDATE bots SET display_name=? WHERE id=?', (name, row['id']))
            conn.execute('UPDATE members SET display_name=? WHERE bot_id=?', (name, row['id']))
            from .project_workspaces import audit
            for project in conn.execute('SELECT DISTINCT project FROM project_members WHERE bot_id=?', (row['id'],)):
                audit(conn, project['project'], identity[0] or identity[1], 'member.display_name',
                      {'bot_id': row['id'], 'profile': profile, 'before': row['display_name'], 'after': name})
        return {'profile': profile, 'bot_id': row['id'], 'host_profile': row['host_profile'], 'display_name': name}
    finally:
        conn.close()


@router_api.patch('/projects/{name}/members/{profile}/role')
def set_project_member_role(name: str, profile: str, body: dict[str, Any], identity=Depends(_resolve_identity)):
    _admin_only(identity, '仅管理员可设置项目成员角色')
    from . import project_team
    conn = _conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        result = project_team.assign_role(conn, name, profile, body.get('role'), identity[0] or identity[1])
        conn.commit()
        return result
    finally:
        conn.close()


@router_api.get('/projects/{name}/roles')
def get_project_roles(name: str):
    """项目角色列表（req#96 task#163）。角色是项目级实体，每项目一套。"""
    conn = _conn()
    try:
        if not conn.execute('SELECT 1 FROM projects WHERE name=?', (name,)).fetchone():
            raise HTTPException(404, '项目不存在')
        roles = [dict(r) for r in conn.execute(
            'SELECT pr.id, pr.project, pr.name, pr.created_at, '
            '(SELECT COUNT(*) FROM project_members pm WHERE pm.project=pr.project '
            ' AND pm.role_id=pr.id) AS member_count '
            'FROM project_roles pr WHERE pr.project=? ORDER BY pr.id', (name,))]
        from .project_team import mark_owner_role
        mark_owner_role(conn, name, roles)
        return {'roles': roles, 'count': len(roles)}
    finally:
        conn.close()


@router_api.post('/projects/{name}/roles')
def post_project_role(name: str, body: dict[str, Any], identity=Depends(_resolve_identity)):
    """创建项目角色（req#96 task#163）。同名角色 409（UNIQUE(project,name)）。"""
    _admin_only(identity, '仅管理员可管理项目角色')
    from . import project_team
    conn = _conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        result = project_team.create_role(conn, name, body.get('name'), identity[0] or identity[1])
        conn.commit()
        return result
    finally:
        conn.close()


@router_api.patch('/projects/{name}/roles/{role_id}')
def patch_project_role(name: str, role_id: int, body: dict[str, Any], identity=Depends(_resolve_identity)):
    """改名项目角色（req#96 task#163：改 id 稳定，改名后成员 role_id 关联不断）。

    改名同步更新该角色下成员的 role 文本列（保持兼容列与权威关联一致）。
    """
    _admin_only(identity, '仅管理员可管理项目角色')
    new_name = (body.get('name') or '').strip()
    if not new_name or len(new_name) > 100 or any(ord(c) < 32 for c in new_name):
        raise HTTPException(400, 'name 必须是长度不超过 100 的单行文本')
    conn = _conn()
    try:
        row = conn.execute('SELECT * FROM project_roles WHERE id=? AND project=?', (role_id, name)).fetchone()
        if not row:
            raise HTTPException(404, '角色不存在')
        if new_name != row['name'] and conn.execute(
                'SELECT 1 FROM project_roles WHERE project=? AND name=? AND id<>?',
                (name, new_name, role_id)).fetchone():
            raise HTTPException(409, '该项目已存在同名角色')
        conn.execute('UPDATE project_roles SET name=? WHERE id=?', (new_name, role_id))
        # 同步兼容列：role_id 关联的成员 role 文本跟随改名（显示一致）
        conn.execute('UPDATE project_members SET role=? WHERE project=? AND role_id=?',
                     (new_name, name, role_id))
        from .project_workspaces import audit
        audit(conn, name, identity[0] or identity[1], 'role.rename',
              {'role_id': role_id, 'before': row['name'], 'after': new_name})
        conn.commit()
        return {'id': role_id, 'project': name, 'name': new_name}
    finally:
        conn.close()


@router_api.delete('/projects/{name}/roles/{role_id}')
def delete_project_role(name: str, role_id: int, identity=Depends(_resolve_identity)):
    """删除项目角色（req#96 task#163）。

    删除后该角色下成员 role_id 置 NULL（role 文本保留，不静默丢文本数据；
    成员回到「未分配角色」状态，role_id 留 NULL 待重新分配）。
    """
    _admin_only(identity, '仅管理员可管理项目角色')
    conn = _conn()
    try:
        row = conn.execute('SELECT * FROM project_roles WHERE id=? AND project=?', (role_id, name)).fetchone()
        if not row:
            raise HTTPException(404, '角色不存在')
        conn.execute('UPDATE project_members SET role_id=NULL WHERE project=? AND role_id=?',
                     (name, role_id))
        # req#96 task#164：删除角色级联清理该角色在各权限下的授权行（role:<id> 前缀）
        from . import project_permissions
        conn.execute('DELETE FROM project_permissions WHERE project=? AND profile=?',
                     (name, project_permissions._role_grant(role_id)))
        conn.execute('DELETE FROM project_roles WHERE id=?', (role_id,))
        from .project_workspaces import audit
        audit(conn, name, identity[0] or identity[1], 'role.delete',
              {'role_id': role_id, 'name': row['name']})
        conn.commit()
        return {'deleted': True, 'id': role_id}
    finally:
        conn.close()


class ProfileConfigEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    description: str = Field(max_length=100000)
    soul: str = Field(max_length=2 * 1024 * 1024)
    env: str = Field(max_length=2 * 1024 * 1024)
    model: str = Field(max_length=500)
    provider: str = Field(max_length=200)
    reasoning_effort: Optional[str] = Field(default=None, max_length=30)
    member_profile: Optional[str] = Field(default=None, max_length=200)
    config_path: Optional[str] = Field(default=None, max_length=4096)
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


class ProjectModelEdit(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    model: str = Field(default='', max_length=500)
    provider: str = Field(default='', max_length=200)
    reasoning_effort: str = Field(default='', max_length=30)
    revision: str = Field(pattern=r'^[a-f0-9]{64}$')


@router_api.get('/projects/{project}/members/{profile}/model')
def get_project_model(project: str, profile: str, identity=Depends(_resolve_identity)):
    _admin_only(identity, 'Only administrators can read project model settings')
    from . import project_models
    conn = _conn()
    try:
        return JSONResponse(project_models.read(conn, project, profile)[0], headers={'Cache-Control': 'no-store'})
    finally:
        conn.close()


@router_api.put('/projects/{project}/members/{profile}/model')
def put_project_model(project: str, profile: str, body: ProjectModelEdit, identity=Depends(_resolve_identity)):
    _admin_only(identity, 'Only administrators can edit project model settings')
    from . import project_models
    conn = _conn()
    try:
        return project_models.save(conn, project, profile, body.model_dump())
    finally:
        conn.close()


def _profile_editor_access(profile: str, identity: tuple[str, str]) -> None:
    _admin_only(identity, "仅管理员可编辑 profile 配置")
    conn = _conn()
    try:
        if profile == "user" or not conn.execute(
            "SELECT 1 FROM members WHERE profile=?", (profile,)
        ).fetchone():
            raise HTTPException(404, "未找到 bot 成员")
    finally:
        conn.close()


@router_api.get("/members/{profile}/config")
def get_member_config(profile: str, identity=Depends(_resolve_identity)):
    _profile_editor_access(profile, identity)
    values = profile_config.read(profile)
    values['member_profile'] = profile
    return JSONResponse(values, headers={"Cache-Control": "no-store"})


@router_api.put("/members/{profile}/config")
def put_member_config(profile: str, body: ProfileConfigEdit, identity=Depends(_resolve_identity)):
    _profile_editor_access(profile, identity)
    if body.member_profile is not None and body.member_profile != profile:
        raise HTTPException(409, 'Member changed; reopen the editor')
    return profile_config.save(profile, body.model_dump(exclude={'member_profile'}))


@router_api.post("/members/{profile}/active")
def set_member_active(profile: str, body: dict[str, Any], identity=Depends(_resolve_identity)):
    """启停一个成员（req#27：按项目维度，body 必带 project）。

    语义（req bot-group#23「跳过不唤醒、不踢出」 + req#27 按项目维度）：
    - 写 project_members(project, profile, active)；缺 project → 400（不再有全局启停）；
    - 停用（active=0）：作废该 profile 在该项目的 pending inbox + 即时推进它在该项目
      占着的 turn（规格 27.3 第 4/5 点，project 作用域）；
    - `threads.members` 与历史发言一律不动（停用≠除名）。
    """
    _admin_only(identity, '仅管理员可设置项目成员启停')
    active = 1 if body.get("active", True) else 0
    project = (body.get("project") or "").strip()
    if not project:
        raise HTTPException(
            status_code=400,
            detail="project is required（启停状态按项目维度，req#27）",
        )
    conn = _conn()
    try:
        if not conn.execute("SELECT 1 FROM members WHERE profile=?", (profile,)).fetchone():
            raise HTTPException(status_code=404, detail=f"member {profile} not found")
        previous = conn.execute('SELECT active FROM project_members WHERE project=? AND profile=?',
                                (project, profile)).fetchone()
        # req#86 task#147 兼容期：按 profile 定位既有行直接 UPDATE（含 NULL bot_id 的
        # 兼容行），避免 (project, bot_id) 主键与既有行并存出现同 profile 双行。
        # profile 在该项目无行时才反推 Bot 走 bot_id 插入。
        if previous is not None:
            conn.execute(
                "UPDATE project_members SET active=? WHERE project=? AND profile=?",
                (active, project, profile),
            )
        else:
            bot_id = _ensure_bot(conn, profile, "", "")
            conn.execute(
                "INSERT INTO project_members (project, bot_id, profile, active) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(project, bot_id) DO UPDATE SET active=excluded.active, "
                "profile=excluded.profile",
                (project, bot_id, profile, active),
            )
        from .project_workspaces import audit
        audit(conn, project, identity[0] or identity[1], 'member.active',
              {'profile': profile, 'before': previous['active'] if previous else 0, 'after': active})
        conn.commit()
        advanced, expired = 0, 0
        if not active:
            # 停用前已积压的 pending 一并作废（规格 27.3 第 5 点，项目作用域）：
            # 只作废该项目消息产生的未读（跨项目的点对点未读不动）。
            expired = conn.execute(
                "UPDATE inbox SET status='expired' WHERE profile=? AND status='pending' "
                "AND message_id IN (SELECT id FROM messages WHERE project=?)",
                (profile, project),
            ).rowcount
            conn.commit()
            # 若它正占着该项目某 open 话题的 turn，当场推进（不等 TURN_TIMEOUT）
            advanced = router.advance_past_profile(conn, profile, project)
        return {"profile": profile, "project": project, "active": active,
                "advanced_topics": advanced, "expired_inbox": expired}
    finally:
        conn.close()


@router_api.post("/heartbeat")
def post_heartbeat(body: dict[str, Any], identity=Depends(_resolve_identity)):
    """MCP server 续租心跳：记录该 profile 的 agent 循环仍在跑（= 正在思考）。

    身份由成员凭据校验，心跳只更新该成员。
    body.thread_id（可选）：chat 唤醒的 MCP 进程带上（wake 注入 HERMES_WAKE_THREAD_ID），
    /thinking 按 thread 归属返回——前端思考条免轮询 /queue 过滤。
    """
    from . import heartbeat
    if identity[1] != "bot":
        raise HTTPException(403, "Heartbeat requires a member credential")
    profile = identity[0]
    if not profile:
        raise HTTPException(status_code=400, detail="profile required")
    thread_id = body.get("thread_id")
    pid = body.get("pid")
    heartbeat.beat(
        profile,
        thread_id=int(thread_id) if thread_id is not None else None,
        pid=int(pid) if pid is not None else None,
    )
    return {"ok": True, "profile": profile}


@router_api.get("/thinking")
def get_thinking():
    """返回 TTL 内仍在心跳的 profile（= 正在思考的 bot），带话题归属。

    thinking 包含 profile/thread_id/pid；能准确关联登记运行时附 session/wake_started_at。
    缺少运行身份的心跳仅用于基本活跃信息，不为某个气泡保活。
    """
    from . import heartbeat
    entries = heartbeat.active_entries(include_pid=True)
    conn = _conn()
    try:
        for entry in entries:
            # PID is the recorded execution identity, not the latest profile wake.
            rows = conn.execute('SELECT session,started_at,project FROM wake_runs WHERE pid=? AND ended_at IS NULL',
                                (entry['pid'],)).fetchall()
            matches = [r for r in rows if r['session'].startswith(entry['profile'] + '-') and
                       r['session'].endswith('-work' if entry['thread_id'] is None else
                                             f"-chat-{entry['thread_id']}")]
            if len(matches) == 1:
                session = matches[0]['session']
                # req#72 中危：project 直读 wake_runs.project（spawn 时落库），
                # 不再从 session 字符串切段反推——项目名带横杠（如 bot-group）会切错。
                entry.update(session=session, wake_started_at=matches[0]['started_at'],
                             project=matches[0]['project'],
                             spawned_at=datetime.fromtimestamp(matches[0]['started_at']).isoformat())
        runs = {(r['session'], r['wake_started_at']): r for r in wake.active_runs()}
        for entry in entries:
            if 'session' in entry:
                runs.setdefault((entry['session'], entry['wake_started_at']), entry)
        return {"thinking": entries, "runs": list(runs.values())}
    finally:
        conn.close()


@router_api.get("/reasoning")
def get_reasoning(profile: str, session: str, wake_started_at: float, after_id: int = 0, wake_ended_at: Optional[float] = None):
    """读 bot 的思考过程（reasoning + tool_calls），供前端思考气泡展示。

    profile：bot 的 profile 名；session：会话名（session_name() 派生，精确锁定会话）；
    after_id：增量拉取游标（只返回 id > after_id 的新条目）。

    返回 {entries: [{id, role, reasoning, tool_calls, tool_name, timestamp}], latest_id}。
    """
    from . import reasoning
    profile = (profile or "").strip()
    session = (session or "").strip()
    if not profile or not session:
        raise HTTPException(status_code=400, detail="profile and session required")
    conn = _conn()
    try:
        run = conn.execute("SELECT ended_at,exit_code,runtime_session_id,runtime FROM wake_runs WHERE session=? AND started_at=?",
                           (session, wake_started_at)).fetchone()
    finally:
        conn.close()
    ended_at = run["ended_at"] if run else None
    if ended_at is not None:
        wake_ended_at = min(wake_ended_at, ended_at) if wake_ended_at is not None else ended_at
    options = {}
    if run and run['runtime'] == 'codex':
        from . import codex_reasoning
        result = codex_reasoning.read(session, wake_started_at, after_id, wake_ended_at)
    elif run and run['runtime_session_id']:
        from . import session_adapter
        options['hermes_session_id'] = session_adapter.compression_tip(profile, run['runtime_session_id'])
        result = reasoning.read_reasoning(profile, session, after_id, wake_started_at=wake_started_at, wake_ended_at=wake_ended_at, **options)
    else:
        result = reasoning.read_reasoning(profile, session, after_id, wake_started_at=wake_started_at, wake_ended_at=wake_ended_at, **options)
    result["process_ended_at"] = ended_at
    result["exit_code"] = run["exit_code"] if run else None
    # Read links after Hermes entries: observing a final assistant row must also
    # observe every send committed before it, including the last HTTP reply.
    from .message_execution import replies
    conn = _conn()
    try:
        result["replies"] = replies(conn, session, wake_started_at)
    finally:
        conn.close()
    return result


@router_api.post("/approval/{task_id}/resolve")
def resolve_approval(task_id: int, body: dict[str, Any]):
    """用户审批放行或拒绝一个 decision 类 blocked 任务。

    body: {action: 'approve'|'reject', project?}
    approve → 引擎发 approved 信号重触发（同一 work 会话名断点续）
    reject → 任务保持 blocked，由用户后续处理
    """
    action = (body.get("action") or "").strip()
    project = (body.get("project") or "xjk").strip()

    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="action must be 'approve' or 'reject'")

    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        task = conn.execute(
            "SELECT * FROM tasks WHERE id=? AND project=?", (task_id, project)
        ).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        from .requirement_lifecycle import check_task
        inactive = check_task(conn, task)
        if inactive:
            raise HTTPException(409, inactive)
        if task["status"] != "blocked" or task["block_kind"] != "decision":
            raise HTTPException(status_code=400, detail="task is not a decision-blocked task")

        if action == "approve":
            # 放行：任务回 pending，next_bot 保持（bot 重新 claim），发 approved 信号重触发
            conn.execute(
                "UPDATE tasks SET status='pending', block_kind='' WHERE id=?",
                (task_id,),
            )
            conn.commit()
            # 重触发：approved 信号 + 同一 work 会话名（断点续）
            from . import wake
            assignee = task["claimed_by"] or task["next_bot"]
            result = (wake.deliver_approved(assignee, task_id, project)
                      if assignee and router._profile_active(conn, project, assignee)
                      else {'ok': False, 'detail': '任务接手人尚未加入项目或已停用'})
            return {"resolved": "approved", "task_id": task_id, "wake": result}
        else:
            # 拒绝：保持 blocked，将 block_kind 设为 rejected，退出待审队列。
            conn.execute(
                "UPDATE tasks SET block_kind='rejected' WHERE id=?", (task_id,)
            )
            conn.commit()
            return {"resolved": "rejected", "task_id": task_id}
    finally:
        conn.close()


@router_api.get("/wake/check")
def wake_check(profile: str):
    """MCP server 执行类工具的运行时校验入口（防误开工引擎侧硬约束）。

    返回 {chat_active: bool}——True 表示该 profile 当前处于 chat（话题）唤醒
    运行窗口，task_claim/task_log/task_complete/task_block 必须拒绝。
    MCP server 每次执行类工具调用前查询；不可达时 fail-open（放行），
    因为正常 work 通道下引擎也可能短暂不可用，硬 fail-close 会误伤干活。
    """
    from . import wake as _wake
    return {"chat_active": _wake.is_chat_active(profile)}


# --------------------------------------------------------------------------
# 读
# --------------------------------------------------------------------------

@router_api.get("/inbox")
def get_inbox(
    profile: str,
    limit: int = 1,
    kind: str = "",
    thread_id: Optional[int] = None,
    x_group_profile: Optional[str] = Header(default=None),
    project: str = "",
    after_id: int = 0,
    upper_id: Optional[int] = None,
    request: Request = None,
):
    if x_group_profile:
        profile = x_group_profile.strip()
    conn = _conn()
    try:
        # kind 过滤：work=点对点（thread_id IS NULL），chat=话题（thread_id IS NOT NULL）
        # 不传 kind 时优先返回点对点消息。
        params: list = [profile]
        # req#82 退化分页：upper_id 非空 = 退化纯信号路径（I2/R1），按固定上界内
        # 仍有效的 pending 分页读取，游标 after_id（after_id < message_id <= upper_id）。
        # 分页只允许 work 退化路径使用（chat 单 thread 全量现场，无需分页）。
        paginated = upper_id is not None
        if paginated:
            if kind != "work":
                raise HTTPException(status_code=400, detail="paginated inbox requires kind=work")
            if project.strip():
                project = project.strip()
            else:
                raise HTTPException(status_code=400, detail="work inbox requires project")
            kind_clause = "AND m.thread_id IS NULL AND m.project = ?"
            params.append(project)
            order = "m.id ASC"
        elif kind == "work":
            project = project.strip()
            if not project:
                raise HTTPException(status_code=400, detail="work inbox requires project")
            kind_clause = "AND m.thread_id IS NULL AND m.project = ?"
            params.append(project)
            order = "m.id ASC"
        elif kind == "chat":
            # MCP 从会话名传入准确身份；校验失败必须报错，禁止猜测读取范围。
            if thread_id is None:
                raise HTTPException(status_code=400, detail="chat inbox requires thread_id")
            tid = thread_id
            t_row = conn.execute(
                "SELECT status FROM threads WHERE id=?", (tid,)
            ).fetchone()
            if t_row is None:
                raise HTTPException(status_code=404, detail=f"thread {tid} does not exist")
            if t_row["status"] != "open":
                raise HTTPException(status_code=409, detail=f"thread {tid} is not open")
            active = conn.execute(
                "SELECT 1 FROM wake_jobs WHERE profile=? AND kind='chat' "
                "AND thread_id=? AND chat_context_until IS NOT NULL "
                "AND chat_context_until > ? LIMIT 1",
                (profile, tid, db.now_iso()),
            ).fetchone()
            if active is None:
                raise HTTPException(status_code=409, detail=f"no active chat window for {profile} in thread {tid}")
            kind_clause = "AND m.thread_id = ?"
            params.append(tid)
            order = "m.id ASC"
        else:
            kind_clause = ""
            order = "(m.thread_id IS NOT NULL) ASC, m.id ASC"  # 点对点优先

        # 退化分页游标：after_id < message_id <= upper_id（R1 固定上界），message_id ASC。
        if paginated:
            kind_clause += " AND m.id > ? AND m.id <= ?"
            params.append(after_id)
            params.append(upper_id)

        access_scope = ""
        if request is not None and request.state.identity[1] == "bot":
            access_scope = (
                " AND EXISTS (SELECT 1 FROM project_members pm JOIN projects p ON p.name=pm.project "
                "WHERE pm.project=m.project AND pm.profile=i.profile AND pm.active=1 "
                "AND p.archived=0 AND p.removed=0)"
                " AND (m.thread_id IS NULL OR EXISTS (SELECT 1 FROM threads t,json_each(t.members) tm "
                "WHERE t.id=m.thread_id AND tm.value=i.profile))"
            )
            kind_clause += access_scope
        params.append(max(1, min(limit, 100)))
        rows = conn.execute(
            f"""
            SELECT i.id AS inbox_id, i.status, m.id AS message_id, m.ts, m.sender,
                   m.sender_type, m.content, m.mentions, m.thread_id, m.project, m.need_reply
            FROM inbox i
            JOIN messages m ON m.id = i.message_id
            WHERE i.profile = ? AND i.status = 'pending' {kind_clause}
            ORDER BY {order}
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        # 字段级标注（ENGINEERING.md 5.7 实现注承诺，自查补齐）：
        # mentioned_you——该消息是否@了此 profile；pending_total/pending_p2p/pending_thread——
        # 未读全量与分计数，bot 拿到现场即知后面压着几条活几条话，自主决定处理节奏
        msgs = []
        for r in rows:
            d = dict(r)
            try:
                mentions = json.loads(d.get("mentions") or "[]")
            except Exception:  # noqa: BLE001
                mentions = []
            d["mentioned_you"] = profile in mentions
            d["need_reply"] = effective_need_reply(d, profile)
            msgs.append(d)
        count_scope = (" AND m.project=?" if kind == "work" else "") + access_scope
        count_params = (profile, project) if kind == "work" else (profile,)
        p2p = conn.execute(
            "SELECT COUNT(*) AS n FROM inbox i JOIN messages m ON m.id=i.message_id "
            "WHERE i.profile=? AND i.status='pending' AND m.thread_id IS NULL" + count_scope,
            count_params,
        ).fetchone()["n"]
        thr = conn.execute(
            "SELECT COUNT(*) AS n FROM inbox i JOIN messages m ON m.id=i.message_id "
            "WHERE i.profile=? AND i.status='pending' AND m.thread_id IS NOT NULL" + count_scope,
            count_params,
        ).fetchone()["n"]
        # req#82 退化分页（I2/R1）：分页模式下 has_more = 固定上界内、当前页之后是否
        # 还有仍有效的 pending。读完所有页只证明读取流程完成，bot 统一处理后再 mark_seen。
        has_more = False
        if paginated:
            last_id = msgs[-1]["message_id"] if msgs else after_id
            has_more = conn.execute(
                "SELECT 1 FROM inbox i JOIN messages m ON m.id=i.message_id "
                "WHERE i.profile=? AND i.status='pending' AND m.thread_id IS NULL "
                "AND m.project=? AND m.id > ? AND m.id <= ? LIMIT 1",
                (profile, project, last_id, upper_id),
            ).fetchone() is not None
        # agents.md 不再经 /inbox 返回：方案B终案（07e6d5f）后 agents.md 只走
        # --in 项目root 原生加载进 system prompt，bot 不再 read_file 塞历史。
        result = {
            "profile": profile,
            "kind": kind or "all",
            "messages": attachments.enrich(conn, msgs),
            "count": len(msgs),
            "pending_total": p2p if kind == "work" else p2p + thr,
            "pending_p2p": p2p,
            "pending_thread": 0 if kind == "work" else thr,
        }
        if paginated:
            result["has_more"] = has_more
            result["upper_id"] = upper_id
            result["after_id"] = last_id
        return result
    finally:
        conn.close()


@router_api.get("/thread")
def get_thread(id: int, profile: str = "", x_group_profile: Optional[str] = Header(default=None)):
    if x_group_profile:
        profile = x_group_profile.strip()
    conn = _conn()
    try:
        thread = conn.execute("SELECT * FROM threads WHERE id = ?", (id,)).fetchone()
        if thread is None:
            raise HTTPException(status_code=404, detail=f"thread {id} not found")
        msgs = conn.execute(
            "SELECT * FROM messages WHERE thread_id = ? ORDER BY id ASC", (id,)
        ).fetchall()

        # 请求者的已读水位线 + 最新 seq
        watermark = None
        if profile:
            wm = conn.execute(
                """
                SELECT MAX(m.id) AS wm FROM inbox i
                JOIN messages m ON m.id = i.message_id
                WHERE i.profile = ? AND m.thread_id = ? AND i.status = 'seen'
                """,
                (profile, id),
            ).fetchone()
            watermark = wm["wm"] if wm else None

        latest_seq = msgs[-1]["id"] if msgs else None
        if profile:
            msgs = [dict(msg) for msg in msgs]
            for msg in msgs:
                msg["need_reply"] = effective_need_reply(msg, profile)
        return {
            "thread": dict(thread),
            "messages": attachments.enrich(conn, msgs),
            "watermark": watermark,
            "latest_seq": latest_seq,
        }
    finally:
        conn.close()


@router_api.get("/threads")
def get_threads(include_hidden: bool = False, request: Request = None):
    """List topics; all_responded uses the scheduler's all-pass/skipped condition.

    Hidden topics are included only when include_hidden=True.
    """
    conn = _conn()
    try:
        # 默认 hidden=1 的话题不返回（隐藏=不显示，有新消息时 router 已置回 hidden=0）
        # include_hidden=True 时返回全部，前端自己按 hidden 字段分组
        if include_hidden:
            rows = conn.execute("SELECT * FROM threads ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM threads WHERE hidden=0 ORDER BY id DESC"
            ).fetchall()
        threads = []
        for r in rows:
            d = dict(r)
            if request is not None and request.state.identity[1] == "bot":
                from .access import require_project
                profile = request.state.identity[0]
                if profile not in json.loads(d["members"]):
                    continue
                try:
                    require_project(conn, profile, d["project"])
                except HTTPException:
                    continue
            d["first_message_id"] = conn.execute(
                "SELECT MIN(id) FROM messages WHERE thread_id = ?", (d["id"],)
            ).fetchone()[0]
            d["all_responded"] = False
            if d.get("status") == "open":
                try:
                    members = json.loads(d.get("members") or "[]")
                except Exception:  # noqa: BLE001
                    members = []
                bots = [m for m in members if not db.is_builtin_user(m)]
                if bots:
                    d["all_responded"] = router._next_speaker(conn, d["id"]) is None
            threads.append(d)
        return {"threads": threads, "count": len(threads)}
    finally:
        conn.close()


@router_api.post("/thread/{thread_id}/members")
def post_thread_members(thread_id: int, body: dict[str, Any]):
    """话题成员增删（需求1a）。body: {add: [profile], remove: [profile]}。

    - add：补齐历史未读，按话题发言轮次唤醒（chat）
    - remove：删 bot，清它在该话题的 pending inbox 行
    """
    add = body.get("add") or []
    remove = body.get("remove") or []

    conn = _conn()
    try:
        # Serialize the membership snapshot and all associated inbox changes.
        conn.execute("BEGIN IMMEDIATE")
        thread = conn.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if thread is None:
            raise HTTPException(status_code=404, detail=f"thread {thread_id} not found")
        if thread["status"] == "closed":
            raise HTTPException(status_code=400, detail="话题已关闭，不能改成员")

        members = json.loads(thread["members"] or "[]")
        members = [str(m) for m in members]

        # 校验新增成员是注册的 bot，且在该话题所属项目下启用（req#27 按项目维度）
        registered = {r["profile"] for r in conn.execute("SELECT profile FROM members")}
        registered.add("user")
        for m in add:
            if m not in registered:
                raise HTTPException(status_code=400, detail=f"unknown member: {m}")
        inactive = [
            m for m in add
            if not db.is_builtin_user(m) and not router._profile_active(conn, thread["project"], m)
        ]
        if inactive:
            raise HTTPException(
                status_code=400,
                detail=f"members disabled in this project: {inactive}（先在成员管理里为该项目启用）",
            )

        added = []
        for m in add:
            m = str(m)
            if m not in members:
                members.append(m)
                added.append(m)

        removed = []
        for m in remove:
            m = str(m)
            if m in members and m != thread["creator"]:
                members.remove(m)
                removed.append(m)

        conn.execute(
            "UPDATE threads SET members=? WHERE id=?",
            (json.dumps(members, ensure_ascii=False), thread_id),
        )
        # New members receive the existing context; existing read states survive rejoining.
        enqueued = []
        for m in added:
            if db.is_builtin_user(m) or m not in members:
                continue
            conn.execute(
                "INSERT INTO inbox(message_id,profile,status) "
                "SELECT id,?,? FROM messages m WHERE thread_id=? AND sender!=? AND retract_by IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM inbox i WHERE i.message_id=m.id AND i.profile=? "
                "AND i.status IN ('pending','seen'))",
                (m, 'pending' if thread['status'] == 'open' else 'expired', thread_id, m, m),
            )
        # 删人：清它在该话题的 pending inbox 行
        for m in removed:
            conn.execute(
                "UPDATE inbox SET status='expired' WHERE profile=? AND status='pending' "
                "AND message_id IN (SELECT id FROM messages WHERE thread_id=?)",
                (m, thread_id),
            )
        conn.commit()

        for m in added:
            if (not db.is_builtin_user(m) and m in members and thread['status'] == 'open' and
                    thread['turn_profile'] == m and
                    wake.enqueue(conn, m, "chat", "thread-member-add", thread_id=thread_id, project=thread["project"])):
                enqueued.append(m)

        return {"thread_id": thread_id, "members": members, "added": added, "removed": removed, "wake": enqueued}
    finally:
        conn.close()


@router_api.post("/thread/{thread_id}/close")
def post_thread_close(thread_id: int, body: dict[str, Any]):
    """用户关闭话题并填写结论，关闭后禁止继续发言。"""
    conclusion = (body.get("conclusion") or "").strip()

    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        thread = conn.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if thread is None:
            raise HTTPException(status_code=404, detail=f"thread {thread_id} not found")
        if thread["status"] == "closed":
            raise HTTPException(status_code=400, detail="话题已关闭")

        ts = db.now_iso()
        # 保存用户关闭话题的消息，sender=user。
        text = f"[关闭] {conclusion}" if conclusion else "[关闭]"
        cur = conn.execute(
            "INSERT INTO messages (project, ts, sender, sender_type, content, mentions, thread_id) "
            "VALUES (?, ?, ?, 'user', ?, '[]', ?)",
            (thread["project"], ts, db.BUILTIN_USER_PROFILE, text, thread_id),
        )
        close_msg_id = cur.lastrowid

        router.settle_thread(conn, thread_id)
        conn.execute(
            "UPDATE threads SET status='closed', closed_at=?, close_conclusion=? WHERE id=?",
            (ts, conclusion, thread_id),
        )
        # 该话题 pending 全部 expired
        conn.execute(
            "UPDATE inbox SET status='expired', closed_by=? "
            "WHERE message_id IN (SELECT id FROM messages WHERE thread_id=?) AND status='pending'",
            (close_msg_id, thread_id),
        )
        conn.commit()
        events.bus.publish("thread.closed", {"thread_id": thread_id, "by": "user"})
        events.bus.publish("message.new", {"thread_id": thread_id, "sender": "user", "message_id": close_msg_id})
        return {"closed": True, "thread_id": thread_id, "close_message_id": close_msg_id}
    finally:
        conn.close()


@router_api.post("/thread/{thread_id}/settle")
def post_thread_settle(thread_id: int, body: dict[str, Any]):
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        return _settle_thread(conn, "user", "[收口]", thread_id)
    finally:
        conn.close()


@router_api.post("/thread/{thread_id}/reopen")
def post_thread_reopen(thread_id: int):
    conn = _conn()
    try:
        changed = conn.execute("UPDATE threads SET status='settled',closed_at=NULL WHERE id=? AND status='closed'", (thread_id,)).rowcount
        if not changed:
            raise HTTPException(status_code=400, detail="只有已关闭话题需要重新打开。")
        conn.commit()
        events.bus.publish("thread.updated", {"thread_id": thread_id})
        return {"status": "settled", "thread_id": thread_id}
    finally:
        conn.close()


@router_api.post("/thread/{thread_id}/hide")
def post_thread_hide(thread_id: int, body: dict[str, Any]):
    """隐藏/取消隐藏话题（需求1d）。body: {hidden: bool}。有新消息时自动置回 hidden=0。"""
    hidden = 1 if body.get("hidden", True) else 0

    conn = _conn()
    try:
        thread = conn.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if thread is None:
            raise HTTPException(status_code=404, detail=f"thread {thread_id} not found")
        conn.execute("UPDATE threads SET hidden=? WHERE id=?", (hidden, thread_id))
        conn.commit()
        return {"thread_id": thread_id, "hidden": bool(hidden)}
    finally:
        conn.close()


@router_api.post("/message/{message_id}/retract")
def post_message_retract(message_id: int, body: dict[str, Any],
                         x_group_profile: Optional[str] = Header(default=None),
                         authorization: Optional[str] = Header(default=None),
                         x_group_key: Optional[str] = Header(default=None),
                         request: Request = None):
    """撤回一条消息（req#41 task#78）：发错的消息可撤回，不物理删、只留痕。

    权限：只许 sender 本人 / user 撤回（bot 不能撤别人的发言，他人 403）。
    留痕：写 messages.retract_by / retract_at（标记列，绝不 UPDATE content——
    append-only 红线：本表只 INSERT，撤回是标记不是改内容）。
    副作用：该消息造成的 pending inbox 行作废（已 @ 的 bot 不再被唤醒处理这条）。
    """
    identity, kind = _resolve_identity(x_group_profile, authorization, x_group_key, request)
    requester = identity if kind == "bot" else "user"

    conn = _conn()
    try:
        msg = conn.execute(
            "SELECT id, sender, thread_id, retract_by FROM messages WHERE id=?", (message_id,)
        ).fetchone()
        if msg is None:
            raise HTTPException(status_code=404, detail=f"message {message_id} not found")
        if msg["retract_by"] is not None:
            raise HTTPException(status_code=400, detail="消息已撤回")
        # 权限：本人（sender==requester）或 user；他人 403
        if requester != msg["sender"] and not db.is_builtin_user(requester):
            raise HTTPException(status_code=403, detail="只有发送者本人或用户能撤回这条消息")
        ts = db.now_iso()
        conn.execute(
            "UPDATE messages SET retract_by=?, retract_at=? WHERE id=?",
            (requester, ts, message_id),
        )
        # 作废这条消息造成的 pending inbox 行（已 @ 的 bot 不再被唤醒）
        conn.execute(
            "UPDATE inbox SET status='expired', closed_by=? "
            "WHERE message_id=? AND status='pending'",
            (message_id, message_id),
        )
        conn.commit()
        # req#51 task#94：事件自包含撤回者/时间——前端按 message_id 就地 patch 需要
        # 这两个值，不能只发 id 让前端假占位或重拉（群总览 since_id 增量拉不到已加载消息）。
        events.bus.publish("message.retracted", {
            "message_id": message_id, "retract_by": requester, "retract_at": ts,
        })
        if msg["thread_id"] is not None:
            events.bus.publish("thread.updated", {"thread_id": msg["thread_id"]})
        return {"message_id": message_id, "retracted": True,
                "retract_by": requester, "retract_at": ts}
    finally:
        conn.close()


@router_api.get("/messages")
def get_messages(since_id: int = 0, thread_id: Optional[int] = None,
                 before_id: Optional[int] = None,
                 limit: Optional[int] = Query(default=None, ge=1, le=500),
                 project: Optional[str] = None):
    conn = _conn()
    try:
        if limit is not None or project is not None:
            clauses, params = ["id > ?"], [since_id]
            if project is not None:
                clauses.append("project = ?")
                params.append(project)
            if thread_id is not None:
                clauses.append("thread_id = ?")
                params.append(thread_id)
            if before_id is not None:
                clauses.append("id < ?")
                params.append(before_id)
            descending = limit is not None and since_id == 0
            order = "DESC" if descending else "ASC"
            page_clause = " LIMIT ?" if limit is not None else ""
            rows = conn.execute(
                f"SELECT * FROM messages WHERE {' AND '.join(clauses)} ORDER BY id {order}{page_clause}",
                (*params, limit + 1) if limit is not None else params,
            ).fetchall()
            has_more = limit is not None and len(rows) > limit
            if limit is not None:
                rows = rows[:limit]
            if descending:
                rows.reverse()
            return {"messages": attachments.enrich(conn, rows), "count": len(rows),
                    "has_more": has_more}
        if thread_id is not None:
            rows = conn.execute(
                "SELECT * FROM messages WHERE thread_id = ? AND id > ? ORDER BY id ASC",
                (thread_id, since_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM messages WHERE id > ? ORDER BY id ASC", (since_id,)
            ).fetchall()
        return {"messages": attachments.enrich(conn, rows), "count": len(rows)}
    finally:
        conn.close()


@router_api.get("/inbox/summary")
def get_inbox_summary():
    conn = _conn()
    try:
        rows = conn.execute(
            """
            SELECT i.profile, COUNT(*) AS pending,
                   MIN(m.ts) AS earliest
            FROM inbox i
            JOIN messages m ON m.id = i.message_id
            WHERE i.status = 'pending'
            GROUP BY i.profile
            ORDER BY pending DESC
            """
        ).fetchall()
        threads = conn.execute(
            "SELECT m.thread_id, i.profile, COUNT(*) AS pending FROM inbox i "
            "JOIN messages m ON m.id=i.message_id "
            "WHERE i.status='pending' AND m.thread_id IS NOT NULL "
            "GROUP BY m.thread_id, i.profile"
        ).fetchall()
        return {"summary": [dict(r) for r in rows], "threads": [dict(r) for r in threads]}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 项目管理路由：将 HTTP 请求连接到 taskpool 的项目管理接口。
# ---------------------------------------------------------------------------
@router_api.get("/projects")
def get_projects(include_archived: bool = False):
    """项目列表。默认活跃项目（chip/切换器）；include_archived=1 全部（管理弹窗含恢复入口）。"""
    from . import taskpool
    return taskpool.project_list(include_archived=include_archived)


@router_api.post("/projects")
def post_project(body: dict[str, Any], identity=Depends(_resolve_identity)):
    """登记已有目录，保持文件原样；省略 name 时自动生成稳定 ID。"""
    from . import taskpool
    for field in ('name', 'root', 'display_name', 'description', 'directory_mode', 'worktree_root'):
        if field in body and not isinstance(body[field], str):
            raise HTTPException(400, f'{field} 必须是文本')
    name = (body.get("name") or "").strip()
    root = (body.get("root") or "").strip()
    if not name and not (body.get('display_name') or '').strip():
        raise HTTPException(status_code=400, detail="display_name is required")
    if not root:
        raise HTTPException(status_code=400, detail="root is required")
    from . import project_workspaces as ws
    _admin_only(identity, '仅管理员可管理项目')
    ws.validate_config(body)
    result = taskpool.project_add(name, root, description=body.get("description"), owner_profile=body.get('owner_profile'),
                                 display_name=body.get('display_name'), directory_mode=body.get('directory_mode', 'shared'),
                                 worktree_root=body.get('worktree_root', ''), actor=identity[0] or identity[1])
    if 'error' in result:
        raise HTTPException(status_code=400, detail=result['error'])
    return result


@router_api.post('/projects/{name}/remove')
def remove_project(name: str, identity=Depends(_resolve_identity)):
    _admin_only(identity, '仅管理员可移除项目')
    from . import project_removal
    return project_removal.change(name, True, identity[0] or identity[1])


@router_api.post("/projects/{name}/archive")
def post_project_archive(name: str, body: dict[str, Any] = None,
                         identity: tuple[str, str] = Depends(_resolve_identity)):
    """归档/取消归档项目（archived=true 不在 UI 展示）。"""
    _admin_only(identity, "project archive requires user identity")
    from . import taskpool
    archived = True if not body else bool(body.get("archived", True))
    result = taskpool.project_archive(name, archived)
    if 'error' in result:
        raise HTTPException(409, result['error'])
    return result


@router_api.post('/projects/import/preview')
def preview_project_import(body: dict[str, Any], identity=Depends(_resolve_identity)):
    _admin_only(identity, '仅管理员可预览已有项目导入')
    from . import project_import
    return project_import.preview(body)


@router_api.post('/projects/import/prepare')
def prepare_project_import(body: dict[str, Any], identity=Depends(_resolve_identity)):
    _admin_only(identity, '仅管理员可清理初始化残留并准备项目目录')
    from . import project_setup_history
    return project_setup_history.prepare(body, identity[0] or identity[1])


@router_api.post('/projects/import')
def post_project_import(body: dict[str, Any], identity=Depends(_resolve_identity)):
    _admin_only(identity, '仅管理员可导入已有项目')
    from . import project_import
    return project_import.submit(body, identity[0] or identity[1])


@router_api.post('/projects/initializations/preview')
def preview_project_initialization(body: dict[str, Any], identity=Depends(_resolve_identity)):
    _admin_only(identity, '仅管理员可预览项目初始化')
    from . import project_initialization
    return project_initialization.preview(body, identity[0] or identity[1])


@router_api.get('/projects/setup-progress/{request_id}')
def get_project_setup_progress(request_id: str, identity=Depends(_resolve_identity)):
    _admin_only(identity, '仅管理员可查询项目创建进度')
    from . import project_setup_progress
    return project_setup_progress.view(request_id)


@router_api.get('/projects/templates')
def get_project_templates(identity=Depends(_resolve_identity)):
    _admin_only(identity, '仅管理员可查看模板')
    from . import project_templates
    return project_templates.catalogue()


@router_api.get('/projects/templates/{template_id}')
def get_project_template(template_id: str, identity=Depends(_resolve_identity)):
    _admin_only(identity, '仅管理员可查看模板详情')
    from . import project_templates
    return project_templates.detail(template_id)


@router_api.get('/projects/{name}/template-reference')
def get_project_template_reference(name: str, identity=Depends(_resolve_identity)):
    _admin_only(identity, '仅管理员可查看模板参考')
    from . import project_templates
    return project_templates.reference(name)


@router_api.get('/projects/initializations/{initialization_id}')
def get_project_initialization(initialization_id: str, identity=Depends(_resolve_identity)):
    _admin_only(identity, '仅管理员可查看项目初始化')
    from . import project_initialization
    return project_initialization.progress(initialization_id)


@router_api.post('/projects/initializations/{initialization_id}/submit')
@router_api.post('/projects/initializations/{initialization_id}/retry')
def post_project_initialization(initialization_id: str, body: dict[str, Any], identity=Depends(_resolve_identity)):
    _admin_only(identity, '仅管理员可确认或重试项目初始化')
    from . import project_initialization
    return project_initialization.submit(initialization_id, body, identity[0] or identity[1])


@router_api.patch("/projects/{name}")
def patch_project(name: str, body: dict[str, Any], identity=Depends(_resolve_identity)):
    """更新显示名及运行配置；URL 中的 name 为稳定 ID，历史关联及会话名保持固定。"""
    from . import taskpool
    if set(body) - {'new_name', 'display_name', 'root', 'description', 'owner_profile', 'directory_mode', 'worktree_root'}:
        raise HTTPException(400, '包含未知配置项；项目稳定 ID 保持固定')
    for field in ('new_name', 'display_name', 'root', 'description', 'directory_mode', 'worktree_root'):
        if field in body and not isinstance(body[field], str):
            raise HTTPException(400, f'{field} 必须是文本')
    new_name = (body.get("new_name") or "").strip()
    root = (body.get("root") or "").strip()
    description = (body.get("description") or "").strip()
    # 空 description 表示清空描述，是合法请求，
    # 校验改为「字段是否出现在 body」而非「字段值非空」
    if not body:
        raise HTTPException(status_code=400, detail="new_name/root/description/owner_profile at least one required")
    from . import project_workspaces as ws
    _admin_only(identity, '仅管理员可管理项目')
    with ws.LOCK:
        conn = _conn()
        try:
            before = ws.project(conn, name)
        finally:
            conn.close()
        ws.validate_config(body, before)
        if 'display_name' in body and not str(body['display_name']).strip():
            raise HTTPException(400, '显示名不能为空')
        result = taskpool.project_update(name, new_name=new_name, root=root, description=description,
                                         desc_present="description" in body, owner_profile=body.get('owner_profile'),
                                         display_name=body.get('display_name'), directory_mode=body.get('directory_mode'),
                                         worktree_root=body.get('worktree_root'), actor=identity[0] or identity[1])
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router_api.get('/projects/{name}/workspaces')
def get_project_workspaces(name: str, identity=Depends(_resolve_identity), profile: str | None = None):
    _admin_only(identity, '仅管理员可管理项目目录')
    from . import project_workspaces
    return project_workspaces.view(name, profile=profile)


@router_api.post('/projects/{name}/workspaces/{profile}')
def manage_project_workspace(name: str, profile: str, body: dict[str, Any], identity=Depends(_resolve_identity)):
    _admin_only(identity, '仅管理员可管理项目目录')
    from . import project_workspaces
    return project_workspaces.manage(name, profile, body, identity[0] or identity[1])


@router_api.get('/projects/{name}/workspaces/{profile}/resources')
def get_workspace_resources(name: str, profile: str, identity=Depends(_resolve_identity)):
    _admin_only(identity, '管理员可配置工作区资源')
    from . import workspace_resources
    return workspace_resources.member(name, profile)


@router_api.put('/projects/{name}/workspaces/{profile}/resources')
def put_workspace_resources(name: str, profile: str, body: dict[str, Any], identity=Depends(_resolve_identity)):
    _admin_only(identity, '管理员可配置工作区资源')
    from . import workspace_resources
    return workspace_resources.member(name, profile, body, identity[0] or identity[1])


@router_api.get('/projects/{name}/members/{profile}/removal')
def preview_project_member_removal(name: str, profile: str, identity=Depends(_resolve_identity)):
    _admin_only(identity, '仅管理员可移出项目成员')
    from . import project_membership
    return project_membership.preview(name, profile)


@router_api.delete('/projects/{name}/members/{profile}')
def remove_project_member(name: str, profile: str, identity=Depends(_resolve_identity)):
    _admin_only(identity, '仅管理员可移出项目成员')
    from . import project_membership
    return project_membership.remove(name, profile, identity[0] or identity[1])


@router_api.post('/projects/{name}/members/{profile}/reset-session')
def reset_project_member_session(name: str, profile: str, identity=Depends(_resolve_identity)):
    _admin_only(identity, '管理员可重置项目成员会话')
    conn = _conn()
    try:
        if not conn.execute('SELECT 1 FROM projects WHERE name=?', (name,)).fetchone():
            raise HTTPException(404, '项目不存在')
        if db.is_builtin_user(profile) or not conn.execute(
            "SELECT 1 FROM members WHERE profile=?", (profile,)
        ).fetchone():
            raise HTTPException(400, '目标必须是已注册的 bot')
        try:
            result = commands._handle_new(conn, name, [profile], '', None, existing_only=True)
        except Exception as exc:
            result = {'handled': True, 'ok': False, 'command': '/new', 'detail': str(exc)}
        commands._record_command(conn, name, 'user', '/new', '', [profile], None,
                                 'ok' if result.get('ok') else 'error', result.get('detail', ''))
        conn.commit()
        return result
    finally:
        conn.close()


@router_api.get('/projects/{name}/permissions')
def get_project_permissions(name: str, identity=Depends(_resolve_identity)):
    _admin_only(identity, '仅用户可查看项目授权配置')
    from . import project_permissions
    return project_permissions.view(name)


@router_api.put('/projects/{name}/permissions')
def put_project_permissions(name: str, body: dict[str, Any], identity=Depends(_resolve_identity)):
    _admin_only(identity, '仅用户可修改项目授权配置')
    from . import project_permissions
    return project_permissions.save(name, body, identity[0] or identity[1])


class AgentsMdEdit(BaseModel):
    """PUT /projects/{name}/agents-md 的 body（req bot-group#23，规格 27.2）。

    类型与长度不在 Pydantic 层拦（那会返回 422），统一交给 agents_md.save 判，
    返回规格钉死的 400。
    """
    model_config = ConfigDict(extra="forbid")
    content: Any
    revision: Any = ""


@router_api.get("/projects/{name}/agents-md")
def get_project_agents_md(name: str, identity=Depends(_resolve_identity)):
    """读 {projects.root}/AGENTS.md 原文（含 revision / worktree 提示）。

    鉴权与 /members/{profile}/config 同口径：管理员（user/dev）可读写，bot 403。
    """
    from . import agents_md
    _admin_only(identity, "仅管理员可编辑项目 AGENTS.md")
    return JSONResponse(agents_md.read(name), headers={"Cache-Control": "no-store"})


@router_api.put("/projects/{name}/agents-md")
def put_project_agents_md(name: str, body: AgentsMdEdit, identity=Depends(_resolve_identity)):
    """写 {projects.root}/AGENTS.md（原子替换、保留 CRLF/BOM、不自动提交）。"""
    from . import agents_md
    _admin_only(identity, "仅管理员可编辑项目 AGENTS.md")
    return agents_md.save(name, body.content, body.revision)


@router_api.post("/stop")
def post_stop(body: dict[str, Any],
              identity: tuple[str, str] = Depends(_resolve_identity)):
    """停止一个 Bot：终止进程树、软删除同会话消息、追回当前指令。

    body: {profile: str, project: str, thread_id?: int}
    返回 {profile, killed, soft_deleted, seen, cancelled, released}。
    """
    _admin_only(identity, "stopping a bot requires user identity")
    from . import stop

    profile = (body.get("profile") or "").strip()
    if not profile:
        raise HTTPException(status_code=400, detail="profile is required")
    # 内置 user 代表用户，排除在 Bot 停止范围之外。
    if db.is_builtin_user(profile):
        raise HTTPException(status_code=400, detail="cannot stop 'user'")
    project = (body.get("project") or "").strip()
    if not project:
        raise HTTPException(400, "project is required")
    try:
        return stop.stop_profile(profile, project, body.get("thread_id"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


# --------------------------------------------------------------------------
# 管理员手动任务操作：侧栏支持 block、恢复和 cancel。
# bot 身份 403；block/cancel 命中 in_progress 任务时自动把正在干活的 bot 停下来
# （复用 /stop 的 stop_profile：硬杀+软删+追回指令+退回 pending）。
# --------------------------------------------------------------------------
@router_api.post("/admin/task/{task_id}/block")
def admin_block_task(task_id: int, body: dict[str, Any],
                     identity: tuple[str, str] = Depends(_resolve_identity)):
    _admin_only(identity, "admin task block requires user identity")
    from . import taskpool
    project = (body.get("project") or "").strip()
    if not project:
        raise HTTPException(400, "project is required")
    result = taskpool.admin_task_block(project, task_id, body.get("reason") or "")
    if "error" in result:
        raise HTTPException(400, result["error"])
    if result.get("stop_profile"):
        from . import stop
        result["stopped"] = stop.stop_profile(result.pop("stop_profile"), project)
    return result


@router_api.post("/admin/task/{task_id}/resume")
def admin_resume_task(task_id: int, body: dict[str, Any],
                      identity: tuple[str, str] = Depends(_resolve_identity)):
    _admin_only(identity, "admin task resume requires user identity")
    from . import taskpool
    project = (body.get("project") or "").strip()
    if not project:
        raise HTTPException(400, "project is required")
    if 'next_role' in body:
        raise HTTPException(400, '派单字段已改名为 next_bot，请指定具体成员')
    result = taskpool.admin_task_resume(project, task_id, body.get("next_bot") or "")
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router_api.post("/admin/task/{task_id}/cancel")
def admin_cancel_task(task_id: int, body: dict[str, Any],
                      identity: tuple[str, str] = Depends(_resolve_identity)):
    _admin_only(identity, "admin task cancel requires user identity")
    from . import taskpool
    project = (body.get("project") or "").strip()
    if not project:
        raise HTTPException(400, "project is required")
    result = taskpool.admin_task_cancel(project, task_id, body.get("reason") or "")
    if "error" in result:
        raise HTTPException(400, result["error"])
    if result.get("stop_profile"):
        from . import stop
        result["stopped"] = stop.stop_profile(result.pop("stop_profile"), project)
    return result


@router_api.post("/admin/requirement/{requirement_id}/complete")
def admin_complete_requirement(requirement_id: int, body: dict[str, Any],
                              identity: tuple[str, str] = Depends(_resolve_identity)):
    _admin_only(identity, "admin requirement complete requires user identity")
    from . import taskpool
    project = (body.get("project") or "").strip()
    if not project:
        raise HTTPException(400, "project is required")
    result = taskpool.admin_req_complete(project, requirement_id)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router_api.post("/admin/requirement/{requirement_id}/resume")
def admin_resume_requirement(requirement_id: int, body: dict[str, Any],
                             identity: tuple[str, str] = Depends(_resolve_identity)):
    _admin_only(identity, "admin requirement resume requires user identity")
    from . import taskpool
    project = (body.get("project") or "").strip()
    if not project:
        raise HTTPException(400, "project is required")
    result = taskpool.admin_req_resume(project, requirement_id)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router_api.post("/admin/requirement/{requirement_id}/archive")
def admin_archive_requirement(requirement_id: int, body: dict[str, Any],
                             identity: tuple[str, str] = Depends(_resolve_identity)):
    """req#76：归档整个需求（仅 user 身份，只允许 done/cancelled 归档）。"""
    _admin_only(identity, "archive requirement requires user identity")
    from . import taskpool
    project = (body.get("project") or "").strip()
    if not project:
        raise HTTPException(400, "project is required")
    result = taskpool.req_archive(project, requirement_id, archived=True)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router_api.post("/admin/requirement/{requirement_id}/unarchive")
def admin_unarchive_requirement(requirement_id: int, body: dict[str, Any],
                               identity: tuple[str, str] = Depends(_resolve_identity)):
    """req#76：取消归档（恢复展示）。"""
    _admin_only(identity, "unarchive requirement requires user identity")
    from . import taskpool
    project = (body.get("project") or "").strip()
    if not project:
        raise HTTPException(400, "project is required")
    result = taskpool.req_archive(project, requirement_id, archived=False)
    if "error" in result:
        raise HTTPException(400, result["error"])
    return result


@router_api.get("/queue")
def get_queue():
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM wake_jobs ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return {"jobs": [dict(r) for r in rows], "count": len(rows)}
    finally:
        conn.close()


@router_api.get("/tasks")
def get_tasks(include_archived: bool = False):
    """任务池只读投影（方案B：单库 group.db）。含待审队列（block_kind='decision' 的 blocked 任务）。

    req#76：默认过滤已归档需求（archived=1），include_archived=True 返回全部
    （前端「显示已归档」开关用，已归档需求要能取消归档恢复）。
    """
    conn = _conn()
    try:
        req_where = "" if include_archived else "WHERE archived=0"
        reqs = conn.execute(
            f"SELECT * FROM requirements {req_where} ORDER BY priority ASC, id ASC"
        ).fetchall()
        tasks = conn.execute("SELECT * FROM tasks ORDER BY id ASC").fetchall()
        # 待审队列：需要用户决策的 blocked 任务，block_kind='decision'。
        pending_approval = conn.execute(
            "SELECT * FROM tasks WHERE status='blocked' AND block_kind='decision' ORDER BY id ASC"
        ).fetchall()
        return {
            "requirements": [dict(r) for r in reqs],
            "tasks": [dict(t) for t in tasks],
            "pending_approval": [dict(t) for t in pending_approval],
        }
    finally:
        conn.close()


@router_api.get("/requirements/{requirement_id}")
def get_requirement_details(requirement_id: int, project: str = Query(..., min_length=1)):
    from .task_brief import dependency_blockers
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM requirements WHERE id=? AND project=?",
                           (requirement_id, project)).fetchone()
        if row is None:
            raise HTTPException(404, "Requirement not found in this project")
        tasks = []
        for task in conn.execute("SELECT * FROM tasks WHERE requirement_id=? AND project=? ORDER BY id",
                                 (requirement_id, project)).fetchall():
            tasks.append({**dict(task), "dependency_blockers": dependency_blockers(conn, task)})
        return {"requirement": dict(row), "tasks": tasks}
    finally:
        conn.close()


@router_api.get("/tasks/{task_id}")
def get_task_context(task_id: int, project: str = Query(..., min_length=1)):
    from . import taskpool
    result = taskpool.task_get(project, task_id)
    if 'error' in result:
        raise HTTPException(404, result['error'])
    return result


@router_api.get("/tasks/{task_id}/history")
def get_task_history(task_id: int, project: str = Query(..., min_length=1)):
    """Read one task's workflow nodes and events within its exact project."""
    conn = _conn()
    try:
        task = conn.execute("SELECT * FROM tasks WHERE id=? AND project=?",
                            (task_id, project)).fetchone()
        if task is None:
            raise HTTPException(404, "Task not found in this project")
        nodes = conn.execute("SELECT * FROM nodes WHERE task_id=? AND project=? ORDER BY ts,id",
                             (task_id, project)).fetchall()
        task_events = conn.execute(
            "SELECT * FROM events WHERE task_id=? AND project=? ORDER BY ts,id",
            (task_id, project)).fetchall()
        linked = {e['node_id']: e for e in task_events if e['node_id'] is not None}
        node_ids = {n['id'] for n in nodes}
        history = []
        for node in nodes:
            event = linked.get(node['id'])
            history.append({
                'id': f"node:{node['id']}", 'ts': node['ts'], 'actor': node['actor'],
                'type': 'complete' if event and event['event_type'] == 'complete' else node['type'],
                'detail': node['comment'], 'result': node['result'], 'meta': node['meta'],
                '_order': event['id'] if event else 0,
            })
        for event in task_events:
            if event['node_id'] in node_ids:
                continue
            history.append({
                'id': f"event:{event['id']}", 'ts': event['ts'], 'actor': event['actor'],
                'type': event['event_type'], 'detail': event['detail'],
                'result': '', 'meta': '{}', '_order': event['id'],
            })
        history.sort(key=lambda entry: (entry['ts'], entry['_order'], entry['id']))
        for entry in history:
            del entry['_order']
        return {'task': dict(task), 'history': history}
    finally:
        conn.close()


@router_api.get("/events")
async def get_events(request: Request):
    """SSE 事件流。"""
    q = events.bus.subscribe()

    async def gen():
        try:
            # 连接成功先吐一个冒号注释帧（SSE 合法心跳），确认管道通——否则浏览器 onopen 挂起
            yield ": ok\n\n"
            while True:
                stop_event = getattr(request.app.state, 'stop_event', None)
                if stop_event is not None and stop_event.is_set():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=1)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    continue
                except Exception:  # noqa: BLE001
                    break
        finally:
            events.bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
