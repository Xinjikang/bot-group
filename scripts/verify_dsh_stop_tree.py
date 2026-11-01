"""req#138 task#272：dsh /stop 清理验证——真实根及 Node/Electron/MCP/工具后代退出。

#267 lifecycle-stop.json 已记录根消失（pids_after=[]），但未逐项记录后代进程退出。本验证：
  1. 运行中快照 DSH 根及其完整后代进程树（Electron/node/MCP/工具），按进程名归类取证；
  2. /stop 后快照对比：根 + 全部后代无残留；
  3. 引擎重启后再次 /stop（命令行身份定位 + _dsh_spawn_tag），仍正确定位并终止；
  4. 源 DSH home 配置哈希不变。

分段（每段独立数据目录）：
  --segment stop      运行中 /stop：根 + 后代进程树全部退出
  --segment restart   引擎重启后 /stop 仍正确定位并终止（同数据目录重启 + 续跑再停）

运行：python -X utf8 -B scripts/verify_dsh_stop_tree.py --entry <dsh.cmd绝对路径> \
  --source-home <源DSH_HOME> --test-directory <分配隔离目录> --evidence <绝对证据路径> \
  [--segment stop|restart|all]
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

SEGMENTS = ("stop", "restart")


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _isolate_env(data_dir: Path, port: int) -> None:
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


def _process_tree():
    """全进程快照 {pid: (parent, name_lower)}（process_tree.snapshot，非本地化 shell 解析）。"""
    from engine import process_tree
    return process_tree.snapshot()


def _descendants(snap, root_pids) -> dict:
    """root_pids 的完整后代 {pid: (parent, name)}（不含 root 自身）。"""
    roots = set(root_pids)
    children = set(roots)
    while True:
        found = {pid for pid, (parent, _) in snap.items() if parent in children}
        if found <= children:
            break
        children.update(found)
    return {pid: snap[pid] for pid in children if pid not in roots and pid in snap}


def _classify(names):
    electron = [n for n in names if "harness" in n or "electron" in n]
    node = [n for n in names if n.startswith("node") or "node.exe" in n]
    python = [n for n in names if "python" in n]
    return {
        "count": len(names),
        "electron": electron,
        "node": node,
        "python": python,
        "others": [n for n in names if n not in electron + node + python],
    }


class EngineFixture:
    """隔离引擎 + dsh-bot 种子 + HTTP 客户端。"""

    def __init__(self, entry: Path, source_home: Path, test_dir: Path, segment: str,
                 data_dir: Path = None):
        self.segment = segment
        self.data_dir = data_dir or (Path(test_dir) / f"engine-272-{segment}")
        self.workdir = Path(test_dir) / f"work-272-{segment}"
        shutil.rmtree(self.data_dir, ignore_errors=True)
        shutil.rmtree(self.workdir, ignore_errors=True)
        self.workdir.mkdir(parents=True, exist_ok=True)
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
            conn.commit()
        finally:
            conn.close()
        self._start()

    def _start(self):
        import uvicorn
        from engine.main import app as engine_app
        config = uvicorn.Config(engine_app, host="127.0.0.1", port=self.port,
                                log_level="warning", lifespan="on")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True,
                                       name=f"engine-272-{self.segment}")
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
            rows = self.q("SELECT session, started_at FROM wake_runs WHERE runtime='dsh' "
                          "AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1")
            if rows:
                return dict(rows[0])
            time.sleep(1)
        return None

    def restart(self):
        self.server.should_exit = True
        self.thread.join(timeout=15)
        self._start()

    def close(self):
        self.server.should_exit = True
        self.thread.join(timeout=15)


def _stop_one(fx: EngineFixture) -> dict:
    """一轮：起长 DSH 运行 → 持续轮询记录后代进程树并集 → /stop → 快照对比后代退出。"""
    from engine import wake
    fx.post_msg("@dsh-bot 依次调用 group_list_members 工具至少 8 次（每次等 tool_result 再"
                "下一次），过程中保持运行，最后回复 DSH_STOP_272。")
    run = fx.live_run()
    if run is None:
        return {"ok": False, "detail": "no live dsh run observed"}
    roots = []
    for _ in range(40):
        roots = wake.find_bot_processes("dsh-bot", run["session"]) or []
        if roots:
            break
        time.sleep(0.5)
    # 持续轮询后代进程树并集（MCP 服务器 python.exe/node.exe 在模型调工具后才 spawn，
    # 且可能短命——用并集记录，避免单次快照漏采）。
    seen: dict[int, str] = {}
    deadline = time.time() + 20
    while time.time() < deadline:
        snap = _process_tree()
        desc = _descendants(snap, roots)
        for pid, (_parent, name) in desc.items():
            seen.setdefault(pid, name)
        names = {n for n in seen.values()}
        if names - {"conhost.exe", "deepseek harness.exe", "cmd.exe"}:
            time.sleep(1.0)
            snap = _process_tree()
            desc = _descendants(snap, roots)
            for pid, (_parent, name) in desc.items():
                seen.setdefault(pid, name)
            break
        time.sleep(0.3)
    names_before = sorted({n for n in seen.values()})
    resp = fx.post_msg("/stop @dsh-bot")
    time.sleep(3)
    snap_after = _process_tree()
    alive = [pid for pid in seen if pid in snap_after]
    roots_after = wake.find_bot_processes("dsh-bot", run["session"]) or []
    return {
        "ok": bool(resp.get("ok")),
        "roots_before": roots,
        "roots_after": roots_after,
        "root_gone": bool(roots) and not roots_after,
        "descendants_before": names_before,
        "descendants_before_count": len(seen),
        "classify_before": _classify(names_before),
        "descendants_alive_after": [snap_after[p][1] for p in alive],
        "descendants_all_gone": bool(seen) and not alive,
    }


def _run_segment(args, segment: str) -> dict:
    data_dir = Path(args.test_directory) / f"engine-272-{segment}"
    fx = EngineFixture(args.entry, args.source_home, args.test_directory, segment,
                       data_dir=data_dir)
    result = {"segment": segment}
    try:
        r = _stop_one(fx)
        result["result"] = r
        if segment == "restart":
            # 引擎重启（同数据目录、命令行身份定位）后再 /stop，验证重启后仍能定位并终止。
            fx.restart()
            r2 = _stop_one(fx)
            result["result"]["after_restart"] = r2
            result["passed"] = (bool(r.get("ok") and r.get("root_gone")
                                      and r.get("descendants_all_gone")
                                      and r2.get("ok") and r2.get("root_gone")
                                      and r2.get("descendants_all_gone")))
        else:
            result["passed"] = bool(r.get("ok") and r.get("root_gone")
                                    and r.get("descendants_all_gone"))
        return result
    finally:
        fx.close()
        if result.get("passed"):
            shutil.rmtree(data_dir, ignore_errors=True)
            shutil.rmtree(Path(args.test_directory) / f"work-272-{segment}", ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry", type=Path, required=True)
    ap.add_argument("--source-home", type=Path, required=True)
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
