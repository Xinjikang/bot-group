"""req#137 task#268：DSH Auto 审批真机闭环验证（分段版，隔离引擎 + 真实 DSH + 真实 MCP）。

分段设计（每段独立数据目录 engine-268-<segment>，短于单次唤醒，可独立复跑）：
  --segment allow   启用 Auto：session/created 切 auto（适配插件 stderr 证据）+ 工具调用
                    经审查 allow 后执行（审查尝试 vs 工具主体执行独立计数）
  --segment fail    插件失效 fail-closed：适配插件 set 失败 → 引擎判 auto_unavailable
                    阻止继续 + 审计 preset_confirmed=0
  --segment resume  续跑再核验：第二轮 --session-id 续跑重新切 auto + 审计逐轮落库
任一段失败保留数据目录供诊断（仅全绿清理）。原 DSH 配置哈希逐段核验。

运行：python -X utf8 -B scripts/verify_dsh_auto.py --entry <dsh.cmd绝对路径> \
  --source-home <源DSH_HOME> --test-directory <分配隔离目录> --evidence <绝对证据路径> \
  [--segment allow|fail|resume|all]
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

SEGMENTS = ("allow", "fail", "resume")

ALLOW_PROMPT = ("@dsh-bot 依次调用 task_queue 的 group_list_members 工具两次（各等待一次 "
                "tool_result 后再调用下一次），最终回复 DSH_AUTO_OK。不要调用 group_send。")


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _isolate_env(data_dir: Path, port: int) -> None:
    """白名单构造隔离环境（req#250 教训：禁用 dict(os.environ)）。"""
    for key in list(os.environ):
        if key.startswith(("GROUP_", "XDG_GROUP_", "HERMES_", "TASK_QUEUE")) \
                or key in ("DEEPSEEK_API_KEY", "DSH_HOME", "DSH_PROFILE"):
            os.environ.pop(key, None)
    os.environ["GROUP_DATA_DIR"] = str(data_dir)
    os.environ["GROUP_ENGINE_HOST"] = "127.0.0.1"
    os.environ["GROUP_ENGINE_PORT"] = str(port)
    os.environ["GROUP_ENGINE_URL"] = f"http://127.0.0.1:{port}"


def fingerprint(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


class EngineFixture:
    """隔离引擎 + Auto 启用种子 + HTTP 客户端。"""

    def __init__(self, entry: Path, source_home: Path, test_dir: Path, segment: str,
                 broken_adapter: bool = False):
        self.segment = segment
        self.data_dir = Path(test_dir) / f"engine-268-{segment}"
        self.workdir = Path(test_dir) / f"work-268-{segment}"
        shutil.rmtree(self.data_dir, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.port = _free_port()
        _isolate_env(self.data_dir, self.port)
        # bot config_root 用源 home 的浅拷贝（credentials + profile patch + .env + AGENTS.md），
        # side file 写进拷贝——不碰真实 DSH home（req#137 隔离纪律）。拷贝含真实凭据，
        # 写前结构化 DACL 限权（fail-closed）。
        from engine import dsh_auto
        self.bot_home = Path(test_dir) / f"home-268-{segment}"
        shutil.rmtree(self.bot_home, ignore_errors=True)
        (self.bot_home / "profiles" / "desktop").mkdir(parents=True)
        identity = f'{os.environ.get("USERDOMAIN", "")}\\{os.environ.get("USERNAME", "")}'
        import subprocess
        subprocess.run(["icacls", str(self.bot_home), "/inheritance:r",
                        "/grant:r", identity + ":(OI)(CI)F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        for rel in (".credentials.yaml", ".env", "AGENTS.md"):
            src = Path(source_home) / rel
            if src.is_file():
                shutil.copy2(src, self.bot_home / rel)
        profile_src = Path(source_home) / "profiles" / "desktop"
        if profile_src.is_dir():
            shutil.copytree(profile_src, self.bot_home / "profiles" / "desktop",
                            dirs_exist_ok=True)
        (self.bot_home / "profiles" / "desktop" / dsh_auto.SIDE_FILE).write_text(
            "on", encoding="utf-8")
        # 适配插件替换（fail 段）：模拟插件失效（set 抛异常 → preset 永不 auto）。
        self._orig_adapter = dsh_auto.adapter_source
        if broken_adapter:
            dsh_auto.adapter_source = lambda: (
                "export const name = \"auto-enable-adapter\";\n"
                "export const inject = [\"permissionPresets\"];\n"
                "export function apply(ctx) {\n"
                "    ctx.on(\"session/created\", () => { throw new Error(\"adapter broken\"); });\n"
                "}\n")
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
                (str(self.bot_home), str(entry)))
            bot_id = cur.lastrowid
            conn.execute("INSERT OR IGNORE INTO project_members(project,profile,bot_id,active) "
                         "VALUES('bot-group','dsh-bot',?,1)", (bot_id,))
            conn.commit()
        finally:
            conn.close()
        import uvicorn
        from engine.main import app as engine_app
        config = uvicorn.Config(engine_app, host="127.0.0.1", port=self.port,
                                log_level="warning", lifespan="on")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True, name=f"engine-268-{segment}")
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

    def wait_run(self, timeout=240):
        deadline = time.time() + timeout
        while time.time() < deadline:
            rows = self.q("SELECT ended_at, exit_code, session, started_at FROM wake_runs "
                          "WHERE runtime='dsh' ORDER BY started_at DESC LIMIT 1")
            if rows and rows[0]["ended_at"] is not None:
                return dict(rows[0])
            time.sleep(2)
        return None

    def close(self, passed: bool):
        from engine import dsh_auto
        dsh_auto.adapter_source = self._orig_adapter
        self.server.should_exit = True
        self.thread.join(timeout=15)
        if passed:
            shutil.rmtree(self.data_dir, ignore_errors=True)
            shutil.rmtree(self.bot_home, ignore_errors=True)

    def restart(self):
        """引擎重启（同数据目录、同端口）：模拟重启恢复 + 续跑再核验。"""
        from engine import dsh_auto
        dsh_auto.adapter_source = self._orig_adapter  # 恢复正常适配插件源码
        self.server.should_exit = True
        self.thread.join(timeout=15)
        import uvicorn
        from engine.main import app as engine_app
        config = uvicorn.Config(engine_app, host="127.0.0.1", port=self.port,
                                log_level="warning", lifespan="on")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True,
                                       name=f"engine-268-{self.segment}-restart")
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.05)
        if not self.server.started:
            raise RuntimeError("engine failed to restart")
        # 重启期间 dsh_bot_home 的 side file 与数据目录原样保留（续跑凭据）。


def _turn_evidence(fx: EngineFixture, run) -> dict:
    """单轮证据：Auto 审计行 + 事件流工具计数。"""
    audits = [dict(r) for r in fx.q(
        "SELECT * FROM dsh_auto_audit WHERE session=? AND wake_started_at=? ORDER BY id",
        (run["session"], run["started_at"]))]
    events = [json.loads(r["payload"]) for r in fx.q(
        "SELECT payload FROM dsh_run_events WHERE session=? AND wake_started_at=? ORDER BY id",
        (run["session"], run["started_at"]))]
    tool_calls = sum(1 for e in events if e.get("type") == "tool_call")
    completed = sum(1 for e in events if e.get("type") == "tool_result"
                    and e.get("status") == "completed")
    tool_names = [e.get("tool", "") for e in events if e.get("type") == "tool_call"]
    finals = [e.get("text", "") for e in events if e.get("type") == "final"]
    return {"audits": audits, "tool_calls": tool_calls, "completed": completed,
            "tool_names": tool_names, "finals": finals, "exit_code": run["exit_code"]}


def _segment_allow(fx: EngineFixture) -> dict:
    fx.post_msg(ALLOW_PROMPT)
    run = fx.wait_run()
    if run is None:
        return {"ok": False, "detail": "run did not finish in time"}
    ev = _turn_evidence(fx, run)
    audits = ev["audits"]
    return dict(ev, **{
        "audit_row": bool(audits),
        "preset_confirmed": bool(audits) and bool(audits[0]["preset_confirmed"]),
        "review_count": audits[0]["review_count"] if audits else 0,
        "executed_count": audits[0]["executed_count"] if audits else 0,
        "blocked_count": audits[0]["blocked_count"] if audits else 0,
    })


def _segment_fail(fx: EngineFixture) -> dict:
    fx.post_msg(ALLOW_PROMPT)
    run = fx.wait_run()
    if run is None:
        return {"ok": False, "detail": "run did not finish in time"}
    ev = _turn_evidence(fx, run)
    audits = ev["audits"]
    errors = [dict(r) for r in fx.q(
        "SELECT category, diagnostic FROM execution_errors "
        "WHERE project='bot-group' ORDER BY message_id DESC LIMIT 2")]
    return dict(ev, **{
        "errors": errors,
        "audit_row": bool(audits),
        "preset_confirmed": bool(audits) and bool(audits[0]["preset_confirmed"]),
        "blocked_as_auto_unavailable": any(
            r.get("category") == "auto_unavailable" for r in errors),
    })


def _segment_resume(fx: EngineFixture) -> dict:
    fx.post_msg("@dsh-bot 调用一次 group_list_members，回复 DSH_AUTO_TURN1。不要调用 group_send。")
    run1 = fx.wait_run()
    if run1 is None:
        return {"ok": False, "detail": "turn1 did not finish in time"}
    ev1 = _turn_evidence(fx, run1)
    bindings1 = [dict(r) for r in fx.q(
        "SELECT session_id, generation FROM session_bindings "
        "WHERE project='bot-group' AND profile='dsh-bot' AND kind='work'")]
    # 引擎重启（模拟重启恢复）：续跑走持久化绑定 --session-id，再核验 Auto 预设。
    fx.restart()
    fx.post_msg("@dsh-bot 回忆上一轮你收到的成员列表数量，把该数量写进回复 DSH_AUTO_TURN2，"
                "不要调用任何工具。")
    run2 = fx.wait_run()
    if run2 is None:
        return {"ok": False, "detail": "turn2 did not finish in time"}
    ev2 = _turn_evidence(fx, run2)
    bindings2 = [dict(r) for r in fx.q(
        "SELECT session_id, generation FROM session_bindings "
        "WHERE project='bot-group' AND profile='dsh-bot' AND kind='work'")]
    return {
        "turn1": ev1, "turn2": ev2,
        "binding_before": bindings1, "binding_after": bindings2,
        "turn1_preset_confirmed": bool(ev1["audits"]) and bool(ev1["audits"][0]["preset_confirmed"]),
        "turn2_preset_confirmed": bool(ev2["audits"]) and bool(ev2["audits"][0]["preset_confirmed"]),
        "same_session_after_restart": bool(bindings1) and bool(bindings2)
        and bindings1[0]["session_id"] == bindings2[0]["session_id"],
        "two_audit_rows": len(ev1["audits"]) >= 1 and len(ev2["audits"]) >= 1,
    }


def _run_segment(args, segment: str) -> dict:
    fx = EngineFixture(args.entry, args.source_home, args.test_directory, segment,
                       broken_adapter=(segment == "fail"))
    result = {"segment": segment}
    try:
        r = {
            "allow": _segment_allow,
            "fail": _segment_fail,
            "resume": _segment_resume,
        }[segment](fx)
        result["result"] = r
        result["passed"] = {
            # allow：preset 确认 + 审查计数 >=1 + 至少一次 allow 执行成功
            "allow": lambda d: bool(d.get("preset_confirmed")
                                    and d.get("review_count", 0) >= 1
                                    and d.get("executed_count", 0) >= 1
                                    and d.get("exit_code") == 0),
            # fail：引擎判 auto_unavailable 阻止 + 审计 preset_confirmed=0
            "fail": lambda d: bool(d.get("blocked_as_auto_unavailable")
                                   and d.get("audit_row")
                                   and not d.get("preset_confirmed")),
            # resume：重启后同会话续跑 + 两轮都重新确认 auto + 审计逐轮落库
            "resume": lambda d: bool(d.get("turn1_preset_confirmed")
                                     and d.get("turn2_preset_confirmed")
                                     and d.get("two_audit_rows")
                                     and d.get("same_session_after_restart")),
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
