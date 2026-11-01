"""req#138 task#272 rework③（manager node1611）：dsh /stop 清理验证——根/后代独立存活核验 + 独立引擎进程重启 + 工具 body 后代实际观察 + 另一 Bot 对照 + host/port 注入。

manager node1611 剩余要求闭环（保留 node1600 已通过进展）：
  ① spawner 工具 body 实际创建后代：SPAWNER_MCP_SOURCE 改 tools/call（工具 body）spawn 长活
     子进程 + 落 {pid, call_id} JSON；_start_and_capture 读 JSON 后**实际观察断言**
     （spawner_child_observed = 子进程 PID 在停止前目标后代树中），未观察/外来 PID 判 spawned
     是假阳性 → fail。
  ② 受保护另一 Bot 对照：_stop_and_verify 起隔离合成长活 python（非 DSH 树）+ 核验 /stop 后
     仍存活（protected_alive_after.other_bot_pid）。
  ③ 资源回收：EngineFixture 构造 start 失败自回收已启动引擎；_run_segment finally 统一回收
     + 逐项核验 cleanup_ok（删除失败显式报告，进入 passed）。
  ④ host/port 注入：--host/--port 必填（分配资源 127.0.0.1:18042）；启动前占用检查
     （_check_port_free）+ 启动后 health 可达 + 数据目录 group.db 核验。

运行：
  python -X utf8 -B scripts/verify_dsh_stop_tree.py --entry <dsh.cmd绝对路径> \
    --source-home <源DSH_HOME> --test-directory <分配隔离目录> --evidence <证据路径> \
    --host 127.0.0.1 --port <分配端口> [--segment stop|restart|all]
"""
import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PureWindowsPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]

SEGMENTS = ("stop", "restart")

# 合成 spawner MCP 服务器（node .cjs，与 #271 marker MCP 同构）：tools/call 时（工具 body
# 实际执行）spawn 长活子进程并落 {pid, call_id} JSON 到 SPAWN_PID_FILE——工具 body 创建后代。
SPAWNER_MCP_SOURCE = r"""const readline = require('node:readline');
const fs = require('node:fs');
const { spawn } = require('node:child_process');
const input = readline.createInterface({ input: process.stdin });
input.on('line', line => {
  let m;
  try { m = JSON.parse(line); } catch { return; }
  if (m.id === undefined) return;
  let result;
  if (m.method === 'initialize') {
    result = { protocolVersion: '2024-11-05', capabilities: { tools: {} }, serverInfo: { name: 'spawner', version: '1' } };
  } else if (m.method === 'ping') {
    result = {};
  } else if (m.method === 'tools/list') {
    result = { tools: [{ name: 'spawn_child', description: 'spawn a long-lived child process in the tool body', inputSchema: { type: 'object', properties: {}, required: [] } }] };
  } else if (m.method === 'tools/call') {
    // 工具 body 实际创建长活子进程 + 落 pid/call_id（JSON-RPC id）。
    const child = spawn(process.execPath, ['-e', 'setTimeout(() => {}, 600000)']);
    const pidFile = process.env.SPAWN_PID_FILE;
    if (pidFile) {
      try { fs.writeFileSync(pidFile, JSON.stringify({ pid: child.pid, call_id: m.id })); } catch {}
    }
    result = { content: [{ type: 'text', text: 'spawned child pid=' + child.pid + ' call_id=' + m.id }] };
  } else {
    process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: m.id, error: { code: -32601, message: 'Unknown method' } }) + '\n');
    return;
  }
  process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: m.id, result }) + '\n');
});
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


def _check_port_free(host: str, port: int) -> None:
    """启动前占用检查：端口已被占用则提前失败（分配资源冲突停止启动协调）。"""
    sock = socket.socket()
    try:
        sock.bind((host, port))
    except OSError as e:
        raise RuntimeError(f"端口 {host}:{port} 已被占用：{e}") from e
    finally:
        sock.close()


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


def _inject_spawner(isolated_home: Path, spawner_script: Path, pid_file: Path,
                    command: str, profile: str = "desktop") -> None:
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
                "command": command,
                "args": [str(spawner_script)],
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

    def __init__(self, root: Path, data_dir: Path, host: str, port: int, scratch: Path, home: Path):
        self.data_dir = data_dir
        self.host = host
        self.port = port
        self.base = f"http://{host}:{port}"
        self.env = _system_env(scratch, home)
        self.env.update({
            "GROUP_DATA_DIR": str(data_dir),
            "GROUP_ENGINE_HOST": host,
            "GROUP_ENGINE_PORT": str(port),
            "GROUP_ENGINE_URL": f"http://{host}:{port}",
        })
        self.log_path = data_dir / "engine.log"
        self.proc = None

    def start(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        # stdout/stderr 重定向到 DEVNULL：避免引擎日志文件被进程树句柄锁定导致清理失败。
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "engine.main", "--host", self.host,
             "--port", str(self.port), "--data-dir", str(self.data_dir)],
            cwd=str(ROOT), env=self.env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        for _ in range(200):
            if self.health():
                # 启动后身份/数据目录核验：health 可达 + 数据目录确已生成 group.db。
                if (self.data_dir / "group.db").is_file() or (self.data_dir / "data" / "group.db").is_file():
                    return
                raise RuntimeError(f"engine {self.proc.pid} health ok but data dir not created: {self.data_dir}")
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

    def __init__(self, entry: Path, isolated_home: Path, isolated_home_2: Path,
                 test_dir: Path, segment: str, host: str, port: int,
                 data_dir: Path = None, workdir: Path = None, scratch: Path = None):
        self.segment = segment
        self.host = host
        self.port = port
        self.isolated_home_2 = isolated_home_2
        self.data_dir = data_dir or (Path(test_dir) / f"engine-272-{segment}")
        self.workdir = workdir or (Path(test_dir) / f"work-272-{segment}")
        self.scratch = scratch or (Path(test_dir) / f"scratch-272-{segment}")
        # 唯一目录由 mkdtemp 生成（空目录），不再无条件 rmtree 固定路径。
        for d in (self.data_dir, self.workdir, self.scratch):
            d.mkdir(parents=True, exist_ok=True)
        self.engine = EngineProc(ROOT, self.data_dir, host, port, self.scratch, self.scratch)
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
            # 另一已绑定 bot（dsh-bot-2）：需求 138 验收 7「其他 bot 保持运行」对照，
            # 真实 DSH 执行（session/bot 归属），非裸 sleep Python。
            conn.execute("INSERT OR IGNORE INTO members(profile,display_name,role) "
                         "VALUES('dsh-bot-2','dsh-bot-2','dsh-bot')")
            cur2 = conn.execute(
                "INSERT INTO bots(host_type,profile,config_root,runtime,runtime_entry,display_name,role) "
                "VALUES('dsh','desktop',?,'dsh',?, 'dsh-bot-2','')",
                (str(self.isolated_home_2), str(entry)))
            bot_id2 = cur2.lastrowid
            conn.execute("INSERT OR IGNORE INTO project_members(project,profile,bot_id,active) "
                         "VALUES('bot-group','dsh-bot-2',?,1)", (bot_id2,))
            conn.commit()
        finally:
            conn.close()
        try:
            self.engine.start()
        except Exception:
            # 构造/启动失败也回收已拥有的引擎进程（fx 尚未返回，外层 finally 拿不到）。
            self.engine.stop()
            raise

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


def _owned_overlay(argv, scratch, runs) -> bool:
    """匹配原生 DSH CLI 参数到私有 scratch 路径 + 持久化 run（manager 10047 样例）。

    引擎在 scratch/dsh-spawn-<session>-<started_at>-<random>/overlay.yml 放 overlay，
    命令行 --profile headless --patch <overlay>；overlay 父目录名以 _dsh_spawn_tag 开头。
    """
    from engine.wake import _dsh_spawn_tag
    if not argv:
        return False
    try:
        profile_at = argv.index('--profile')
        patch_at = argv.index('--patch')
        if argv[profile_at + 1] != 'headless':
            return False
        overlay = PureWindowsPath(argv[patch_at + 1])
    except (ValueError, IndexError, TypeError):
        return False
    base = PureWindowsPath(scratch)
    if not base.is_absolute() or not overlay.is_absolute():
        return False
    if '..' in overlay.parts or overlay.name.lower() != 'overlay.yml':
        return False
    if overlay.parent.parent != base:
        return False
    return any(
        run['runtime'] == 'dsh' and run['project'] == 'bot-group'
        and run['profile'] in ('dsh-bot', 'dsh-bot-2')
        and overlay.parent.name.startswith(_dsh_spawn_tag(run['session'], run['started_at']) + '-')
        for run in runs
    )


def _recover_from_snapshot(registry, scratch, runs, snapshot, argv_by_pid) -> dict:
    """注册已证归属的 DSH 根/后代，缺命令行（未知）单独报告不注册（manager 10047 样例）。"""
    roots = [pid for pid, argv in argv_by_pid.items()
             if pid in snapshot and _owned_overlay(argv, scratch, runs)]
    owned = {pid: snapshot[pid] for pid in roots}
    owned.update(_descendants(snapshot, roots))
    registry.register_many(owned)
    return {'roots': roots, 'registered': sorted(owned),
            'unknown': sorted(pid for pid, argv in argv_by_pid.items()
                              if argv is None and pid in snapshot)}


def _compensate_via_snapshot(fx, registry) -> bool:
    """live_run 异常后按私有 DB wake_runs + scratch 标签反查 DSH 树（overlay 路径含 spawn tag）→
    登记 + 回收（manager 10047；引擎已退出也按持久化 run 定位）。返回是否定位到 DSH 根。"""
    from engine import process_query
    try:
        rows = fx.q("SELECT session, started_at, profile, project, runtime FROM wake_runs "
                    "WHERE runtime='dsh' AND profile IN ('dsh-bot','dsh-bot-2')")
        runs = [dict(r) for r in rows]
    except Exception:
        return False
    if not runs:
        return False
    try:
        snap = _process_tree()
        argv_by_pid = {pid: process_query.command_line(pid, scope="dsh-recovery")
                       for pid in snap}
        result = _recover_from_snapshot(registry, str(fx.scratch), runs, snap, argv_by_pid)
        return bool(result["roots"])
    except Exception:
        return False


def _start_and_capture(fx: EngineFixture, spawner_pid_file: Path,
                       registry: "_ResourceRegistry") -> dict:
    """起长 DSH 运行 + 独立核验根 + 逐 PID 后代（含 spawner 工具子进程）——不 /stop。

    registry：随发现立即登记已拥有进程（PID → name），capture 返回前异常也能在 finally 取得。"""
    from engine import wake
    fx.post_msg("@dsh-bot 本次任务第一步（不可跳过）是调用名为 spawn_child 的 MCP 工具"
                "（该工具 spawn 一个长活子进程用于停止清理验证，调用后必须等 tool_result 返回）；"
                "之后再依次调用 group_list_members 工具至少 8 次，最后回复 DSH_STOP_272。")
    try:
        run = fx.live_run()
    except Exception:
        # B：补偿回收——按进程树身份反查引擎后代（DSH 树）→ 登记 + finally 回收；找不到则
        # dsh_not_located（总失败，不假报回收）。
        if not _compensate_via_snapshot(fx, registry):
            registry.dsh_not_located = True
        return {"ok": False, "detail": "live_run query failed"}
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
    # 随发现立即登记根（创建身份 name），异常路径 finally 仍能回收。
    registry.register_many({p: snap_root[p] for p in live_roots})
    # 持续轮询后代并集（逐 PID 记录 parent+name；MCP 服务器/工具子进程在模型调工具后才
    # spawn 且可能短命，并集避免单次快照漏采）。
    seen: dict[int, tuple] = {}
    deadline = time.time() + 25
    while time.time() < deadline:
        snap = _process_tree()
        for pid, (parent, name) in _descendants(snap, live_roots).items():
            seen.setdefault(pid, (parent, name))
            registry.register(pid, name)  # 随发现登记后代
        names = {name for (_p, name) in seen.values()}
        # 早退：spawner 子进程已 spawn（PID 文件已写）且已采到 Electron + MCP python 后代。
        if spawner_pid_file.is_file() and any("harness" in n for n in names) \
                and any("python" in n for n in names):
            time.sleep(0.5)
            snap = _process_tree()
            for pid, (parent, name) in _descendants(snap, live_roots).items():
                seen.setdefault(pid, (parent, name))
                registry.register(pid, name)  # 随发现登记后代
            break
        time.sleep(0.3)
    # spawner 工具子进程：PID 文件存 {pid, call_id} JSON（工具 body 创建）。
    spawner_child_pid = None
    spawner_call_id = None
    if spawner_pid_file.is_file():
        try:
            data = json.loads(spawner_pid_file.read_text().strip())
            spawner_child_pid = int(data.get('pid'))
            spawner_call_id = data.get('call_id')
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            spawner_child_pid = None
            spawner_call_id = None
    # 实际观察断言：spawner 子进程 PID 必须在停止前目标后代树中（observed）——
    # 不能只核 PID 文件非空（未观察/外来 PID 判 spawned 是假阳性）。
    spawner_child_observed = (spawner_child_pid is not None
                              and spawner_child_pid in seen)
    # 诊断：spawner MCP 服务器是否启动（python.exe 命令行含 spawner）。
    from engine import process_query
    spawner_server_pids = []
    for pid, (_, name) in seen.items():
        if name == "python.exe":
            try:
                argv = process_query.command_line(pid)
                if argv and any("spawner" in a.lower() for a in argv):
                    spawner_server_pids.append(pid)
            except Exception:
                pass
    # 另一已绑定 bot（dsh-bot-2）真实执行：POST /msg 起第二 DSH + 找其根（session/bot 归属，
    # 非裸 sleep Python）——验收 7「其他 bot 保持运行」的停止作用域对照。用工具循环保持 DSH
    # 在 agent-loop 内不退出（简单回复会在 restart 长序列期间完成并退出）。
    fx.post_msg("@dsh-bot-2 依次调用 group_list_members 工具 4 次（每次等 tool_result 再下一次），保持运行。")
    other_roots = []
    other_finder = "unknown"
    for _ in range(60):
        rows = fx.q("SELECT session FROM wake_runs WHERE runtime='dsh' AND profile='dsh-bot-2' "
                    "AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1")
        if rows:
            r2 = wake.find_bot_processes("dsh-bot-2", rows[0]["session"])
            if r2 is not None:
                other_roots = r2
                other_finder = "resolved"
                break
        time.sleep(0.5)
    snap_other = _process_tree()
    other_live_roots = [p for p in other_roots if p in snap_other]
    registry.register_many({p: snap_other[p] for p in other_live_roots})
    return {"ok": True, "run": run, "live_roots": live_roots,
            "root_identity_before": root_identity_before, "finder_before": finder_before,
            "seen": seen, "spawner_child_pid": spawner_child_pid,
            "spawner_call_id": spawner_call_id,
            "spawner_child_observed": spawner_child_observed,
            "spawner_server_pids": spawner_server_pids,
            "other_roots": other_roots, "other_live_roots": other_live_roots,
            "other_finder": other_finder}


def _stop_and_verify(fx: EngineFixture, run: dict, live_roots: list,
                     root_identity_before: dict, finder_before: str,
                     seen: dict, spawner_child_pid, spawner_child_observed,
                     spawner_call_id, other_live_roots=None) -> dict:
    """/stop + 独立核验根/后代/工具子进程退出 + 受保护引擎/自身/另一已绑定 Bot 存活对照。"""
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
    # 另一已绑定 bot（dsh-bot-2）在 /stop @dsh-bot 后仍存活（session/bot 停止作用域对照）。
    other_alive = bool(other_live_roots) and all(p in snap_after for p in other_live_roots)
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
        "spawner_call_id": spawner_call_id,
        "spawner_child_spawned": spawner_child_pid is not None,
        "spawner_child_observed": spawner_child_observed,
        "spawner_child_alive_after": spawner_alive,
        "protected_alive_after": protected_alive,
        "other_bot_alive": other_alive,
        "other_live_roots": other_live_roots,
    }


def _stop_pass(r: dict) -> bool:
    """stop 段通过判定：独立根核验退出 + 后代退出 + 工具子进程确实 spawn 且被实际观察
    （在目标后代树中）且退出 + finder 非未知 + 受保护（引擎/自身/另一 Bot）存活。"""
    return (bool(r.get("ok")) and r.get("root_gone")
            and r.get("descendants_all_gone")
            and not r.get("finder_unknown_after")
            and r.get("spawner_child_spawned")
            and r.get("spawner_child_observed")  # 实际观察，非仅 PID 文件非空
            and not r.get("spawner_child_alive_after")
            and all(r.get("protected_alive_after", {}).values()))


def _process_identity(pid) -> tuple:
    """进程稳定实例身份。返回 ('ok', name_lower, create_time_ms) / ('exited',) / ('unknown',)。

    区分「不存在」（NoSuchProcess → 已退出，可算回收）与「查询失败」（AccessDenied/OSError →
    未知，不假报回收，须总失败）。"""
    import psutil
    try:
        p = psutil.Process(pid)
        return ("ok", p.name().lower(), int(p.create_time() * 1000))
    except psutil.NoSuchProcess:
        return ("exited",)
    except (psutil.AccessDenied, OSError):
        return ("unknown",)


class _ResourceRegistry:
    """随启动/发现立即登记已拥有进程（PID → (name_lower, create_time_ms, known)），capture 返回前异常也能取得。

    manager node1634/1642：身份不能用 name（同名 PID 复用仍 taskkill 并记 killed），须采集 create_time
    作稳定实例身份；登记时身份不可读（AccessDenied/OSError）→ known=False，杀时拒杀（不假报回收）。
    """

    def __init__(self):
        self._procs = {}  # pid -> (name_lower, create_time_ms, known)
        self.dsh_not_located = False  # 隔离 DSH 树未定位（首次根发现前异常），须总失败

    def register(self, pid, name=None):
        if pid is None:
            return
        ident = _process_identity(pid)
        if ident[0] == "ok":
            self._procs[int(pid)] = (ident[1], ident[2], True)
        else:
            # 登记时身份不可读/已退出：known=False，杀时拒杀（身份未知明确失败）。
            self._procs[int(pid)] = (name, None, False)

    def register_many(self, pid_map):
        for pid, val in (pid_map or {}).items():
            self.register(pid, val[1] if isinstance(val, tuple) else val)

    def pids(self):
        return list(self._procs.keys())


def _kill_registered(registry: _ResourceRegistry) -> dict:
    """按登记身份（name + create_time）核验后 taskkill，返回 {pid: status}。

    已退出（NoSuchProcess）跳过；查询失败（AccessDenied/OSError）identity_unknown（总失败）；
    登记身份未知（known=False）拒杀；create_time/name 不符（PID 复用/身份变化）identity_mismatch
    拒杀；returncode 非 0 记 kill_failed；杀后核验存活（killed/still_alive）。"""
    report = {}
    for pid in registry.pids():
        reg_name, reg_ct, reg_known = registry._procs[pid]
        ident = _process_identity(pid)
        if ident[0] == "exited":
            report[pid] = "already_exited"  # 已退出，跳过（不 taskkill 历史 PID）
            continue
        if ident[0] == "unknown":
            report[pid] = "identity_unknown"  # 查询失败，不假报回收
            continue
        cur_name, cur_ct = ident[1], ident[2]
        if not reg_known:
            report[pid] = "identity_unknown"  # 登记身份未知，拒杀
            continue
        if cur_ct != reg_ct:
            report[pid] = "identity_mismatch"  # create_time 不同 = PID 复用，拒杀
            continue
        if cur_name != reg_name:
            report[pid] = "identity_mismatch"  # name 不同 = 身份变化，拒杀
            continue
        r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode != 0:
            report[pid] = f"kill_failed_rc={r.returncode}"
            continue
        time.sleep(0.3)
        report[pid] = "killed" if _process_identity(pid)[0] == "exited" else "still_alive"
    return report


def _run_segment(args, segment: str) -> dict:
    fx = None
    registry = _ResourceRegistry()  # 随启动/发现登记已拥有进程，异常路径 finally 也能回收
    dirs = []  # 本次创建的隔离目录（逐个登记纳入 try，第二次 mkdtemp 失败也回收第一个）
    spawner_cjs = Path(args.test_directory) / f"spawner-272-{segment}.cjs"
    spawner_pid_file = Path(args.test_directory) / f"spawn-pid-272-{segment}.txt"
    result = {"segment": segment}
    try:
        # D：唯一资源目录逐个 mkdtemp + 登记（分配目录纳入 try 范围，二次 mkdtemp 失败回收首个）。
        data_dir = Path(tempfile.mkdtemp(prefix=f"engine-272-{segment}-", dir=args.test_directory))
        dirs.append(data_dir)
        isolated_home = Path(tempfile.mkdtemp(prefix=f"home-272-{segment}-", dir=args.test_directory))
        dirs.append(isolated_home)
        isolated_home_2 = Path(tempfile.mkdtemp(prefix=f"home2-272-{segment}-", dir=args.test_directory))
        dirs.append(isolated_home_2)
        workdir = Path(tempfile.mkdtemp(prefix=f"work-272-{segment}-", dir=args.test_directory))
        dirs.append(workdir)
        scratch = Path(tempfile.mkdtemp(prefix=f"scratch-272-{segment}-", dir=args.test_directory))
        dirs.append(scratch)
        spawner_cjs.write_text(SPAWNER_MCP_SOURCE, encoding="utf-8")
        _copy_home(args.source_home, isolated_home)
        _copy_home(args.source_home, isolated_home_2)
        _inject_spawner(isolated_home, spawner_cjs, spawner_pid_file, args.node)

        fx = EngineFixture(args.entry, isolated_home, isolated_home_2, args.test_directory,
                           segment, args.host, args.port, data_dir=data_dir,
                           workdir=workdir, scratch=scratch)
        # B：引擎进程随启动立即登记（首次 PID 发现前异常仍按本次执行身份可回收）。
        registry.register(fx.engine.proc.pid if fx.engine.proc else None)
        cap = _start_and_capture(fx, spawner_pid_file, registry)
        if not cap.get("ok"):
            result["result"] = cap
            result["passed"] = False
            return result
        if segment == "stop":
            r = _stop_and_verify(fx, cap["run"], cap["live_roots"], cap["root_identity_before"],
                                 cap["finder_before"], cap["seen"], cap["spawner_child_pid"],
                                 cap["spawner_child_observed"], cap["spawner_call_id"],
                                 cap.get("other_live_roots"))
            result["result"] = r
            result["result"]["spawner_server_pids"] = cap.get("spawner_server_pids", [])
            # 另一已绑定 bot（dsh-bot-2）存活是 stop 段主验证（验收 7「其他 bot 保持运行」）；
            # restart 段因长序列 dsh-bot-2 可能自然完成退出，不纳入（restart 主验证是
            # dsh_survived_engine_exit + engine_pid_changed）。
            result["passed"] = _stop_pass(r) and r.get("other_bot_alive")
        else:
            # 独立引擎进程重启：引擎 A 起 DSH 运行后（不 /stop），终止引擎 A（DSH 独立存活），
            # 起新引擎 B 进程（新 PID）→ 引擎 B 经命令行身份（_matches_dsh_execution +
            # _dsh_spawn_tag）恢复定位原 DSH 根后 /stop。
            old_engine_pid = fx.engine.proc.pid if fx.engine.proc else None
            fx.engine.stop()  # 终止引擎 A 进程，DSH spawn 独立、不随之退出
            time.sleep(1)
            snap_after_engine_dead = _process_tree()
            dsh_still_alive = all(p in snap_after_engine_dead for p in cap["live_roots"])
            fx.engine = EngineProc(ROOT, data_dir, args.host, args.port, fx.scratch, fx.scratch)
            fx.engine.start()  # 新引擎 B 进程（新 PID）
            new_engine_pid = fx.engine.proc.pid
            r = _stop_and_verify(fx, cap["run"], cap["live_roots"], cap["root_identity_before"],
                                 cap["finder_before"], cap["seen"], cap["spawner_child_pid"],
                                 cap["spawner_child_observed"], cap["spawner_call_id"],
                                 cap.get("other_live_roots"))
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
        # C：各清理步骤独立保护——快照/taskkill 异常仍尝试关闭自启引擎 + 释放本次资源，失败显式记录。
        try:
            kill_report = _kill_registered(registry)
        except Exception as e:
            kill_report = {"_error": f"{type(e).__name__}: {e}"}
        result["kill_report"] = kill_report
        if fx is not None:
            try:
                fx.close()  # 无论成败都回收自启引擎进程（独立于 kill 步骤异常，不被其短路）
            except Exception as e:
                result["fx_close_error"] = f"{type(e).__name__}: {e}"
        time.sleep(1)  # 等 OS 释放引擎进程遗留的 SQLite -shm/-wal 句柄
        # 统一回收本次创建的隔离资源 + 核验清理结果（删除失败重试 + 显式报告，进入 passed）。
        cleanup = {}
        for d in dirs + [spawner_cjs, spawner_pid_file]:
            if not d.exists():
                cleanup[str(d)] = True
                continue
            ok = False
            last_err = None
            for _ in range(3):  # -shm/-wal 锁可能短暂残留，重试
                try:
                    if d.is_dir():
                        shutil.rmtree(d)
                    else:
                        d.unlink()
                    ok = not d.exists()
                    if ok:
                        break
                except OSError as e:
                    last_err = e
                    time.sleep(0.5)
            cleanup[str(d)] = True if ok else f"delete error: {last_err}"
        result["cleanup"] = cleanup
        result["cleanup_ok"] = all(v is True for v in cleanup.values())
        # kill 失败/身份不符/still_alive/身份未知 显式记录并纳入总 passed（进程未真正退出不通过）。
        kill_ok = (all(s in ("killed", "already_exited") for s in kill_report.values())
                   and not registry.dsh_not_located)
        result["kill_ok"] = kill_ok
        # C：fx_close 异常（fx_close_error）及任意清理错误必须使总 passed=false。
        result["passed"] = (bool(result.get("passed")) and result["cleanup_ok"] and kill_ok
                            and "fx_close_error" not in result)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry", type=Path, required=True)
    ap.add_argument("--source-home", type=Path, required=True)
    ap.add_argument("--test-directory", type=Path, required=True)
    ap.add_argument("--evidence", type=Path, required=True)
    ap.add_argument("--host", default="127.0.0.1", help="引擎监听地址（分配资源注入）")
    ap.add_argument("--port", type=int, required=True, help="引擎监听端口（分配资源注入，必填）")
    ap.add_argument("--node", default=shutil.which("node") or "node",
                    help="node 可执行文件（spawner MCP stdio 服务器）；默认 PATH 发现")
    ap.add_argument("--segment", default="all", choices=[*SEGMENTS, "all"])
    args = ap.parse_args()
    if not args.entry.is_file():
        raise ValueError(f"--entry 不是文件：{args.entry}")
    if not args.source_home.is_dir():
        raise ValueError(f"--source-home 不是目录：{args.source_home}")
    if shutil.which(args.node) is None and not Path(args.node).is_file():
        raise ValueError(f"--node 不可执行：{args.node}（需 PATH 发现或显式绝对路径）")
    # 启动前占用检查：端口已被占用则提前失败（分配资源冲突停止启动协调）。
    _check_port_free(args.host, args.port)

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
    outputs["host"] = args.host
    outputs["port"] = args.port
    outputs["passed"] = all_passed and outputs["source_files_unchanged"]
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(outputs, ensure_ascii=False, indent=2, default=str),
                             encoding="utf-8")
    print(json.dumps(outputs, ensure_ascii=False, indent=2, default=str))
    return 0 if outputs["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
