"""req#83 task#146：斜杠命令分发框架。

设计文档第 7 条（SLASH-COMMAND-DESIGN-2026-09-27.md）+ S5 批注：
- 拦截落到引擎 API 层（不是前端），且只对 sender=user 走命令分发，bot 的 /xxx 当普通消息。
- 命令至少落 commands 表留痕（审计纪律，rejected/unknown 也留）。
- 目标 bot 用 @ 带目标（沿用现有 @ 菜单），多个 @、缺目标、未知命令、目标不存在返回确定错误。
- /help 由同一命令注册表生成，展示各命令作用域、忙碌策略。

命令注册表（COMMAND_REGISTRY）：
- /new  → 调 session_adapter.switch_session（#144 交付），目标 bot 忙碌时拒绝
- /steer → 定位运行中执行 + 调网关 session.steer（#261），回执映射回账本
- /help → 渲染命令表

本模块只做「分发 + 留痕 + 目标解析 + /help」，steer 传输由 hermes_gateway_steer 落地。
"""

from __future__ import annotations

import json
import sqlite3

from . import db
from .mentions import parse_mentions


class CommandError(ValueError):
    """命令参数/目标校验失败（对应明确错误响应，不落普通消息）。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _strip_command_word(text: str) -> tuple[str, str]:
    """拆出命令名与剩余文本。text 形如 '/new @bot-1' → ('/new', '@bot-1')。"""
    text = text.strip()
    if not text.startswith("/"):
        return "", text
    # 命令名 = 首个 '/' 后到空白为止的 token
    rest = text[1:]
    if " " in rest or "\t" in rest:
        word, tail = rest.split(None, 1)
    else:
        word, tail = rest, ""
    return "/" + word, tail.strip()


def _extract_args(tail: str, targets: list[str]) -> str:
    """从剩余文本里去掉 @目标名，得到纯参数（/steer 的纠偏文本、/new 无参数）。"""
    args = tail
    for t in targets:
        # 去掉 '@name' 字面（含前后空白归一）
        import re
        args = re.sub(r"@" + re.escape(t) + r"(?![A-Za-z0-9_\-])", " ", args)
    return " ".join(args.split())


def _record_command(conn: sqlite3.Connection, project: str, sender: str, command: str,
                    args: str, targets: list[str], thread_id: int | None,
                    status: str, result: str = "") -> int:
    """落 commands 表留痕，返回 command id。"""
    cur = conn.execute(
        "INSERT INTO commands (project, sender, command, args, targets, thread_id, status, result, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (project, sender, command, args, json.dumps(targets, ensure_ascii=False),
         thread_id, status, result, db.now_iso()),
    )
    return cur.lastrowid


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------

def _handle_new(conn: sqlite3.Connection, project: str, targets: list[str],
                args: str, thread_id: int | None, *, existing_only=False) -> dict:
    """/new：切目标 bot 的会话（原生 new_session，#144 交付的 switch_session）。"""
    if not targets:
        raise CommandError("请用 @ 指定目标 bot（如 /new @苏瑾）")
    if len(targets) > 1:
        raise CommandError("/new 一次只支持一个目标 bot")
    target = targets[0]
    if db.is_builtin_user(target):
        raise CommandError("/new 目标必须是 bot，不能是 user")

    from . import session_adapter, wake
    # 会话作用域：话题内 → 该话题的 chat 会话；点对点 → work 主会话
    kind = "chat" if thread_id is not None else "work"
    tid = thread_id if kind == "chat" else None
    title = wake.session_name(target, kind, tid, project)
    def busy_check(profile, k, t, prj):
        return wake.session_busy(profile, k, t, prj)

    # req#88 task#153：/new 按目标 Bot runtime 分叉（第四节第 6 条 codex /new 必做）。
    # codex Bot 走 codex_new（弃旧 thread、清空绑定、落审计）；hermes Bot 走 switch_session。
    if session_adapter._codex_bot_id(conn, project, target) is not None:
        result = session_adapter.codex_new(conn, project, target, kind, tid, title,
                                          busy_check=busy_check, existing_only=existing_only)
        if not result.get("ok"):
            return {"handled": True, "ok": False, "command": "/new",
                    "detail": result.get("detail", "未知错误")}
        return {"handled": True, "ok": True, "command": "/new", "target": target,
                "detail": f"@{target} 的{'当前话题' if thread_id is not None else '工作'}会话已重置。下一条发给该会话的消息将使用新的 codex 会话。",
                "operation_id": result.get("operation_id")}

    options = {"existing_only": True} if existing_only else {}
    result = session_adapter.switch_session(
        conn, project, target, kind, tid, title, busy_check=busy_check, **options)
    if not result.get("ok"):
        return {"handled": True, "ok": False, "command": "/new",
                "detail": result.get("detail", "未知错误")}
    return {"handled": True, "ok": True, "command": "/new", "target": target,
            "detail": f"@{target} 的{'当前话题' if thread_id is not None else '工作'}会话已切换。下一条发给该会话的消息将使用新会话。",
            "session_id": result["session_id"], "generation": result["generation"],
            "operation_id": result.get("operation_id")}


def _handle_stop(conn: sqlite3.Connection, project: str, targets: list[str],
                 args: str, thread_id: int | None) -> dict:
    if len(targets) != 1 or db.is_builtin_user(targets[0]):
        raise CommandError("/stop 需要 @ 指定一个 bot")
    if args:
        raise CommandError("/stop 只接受一个 @bot 目标")
    from . import stop
    target = targets[0]
    result = stop.stop_profile(target, project, thread_id)
    ok = result.get("stopped") is True
    scope = "当前话题" if thread_id is not None else "当前项目的工作会话"
    detail = f"@{target} 的{scope}：" + ("已确认停止。" if ok else "停止状态待确认。")
    return {"handled": True, "ok": ok, "command": "/stop", "target": target,
            "detail": detail, "result": result}


def _handle_steer(conn: sqlite3.Connection, project: str, targets: list[str],
                  args: str, thread_id: int | None) -> dict:
    """/steer：向运行中目标注入纠偏——定位运行中执行 + 调网关 session.steer + 回执映射回账本。

    命令框架层落 steer_operations 账本（queued），实际传输由 hermes_gateway_steer
    定位运行中执行并经 session.steer 投递，回执映射走 steer_ledger 五层状态机。
    """
    if not targets:
        raise CommandError("请用 @ 指定目标 bot（如 /steer @苏瑾 请改用方案 B）")
    if not args:
        raise CommandError("/steer 需要纠偏正文（如 /steer @苏瑾 请改用方案 B）")
    from . import steer_ledger, hermes_gateway_steer
    kind = "chat" if thread_id is not None else "work"
    # 每个 target 落一条 queued 账本，随即定位运行中执行并投递，回执映射回账本
    ops = []
    statuses = []
    details = []
    for t in targets:
        if db.is_builtin_user(t):
            continue  # 目标必须 bot，user 不记账本
        op = steer_ledger.new_operation(conn, project, t, kind, thread_id, args)
        ops.append(op["operation_id"])
        outcome = hermes_gateway_steer.deliver_steer(
            conn, project, t, kind, thread_id, args, op["operation_id"])
        statuses.append(outcome["status"])
        details.append(f"@{t}：{outcome['detail']}")
    if not ops:
        raise CommandError("/steer 目标必须是 bot（@ 里不含 user）")
    ok = all(s == "queued" for s in statuses)
    return {"handled": True, "ok": ok, "command": "/steer", "targets": targets,
            "operations": ops, "statuses": statuses,
            "detail": "；".join(details)}


def _handle_help(conn: sqlite3.Connection, project: str, targets: list[str],
                 args: str, thread_id: int | None) -> dict:
    """/help：渲染命令表（由注册表生成）。"""
    lines = []
    for name, meta in COMMAND_REGISTRY.items():
        lines.append(f"{meta['name']} — {meta['help']}")
        lines.append(f"  作用域：{meta.get('scope', '—')}")
        lines.append(f"  忙碌策略：{meta.get('busy_policy', '—')}")
    return {"handled": True, "ok": True, "command": "/help",
            "detail": "\n".join(lines)}


COMMAND_REGISTRY = {
    "new": {
        "name": "/new",
        "requires_target": True,
        "menu_description": "开启新会话，需要 @ 指定 bot",
        "help": "重置目标 bot 的会话（上下文清零），走原生 new_session 拿新 session_id。",
        "scope": "点对点或话题内，@ 目标 bot",
        "busy_policy": "目标忙碌时拒绝，请等空闲后再 /new",
        "handler": _handle_new,
    },
    "stop": {
        "name": "/stop",
        "requires_target": True,
        "menu_description": "叫停当前项目工作会话或当前话题的 bot",
        "help": "叫停当前会话的 bot，按项目和话题隔离。",
        "scope": "@ 一个 bot；话题内限定当前话题，话题外限定本项目工作会话",
        "busy_policy": "立即叫停",
        "handler": _handle_stop,
    },
    "steer": {
        "name": "/steer",
        "requires_target": True,
        "menu_description": "向运行中的目标 bot 注入纠偏（网关运行中执行）",
        "help": "向运行中的目标 bot 注入纠偏；已接入网关 session.steer 运行中纠偏，子进程/Codex 后端不支持。",
        "scope": "@ 目标 bot + 纠偏正文",
        "busy_policy": "目标空闲时无法投递；仅网关（hermes）运行中执行可纠偏",
        "handler": _handle_steer,
    },
    "help": {
        "name": "/help",
        "menu_description": "查看命令说明",
        "help": "显示命令列表。",
        "scope": "任意",
        "busy_policy": "—",
        "handler": _handle_help,
    },
}


# ---------------------------------------------------------------------------
# 分发入口
# ---------------------------------------------------------------------------

def is_command(text: str | None) -> bool:
    """是否为斜杠命令候选（text 以 '/' 开头）。"""
    return bool(text) and text.strip().startswith("/")


def dispatch(conn: sqlite3.Connection, project: str, sender: str, sender_type: str,
             text: str | None, thread_id: int | None) -> dict | None:
    """命令分发入口。只对 sender=user 分发；非 user 返回 None（走普通消息路由）。

    返回 None 表示「不是命令或不该走命令分发」（调用方继续普通路由）；
    返回 dict 表示「命令已处理」（含 handled=True），调用方直接返回该 dict。
    """
    # 设计文档第 7 条：只对 sender=user 走命令分发，bot 的 /xxx 当普通消息。
    if sender_type != "user":
        return None
    if not is_command(text):
        return None
    text = text.strip()
    command, tail = _strip_command_word(text)
    name = command[1:]  # 去斜杠
    meta = COMMAND_REGISTRY.get(name)
    targets = parse_mentions(tail, conn, project)

    # 未知命令：留痕 + 明确错误
    if meta is None:
        _record_command(conn, project, sender, command, tail, targets, thread_id,
                        "rejected", f"未知命令 {command}")
        conn.commit()
        available = " ".join(meta["name"] for meta in COMMAND_REGISTRY.values())
        raise CommandError(f"未知命令 {command}。可用命令：{available}")

    args = _extract_args(tail, targets)
    # 先落一条 accepted 留痕，handler 执行结果再回填 status/result
    cmd_id = _record_command(conn, project, sender, command, args, targets, thread_id, "accepted")
    # Release SQLite's writer before waiting for the spawn/switch mutex.
    conn.commit()
    try:
        result = meta["handler"](conn, project, targets, args, thread_id)
    except CommandError as exc:
        # 参数/目标校验失败 → 留痕 error 后冒泡，由 API 层转 400（S5 确定错误）。
        conn.execute("UPDATE commands SET status='error', result=? WHERE id=?",
                     (exc.message, cmd_id))
        conn.commit()
        raise
    except Exception as exc:  # noqa: BLE001
        conn.execute("UPDATE commands SET status='error', result=? WHERE id=?",
                     (str(exc), cmd_id))
        conn.commit()
        return {"handled": True, "ok": False, "command": command,
                "detail": f"命令执行失败：{exc}"}
    # 成功回填
    conn.execute("UPDATE commands SET status=?, result=? WHERE id=?",
                 ("ok" if result.get("ok") else "error", result.get("detail", ""), cmd_id))
    conn.commit()
    return result


def help_text() -> str:
    """/help 渲染（供 API 或测试直接取命令表文本）。"""
    return _handle_help(None, "", [], "", None)["detail"]


def command_menu() -> list[dict]:
    """Selectable commands, derived from the same registry as dispatch/help."""
    return [{"name": meta["name"], "description": meta.get("menu_description", meta["help"]),
             "requires_target": meta.get("requires_target", False)}
            for meta in COMMAND_REGISTRY.values() if meta.get("available", True)]
