"""Real /steer delivery to a running isolated Hermes execution (req#136 task#261).

Verifies the inserted consumption-evidence observation path end-to-end against a
real isolated gateway: spawn a running turn whose MCP tool blocks, deliver /steer
via session.steer, then assert the OUT-OF-BAND marker appears in the session
history and the ledger reaches consumed/inserted.

Prerequisites (reuse task-250 isolated install):
- GW_PROBE_DATA points to the allocated test-data root.
- The Hermes source worktree (.probe-worktrees/hermes-v0225) is present; the
  gateway runs from that source via PYTHONPATH (install venv has no hermes_cli).
- A configured model + credentials in the isolated install (config.yaml/.env).

Usage:
    python scripts/verify_gateway_steer_live.py --preflight-only
    python scripts/verify_gateway_steer_live.py --backend http://127.0.0.1:<port> --evidence <abs dir>
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def preflight() -> dict:
    """核对真实隔离验收的前置资源，缺失时给出具体原因。"""
    import yaml
    import gw_probe as probe
    from scripts.probe_config import absolute_path
    checks = {}
    try:
        data = absolute_path(os.environ.get("GW_PROBE_DATA", ""), "GW_PROBE_DATA")
        checks["gw_probe_data"] = str(data)
    except ValueError as exc:
        return {"ok": False, "reason": str(exc), "checks": checks}
    record, detail = probe.install_record()
    if not record:
        return {"ok": False, "reason": f"install_record 不可用: {detail}", "checks": checks}
    checks["install"] = {"install_id": detail.get("install_id"), "python": detail.get("python")}
    src = probe.WORKTREE_SRC
    checks["worktree_src"] = str(src)
    if not (src / "hermes_cli").is_dir():
        return {"ok": False,
                "reason": f"Hermes 源码 worktree 缺失（{src}），网关无法启动（安装 venv 无 hermes_cli，PYTHONPATH 依赖该源码）",
                "checks": checks}
    checks["worktree_ok"] = True
    cfg = yaml.safe_load((probe.HERMES_HOME / "config.yaml").read_text(encoding="utf-8"))
    checks["model"] = cfg.get("model", {})
    return {"ok": True, "checks": checks}


def steer_mcp():
    """隔离 MCP：运行中 turn 阻塞，等待 /steer 注入后返回被纠偏的结果。"""
    from mcp.server.fastmcp import FastMCP
    server = FastMCP("steer_probe")

    @server.tool()
    def steer_wait() -> str:
        """Wait, then report the steer correction observed by the controller."""
        root = Path(os.environ["STEER_PROBE_WORK"])
        (root / "ready").touch()
        deadline = time.monotonic() + 300
        while not (root / "steered").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Steer controller did not confirm delivery")
            time.sleep(0.2)
        return "release acknowledged"

    server.run()


def serve(url):
    """隔离引擎：暴露 /probe/spawn，用真实网关 spawn 运行中执行。

    长连接路径：不覆盖 gateway.connection，引擎经 hermes_connections.acquire →
    discover(HERMES_HOME) 复用 verify() 已 ensure 的隔离网关（不退回短连接）。
    """
    from fastapi import Depends
    from engine import main, hermes_gateway as gateway, db
    issued = {}
    original_configure = gateway.sessions.configure_tracked

    def configure(conn, project, client, snapshot, **kwargs):
        snapshot = original_configure(conn, project, client, snapshot, **kwargs)
        server = kwargs["server"]
        context = json.loads(server["env"]["XDG_GROUP_ENGINE_EXECUTION"])
        issued[context["thread_id"]] = server["env"]["GROUP_ENGINE_TOKEN"]
        specs = [{"name": "task_queue", "command": server["command"], "args": server["args"],
                  "env": [{"name": k, "value": v} for k, v in server["env"].items()]},
                 {"name": "steer_probe", "command": sys.executable, "args": [__file__, "--mcp"],
                  "env": [{"name": "STEER_PROBE_WORK", "value": os.environ["STEER_PROBE_WORK"]}]}]
        gateway.sessions.mutate(client, snapshot, "editor_mcp", {"mcp_servers": specs})
        return gateway.sessions.attach(client, snapshot["session_id"])

    gateway.sessions.configure_tracked = configure

    @main.app.post("/probe/spawn")
    def spawn(payload: dict, identity=Depends(main._resolve_identity)):
        main._admin_only(identity, "Probe administrator required")
        conn = db.get_connection()
        try:
            bot_id = db._ensure_hermes_bot(conn, "default")
            bot = conn.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
        finally:
            conn.close()
        result = gateway.spawn("default", "chat", payload["signal"], payload["thread"], "alpha",
                               bot=bot, workdir=os.environ["STEER_PROBE_WORK"], model_args=[])
        return {**result, "credential": issued.pop(payload["thread"], None)}

    target = urlsplit(url)
    main.main(["--host", target.hostname, "--port", str(target.port), "--data-dir", os.environ["GROUP_DATA_DIR"]])


def verify(args):
    import httpx
    import yaml
    import gw_probe as probe
    from engine import db, auth
    from engine.hermes_gateway_client import ensure
    from scripts.probe_config import absolute_path, loopback_url
    from scripts.project_test_support import enable_members

    url = loopback_url(args.backend)
    target = urlsplit(url)
    with socket.socket() as check:
        check.bind((target.hostname, target.port))
    evidence = absolute_path(args.evidence, "--evidence")
    evidence.mkdir(parents=True, exist_ok=False)
    record, detail = probe.install_record()
    if not record:
        raise RuntimeError(detail)
    run = probe.PROBE_DATA / "task-261" / uuid.uuid4().hex
    home, workspace, data = run / "home", run / "workspace", run / "engine"
    for directory in (home, workspace, data):
        directory.mkdir(parents=True)
    config = yaml.safe_load((probe.HERMES_HOME / "config.yaml").read_text(encoding="utf-8"))
    config.update(mcp_servers={}, approvals={"mode": "smart"},
                  terminal={"backend": "local", "cwd": str(workspace), "timeout": 120})
    config["platform_toolsets"] = {"acp": ["todo"]}
    config.setdefault("agent", {})["max_turns"] = 12
    (home / "config.yaml").write_text(json.dumps(config), encoding="utf-8")
    shutil.copy2(probe.HERMES_HOME / ".env", home / ".env")
    command = [str(record["python"]), "-X", "utf8", "-B", "-m", "hermes_cli.main"]
    env = probe.probe_env()
    env.update(HERMES_HOME=str(home), HERMES_RUNTIME_DIR=str(run / "runtime"),
               PYTHONPATH=os.pathsep.join((str(ROOT), str(probe.WORKTREE_SRC))),
               GROUP_DATA_DIR=str(data), GROUP_DB=str(data / "group.db"),
               GROUP_ENGINE_HOST=target.hostname, GROUP_ENGINE_PORT=str(target.port),
               GROUP_ENGINE_URL=url, GROUP_ATTACHMENTS_DIR=str(data / "attachments"),
               GROUP_ADMIN_PASSWORD="isolated-steer-password", GROUP_SECRET=secrets.token_hex(32),
               GROUP_ENGINE_KEY=secrets.token_hex(32), STEER_PROBE_WORK=str(workspace),
               STEER_PROBE_COMMAND=json.dumps(command), GROUP_MCP_PYTHON=sys.executable)
    os.environ.update(env)
    subprocess.run([sys.executable, str(ROOT / "tools" / "install.py"), "repair"],
                   cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    conn = db.get_connection()
    db.init_schema(conn)
    conn.execute("INSERT OR IGNORE INTO members(profile,display_name,role) VALUES('default','probe','dev')")
    conn.execute("INSERT INTO projects(name,root,created_at) VALUES('alpha',?,?)", (str(workspace), db.now_iso()))
    enable_members(conn, ("alpha",), ("default", "user"))
    thread = conn.execute(
        "INSERT INTO threads(project,title,creator,members,created_at,last_active_at,turn_profile,turn_wake_at) "
        "VALUES('alpha','steer','default','[\"user\",\"default\"]',?,?, 'default',?)",
        (db.now_iso(), db.now_iso(), db.now_iso())).lastrowid
    conn.commit()
    headers = {"Authorization": "Bearer " + auth.login("isolated-steer-password")["token"]}
    client = httpx.Client(base_url=url, headers=headers, timeout=10, trust_env=False)
    result = {"url": url, "data_dir": str(data), "database": str(data / "group.db"),
              "checks": {}, "resources_released": False}
    endpoint = None
    initial = None
    try:
        endpoint = ensure(command, home, env=env, timeout=120)
        env["STEER_PROBE_ENDPOINT"] = json.dumps(endpoint)
        with (evidence / "boot.log").open("wb") as log:
            initial = subprocess.Popen([sys.executable, "-X", "utf8", "-B", __file__, "--serve", "--backend", url],
                                      cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + 75
        boot = None
        while time.monotonic() < deadline:
            try:
                value = client.get("/health").json()
                if value.get("instance_id"):
                    boot = value
                    break
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(0.2)
        assert boot is not None, "isolated engine readiness deadline exceeded"
        signal = ("Isolated steer test. First call group_read_inbox, then call tool_search for steer_wait, "
                  "then call steer_wait exactly once and wait for its result. "
                  "After it returns, call group_send exactly once with text \"[收口] @user STEER_ORIGINAL\" "
                  "and need_reply=false, then finish.")
        response = client.post("/probe/spawn", json={"thread": thread, "signal": signal}, timeout=120)
        response.raise_for_status()
        assert response.json()["ok"], response.json()
        credential = response.json()["credential"]
        deadline = time.monotonic() + 150
        while not (workspace / "ready").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("model did not enter steer_wait")
            time.sleep(1)
        row = conn.execute("SELECT * FROM hermes_executions WHERE thread_id=? ORDER BY started_at DESC LIMIT 1",
                           (thread,)).fetchone()
        assert row is not None and row["state"] == "running", row
        # 发送 /steer，投递到运行中执行
        steer_text = "请改为发送 STEER_CORRECTED 代替 STEER_ORIGINAL，并在最终回复中写明 STEER_CORRECTED"
        from engine import commands, steer_ledger
        outcome = commands.dispatch(conn, "alpha", "user", "user", f"/steer @default {steer_text}", thread)
        result["steer_outcome"] = outcome
        assert outcome["statuses"] == ["queued"], outcome
        op_id = outcome["operations"][0]
        (workspace / "steered").touch()
        (workspace / "correction").write_text(steer_text, encoding="utf-8")
        # 轮询观察路径：marker 出现 → report_consumed(inserted)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            op = steer_ledger.get_operation(conn, op_id)
            if op["status"] == "consumed":
                break
            time.sleep(1)
        op = steer_ledger.get_operation(conn, op_id)
        result["final_operation"] = dict(op)
        result["checks"]["inserted_evidence"] = (op["status"] == "consumed"
                                                 and op["deliver_layer"] == "inserted")
        # 行为改变：模型收到纠偏后返回 STEER_APPLIED / STEER_B
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            exec_row = conn.execute("SELECT * FROM hermes_executions WHERE execution_id=?",
                                    (row["execution_id"],)).fetchone()
            if exec_row["state"] == "terminal" and not exec_row["cleanup_pending"]:
                break
            time.sleep(1)
        from engine.hermes_gateway_client import GatewayClient
        with GatewayClient(endpoint) as viewer:
            snapshot = viewer.rpc("session.resume", session_id=row["session_id"])
            history = viewer.rpc("session.export", session_id=row["session_id"], format="json")["content"]
        # 行为改变：目标 assistant 最终输出反映纠偏（不搜整段 history，防 steer 输入/工具返回泄露假阳性）
        final_assistant = ""
        for message in reversed(snapshot.get("messages") or []):
            if isinstance(message, dict) and message.get("role") == "assistant":
                content = message.get("content")
                if isinstance(content, str) and content.strip():
                    final_assistant = content
                    break
        for secret in [env["GROUP_SECRET"], env["GROUP_ENGINE_KEY"], env["GROUP_ADMIN_PASSWORD"], credential]:
            history = history.replace(secret, "[REDACTED]")
        (evidence / "history.json").write_text(history, encoding="utf-8")
        result["checks"]["behavior_observed"] = bool(final_assistant) and "STEER_CORRECTED" in final_assistant
        result["checks"]["steer_message_recorded"] = conn.execute(
            "SELECT count(*) FROM messages WHERE thread_id=? AND content LIKE ?",
            (thread, "%STEER_CORRECTED%")).fetchone()[0] == 1
        result["history"] = {"marker_present": "OUT-OF-BAND USER MESSAGE" in history,
                             "steer_text_present": steer_text in history,
                             "final_assistant": final_assistant}
    finally:
        try:
            from engine.maintenance.stop_engine import stop
            stop(url, timeout=25)
        finally:
            if initial is not None:
                initial.wait(timeout=10)
            if endpoint:
                subprocess.run(command + ["gateway", "stop", "--yes"], env=env,
                               capture_output=True, text=True, encoding="utf-8", timeout=60,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with socket.socket() as check:
                result["resources_released"] = check.connect_ex((target.hostname, target.port)) != 0
            client.close()
            conn.close()
            (evidence / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    required_checks = ("inserted_evidence", "behavior_observed", "steer_message_recorded")
    failed = [k for k in required_checks if not result["checks"].get(k)]
    if not result["resources_released"]:
        failed.append("resources_released")
    if failed:
        print(json.dumps({"FAILED_CHECKS": failed, "checks": result["checks"]}, ensure_ascii=False), flush=True)
        sys.exit(1)
    print(json.dumps(result["checks"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend")
    parser.add_argument("--evidence")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--mcp", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    if args.mcp:
        steer_mcp()
    elif args.serve and args.backend:
        serve(args.backend)
    elif args.preflight_only:
        print(json.dumps(preflight(), ensure_ascii=False, indent=2))
    elif args.evidence and args.backend:
        verify(args)
    else:
        parser.error("--evidence and --backend are required")
