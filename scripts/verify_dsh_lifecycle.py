"""req#137 task#267：dsh 命令与生命周期闭环真实验证（分段版，隔离引擎 + 真实 DSH + 真实 MCP）。

分段设计（manager 指示：单段短于单次唤醒，可独立复跑）：
  --segment business  业务闭环：group_send 出站 + 回复（真实引擎 MCP 身份）
  --segment stop      运行中真实进程树 /stop：killed=1 + 队列取消 + 绑定保留
  --segment new       /new：切换 committed + generation+1 + 旧会话审计保留
  --segment marker    项目指令：AGENTS.md 标记经 cwd 自动加载召回（新会话）
  --segment tools     工具权限：task_claim 领取 + group_mark_seen 已读确认
每段独立数据目录 engine-267-<segment>，证据写 <evidence>.<segment>.json，
任一段失败保留数据目录供诊断（仅全绿清理）。原 DSH 配置哈希逐段核验。

运行：python -X utf8 -B scripts/verify_dsh_lifecycle.py --entry <dsh.cmd绝对路径> \
  --source-home <源DSH_HOME> --test-directory <分配隔离目录> --evidence <绝对证据路径> \
  [--segment business|stop|new|marker|tools|all]
"""
import argparse
import hashlib
import json
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SEGMENTS = ("business", "stop", "new", "marker", "tools")


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


class EngineFixture:
    """隔离引擎 + 种子 + HTTP 客户端。"""

    def __init__(self, entry: Path, source_home: Path, test_dir: Path, segment: str,
                 with_task: bool = False):
        self.segment = segment
        self.data_dir = Path(test_dir) / f"engine-267-{segment}"
        self.workdir = Path(test_dir) / f"work-267-{segment}"
        shutil.rmtree(self.data_dir, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)
        self.workdir.mkdir(parents=True, exist_ok=True)
        (self.workdir / "AGENTS.md").write_text(
            "# Project Rules\n\nTHE_PROJECT_MARKER_267 = alpha-7.\n"
            "If asked for the marker, reply alpha-7.\n", encoding="utf-8")
        self.port = _free_port()
        _isolate_env(self.data_dir, self.port)
        import httpx
        self.httpx = httpx
        from engine import db
        self.db = db
        conn = db.get_connection()
        try:
            db.init_schema(conn)
            conn.execute("INSERT OR IGNORE INTO members(profile,display_name,role) "
                         "VALUES('user','user','user')")
            conn.execute("INSERT OR IGNORE INTO members(profile,display_name,role) "
                         "VALUES('dsh-bot','dsh-bot','dsh-bot')")
            conn.execute("INSERT INTO projects(name,root,worktree_root,created_at) VALUES(?,?, '', ?)",
                         ("bot-group", str(self.workdir), db.now_iso()))
            conn.execute("INSERT OR IGNORE INTO project_members(project,profile,active) "
                         "VALUES('bot-group','user',1)")
            cur = conn.execute(
                "INSERT INTO bots(host_type,profile,config_root,runtime,runtime_entry,display_name,role) "
                "VALUES('dsh','desktop',?,'dsh',?, 'dsh-bot','')",
                (str(source_home), str(entry)))
            bot_id = cur.lastrowid
            conn.execute("INSERT OR IGNORE INTO project_members(project,profile,bot_id,active) "
                         "VALUES('bot-group','dsh-bot',?,1)", (bot_id,))
            if with_task:
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
        import uvicorn
        from engine.main import app as engine_app
        config = uvicorn.Config(engine_app, host="127.0.0.1", port=self.port,
                                log_level="warning", lifespan="on")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True, name=f"engine-267-{segment}")
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.05)
        if not self.server.started:
            raise RuntimeError("engine failed to start")
        self.base = f"http://127.0.0.1:{self.port}"

    def q(self, sql: str, params=()):
        conn = self.db.get_connection()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def post_msg(self, text: str) -> dict:
        resp = self.httpx.post(f"{self.base}/msg", json={
            "project": "bot-group", "sender": "user", "text": text, "need_reply": 1}, timeout=60)
        if resp.status_code >= 400:
            raise RuntimeError(f"POST /msg {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def wait_run(self, timeout=180):
        deadline = time.time() + timeout
        while time.time() < deadline:
            rows = self.q("SELECT ended_at, exit_code, session FROM wake_runs "
                          "WHERE runtime='dsh' ORDER BY started_at DESC LIMIT 1")
            if rows and rows[0]["ended_at"] is not None:
                return dict(rows[0])
            time.sleep(2)
        return None

    def live_run(self, timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            rows = self.q("SELECT session, pid, started_at FROM wake_runs WHERE runtime='dsh' "
                          "AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1")
            if rows:
                return dict(rows[0])
            time.sleep(1)
        return None

    def close(self, passed: bool):
        self.server.should_exit = True
        self.thread.join(timeout=15)
        if passed:
            shutil.rmtree(self.data_dir, ignore_errors=True)


def _segment_business(fx: EngineFixture) -> dict:
    fx.post_msg("@dsh-bot 请先调用 task_queue 的 group_send 工具发送一条内容为 "
                "DSH_MCP_OK_alpha-7 的消息，然后只回复 DSH_LIFECYCLE_OK。")
    run = fx.wait_run()
    if run is None:
        return {"ok": False, "detail": "run did not finish in time"}
    events = fx.q("SELECT payload FROM dsh_run_events WHERE session=? ORDER BY id",
                  (run["session"],))
    parsed = [json.loads(r["payload"]) for r in events]
    from engine import dsh_reasoning
    sent = fx.q("SELECT id, content, sender FROM messages WHERE content LIKE ? ORDER BY id DESC",
                ("%DSH_MCP_OK_alpha-7%",))
    return {
        "run_exit": run["exit_code"],
        "event_types": [p.get("type") for p in parsed],
        "sent_rows": len(sent) if sent is not None else 0,
        "sent_by_bot": bool(sent) and sent[0]["sender"] == "dsh-bot",
        "group_send_called": any(p.get("tool", "").endswith("group_send")
                                 for p in parsed if p.get("type") == "tool_call"),
        "reply_message_id": [dsh_reasoning._message_id(p.get("result"))
                             for p in parsed if p.get("type") == "tool_result"],
    }


def _segment_stop(fx: EngineFixture) -> dict:
    from engine import wake
    # 第一轮完整跑完建会话绑定（/stop「恢复原会话」的前提）
    fx.post_msg("@dsh-bot 只回复 DSH_STOP_PRE。")
    run0 = fx.wait_run()
    if run0 is None:
        return {"ok": False, "detail": "pre-run did not finish in time"}
    binding_before = [dict(r) for r in fx.q(
        "SELECT session_id, generation FROM session_bindings "
        "WHERE project='bot-group' AND profile='dsh-bot' AND kind='work'")]
    # 第二轮运行中 /stop：真实进程树终止 + 绑定保留
    fx.post_msg("@dsh-bot 从 1 数到 20，每个数调用一次 group_list_members 工具，"
                "最后回复 DSH_STOP_TARGET。")
    run = fx.live_run()
    if run is None:
        return {"ok": False, "detail": "no live dsh run observed"}
    pids_before = []
    for _ in range(20):
        pids_before = wake.find_bot_processes("dsh-bot", run["session"])
        if pids_before:
            break
        time.sleep(0.5)
    resp = fx.post_msg("/stop @dsh-bot")
    time.sleep(2)
    pids_after = wake.find_bot_processes("dsh-bot", run["session"])
    binding_after = [dict(r) for r in fx.q(
        "SELECT session_id, generation FROM session_bindings "
        "WHERE project='bot-group' AND profile='dsh-bot' AND kind='work'")]
    return {
        "pids_before": pids_before,
        "pids_after": pids_after,
        "proc_gone": bool(pids_before) and not pids_after,
        "stop_body": resp,
        "jobs": [dict(r) for r in fx.q("SELECT id, status FROM wake_jobs")],
        "binding_before": binding_before,
        "binding_after": binding_after,
        "binding_preserved": bool(binding_before) and bool(binding_after)
        and binding_before[0]["session_id"] == binding_after[0]["session_id"],
    }


def _segment_new(fx: EngineFixture) -> dict:
    fx.post_msg("@dsh-bot 只回复 DSH_NEW_PRE。")
    run = fx.wait_run()
    if run is None:
        return {"ok": False, "detail": "pre-run did not finish in time"}
    before = fx.q("SELECT session_id, generation FROM session_bindings "
                  "WHERE project='bot-group' AND profile='dsh-bot' AND kind='work'")
    resp = fx.post_msg("/new @dsh-bot")
    after = fx.q("SELECT session_id, generation FROM session_bindings "
                 "WHERE project='bot-group' AND profile='dsh-bot' AND kind='work'")
    ops = fx.q("SELECT status, old_session_id, new_session_id FROM session_switch_ops "
               "ORDER BY id DESC")
    return {
        "new_body": resp,
        "binding_before": [dict(r) for r in before],
        "binding_after": [dict(r) for r in after],
        "switch_ops": [dict(r) for r in ops],
        "generation_bumped": bool(before) and bool(after)
        and after[0]["generation"] > before[0]["generation"],
    }


def _segment_marker(fx: EngineFixture) -> dict:
    fx.post_msg("@dsh-bot 阅读你工作区的 AGENTS.md 文件，把 THE_PROJECT_MARKER_267 的值"
                "作为最终回复内容。不要调用任何工具，直接回复该值。")
    run = fx.wait_run()
    if run is None:
        return {"ok": False, "detail": "run did not finish in time"}
    events = fx.q("SELECT payload FROM dsh_run_events WHERE session=? ORDER BY id",
                  (run["session"],))
    finals = [json.loads(r["payload"]).get("text", "")
              for r in events if json.loads(r["payload"]).get("type") == "final"]
    return {"run_exit": run["exit_code"], "finals": finals,
            "marker_recalled": any("alpha-7" in (f or "") for f in finals)}


def _segment_tools(fx: EngineFixture) -> dict:
    fx.post_msg("@dsh-bot 依次执行：1) 调用 task_queue 的 task_claim 领取你的待办任务；"
                "2) 调用 group_send 发送 '@user DSH_TOOLS_CLAIMED' 作为实质回复；"
                "3) 调用 group_read_inbox 读收件箱并对其中 pending 消息调用 group_mark_seen；"
                "4) 最终回复 DSH_TOOLS_OK。")
    run = fx.wait_run()
    if run is None:
        return {"ok": False, "detail": "run did not finish in time"}
    events = fx.q("SELECT payload FROM dsh_run_events WHERE session=? ORDER BY id",
                  (run["session"],))
    parsed = [json.loads(r["payload"]) for r in events]
    tools = [p.get("tool", "") for p in parsed if p.get("type") == "tool_call"]
    results = [json.loads(p.get("result") or "{}")
                for p in parsed if p.get("type") == "tool_result"]
    # 领取证据取工具回执（引擎在 run 结束后会释放未交接任务，落库终态不可靠）
    claim_ok = any(isinstance(r, dict) and r.get("claimed") is True for r in results)
    seen_ok = any(isinstance(r, dict) and bool(r.get("seen", 0)) for r in results)
    inbox = fx.q("SELECT message_id, status FROM inbox WHERE profile='dsh-bot'")
    sent = fx.q("SELECT id, sender FROM messages WHERE content LIKE '%DSH_TOOLS_CLAIMED%'")
    return {"run_exit": run["exit_code"], "tools_called": tools,
            "task_claimed": claim_ok,
            "mark_seen_called": any("group_mark_seen" in t for t in tools),
            "mark_seen_ok": seen_ok,
            "replied_before_mark_seen": bool(sent),
            "inbox": [dict(r) for r in inbox]}


def _run_segment(args, segment: str) -> dict:
    with_task = segment == "tools"
    fx = EngineFixture(args.entry, args.source_home, args.test_directory, segment,
                       with_task=with_task)
    result = {"segment": segment}
    try:
        r = {
            "business": _segment_business,
            "stop": _segment_stop,
            "new": _segment_new,
            "marker": _segment_marker,
            "tools": _segment_tools,
        }[segment](fx)
        result["result"] = r
        result["passed"] = {
            "business": lambda d: d.get("sent_rows", 0) >= 1 and d.get("sent_by_bot")
            and d.get("group_send_called") and bool(d.get("reply_message_id")),
            "stop": lambda d: bool(d.get("proc_gone") and d.get("binding_preserved")
                                   and d.get("stop_body", {}).get("ok")),
            "new": lambda d: bool(d.get("generation_bumped") and d.get("switch_ops")),
            "marker": lambda d: bool(d.get("marker_recalled")),
            "tools": lambda d: bool(d.get("task_claimed") and d.get("mark_seen_ok")
                                    and d.get("replied_before_mark_seen")),
        }[segment](r)
        return result
    finally:
        fx.close(result.get("passed") is True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry", type=Path, required=True, help="dsh.cmd 绝对路径")
    ap.add_argument("--source-home", type=Path, required=True, help="源 DSH_HOME")
    ap.add_argument("--test-directory", type=Path, required=True)
    ap.add_argument("--evidence", type=Path, required=True)
    ap.add_argument("--segment", default="all", choices=[*SEGMENTS, "all"])
    args = ap.parse_args()
    if not args.entry.is_file():
        raise ValueError(f"--entry 不是文件：{args.entry}")
    if not args.source_home.is_dir():
        raise ValueError(f"--source-home 不是目录：{args.source_home}")

    source_files = [args.source_home / ".credentials.yaml",
                    args.source_home / "profiles" / "desktop" / "cordis.patch.yml"]
    before = {str(p): fingerprint(p) for p in source_files if p.is_file()}
    segs = SEGMENTS if args.segment == "all" else (args.segment,)
    outputs = {}
    all_passed = True
    for seg in segs:
        outputs[seg] = _run_segment(args, seg)
        all_passed = all_passed and bool(outputs[seg].get("passed"))
    outputs["source_files_unchanged"] = before == {
        str(p): fingerprint(p) for p in source_files if p.is_file()}
    outputs["passed"] = all_passed and outputs["source_files_unchanged"]
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(outputs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(outputs, ensure_ascii=False, indent=2))
    return 0 if outputs["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
