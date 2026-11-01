"""req#138 task#272 rework②（manager node1600）：dsh /stop 清理验证——根 + 后代独立存活核验 + 独立引擎进程重启。

manager node1600 三组缺口闭环：
  ① 假阳性：原版 _stop_one 把 find_bot_processes 的 None（未知）经 ``or []`` 变「已消失」，
     root_gone 只看同一查找函数，无独立原 PID 生存核验。本版：根/后代用**停止前身份
     （PID+名+父）独立存活核验**（process_tree.snapshot，不走 find_bot_processes）；finder
     返回 None（未知/访问失败）显式记 finder_unknown_after 并 fail（不当作「已消失」）。
  ② restart 段证据不足：原版同 Python 进程内重启 uvicorn 线程，模块内存 _native_procs 仍存，
     不能证明「新引擎进程接管原运行的命令行身份定位」。本版：**独立引擎子进程**
     （python -m engine.main --host --port --data-dir）——记录旧引擎 PID 退出、新引擎 PID 变更、
     原 DSH 执行保持存活，由新引擎进程经命令行身份（_matches_dsh_execution + _dsh_spawn_tag）
     恢复定位后 /stop。
  ③ 后代证据仅名称集合 + 工具 body 子进程缺覆盖 + 无受保护对照。本版：后代逐 PID 记
     (parent, name)；注入合成 spawner MCP 工具（body 里 spawn 长活子进程 + 写 PID 文件）覆盖
     「工具启动的子进程」；受保护对照=引擎子进程 PID + 验证脚本自身 PID 在 /stop 后仍存活。
  另：config_root 不再指 source_home，改**隔离 HOME 副本**（复制前 restrict_dir 限权，原
  source_home 哈希保留核对）；服务端口由 --port 参数注入 + 启动 /health 核对；失败也回收
  自启引擎进程与临时目录（不再无条件 rmtree 固定路径）。

运行：
  python -X utf8 -B scripts/verify_dsh_stop_tree.py --entry <dsh.cmd绝对路径> \
    --source-home <源DSH_HOME> --test-directory <分配隔离目录> --evidence <证据路径> \
    [--port <引擎端口>] [--segment stop|restart|all]
"""
import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]

SEGMENTS = ("stop", "restart")

# 合成 spawner MCP 服务器：tools/call 时 spawn 一个长活子进程并把 PID 写入 SPAWN_PID_FILE。
SPAWNER_MCP_SOURCE = r"""
import json, os, subprocess, sys

def main():
    # 启动即 spawn 一个长活子进程（确定性可观察后代，不依赖模型是否调用工具）并写 PID。
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])
    pid_file = os.environ.get('SPAWN_PID_FILE')
    if pid_file:
        try:
            with open(pid_file, 'w') as f:
                f.write(str(child.pid))
        except OSError:
            pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict) or 'id' not in msg:
            continue
        result = None
        method = msg.get('method')
        if method == 'initialize':
            result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'spawner', 'version': '1'}}
        elif method == 'ping':
            result = {}
        elif method == 'tools/list':
            result = {'tools': [{'name': 'spawn_child',
                                 'description': 'report the pre-spawned child pid',
                                 'inputSchema': {'type': 'object', 'properties': {}, 'required': []}}]}
        elif method == 'tools/call':
            result = {'content': [{'type': 'text', 'text': 'spawned child pid=%d' % child.pid}]}
        if result is not None:
            sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}) + '\n')
            sys.stdout.flush()

if __name__ == '__main__':
    main()
"""


def _process_tree() -> dict:
    """全进程快照 {pid: (parent, name_lower)}（process_tree.snapshot）。"""
    from engine import process_tree
    return process_tree.snapshot()


def _descendants(snap: dict, root_pids) -> dict:
    """root_pids 的完整后代 {pid: (parent, name)}（不含 root 自身）。"""
    roots = set(root_pids)
    children = set(roots)
    while True:
        found = {pid for pid, (parent, _) in snap.items() if parent in children}
        if found <= children:
            break
        children.update(found)
    return {pid: snap[pid] for pid in children if pid not in roots and pid in snap}


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def fingerprint(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _system_env(scratch: Path, home: Path) -> dict:
    """子进程最小环境（白名单，不含宿主 GROUP_/HERMES_ 业务变量，也不继承宿主 PYTHONPATH）。"""
    env = {
        **{k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR") if k in os.environ},
        **{k: os.environ[k] for k in ("USERNAME", "USERDOMAIN", "COMPUTERNAME",
                                       "LOGONSERVER", "NUMBER_OF_PROCESSORS",
                                       "PROCESSOR_ARCHITECTURE") if k in os.environ},
        "PATH": os.environ.get("PATH", ""),
        "TEMP": str(scratch), "TMP": str(scratch),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(ROOT),
    }
    if os.name == "nt":
        env.update({
            "USERPROFILE": str(home), "HOMEDRIVE": home.drive,
            "HOMEPATH": str(home).split(":", 1)[-1],
            "APPDATA": str(home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(home / "AppData" / "Local"),
        })
    return env


def _copy_home(source_home: Path, isolated_home: Path) -> Path:
    """复制源 DSH home 到隔离目录（先限权再复制），返回隔离 home；不碰源 home。

    跳过 attachments/logs/sessions/cache 等运行时大目录（内容寻址 blob 有独立 ACL、
    复制易 Permission denied，且停止验证不需要）；只复制配置 + profiles。
    """
    from engine import dsh_auth
    isolated_home.parent.mkdir(parents=True, exist_ok=True)
    isolated_home.mkdir(parents=True, exist_ok=True)
    dsh_auth.restrict_dir(str(isolated_home))  # 复制前限权（结构化 DACL fail-closed）
    skip_dirs = {"attachments", "logs", "sessions", "cache", "backups", "node_modules"}
    for item in source_home.iterdir():
        dst = isolated_home / item.name
        if item.is_dir():
            if item.name in skip_dirs:
                continue
            shutil.copytree(item, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dst)
    return isolated_home


def _inject_spawner(isolated_home: Path, spawner_py: Path, pid_file: Path,
                    python: Path, profile: str = "desktop") -> None:
    """向隔离 home 的 cordis.patch.yml 追加 spawner MCP 客户端 insert 条目（ruamel round-trip）。"""
    from ruamel.yaml import YAML
    import io
    patch = isolated_home / "profiles" / profile / "cordis.patch.yml"
    parser = YAML(typ="rt")
    loaded = parser.load(patch.read_text(encoding="utf-8-sig")) if patch.is_file() else []
    entries = [e for e in (loaded or []) if isinstance(e, dict)]
    entries.append({
        "insert": [{
            "id": "spawner-mcp",
            "name": "@deepseek-ai/dsh-mcp-client",
            "config": {
                "serverName": "spawner",
                "transport": "stdio",
                "command": str(python),
                "args": [str(spawner_py)],
                "cwd": str(isolated_home),
                "env": {"SPAWN_PID_FILE": str(pid_file)},
                "failOnStartupError": False,
                "reconnect": {"enabled": False},
                "toolCallTimeoutMs": 8000,
            },
        }],
    })
    buf = io.StringIO()
    parser.dump(entries, buf)
    patch.write_text(buf.getvalue(), encoding="utf-8")


class EngineProc:
    """独立引擎子进程（python -m engine.main --host --port --data-dir）。"""

    def __init__(self, root: Path, data_dir: Path, port: int, scratch: Path, home: Path):
        self.data_dir = data_dir
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.env = _system_env(scratch, home)
        self.env.update({
            "GROUP_DATA_DIR": str(data_dir),
            "GROUP_ENGINE_HOST": "127.0.0.1",
            "GROUP_ENGINE_PORT": str(port),
            "GROUP_ENGINE_URL": f"http://127.0.0.1:{port}",
        })
        self.log_path = data_dir / "engine.log"
        self.proc = None

    def start(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        logf = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "engine.main", "--host", "127.0.0.1",
             "--port", str(self.port), "--data-dir", str(self.data_dir)],
            cwd=str(ROOT), env=self.env, stdout=logf, stderr=subprocess.STDOUT)
        for _ in range(200):
            if self.health():
                return
            time.sleep(0.1)
        raise RuntimeError(f"engine process {self.proc.pid} failed to start")

    def health(self) -> bool:
        import urllib.request
        try:
            with urllib.request.urlopen(f"{self.base}/health", timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self.proc:
            self.proc = None


class EngineFixture:
    """隔离数据目录 + 引擎子进程 + 种子 dsh-bot + HTTP 客户端。"""

    def __init__(self, entry: Path, isolated_home: Path, test_dir: Path, segment: str,
                 port: int, data_dir: Path = None):
        self.segment = segment
        self.port = port
        self.data_dir = data_dir or (Path(test_dir) / f"engine-272-{segment}")
        self.workdir = Path(test_dir) / f"work-272-{segment}"
        self.scratch = Path(test_dir) / f"scratch-272-{segment}"
        for d in (self.data_dir, self.workdir, self.scratch):
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True, exist_ok=True)
        self.engine = EngineProc(ROOT, self.data_dir, port, self.scratch, self.scratch)
        # 父进程侧 DB 访问指向同一数据目录（config.db_path 读 GROUP_DATA_DIR）。
        os.environ["GROUP_DATA_DIR"] = str(self.data_dir)
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
                (str(isolated_home), str(entry)))
            bot_id = cur.lastrowid
            conn.execute("INSERT OR IGNORE INTO project_members(project,profile,bot_id,active) "
                         "VALUES('bot-group','dsh-bot',?,1)", (bot_id,))
            conn.commit()
        finally:
            conn.close()
        self.engine.start()

    def q(self, sql: str, params=()):
        conn = self.db.get_connection()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def post_msg(self, text: str) -> dict:
        resp = self.httpx.post(f"{self.engine.base}/msg", json={
            "project": "bot-group", "sender": "user", "text": text, "need_reply": 1}, timeout=60)
        if resp.status_code >= 400:
            raise RuntimeError(f"POST /msg {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def live_run(self, timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            rows = self.q("SELECT session, started_at FROM wake_runs WHERE runtime='dsh' "
                          "AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1")
            if rows:
                return dict(rows[0])
            time.sleep(1)
        return None

    def close(self):
        self.engine.stop()


def _start_and_capture(fx: EngineFixture, spawner_pid_file: Path) -> dict:
    """起长 DSH 运行 + 独立核验根 + 逐 PID 后代（含 spawner 工具子进程）——不 /stop。"""
    from engine import wake
    fx.post_msg("@dsh-bot 先调用一次 spawn_child 工具，再依次调用 group_list_members 工具"
                "至少 8 次（每次等 tool_result 再下一次），过程中保持运行，最后回复 DSH_STOP_272。")
    run = fx.live_run()
    if run is None:
        return {"ok": False, "detail": "no live dsh run observed"}
    # 停止前找根 PID；finder 状态显式记录（None=未知，不是「已消失」）。
    roots = []
    finder_before = "unknown"
    for _ in range(60):
        r = wake.find_bot_processes("dsh-bot", run["session"])
        if r is not None:
            roots = r
            finder_before = "resolved"
            break
        time.sleep(0.5)
    # 独立核验根存活（snapshot，不走 find_bot_processes）。
    snap_root = _process_tree()
    live_roots = [p for p in roots if p in snap_root]
    if not live_roots:
        return {"ok": False, "detail": "no independently-live root pid",
                "roots": roots, "finder_before": finder_before}
    root_identity_before = {p: snap_root[p][1] for p in live_roots}
    # 持续轮询后代并集（逐 PID 记录 parent+name；MCP 服务器/工具子进程在模型调工具后才
    # spawn 且可能短命，并集避免单次快照漏采）。
    seen: dict[int, tuple] = {}
    deadline = time.time() + 25
    while time.time() < deadline:
        snap = _process_tree()
        for pid, (parent, name) in _descendants(snap, live_roots).items():
            seen.setdefault(pid, (parent, name))
        names = {name for (_p, name) in seen.values()}
        # 早退：spawner 子进程已 spawn（PID 文件已写）且已采到 Electron + MCP python 后代。
        if spawner_pid_file.is_file() and any("harness" in n for n in names) \
                and any("python" in n for n in names):
            time.sleep(0.5)
            snap = _process_tree()
            for pid, (parent, name) in _descendants(snap, live_roots).items():
                seen.setdefault(pid, (parent, name))
            break
        time.sleep(0.3)
    # spawner 工具子进程 PID（独立核验其是否 DSH 后代 + 停止后是否退出）。
    spawner_child_pid = None
    if spawner_pid_file.is_file():
        try:
            spawner_child_pid = int(spawner_pid_file.read_text().strip())
        except (OSError, ValueError):
            spawner_child_pid = None
    return {"ok": True, "run": run, "live_roots": live_roots,
            "root_identity_before": root_identity_before, "finder_before": finder_before,
            "seen": seen, "spawner_child_pid": spawner_child_pid}


def _stop_and_verify(fx: EngineFixture, run: dict, live_roots: list,
                     root_identity_before: dict, finder_before: str,
                     seen: dict, spawner_child_pid) -> dict:
    """/stop + 独立核验根/后代/工具子进程退出 + 受保护引擎/自身存活对照。"""
    from engine import wake
    protected = {"engine_pid": fx.engine.proc.pid if fx.engine.proc else None,
                 "self_pid": os.getpid()}
    resp = fx.post_msg("/stop @dsh-bot")
    time.sleep(3)
    snap_after = _process_tree()
    roots_alive = [p for p in live_roots if p in snap_after]
    descendants_alive = [p for p in seen if p in snap_after]
    spawner_alive = spawner_child_pid is not None and spawner_child_pid in snap_after
    finder_after = wake.find_bot_processes("dsh-bot", run["session"])  # 不 or []
    protected_alive = {k: bool(v and v in snap_after) for k, v in protected.items()}
    return {
        "ok": bool(resp.get("ok")),
        "roots_before": live_roots,
        "root_identity_before": root_identity_before,
        "finder_before": finder_before,
        "finder_after": finder_after,
        "finder_unknown_after": finder_after is None,
        "roots_alive_after": roots_alive,
        "root_gone": not roots_alive,  # 独立核验，非 find_bot_processes 空
        "descendants_before": {p: {"parent": parent, "name": name}
                               for p, (parent, name) in seen.items()},
        "descendants_before_count": len(seen),
        "descendants_alive_after": descendants_alive,
        "descendants_all_gone": bool(seen) and not descendants_alive,
        "spawner_child_pid": spawner_child_pid,
        "spawner_child_spawned": spawner_child_pid is not None,
        "spawner_child_alive_after": spawner_alive,
        "protected_alive_after": protected_alive,
    }


def _stop_pass(r: dict) -> bool:
    """stop 段通过判定：独立根核验退出 + 后代退出 + 工具子进程确实 spawn 且退出 +
    finder 非未知 + 受保护存活。"""
    return (bool(r.get("ok")) and r.get("root_gone")
            and r.get("descendants_all_gone")
            and not r.get("finder_unknown_after")
            and r.get("spawner_child_spawned")
            and not r.get("spawner_child_alive_after")
            and all(r.get("protected_alive_after", {}).values()))


def _run_segment(args, segment: str) -> dict:
    data_dir = Path(args.test_directory) / f"engine-272-{segment}"
    isolated_home = Path(args.test_directory) / f"home-272-{segment}"
    workdir = Path(args.test_directory) / f"work-272-{segment}"
    scratch = Path(args.test_directory) / f"scratch-272-{segment}"
    spawner_py = Path(args.test_directory) / f"spawner-272-{segment}.py"
    spawner_pid_file = Path(args.test_directory) / f"spawn-pid-272-{segment}.txt"

    fx = None
    result = {"segment": segment}
    try:
        # 先清理上次失败可能残留的同名资源，再复制/注入。
        for d in (data_dir, isolated_home, workdir, scratch):
            shutil.rmtree(d, ignore_errors=True)
        spawner_py.write_text(SPAWNER_MCP_SOURCE, encoding="utf-8")
        _copy_home(args.source_home, isolated_home)
        _inject_spawner(isolated_home, spawner_py, spawner_pid_file, Path(sys.executable))

        fx = EngineFixture(args.entry, isolated_home, args.test_directory, segment,
                           args.port, data_dir=data_dir)
        cap = _start_and_capture(fx, spawner_pid_file)
        if not cap.get("ok"):
            result["result"] = cap
            result["passed"] = False
            return result
        if segment == "stop":
            r = _stop_and_verify(fx, cap["run"], cap["live_roots"], cap["root_identity_before"],
                                 cap["finder_before"], cap["seen"], cap["spawner_child_pid"])
            result["result"] = r
            result["passed"] = _stop_pass(r)
        else:
            # 独立引擎进程重启：引擎 A 起 DSH 运行后（不 /stop），终止引擎 A（DSH 独立存活），
            # 起新引擎 B 进程（新 PID）→ 引擎 B 经命令行身份（_matches_dsh_execution +
            # _dsh_spawn_tag）恢复定位原 DSH 根后 /stop。
            old_engine_pid = fx.engine.proc.pid if fx.engine.proc else None
            fx.engine.stop()  # 终止引擎 A 进程，DSH spawn 独立、不随之退出
            time.sleep(1)
            snap_after_engine_dead = _process_tree()
            dsh_still_alive = all(p in snap_after_engine_dead for p in cap["live_roots"])
            fx.engine = EngineProc(ROOT, data_dir, args.port, fx.scratch, fx.scratch)
            fx.engine.start()  # 新引擎 B 进程（新 PID）
            new_engine_pid = fx.engine.proc.pid
            r = _stop_and_verify(fx, cap["run"], cap["live_roots"], cap["root_identity_before"],
                                 cap["finder_before"], cap["seen"], cap["spawner_child_pid"])
            result["result"] = r
            result["result"]["restart"] = {
                "old_engine_pid": old_engine_pid,
                "new_engine_pid": new_engine_pid,
                "engine_pid_changed": new_engine_pid != old_engine_pid,
                "dsh_survived_engine_exit": dsh_still_alive,
            }
            result["passed"] = (_stop_pass(r)
                                and new_engine_pid != old_engine_pid and dsh_still_alive)
        return result
    finally:
        if fx is not None:
            fx.close()  # 无论成败都回收自启引擎进程
        # 失败也清理本次创建的隔离目录（不无条件 rmtree 固定路径）。
        for d in (data_dir, isolated_home, Path(args.test_directory) / f"work-272-{segment}",
                  Path(args.test_directory) / f"scratch-272-{segment}",
                  spawner_py, spawner_pid_file):
            try:
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
                elif d.is_file():
                    d.unlink(missing_ok=True)
            except OSError:
                pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry", type=Path, required=True)
    ap.add_argument("--source-home", type=Path, required=True)
    ap.add_argument("--test-directory", type=Path, required=True)
    ap.add_argument("--evidence", type=Path, required=True)
    ap.add_argument("--port", type=int, default=None, help="引擎监听端口（默认自动分配空闲口）")
    ap.add_argument("--segment", default="all", choices=[*SEGMENTS, "all"])
    args = ap.parse_args()
    if not args.entry.is_file():
        raise ValueError(f"--entry 不是文件：{args.entry}")
    if not args.source_home.is_dir():
        raise ValueError(f"--source-home 不是目录：{args.source_home}")
    args.port = args.port or _free_port()

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
    outputs["port"] = args.port
    outputs["passed"] = all_passed and outputs["source_files_unchanged"]
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(outputs, ensure_ascii=False, indent=2, default=str),
                             encoding="utf-8")
    print(json.dumps(outputs, ensure_ascii=False, indent=2, default=str))
    return 0 if outputs["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
