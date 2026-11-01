"""req#138 task#269：dsh 后端返修真机验证（F8 入口发现 + F10 协议失败终态）。

两段（各自独立数据目录，可独立复跑）：
  --segment f8   桌面 bundled dsh.cmd 无需 DSH_BIN/PATH 即可发现（真实注册表/文件系统）
  --segment f10  exit 0 但缺 final 的 fake dsh → wake_runs.exit_code != 0 + 执行错误落库
运行：python -X utf8 -B scripts/verify_dsh_rework.py --test-directory <隔离目录> \\
  --evidence <绝对证据路径> [--segment f8|f10|all]
"""
import argparse
import json
import os
import shutil
import socket
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
    for key in list(os.environ):
        if key.startswith(("GROUP_", "XDG_GROUP_", "HERMES_", "TASK_QUEUE")) \
                or key in ("DEEPSEEK_API_KEY", "DSH_HOME", "DSH_PROFILE", "DSH_BIN"):
            os.environ.pop(key, None)
    os.environ["GROUP_DATA_DIR"] = str(data_dir)
    os.environ["GROUP_ENGINE_HOST"] = "127.0.0.1"
    os.environ["GROUP_ENGINE_PORT"] = str(port)
    os.environ["GROUP_ENGINE_URL"] = f"http://127.0.0.1:{port}"


def _segment_f8() -> dict:
    from engine import discovery
    entry = discovery._dsh_entry()
    return {
        "bundled_entry": entry,
        "available": discovery._dsh_entry_available(entry),
        "passed": bool(entry) and discovery._dsh_entry_available(entry),
    }


def _segment_f10(test_dir: Path) -> dict:
    from engine import db, discovery
    data_dir = Path(test_dir) / "engine-269-f10"
    workdir = Path(test_dir) / "work-269-f10"
    shutil.rmtree(data_dir, ignore_errors=True)
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    _isolate_env(data_dir, port)
    # fake dsh：exit 0 且无任何 JSON 输出（缺 final 的协议失败场景）。
    home = Path(test_dir) / "home-269-f10"
    shutil.rmtree(home, ignore_errors=True)
    (home / "profiles" / "desktop").mkdir(parents=True)
    (home / "profiles" / "desktop" / "cordis.patch.yml").write_text(
        "- id: agent-default-model\n  config:\n    provider: deepseek-account\n    model: deepseek-flash\n",
        encoding="utf-8")
    fake_entry = Path(test_dir) / "fake-dsh.cmd"
    fake_entry.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")

    # 引擎模块须在隔离环境就绪后再 import（config.engine_url 派生）。
    import uvicorn
    from engine.main import app as engine_app

    conn = db.get_connection()
    db.init_schema(conn)
    conn.execute("INSERT OR IGNORE INTO members(profile,display_name,role) "
                 "VALUES('user','user','user')")
    conn.execute("INSERT OR IGNORE INTO members(profile,display_name,role) "
                 "VALUES('dsh-bot','dsh-bot','dsh-bot')")
    conn.execute("INSERT INTO projects(name,root,worktree_root,created_at) VALUES(?,?, '', ?)",
                 ("bot-group", str(workdir), db.now_iso()))
    conn.execute("INSERT OR IGNORE INTO project_members(project,profile,active) "
                 "VALUES('bot-group','user',1)")
    cur = conn.execute(
        "INSERT INTO bots(host_type,profile,config_root,runtime,runtime_entry,display_name,role) "
        "VALUES('dsh','desktop',?,'dsh',?, 'dsh-bot','')",
        (str(home), str(fake_entry)))
    bot_id = cur.lastrowid
    conn.execute("INSERT OR IGNORE INTO project_members(project,profile,bot_id,active) "
                 "VALUES('bot-group','dsh-bot',?,1)", (bot_id,))
    conn.commit()
    conn.close()

    config = uvicorn.Config(engine_app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True, name="engine-269-f10")
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    result = {}
    try:
        if not server.started:
            result = {"ok": False, "detail": "engine failed to start"}
        else:
            import urllib.request
            body = json.dumps({"project": "bot-group", "sender": "user", "text": "@dsh-bot 回复 OK"}).encode()
            req = urllib.request.Request(f"http://127.0.0.1:{port}/msg", data=body,
                                        headers={"Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=30)
            result["post_status"] = resp.status
            # 等该轮结束（wake_runs 落 ended_at）。
            conn = db.get_connection()
            conn.row_factory = None
            for _ in range(120):
                row = conn.execute(
                    "SELECT exit_code, ended_at FROM wake_runs WHERE session='dsh-bot-bot-group-work' "
                    "ORDER BY started_at DESC LIMIT 1").fetchone()
                if row and row[1] is not None:
                    break
                time.sleep(1)
            result["exit_code"] = row[0] if row else None
            errs = conn.execute(
                "SELECT category, diagnostic FROM execution_errors WHERE profile='dsh-bot' "
                "ORDER BY message_id DESC LIMIT 1").fetchall()
            result["error_rows"] = len(errs)
            result["error_diagnostic"] = (errs[0][1] if errs else "")[:300]
            result["protocol_failure_reported"] = any(
                "without a final event" in (e[1] or "") for e in errs)
            conn.close()
            result["passed"] = (result["exit_code"] not in (0, None)
                                and result["error_rows"] >= 1
                                and result["protocol_failure_reported"])
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        shutil.rmtree(data_dir, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(workdir, ignore_errors=True)
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test-directory", type=Path, required=True)
    ap.add_argument("--evidence", type=Path, required=True)
    ap.add_argument("--segment", choices=("f8", "f10", "all"), default="all")
    args = ap.parse_args()
    if not args.test_directory.is_dir():
        raise ValueError("--test-directory 必须是已分配的隔离目录")
    segments = ("f8", "f10") if args.segment == "all" else (args.segment,)
    result = {}
    for seg in segments:
        result[seg] = _segment_f8() if seg == "f8" else _segment_f10(args.test_directory)
    result["passed"] = all(result[s].get("passed") for s in segments)
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
