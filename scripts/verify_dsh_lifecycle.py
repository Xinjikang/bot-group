"""req#137 task#267：dsh 命令与生命周期闭环真实验证（隔离引擎 + 真实 DSH + 真实 MCP）。

流程（全资源隔离，白名单环境，不触碰生产配置；凭据值不输出）：
1. 隔离数据目录 + 固定空闲端口启动真实引擎（engine.main.app，uvicorn 线程）。
   环境白名单构造，清 GROUP_*/XDG_GROUP_*/HERMES_* 生产注入键。
2. 种子：project（root=隔离工作区）+ user + dsh Bot 成员（真实 DSH 宿主引用，
   bots.profile=desktop）+ 工作区 AGENTS.md（项目指令标记 THE_PROJECT_MARKER_267）。
3. 业务闭环：user POST @dsh-bot 指令（调用 group_send 出站 + 回复）→ 引擎唤醒
   dsh headless 子进程（真实引擎 MCP overlay，execution token）→ 模型 group_send
   出站消息落库 + dsh_run_events 记录 tool_call/reply_message_id。
4. 项目指令：@dsh-bot 询问 AGENTS.md 标记 → 召回 alpha-7。
5. /steer：dsh 目标返回能力不支持（无 steer 账本，命令留痕）。
6. /new：会话绑定重置（generation+1，旧 session 审计保留）。
7. /stop：运行中真实进程树终止（taskkill）+ 队列取消 + 会话绑定保留。
8. 原 DSH 配置哈希不变；结束停引擎、清理隔离目录与 dsh 私有临时目录。

运行：python -X utf8 -B scripts/verify_dsh_lifecycle.py --entry <dsh.cmd绝对路径> \
  --source-home <源DSH_HOME> --test-directory <分配隔离目录> --evidence <绝对证据路径>
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _isolate_env(data_dir: Path, port: int) -> None:
    """白名单构造隔离环境（req#250 探针隔离教训：禁用 dict(os.environ)）。"""
    for key in list(os.environ):
        if key.startswith(("GROUP_", "XDG_GROUP_", "HERMES_", "TASK_QUEUE")) \
                or key in ("DEEPSEEK_API_KEY",):
            os.environ.pop(key, None)
    os.environ["GROUP_DATA_DIR"] = str(data_dir)
    os.environ["GROUP_ENGINE_HOST"] = "127.0.0.1"
    os.environ["GROUP_ENGINE_PORT"] = str(port)
    os.environ["GROUP_ENGINE_URL"] = f"http://127.0.0.1:{port}"


def fingerprint(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _db_query(sql: str, params=()):
    from engine import db
    conn = db.get_connection()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry", type=Path, required=True, help="dsh.cmd 绝对路径")
    ap.add_argument("--source-home", type=Path, required=True, help="源 DSH_HOME（账号路由凭据）")
    ap.add_argument("--test-directory", type=Path, required=True)
    ap.add_argument("--evidence", type=Path, required=True)
    args = ap.parse_args()
    if not args.entry.is_file():
        raise ValueError(f"--entry 不是文件：{args.entry}")
    if not args.source_home.is_dir():
        raise ValueError(f"--source-home 不是目录：{args.source_home}")

    marker = "alpha-7"
    result = {}
    data_dir = Path(args.test_directory) / "engine-267"
    workdir = Path(args.test_directory) / "work-267"
    shutil.rmtree(data_dir, ignore_errors=True)  # 上次残留（超时中断等）
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "AGENTS.md").write_text(
        "# Project Rules\n\nTHE_PROJECT_MARKER_267 = alpha-7.\nIf asked for the marker, reply alpha-7.\n",
        encoding="utf-8")

    source_files = [args.source_home / ".credentials.yaml",
                    args.source_home / "profiles" / "desktop" / "cordis.patch.yml"]
    before = {str(p): fingerprint(p) for p in source_files if p.is_file()}

    port = _free_port()
    _isolate_env(data_dir, port)
    import httpx  # noqa: E402
    from engine import db, wake  # noqa: E402

    # 1. 种子（引擎启动前，同一进程共享 group.db）
    conn = db.get_connection()
    try:
        db.init_schema(conn)
        conn.execute("INSERT OR IGNORE INTO members(profile,display_name,role) "
                     "VALUES('user','user','user')")
        conn.execute("INSERT OR IGNORE INTO members(profile,display_name,role) "
                     "VALUES('dsh-bot','dsh-bot','dsh-bot')")
        conn.execute("INSERT INTO projects(name,root,worktree_root,created_at) VALUES(?,?, '', ?)",
                     ("bot-group", str(workdir), db.now_iso()))
        conn.execute("INSERT OR IGNORE INTO project_members(project,profile,active) "
                     "VALUES('bot-group','user',1)")
        conn.execute("INSERT INTO project_members(project,profile,active) VALUES('bot-group','dsh-bot',1)")
        cur = conn.execute(
            "INSERT INTO bots(host_type,profile,config_root,runtime,runtime_entry,display_name,role) "
            "VALUES('dsh','desktop',?,'dsh',?, 'dsh-bot','')",
            (str(args.source_home), str(args.entry)))
        bot_id = cur.lastrowid
        conn.execute("UPDATE project_members SET bot_id=? WHERE project='bot-group' AND profile='dsh-bot'",
                     (bot_id,))
        # 任务权限闭环：dsh-bot 名下一条待领任务（briefing ready，可领取）
        conn.execute("INSERT INTO requirements(id,project,title,status,created_at,updated_at) "
                     "VALUES(1,'bot-group','r267','open',?,?)", (db.now_iso(), db.now_iso()))
        conn.execute(
            "INSERT INTO tasks(project,requirement_id,title,status,next_bot,created_at,"
            "description,design_basis,acceptance_criteria,prerequisites,briefing_status) "
            "VALUES('bot-group',1,'t267-probe','pending','dsh-bot',?,"
            "'probe task','design','accept','[]','ready')", (db.now_iso(),))
        conn.commit()
    finally:
        conn.close()

    # 2. 真实引擎（uvicorn 线程）
    import uvicorn  # noqa: E402
    from engine.main import app as engine_app  # noqa: E402
    config = uvicorn.Config(engine_app, host="127.0.0.1", port=port,
                            log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="engine-267")
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    if not server.started:
        result["error"] = "engine failed to start"
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1
    base = f"http://127.0.0.1:{port}"

    def post_message(text, sender="user"):
        resp = httpx.post(f"{base}/msg", json={
            "project": "bot-group", "sender": sender, "text": text, "need_reply": 1}, timeout=60)
        if resp.status_code >= 400:
            raise RuntimeError(f"POST /msg {resp.status_code}: {resp.text[:300]}")
        return resp

    def wait_run(timeout=240):
        deadline = time.time() + timeout
        while time.time() < deadline:
            rows = _db_query("SELECT ended_at, exit_code, session FROM wake_runs "
                             "WHERE runtime='dsh' ORDER BY started_at DESC LIMIT 1")
            if rows and rows[0]["ended_at"] is not None:
                return dict(rows[0])
            time.sleep(2)
        return None

    try:
        # 3. 业务闭环：group_send 出站 + 回复（真实引擎 MCP）
        post_message(f"@dsh-bot 请先调用 task_queue 的 group_send 工具发送一条内容为 "
                     f"DSH_MCP_OK_{marker} 的消息，然后只回复 DSH_LIFECYCLE_OK。")
        run1 = wait_run()
        if run1 is None:
            result["business_loop"] = {"ok": False, "detail": "dsh run did not finish in time",
                                       "jobs": [dict(r) for r in _db_query(
                                           "SELECT profile,status,kind,trigger FROM wake_jobs")]}
        else:
            events = _db_query(
                "SELECT payload FROM dsh_run_events WHERE session=? ORDER BY id",
                (run1["session"],))
            parsed = [json.loads(r["payload"]) for r in events]
            types = [p.get("type") for p in parsed]
            tool_calls = [p for p in parsed if p.get("type") == "tool_call"]
            from engine import dsh_reasoning
            reply_ids = [dsh_reasoning._message_id(p.get("result"))
                         for p in parsed if p.get("type") == "tool_result"]
            sent = _db_query(
                "SELECT id, content, sender FROM messages WHERE content LIKE ? ORDER BY id DESC",
                (f"%DSH_MCP_OK_{marker}%",))
            result["business_loop"] = {
                "run": {"exit_code": run1["exit_code"], "event_types": types},
                "sent_rows": len(sent),
                "sent_by_bot": bool(sent) and sent[0]["sender"] == "dsh-bot",
                "group_send_called": any(t.get("tool", "").endswith("group_send") for t in tool_calls),
                "reply_message_id": [r for r in reply_ids if r],
            }

        # 4. 项目指令：AGENTS.md 标记召回（第二唤醒，期间测 /stop）
        post_message(f"@dsh-bot 你工作区 AGENTS.md 里的 THE_PROJECT_MARKER_267 值是什么？只回复该值。")
        # 等 dsh 进程真正起来（wake_runs 有未结束 dsh 行）后立刻 /stop
        running = None
        for _ in range(60):
            rows = _db_query("SELECT session, pid FROM wake_runs WHERE runtime='dsh' "
                             "AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1")
            if rows:
                running = dict(rows[0])
                break
            time.sleep(1)
        if running:
            # 等 spawn 注册 _native_procs（wake_runs 行先落、proc 注册滞后数毫秒）
            pids_before = []
            for _ in range(20):
                pids_before = wake.find_bot_processes("dsh-bot", running["session"])
                if pids_before:
                    break
                time.sleep(0.5)
            result["stop_live_pids_before"] = pids_before
            stop_resp = httpx.post(f"{base}/msg", json={
                "project": "bot-group", "sender": "user",
                "text": "/stop @dsh-bot", "need_reply": 1}, timeout=60)
            result["stop_command"] = {"status": stop_resp.status_code,
                                      "body": stop_resp.json()}
            time.sleep(2)
            pids_after = wake.find_bot_processes("dsh-bot", running["session"])
            result["stop_live_pids_after"] = pids_after
            result["stop_proc_gone"] = pids_before and not pids_after
        else:
            result["stop_live_pids_before"] = "no live dsh run observed"
        # 等被杀 run 落 ended_at 后再继续（/new 需目标空闲）
        run2 = wait_run(240)
        result["stop_settled"] = run2 is not None
        bindings = _db_query("SELECT session_id, generation, runtime FROM session_bindings "
                             "WHERE project='bot-group' AND profile='dsh-bot' AND kind='work'")
        result["binding_after_stop"] = [dict(r) for r in bindings]
        result["jobs_after_stop"] = [dict(r) for r in _db_query(
            "SELECT status FROM wake_jobs WHERE project='bot-group' AND profile='dsh-bot'")]

        # 5. /steer：dsh 能力不支持
        steer_resp = httpx.post(f"{base}/msg", json={
            "project": "bot-group", "sender": "user",
            "text": "/steer @dsh-bot 请改用方案B", "need_reply": 1}, timeout=60)
        result["steer_command"] = {"status": steer_resp.status_code, "body": steer_resp.json()}
        result["steer_no_ledger"] = len(_db_query(
            "SELECT id FROM steer_operations WHERE project='bot-group' AND profile='dsh-bot'")) == 0
        result["steer_command_audited"] = len(_db_query(
            "SELECT id FROM commands WHERE project='bot-group' AND command='/steer'")) > 0

        # 6. /new：会话绑定重置（generation+1，旧 session 审计保留）
        new_resp = httpx.post(f"{base}/msg", json={
            "project": "bot-group", "sender": "user",
            "text": "/new @dsh-bot", "need_reply": 1}, timeout=60)
        result["new_command"] = {"status": new_resp.status_code, "body": new_resp.json()}
        result["new_switch_ops"] = [dict(r) for r in _db_query(
            "SELECT status, old_session_id, new_session_id FROM session_switch_ops "
            "WHERE project='bot-group' AND profile='dsh-bot' ORDER BY id DESC")]

        # 7. 项目指令：新会话召回 AGENTS.md 标记（/new 后新 session 可用性一并验证）
        post_message("@dsh-bot 阅读你工作区的 AGENTS.md 文件，把 THE_PROJECT_MARKER_267 的值"
                     "作为最终回复内容。不要调用任何工具，直接回复该值。")
        run3 = wait_run(240)
        if run3 is not None:
            events3 = _db_query(
                "SELECT payload FROM dsh_run_events WHERE session=? ORDER BY id",
                (run3["session"],))
            finals3 = [json.loads(r["payload"]) for r in events3
                       if json.loads(r["payload"]).get("type") == "final"]
            result["instructions_turn"] = {
                "run_done": True, "exit_code": run3["exit_code"],
                "marker_recalled": any(marker in (f.get("text") or "") for f in finals3),
                "session_id_after_new": [dict(r) for r in _db_query(
                    "SELECT session_id, generation FROM session_bindings "
                    "WHERE project='bot-group' AND profile='dsh-bot' AND kind='work'")],
            }
        else:
            result["instructions_turn"] = {"run_done": False}

        # 8. 工具权限闭环：task_claim（任务权限）+ group_mark_seen（已读确认）
        post_message("@dsh-bot 依次执行：1) 调用 task_queue 的 task_claim 领取你的待办任务；"
                     "2) 调用 group_read_inbox 读收件箱并对其中 pending 消息调用 group_mark_seen；"
                     "3) 最终回复 DSH_TOOLS_OK，不要调用 group_send。")
        run4 = wait_run(240)
        if run4 is not None:
            events4 = _db_query(
                "SELECT payload FROM dsh_run_events WHERE session=? ORDER BY id",
                (run4["session"],))
            tools4 = [json.loads(r["payload"]).get("tool", "") for r in events4
                      if json.loads(r["payload"]).get("type") == "tool_call"]
            result["tools_turn"] = {
                "run_done": True, "exit_code": run4["exit_code"],
                "tools_called": tools4,
                "task_claimed": bool(_db_query(
                    "SELECT id FROM tasks WHERE project='bot-group' AND title='t267-probe' "
                    "AND status='in_progress' AND claimed_by='dsh-bot'")),
                "inbox_seen": [dict(r) for r in _db_query(
                    "SELECT message_id, status FROM inbox WHERE profile='dsh-bot'")],
            }
        else:
            result["tools_turn"] = {"run_done": False}

        # 运行诊断（失败归因）：final/error 事件 + execution_errors 脱敏摘要
        for key, run in (("run1", run1), ("run3", run3), ("run4", run4)):
            if run is None:
                continue
            rows = _db_query("SELECT payload FROM dsh_run_events WHERE session=? ORDER BY id",
                             (run["session"],))
            parsed = [json.loads(r["payload"]) for r in rows]
            diag = {
                "finals": [p.get("text", "")[:120] for p in parsed if p.get("type") == "final"],
                "errors": [str(p.get("error") or p)[:160] for p in parsed if p.get("type") == "error"],
            }
            err_rows = _db_query(
                "SELECT category, diagnostic FROM execution_errors WHERE attempt LIKE ?",
                (f"%{run['session']}%",))
            diag["execution_errors"] = [{"category": r["category"],
                                          "diagnostic": (r["diagnostic"] or "")[:160]}
                                         for r in err_rows]
            result.setdefault("run_diagnostics", {})[key] = diag

        result["source_files_unchanged"] = before == {str(p): fingerprint(p)
                                                      for p in source_files if p.is_file()}
        ok = (
            result.get("business_loop", {}).get("sent_rows") >= 1
            and result["business_loop"].get("sent_by_bot")
            and result["business_loop"].get("group_send_called")
            and bool(result["business_loop"].get("reply_message_id"))
            and result.get("stop_proc_gone")
            and result.get("steer_command", {}).get("status") == 200
            and "不支持" in json.dumps(result.get("steer_command", {}).get("body", {}),
                                       ensure_ascii=False)
            and result.get("steer_no_ledger") and result.get("steer_command_audited")
            and result.get("new_command", {}).get("status") == 200
            and result.get("new_switch_ops")
            and result.get("instructions_turn", {}).get("marker_recalled")
            and result.get("tools_turn", {}).get("task_claimed")
            and any("group_mark_seen" in t
                    for t in result.get("tools_turn", {}).get("tools_called", []))
            and result.get("source_files_unchanged")
        )
        result["passed"] = ok
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        if result.get("passed") is True:
            shutil.rmtree(data_dir, ignore_errors=True)

    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
