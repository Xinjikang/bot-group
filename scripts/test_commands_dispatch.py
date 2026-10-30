"""req#83 task#146：斜杠命令分发框架。

契约（docs/design/SLASH-COMMAND-DESIGN-2026-09-27.md：命令入口与会话控制）：
- 拦截落到引擎 API 层，只对 sender=user 分发，bot 的 /xxx 当普通消息（dispatch 返回 None）。
- 命令落 commands 表留痕（rejected/unknown 也留，审计纪律）。
- 目标用 @ 带目标；多个 @、缺目标、未知命令、目标=user 返回确定错误。
- /new 调 session_adapter.switch_session（#144），目标忙碌时拒绝。
- /steer 定位运行中执行 + 调网关 session.steer（#261），回执映射回账本；目标无运行中 → target_ended。
- /help 由 COMMAND_REGISTRY 渲染命令表。

运行（纯 stdlib + mock 隔离 Hermes，系统 python 即可）：
    python scripts/test_commands_dispatch.py
不进 unittest discovery（module 级零副作用、无 TestCase、仅 __main__ 执行）。
"""
import os
import sys
import tempfile
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def main() -> int:
    from engine import db, commands

    iso = tempfile.mkdtemp(prefix="t146_")
    group_db = os.path.join(iso, "group.db")
    failures = []

    def check(label, cond, detail=""):
        print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            failures.append(label)

    patcher = patch.object(db, "db_path", return_value=group_db)
    patcher.start()
    try:
        conn = db.get_connection()
        try:
            db.init_schema(conn)
            # 注册成员 + 项目（命令 @ 目标解析需要 members 表 + project_members 启用）
            for profile in ("default", "architect", "be-dev-01", "fe-dev-02", "user"):
                conn.execute("INSERT INTO members(profile,display_name,role) VALUES(?,?,?)",
                             (profile, profile, profile))
            conn.execute("INSERT INTO projects(name,root,created_at) VALUES(?,?,?)",
                         ("bot-group", iso, db.now_iso()))
            for profile in ("default", "architect", "be-dev-01", "fe-dev-02"):
                conn.execute("INSERT INTO project_members(project,profile,active) VALUES(?,?,1)",
                             ("bot-group", profile))
            conn.commit()
        finally:
            conn.close()

        def cmd_rows():
            c = db.get_connection()
            try:
                return c.execute("SELECT * FROM commands ORDER BY id").fetchall()
            finally:
                c.close()

        # 1. 非 user（bot）的 /xxx 当普通消息 → dispatch 返回 None
        conn = db.get_connection()
        try:
            r = commands.dispatch(conn, "bot-group", "be-dev-01", "bot", "/new @default", None)
        finally:
            conn.close()
        check("bot 的 /xxx 不当命令分发（返回 None）", r is None)

        # 2. 普通文本非命令 → None
        conn = db.get_connection()
        try:
            r = commands.dispatch(conn, "bot-group", "user", "user", "今天天气不错", None)
        finally:
            conn.close()
        check("普通文本非命令（返回 None）", r is None)

        # 3. 未知命令 → 留痕 rejected + 抛 CommandError
        conn = db.get_connection()
        try:
            raised = False
            try:
                commands.dispatch(conn, "bot-group", "user", "user", "/frobnicate @be-dev-01", None)
            except commands.CommandError as e:
                raised = "未知命令" in str(e)
            rows = cmd_rows()
            check("未知命令抛 CommandError", raised)
            check("未知命令留痕 rejected", any(x["status"] == "rejected" and x["command"] == "/frobnicate" for x in rows))
        finally:
            conn.close()

        # 4. /new 缺目标 → CommandError
        conn = db.get_connection()
        try:
            raised = False
            try:
                commands.dispatch(conn, "bot-group", "user", "user", "/new", None)
            except commands.CommandError as e:
                raised = "目标" in str(e)
            check("/new 缺目标抛 CommandError", raised)
        finally:
            conn.close()

        # 5. /new 多目标 → CommandError
        conn = db.get_connection()
        try:
            raised = False
            try:
                commands.dispatch(conn, "bot-group", "user", "user", "/new @be-dev-01 @fe-dev-02", None)
            except commands.CommandError as e:
                raised = "一个" in str(e)
            check("/new 多目标抛 CommandError", raised)
        finally:
            conn.close()

        # 6. /new 目标=user → CommandError
        conn = db.get_connection()
        try:
            raised = False
            try:
                commands.dispatch(conn, "bot-group", "user", "user", "/new @user", None)
            except commands.CommandError as e:
                raised = "bot" in str(e)
            check("/new 目标=user 抛 CommandError", raised)
        finally:
            conn.close()

        # 7. /new 正常 → 调 switch_session，commands 留痕 ok
        with patch.object(commands.session_adapter, "switch_session",
                          return_value={"ok": True, "session_id": "20260928_160000_abc123",
                                        "generation": 1, "operation_id": "op123"}) if hasattr(commands, "session_adapter") else patch("engine.session_adapter.switch_session",
                          return_value={"ok": True, "session_id": "20260928_160000_abc123",
                                        "generation": 1, "operation_id": "op123"}):
            conn = db.get_connection()
            try:
                r = commands.dispatch(conn, "bot-group", "user", "user", "/new @be-dev-01", None)
            finally:
                conn.close()
            check("/new 返回 ok + session_id", r.get("ok") and r.get("session_id") == "20260928_160000_abc123", str(r))
            rows = cmd_rows()
            last = rows[-1]
            check("/new success feedback identifies target and scope",
                  "@be-dev-01" in r.get("detail", "") and "工作会话已切换" in r["detail"])
            check("/new success feedback is recorded", last["result"] == r["detail"])
            check("/new 留痕 ok 状态", last["status"] == "ok" and last["command"] == "/new")
            check("/new 留痕 targets 含目标", '"be-dev-01"' in last["targets"])

        # 8. /new 目标忙碌（switch_session ok=False）→ 返回 ok=False + detail
        with patch("engine.session_adapter.switch_session",
                   return_value={"ok": False, "detail": "目标 bot 忙碌，请等空闲后再 /new"}):
            conn = db.get_connection()
            try:
                r = commands.dispatch(conn, "bot-group", "user", "user", "/new @be-dev-01", None)
            finally:
                conn.close()
            check("/new 忙碌返回 ok=False + detail", not r.get("ok") and "忙碌" in r.get("detail", ""), str(r))

        # 9. /steer 缺正文 → CommandError
        conn = db.get_connection()
        try:
            raised = False
            try:
                commands.dispatch(conn, "bot-group", "user", "user", "/steer @be-dev-01", None)
            except commands.CommandError as e:
                raised = "正文" in str(e)
            check("/steer 缺正文抛 CommandError", raised)
        finally:
            conn.close()

        # 10. /steer 有正文但目标无运行中执行 → 落账本 target_ended + 留痕 error
        conn = db.get_connection()
        try:
            r = commands.dispatch(conn, "bot-group", "user", "user", "/steer @be-dev-01 请改用方案 B", None)
        finally:
            conn.close()
        check("/steer 无运行中目标未送达", not r.get("ok") and r.get("operations"), str(r))
        check("/steer 状态 target_ended", r.get("statuses") == ["target_ended"], str(r.get("statuses")))
        rows = cmd_rows()
        last = rows[-1]
        check("/steer 留痕 error 状态", last["status"] == "error" and last["command"] == "/steer")
        # 账本 steer_operations 落 target_ended（目标当前无运行中 turn，finalize_target_ended）
        from engine import steer_ledger
        conn = db.get_connection()
        try:
            ops = [steer_ledger.get_operation(conn, op_id) for op_id in r['operations']]
        finally:
            conn.close()
        check("/steer 落 steer_operations 账本 target_ended",
              any(o["text"] == "请改用方案 B" and o["status"] == "target_ended" for o in ops),
              f"pend={len(ops)}")

        # 11. /steer 参数提取：去掉 @目标，保留正文
        conn = db.get_connection()
        try:
            r = commands.dispatch(conn, "bot-group", "user", "user", "/steer @be-dev-01 改用方案 B", None)
        finally:
            conn.close()
        rows = cmd_rows()
        last = rows[-1]
        check("/steer args 去掉 @目标保留正文", last["args"] == "改用方案 B", repr(last["args"]))

        # 12. /help 渲染命令表（含三个命令）
        conn = db.get_connection()
        try:
            r = commands.dispatch(conn, "bot-group", "user", "user", "/help", None)
        finally:
            conn.close()
        help_text = r.get("detail", "")
        check("/help 渲染命令表含 /new /steer /help",
              all(x in help_text for x in ("/new", "/steer", "/help")), help_text[:80])
        check("/help 留痕 ok 状态", cmd_rows()[-1]["status"] == "ok")

        # 13. _strip_command_word / _extract_args 边界
        cmd, tail = commands._strip_command_word("/new @be-dev-01 额外")
        check("strip_command_word 拆命令名与 tail", cmd == "/new" and tail == "@be-dev-01 额外", f"{cmd}|{tail}")
        args = commands._extract_args("改用方案 B", ["be-dev-01"])
        check("extract_args 无 @目标时原样", args == "改用方案 B", repr(args))

    finally:
        patcher.stop()

    print("\n=== req#83 task#146 斜杠命令分发框架验证 ===")
    if failures:
        print("SOME FAIL:", ", ".join(failures))
        return 1
    print("ALL PASS：user-only 分发/bot 普通消息/未知命令留痕/目标校验/switch 调派/留痕/help 渲染 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
