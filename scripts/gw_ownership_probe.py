"""#253 真实隔离 task_queue + 项目 MCP 的归属/并发/凭据验证（E1–E9）。

与 gw_mcp_probe.py 的分工：
  gw_mcp_probe.py  验证网关侧 MCP 注入（固定配置 / editor_mcp / 反向隔离）；
  本脚本验证引擎侧归属：真实 group-engine（隔离 db + 随机回环端口）+ 真实项目 MCP
  （mcp/server.py）在 work / chat-A / chat-B 执行上下文下的读取分流、写入落点、
  引擎侧 project/thread 权限校验、同 profile 并发与执行凭据生命周期。

关键边界（报告中必须同口径陈述）：
  - MCP 进程的 env 不是探针手写的，而是调用引擎真实函数
    `wake._mcp_server_config(profile, context=..., chat=...)` 生成；managed overlay
    由 `mcp_runtime.hermes_overlay` 生成并回读断言。探针只负责拉起 stdio 进程，
    不改变引擎注入的字段与值。
  - 数据准备用 SQL 直接写隔离库（不触发 wake.enqueue，杜绝拉起任何 Hermes 进程）；
    所有被验证的请求都走真实 HTTP / 真实 MCP 工具。
  - 引擎侧权限校验为真实 403/409；读路径是否校验执行范围单独记录，不合并进结论。

隔离：
  - 引擎 `GROUP_DB`/`GROUP_DATA_DIR` 全部落在 test-data/task-253/ownership，端口内核分配；
  - 引擎子进程环境白名单，不继承 HERMES_*/GROUP_ENGINE_*/XDG_*/TASK_QUEUE*；
  - `GROUP_ENGINE_KEY`/`GROUP_ADMIN_PASSWORD` 是本脚本临时测试值；
  - MCP 子进程只拿到引擎生成配置里的 env + 系统最小变量；
  - 结束停引擎，核验 LISTENING=0。

场景：
  E1 数据准备   两项目 P1/P2、成员、话题 A/B（P1）、话题 C（P2）、p2p(A/B)、wake_runs/wake_jobs；
  E2 配置生成   引擎真实 `_mcp_server_config` + `hermes_overlay` 回读断言；
  E3 读分流     work 只 P1 点对点；chat-A 只 A；chat-B 只 B，互不可见；
  E4 写归属     MCP group_send 落点（A/B/p2p）与 DB 行核对；
  E5 权限校验   跨话题/跨项目写入 403、chat 执行调用 work-only 工具被拒、
                读路径越权实际行为（单独记录）；
  E6 工具集     chat 进程 task 工具物理不存在，work 进程存在；
  E7 并发       同 profile 三个执行同时读写，各自落点正确、无串台；
  E8 凭据       旧执行凭据在 wake_run 结束/被超越/重启/换密钥后的真实响应；
                新执行重新绑定成功；与网关 session ticket 一次性区分；
  E9 网关端到端  隔离网关显式加载引擎生成的 managed overlay，会话可见 task_queue，
                模型真实调用工具返回隔离库话题数据（真实 project/thread，非 tag 回显）。
"""
from __future__ import annotations

import base64
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from probe_config import required_directory, loopback_url, absolute_path

ROOT = Path(__file__).resolve().parents[1]
PY = Path(sys.executable)
BASE = required_directory("GW_PROBE_DATA") / "task-253" / "ownership"
RUN = time.strftime("%H%M%S")
# 每次运行独立数据目录：旧库不会被复用，E1 的计数断言只对本场景行成立。
DATA = BASE / f"run-{RUN}" / "engine"
DB = DATA / "group.db"
HOME = BASE / f"run-{RUN}" / "home"
EVIDENCE = (absolute_path(os.environ["GW_EVIDENCE_DIR"], "GW_EVIDENCE_DIR")
            if os.environ.get("GW_EVIDENCE_DIR") else
            ROOT / "reports" / "research" / "hermes-gateway" / "evidence-2026-10-28-task253-ownership")
SCRATCH = BASE / f"run-{RUN}" / "scratch"
MCP = ROOT / "mcp" / "server.py"

P1 = "probe253"
P2 = "probe253-other"
P3 = "probe253-outsider"
BOT = "be-test"
PEER = "peer-test"
KEY = "test-key-253"
KEY2 = "test-key-253-rotated"
ADMIN_PW = "test-admin-253"

EVID: list[dict] = []


def log(msg, **kw):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", json.dumps(kw, ensure_ascii=False), flush=True)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(method, url, body=None, headers=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw or "{}")
        except ValueError:
            return exc.code, {"raw": raw[:300]}
    except Exception as exc:  # noqa: BLE001
        return -1, {"error": f"{type(exc).__name__}: {exc}"}


def listening(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.6)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def evidence(name: str, payload) -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / f"{RUN}-{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    EVID.append({"name": name, "file": str(path)})


def db_conn():
    conn = sqlite3.connect(DB, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def db_exec(sql, params=()):
    conn = db_conn()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def db_rows(sql, params=()):
    conn = db_conn()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def iso(offset=0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + offset))


def system_env() -> dict:
    """MCP/引擎子进程共用的系统最小环境（不含任何 GROUP_/HERMES_ 业务变量）。"""
    return {
        **{k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR") if k in os.environ},
        "PATH": os.environ.get("PATH", ""),
        "TEMP": str(SCRATCH),
        "TMP": str(SCRATCH),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(ROOT),
        "USERPROFILE": str(HOME),
        "HOMEDRIVE": HOME.drive,
        "HOMEPATH": str(HOME).split(":", 1)[-1],
        "APPDATA": str(HOME / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(HOME / "AppData" / "Local"),
    }


def engine_env(port: int, key: str) -> dict:
    HOME.mkdir(parents=True, exist_ok=True)
    env = system_env()
    env.update({
        "GROUP_DB": str(DB),
        "GROUP_DATA_DIR": str(DATA),
        "GROUP_ENGINE_KEY": key,
        "GROUP_ADMIN_PASSWORD": ADMIN_PW,
        "GROUP_ENGINE_HOST": "127.0.0.1",
        "GROUP_ENGINE_PORT": str(port),
    })
    return env


class Engine:
    def __init__(self, key=KEY, port=None):
        self.key = key
        self.port = port or free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc = None
        self.log_path = BASE / f"engine-{RUN}-{self.port}.log"

    def start(self) -> bool:
        DATA.mkdir(parents=True, exist_ok=True)
        logf = open(self.log_path, "ab")
        self.proc = subprocess.Popen(
            [str(PY), "-m", "engine.main", "--host", "127.0.0.1",
             "--port", str(self.port), "--data-dir", str(DATA)],
            cwd=str(ROOT), env=engine_env(self.port, self.key),
            stdout=logf, stderr=subprocess.STDOUT)
        for _ in range(100):
            time.sleep(0.3)
            st, body = http("GET", self.base + "/health", timeout=2)
            if st == 200 and body.get("status") == "ok":
                return True
        return False

    def login(self) -> str:
        st, body = http("POST", self.base + "/auth/login", {"password": ADMIN_PW})
        return body.get("token", "") if st == 200 else ""

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


class McpProc:
    """stdio MCP 客户端：起 mcp/server.py 子进程，发 JSON-RPC。"""

    def __init__(self, env_extra: dict, name: str):
        env = system_env()
        env.update(env_extra)  # 引擎真实生成的配置 env 原样注入
        self.name = name
        self.proc = subprocess.Popen(
            [str(PY), "-B", str(MCP)], cwd=str(ROOT), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        self._id = 0
        self._init()

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _read(self, timeout=120):
        box = {}

        def rd():
            box["line"] = self.proc.stdout.readline()

        t = threading.Thread(target=rd, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise TimeoutError(f"{self.name}: no MCP response within {timeout}s")
        line = box.get("line", "")
        if not line:
            err = ""
            try:
                err = (self.proc.stderr.read() or "")[-600:]
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(f"{self.name}: mcp exited: {err}")
        # SDK 的校验错误会打到 stdout；跳过非 JSON 行，避免误判为响应。
        for _ in range(20):
            if line.lstrip().startswith("{"):
                break
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(f"{self.name}: mcp output ended on non-json line")
        return json.loads(line)

    def _request(self, method, params=None):
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        return self._read()

    def _init(self):
        self._request("initialize", {"protocolVersion": "2024-11-05",
                                     "capabilities": {},
                                     "clientInfo": {"name": self.name, "version": "1.0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def call(self, tool, args=None):
        resp = self._request("tools/call", {"name": tool, "arguments": args or {}})
        if "error" in resp:
            return {"error": resp["error"]}
        result = resp.get("result") or {}
        try:
            return json.loads(result["content"][0]["text"])
        except Exception:  # noqa: BLE001
            return {"raw": result}

    def tools(self) -> list[str]:
        resp = self._request("tools/list", {})
        return [t.get("name") for t in (resp.get("result") or {}).get("tools") or []]

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            self.proc.kill()


# ---------------------------------------------------------------------------
# 引擎真实配置生成（不是手工拼 env）
# ---------------------------------------------------------------------------

def engine_ctx(profile, project, thread_id, session, started_at):
    return {"profile": profile, "project": project, "thread_id": thread_id,
            "session": session, "wake_started_at": started_at}


def engine_server_config(base: str, ctx: dict, chat: bool) -> dict:
    """调用引擎真实函数生成 MCP server 配置（与 wake.deliver 注入的完全同源）。"""
    os.environ["GROUP_ENGINE_URL"] = base
    from engine import wake
    return wake._mcp_server_config(ctx["profile"], context=ctx, chat=chat)


def mcp_env_from_server(server: dict) -> dict:
    return {k: v for k, v in (server.get("env") or {}).items() if isinstance(v, str)}


# ---------------------------------------------------------------------------
# E1 数据准备
# ---------------------------------------------------------------------------

def prepare(eng: Engine) -> dict:
    token = eng.login()
    out = {"admin_token": bool(token), "steps": {}}
    auth = {"Authorization": "Bearer " + token}
    for name, root_name in ((P1, "root-p1"), (P2, "root-p2"), (P3, "root-p3")):
        root = BASE / root_name
        root.mkdir(parents=True, exist_ok=True)
        (root / "README.md").write_text(f"probe project {name}\n", encoding="utf-8")
        st, body = http("POST", eng.base + "/projects",
                        {"name": name, "root": str(root), "display_name": name}, auth)
        out["steps"][f"project:{name}"] = [st, body]

    for prof in (BOT, PEER):
        for proj in (P1, P2):
            st, body = http("POST", eng.base + "/members",
                            {"profile": prof, "display_name": prof, "project": proj,
                             "active": True}, auth)
            out["steps"][f"member:{proj}:{prof}"] = [st, body]
            st, body = http("PATCH", eng.base + f"/projects/{proj}/members/{prof}/role",
                            {"role": "be-dev"}, auth)
            out["steps"][f"role:{proj}:{prof}"] = [st, body]

    # 立即把项目置非 ready：扫描器每 180s 调 wake.enqueue，ready 项目会被真实投递
    # 到宿主 Hermes；本轮只验 MCP/引擎归属，不允许任何 Hermes 进程被拉起。
    db_exec("UPDATE projects SET readiness_status='pending' WHERE name IN (?,?,?)", (P1, P2, P3))
    out["readiness_forced"] = db_rows("SELECT name, readiness_status FROM projects ORDER BY name")

    # 话题与消息走 SQL：避免 /thread、/msg 的 wake.enqueue 拉起任何 Hermes 进程。
    ts = iso()
    threads = {}
    thread_specs = [(P1, "话题A"), (P1, "话题B"), (P2, "话题C"), (P3, "话题D")]
    for proj, title in thread_specs:
        members = [BOT, PEER] if proj != P3 else [PEER]  # P3 不含 BOT：非成员负向用例
        tid = db_exec(
            "INSERT INTO threads(project,title,creator,task_id,members,status,round,"
            "created_at,last_active_at) VALUES(?,?, 'user', NULL, ?, 'open', 0, ?, ?)",
            (proj, title, json.dumps(members), ts, ts))
        threads[title] = {"id": tid, "project": proj, "members": members}
        mid = db_exec(
            "INSERT INTO messages(project,ts,sender,sender_type,content,mentions,thread_id,"
            "parent_id,round,depth) VALUES(?,?, 'user', 'user', ?, '[]', ?, NULL, 0, 0)",
            (proj, ts, f"{title} 的初始消息", tid))
        for m in members:
            if m != "user":
                db_exec("INSERT INTO inbox(message_id,profile,status) VALUES(?,?,'pending')", (mid, m))
        threads[title]["msg"] = mid

    p2p = {}
    for proj, text in ((P1, "P1 点对点消息"), (P2, "P2 点对点消息")):
        mid = db_exec(
            "INSERT INTO messages(project,ts,sender,sender_type,content,mentions,thread_id,"
            "parent_id,round,depth) VALUES(?,?, 'user', 'user', ?, '[]', NULL, NULL, 0, 0)",
            (proj, ts, text))
        db_exec("INSERT INTO inbox(message_id,profile,status) VALUES(?,?,'pending')", (mid, BOT))
        p2p[proj] = {"msg": mid, "text": text}

    # chat 窗口（/inbox kind=chat 的真实前置）：status 用 done，scanner 不会再投递。
    for title in ("话题A", "话题B", "话题C"):
        t = threads[title]
        db_exec("INSERT INTO wake_jobs(profile,project,kind,trigger,thread_id,status,"
                "enqueued_at,chat_context_until) VALUES(?,?,'chat','probe',?,'done',?,?)",
                (BOT, t["project"], t["id"], ts, iso(1800)))

    out["threads"] = threads
    out["p2p"] = p2p
    out["db_counts"] = {
        "projects": db_rows("SELECT COUNT(*) n FROM projects")[0]["n"],
        "threads": db_rows("SELECT COUNT(*) n FROM threads")[0]["n"],
        "messages": db_rows("SELECT COUNT(*) n FROM messages")[0]["n"],
    }
    out["readiness"] = db_rows("SELECT name, readiness_status FROM projects ORDER BY name")
    out["passed"] = (out["db_counts"]["projects"] == 3 and out["db_counts"]["threads"] == 4
                     and all(t.get("id") for t in threads.values()))
    return out


def insert_wake_run(profile, project, thread_id, session, started_at) -> None:
    db_exec("INSERT INTO wake_runs(session,profile,thread_id,started_at,project,runtime) "
            "VALUES(?,?,?,?,?, 'hermes')", (session, profile, thread_id, started_at, project))


def end_wake_run(session, started_at) -> None:
    db_exec("UPDATE wake_runs SET ended_at=? WHERE session=? AND started_at=?",
            (time.time(), session, started_at))


def token_for(profile: str, ctx: dict) -> str:
    from engine.auth import execution_token
    return execution_token(profile, ctx)


def bot_headers(profile: str, token: str) -> dict:
    return {"X-Group-Profile": profile, "X-Group-Key": token}


# ---------------------------------------------------------------------------
# E2 配置生成
# ---------------------------------------------------------------------------

def e2_config(eng: Engine, threads: dict) -> dict:
    tA = threads["话题A"]["id"]
    start = time.time()
    session = f"{BOT}-{P1}-chat-{tA}"
    ctx = engine_ctx(BOT, P1, tA, session, start)
    server = engine_server_config(eng.base, ctx, chat=True)
    env = server.get("env") or {}
    overlay_dir = None
    overlay = None
    try:
        from engine import mcp_runtime
        src_env = {}
        overlay = mcp_runtime.hermes_overlay(server, src_env, profile_home=None)
        overlay_dir = src_env.get("HERMES_MANAGED_DIR")
    except Exception as exc:  # noqa: BLE001
        return {"passed": False, "error": f"hermes_overlay failed: {type(exc).__name__}: {exc}",
                "server": server}
    import yaml
    cfg = yaml.safe_load((Path(overlay_dir) / "config.yaml").read_text(encoding="utf-8"))
    entry = ((cfg.get("mcp_servers") or {}).get("task_queue") or {})

    # 可证伪对照：向 overlay 的 env 预置父目录值，overlay 必须写自己的临时目录而不是复用它；
    # 若 hermes_overlay 改为沿用传入值，control_dir 将等于 parent_managed，本断言失败。
    # 「父环境变量不进入子进程」由 M3 验证，本项只声明不复用父目录。
    parent_managed = r"X:\parent-managed-should-not-leak"
    control_env = {"HERMES_MANAGED_DIR": parent_managed}
    control_overlay = None
    control_dir = None
    try:
        control_overlay = mcp_runtime.hermes_overlay(server, control_env, profile_home=None)
        control_dir = control_env.get("HERMES_MANAGED_DIR")
    finally:
        if control_overlay is not None:
            control_overlay.cleanup()

    checks = {
        "command_is_python": str(entry.get("command", "")).lower().endswith("python.exe"),
        "args_points_to_mcp_server": str((entry.get("args") or [""])[0]).endswith("mcp\\server.py")
                                      or str((entry.get("args") or [""])[0]).endswith("mcp/server.py"),
        "env_role": entry.get("env", {}).get("TASK_QUEUE_ROLE") == BOT,
        "env_profile": entry.get("env", {}).get("HERMES_PROFILE") == BOT,
        "env_url": entry.get("env", {}).get("GROUP_ENGINE_URL") == eng.base,
        "env_token_execution_prefix": str(entry.get("env", {}).get("GROUP_ENGINE_TOKEN", "")).startswith("run."),
        "env_context_present": json.loads(entry.get("env", {}).get("XDG_GROUP_ENGINE_EXECUTION", "{}"))
                                 .get("thread_id") == tA,
        "enabled": entry.get("enabled") is True,
        "lazy_false": entry.get("lazy") is False,
        "connect_timeout_60": entry.get("connect_timeout") == 60,
        "cwd_is_project_root": str(entry.get("cwd", "")).lower() == str(ROOT).lower(),
        "overlay_does_not_reuse_parent_managed_dir": bool(control_dir) and control_dir != parent_managed,
    }
    result = {"passed": all(checks.values()), "checks": checks,
              "server_env_keys": sorted(env), "overlay": overlay_dir,
              "control_managed_dir": control_dir, "parent_managed": parent_managed,
              "token_prefix": str(env.get("GROUP_ENGINE_TOKEN", ""))[:20] + "…",
              "raw_entry": {k: v for k, v in entry.items() if k != "env"}}
    if overlay is not None:
        overlay.cleanup()
    return result


# ---------------------------------------------------------------------------
# E3 读分流
# ---------------------------------------------------------------------------

def read_view(proc: McpProc) -> dict:
    res = proc.call("group_read_inbox")
    msgs = res.get("messages") or []
    return {
        "count": len(msgs),
        "thread_ids": sorted({m.get("thread_id") for m in msgs}),
        "projects": sorted({m.get("project") for m in msgs}),
        "texts": [str(m.get("content"))[:24] for m in msgs],
        "pending_total": res.get("pending_total"),
        "error": res.get("error"),
    }


def e3_read_routing(eng: Engine, threads: dict, runs: dict) -> dict:
    procs = {}
    out = {}
    try:
        for name, tid in (("work", None), ("chatA", threads["话题A"]["id"]),
                          ("chatB", threads["话题B"]["id"])):
            env = mcp_env_from_server(engine_server_config(eng.base, runs[name]["ctx"], chat=tid is not None))
            procs[name] = McpProc(env, name)
        for name, proc in procs.items():
            out[name] = read_view(proc)
    finally:
        for proc in procs.values():
            proc.close()
    work_texts = " | ".join(out.get("work", {}).get("texts") or [])
    a_texts = " | ".join(out.get("chatA", {}).get("texts") or [])
    b_texts = " | ".join(out.get("chatB", {}).get("texts") or [])
    checks = {
        "work_sees_p1_p2p": "P1 点对点消息" in work_texts,
        "work_not_sees_p2_p2p": "P2 点对点消息" not in work_texts,
        "work_not_sees_topics": out.get("work", {}).get("thread_ids") in ([], [0], [None]),
        "chatA_only_topicA": out.get("chatA", {}).get("thread_ids") == [threads["话题A"]["id"]],
        "chatA_sees_A_text": "话题A" in a_texts,
        "chatA_not_sees_B": "话题B" not in a_texts,
        "chatB_only_topicB": out.get("chatB", {}).get("thread_ids") == [threads["话题B"]["id"]],
        "chatB_sees_B_text": "话题B" in b_texts,
        "chatB_not_sees_A": "话题A" not in b_texts,
        "no_errors": not any(out[k].get("error") for k in out),
    }
    return {"passed": all(checks.values()), "checks": checks, "views": out}


# ---------------------------------------------------------------------------
# E4 写归属
# ---------------------------------------------------------------------------

def grant_turn(thread_id, profile=BOT) -> None:
    """话题发言位：真实 router 只允许 turn_profile 指向的成员发言（409）。"""
    db_exec("UPDATE threads SET turn_profile=?, turn_parent_id=NULL WHERE id=?", (profile, thread_id))


def e4_write_ownership(eng: Engine, threads: dict, runs: dict) -> dict:
    procs = {}
    sends = {}
    try:
        for name, tid in (("work", None), ("chatA", threads["话题A"]["id"]),
                          ("chatB", threads["话题B"]["id"])):
            env = mcp_env_from_server(engine_server_config(eng.base, runs[name]["ctx"], chat=tid is not None))
            procs[name] = McpProc(env, name)
        grant_turn(threads["话题A"]["id"])
        sends["chatA"] = procs["chatA"].call("group_send", {"text": "A 回执 E4", "need_reply": False})
        grant_turn(threads["话题B"]["id"])
        sends["chatB"] = procs["chatB"].call("group_send", {"text": "B 回执 E4", "need_reply": False})
        sends["work"] = procs["work"].call("group_send",
                                           {"text": f"@{PEER} P1 点对点回执 E4", "need_reply": False})
    finally:
        for proc in procs.values():
            proc.close()

    rows = {}
    for name, res in sends.items():
        mid = res.get("message_id")
        rows[name] = db_rows("SELECT id,project,thread_id,sender,sender_type,content FROM messages "
                             "WHERE id=?", (mid,)) if mid else []
    checks = {
        "chatA_written_to_A": bool(rows.get("chatA")) and rows["chatA"][0]["thread_id"] == threads["话题A"]["id"],
        "chatB_written_to_B": bool(rows.get("chatB")) and rows["chatB"][0]["thread_id"] == threads["话题B"]["id"],
        "work_written_p2p_p1": bool(rows.get("work")) and rows["work"][0]["thread_id"] is None
                               and rows["work"][0]["project"] == P1,
        "chatA_not_written_elsewhere": bool(rows.get("chatA")) and rows["chatA"][0]["project"] == P1,
        "work_mention_sent_to_peer": bool(rows.get("work"))
                                     and PEER in json.loads(
                                         (db_rows("SELECT mentions FROM messages WHERE id=?",
                                                  (sends["work"].get("message_id"),)) or [{"mentions": "[]"}])[0]["mentions"]),
        "no_errors": not any("error" in (r or {}) for r in sends.values()),
    }
    return {"passed": all(checks.values()), "checks": checks, "sends": sends, "rows": rows}


# ---------------------------------------------------------------------------
# E5 引擎侧权限校验（真实 403/409）
# ---------------------------------------------------------------------------

def e5_permissions(eng: Engine, threads: dict, runs: dict) -> dict:
    tA, tB = threads["话题A"]["id"], threads["话题B"]["id"]
    tokenA = runs["chatA"]["token"]
    tokenW = runs["work"]["token"]
    tokenB = runs["chatB"]["token"]

    def post_msg(tok, project, thread_id, text):
        body = {"project": project, "thread_id": thread_id, "text": text, "need_reply": False,
                "execution": {"session": runs_by_token(tok)["ctx"]["session"],
                              "wake_started_at": runs_by_token(tok)["ctx"]["wake_started_at"]}}
        return http("POST", eng.base + "/msg", body, bot_headers(BOT, tok))

    def runs_by_token(tok):
        for r in runs.values():
            if r["token"] == tok:
                return r
        raise KeyError("unknown token")

    cases = {}
    st, body = post_msg(tokenA, P1, tB, "跨话题写入 E5")
    cases["chatA_to_topicB"] = {"status": st, "detail": body.get("detail") or body.get("message_id"),
                                "expect": 403}

    st, body = post_msg(tokenA, P2, None, "跨项目写入 E5")
    cases["chatA_to_project_P2_p2p"] = {"status": st, "detail": body.get("detail") or body.get("message_id"),
                                        "expect": 403}

    st, body = post_msg(tokenW, P1, tA, "work 写入话题 E5")
    cases["work_to_topicA"] = {"status": st, "detail": body.get("detail") or body.get("message_id"),
                               "expect": 403}

    # chat 执行调用 work-only 工具（/task-tools 的真实 body 结构：arguments + execution）
    st, body = http("POST", eng.base + "/task-tools/task_claim",
                    {"arguments": {"project": P1},
                     "execution": {"session": runs["chatA"]["ctx"]["session"],
                                   "wake_started_at": runs["chatA"]["ctx"]["wake_started_at"]}},
                    bot_headers(BOT, tokenA))
    cases["chat_task_claim"] = {"status": st, "detail": body.get("detail") or body.get("error"),
                                "expect": 403}

    # 非成员项目（P3 不含 BOT）：用 bot 长期凭据，验证成员资格校验
    from engine.auth import bot_token
    st, body = http("POST", eng.base + "/msg",
                    {"project": P3, "thread_id": None, "text": "非成员项目 E5",
                     "need_reply": False, "execution": {"session": f"{BOT}-{P3}-work",
                                                        "wake_started_at": time.time()}},
                    bot_headers(BOT, bot_token(BOT)))
    cases["bot_to_project_P3_non_member"] = {"status": st,
                                             "detail": body.get("detail") or body.get("message_id"),
                                             "expect": 403}

    # 读路径：给 BOT 留一条 B 的 pending 再读，避免「已读水位清空」导致的假阴性。
    mid_b = db_exec("INSERT INTO messages(project, ts, sender, sender_type, content, mentions, "
                    "thread_id, parent_id, round, depth) VALUES(?,?,?,?,?,?,?,NULL,0,0)",
                    (P1, iso(), PEER, "bot", "话题B 的读越权探针", "[]", tB))
    db_exec("INSERT INTO inbox(message_id, profile, status) VALUES(?,?,'pending')", (mid_b, BOT))
    st, body = http("GET", eng.base + f"/inbox?profile={BOT}&kind=chat&thread_id={tB}&limit=1",
                    headers=bot_headers(BOT, tokenA))
    cases["chatA_credential_reads_topicB"] = {
        "status": st,
        "thread_ids": sorted({m.get("thread_id") for m in (body.get("messages") or [])}),
        "texts": [m.get("content") for m in (body.get("messages") or [])],
        "error": body.get("detail") or body.get("error"),
        "expect": "200 + topic B data（执行凭据不限制读范围）",
    }

    # 成员资格对照：chat-A 凭据读 A 正常
    mid_a = db_exec("INSERT INTO messages(project, ts, sender, sender_type, content, mentions, "
                    "thread_id, parent_id, round, depth) VALUES(?,?,?,?,?,?,?,NULL,0,0)",
                    (P1, iso(), PEER, "bot", "话题A 的读对照探针", "[]", tA))
    db_exec("INSERT INTO inbox(message_id, profile, status) VALUES(?,?,'pending')", (mid_a, BOT))
    st, body = http("GET", eng.base + f"/inbox?profile={BOT}&kind=chat&thread_id={tA}&limit=1",
                    headers=bot_headers(BOT, tokenA))
    cases["chatA_credential_reads_topicA"] = {"status": st,
                                              "thread_ids": sorted({m.get("thread_id") for m in (body.get("messages") or [])}),
                                              "texts": [m.get("content") for m in (body.get("messages") or [])],
                                              "expect": 200}

    checks = {
        "cross_thread_write_403": cases["chatA_to_topicB"]["status"] == 403,
        "cross_project_write_403": cases["chatA_to_project_P2_p2p"]["status"] == 403,
        "work_to_topic_403": cases["work_to_topicA"]["status"] == 403,
        "chat_task_claim_403": cases["chat_task_claim"]["status"] == 403,
        "non_member_project_403": cases["bot_to_project_P3_non_member"]["status"] == 403,
        "own_topic_read_200": cases["chatA_credential_reads_topicA"]["status"] == 200,
        # 读接口的真实行为（实证，不是推断）：执行凭据的 thread 不限制读范围。
        "engine_read_ignores_execution_thread": cases["chatA_credential_reads_topicB"]["thread_ids"] == [tB],
    }
    # E5 的两个读探针只服务本场景；清掉已读水位，避免影响 E7 的并发读断言。
    db_exec("UPDATE inbox SET status='seen' WHERE message_id IN (?,?)", (mid_a, mid_b))
    return {"passed": all(checks.values()), "checks": checks, "cases": cases,
            "note": "写路径校验执行范围；读接口按成员资格授权，不校验执行凭据的 thread。"}


# ---------------------------------------------------------------------------
# E6 工具集隔离
# ---------------------------------------------------------------------------

def e6_toolsets(eng: Engine, runs: dict) -> dict:
    chat = McpProc(mcp_env_from_server(engine_server_config(eng.base, runs["chatA"]["ctx"], chat=True)), "chat-tools")
    work = McpProc(mcp_env_from_server(engine_server_config(eng.base, runs["work"]["ctx"], chat=False)), "work-tools")
    try:
        ctools = chat.tools()
        wtools = work.tools()
    finally:
        chat.close()
        work.close()
    work_only = {"task_claim", "task_log", "task_complete", "task_block"}
    checks = {
        "chat_has_group_tools": {"group_send", "group_read_inbox"} <= set(ctools),
        "chat_lacks_all_work_only": not (work_only & set(ctools)),
        "work_has_work_only": work_only <= set(wtools),
        "work_has_group_tools": {"group_send", "group_read_inbox"} <= set(wtools),
        "counts": len(ctools) < len(wtools),
    }
    return {"passed": all(v for k, v in checks.items() if k != "counts") and checks["counts"],
            "checks": checks, "chat_count": len(ctools), "work_count": len(wtools),
            "chat_tools": sorted(ctools), "work_only_missing_in_chat": sorted(work_only - set(ctools))}


# ---------------------------------------------------------------------------
# E7 同 profile 并发（话题 A/B + work）
# ---------------------------------------------------------------------------

def e7_concurrency(eng: Engine, threads: dict, runs: dict) -> dict:
    # 并发前给三个执行各留一条新的 pending：chat 读到的必须是本话题那条，
    # work 读到的必须是 P1 点对点那条（发送会推进话题已读水位，所以先读后写）。
    ts = iso()
    probe = {}
    for key, proj, tid, text in (
            ("work", P1, None, "并发读 p2p"),
            ("chatA", P1, threads["话题A"]["id"], "并发读 A"),
            ("chatB", P1, threads["话题B"]["id"], "并发读 B")):
        mid = db_exec(
            "INSERT INTO messages(project,ts,sender,sender_type,content,mentions,thread_id,"
            "parent_id,round,depth) VALUES(?,?, 'user', 'user', ?, '[]', ?, NULL, 0, 0)",
            (proj, ts, text, tid))
        db_exec("INSERT INTO inbox(message_id,profile,status) VALUES(?,?,'pending')", (mid, BOT))
        probe[key] = {"message_id": mid, "text": text}

    procs = {}
    results = {}
    lock = threading.Lock()
    try:
        for name, tid in (("work", None), ("chatA", threads["话题A"]["id"]),
                          ("chatB", threads["话题B"]["id"])):
            env = mcp_env_from_server(engine_server_config(eng.base, runs[name]["ctx"], chat=tid is not None))
            procs[name] = McpProc(env, f"cc-{name}")

        # 并发前分别授予两个话题的发言位（不同话题，互不影响）
        grant_turn(threads["话题A"]["id"])
        grant_turn(threads["话题B"]["id"])

        def run(name, proc):
            view = read_view(proc)
            text = {"work": f"@{PEER} 并发 p2p", "chatA": "并发 A", "chatB": "并发 B"}[name]
            sent = proc.call("group_send", {"text": text, "need_reply": False})
            with lock:
                results[name] = {"send": sent, "view": view}

        ts_threads = [threading.Thread(target=run, args=(n, p)) for n, p in procs.items()]
        for t in ts_threads:
            t.start()
        for t in ts_threads:
            t.join(timeout=90)
    finally:
        for proc in procs.values():
            proc.close()

    rows = {}
    for name in ("work", "chatA", "chatB"):
        mid = (results.get(name, {}).get("send") or {}).get("message_id")
        rows[name] = db_rows("SELECT id,project,thread_id,content FROM messages WHERE id=?", (mid,)) if mid else []

    def view_of(name):
        return (results.get(name) or {}).get("view") or {}

    def texts_of(name):
        return " | ".join(view_of(name).get("texts") or [])

    checks = {
        "all_three_sent": all(rows.get(k) for k in rows),
        "chatA_landed_A": bool(rows.get("chatA")) and rows["chatA"][0]["thread_id"] == threads["话题A"]["id"],
        "chatB_landed_B": bool(rows.get("chatB")) and rows["chatB"][0]["thread_id"] == threads["话题B"]["id"],
        "work_landed_p2p": bool(rows.get("work")) and rows["work"][0]["thread_id"] is None,
        "chatA_view_only_A": view_of("chatA").get("thread_ids") == [threads["话题A"]["id"]],
        "chatB_view_only_B": view_of("chatB").get("thread_ids") == [threads["话题B"]["id"]],
        "work_view_no_topic": view_of("work").get("thread_ids") in ([], [0], [None]),
        "chatA_reads_own_probe": "并发读 A" in texts_of("chatA"),
        "chatB_reads_own_probe": "并发读 B" in texts_of("chatB"),
        "work_reads_p2p_probe": "并发读 p2p" in texts_of("work"),
        "no_cross_talk": ("并发读 B" not in texts_of("chatA") and "并发读 A" not in texts_of("chatB")
                          and "并发读 p2p" not in texts_of("chatA") and "并发读 p2p" not in texts_of("chatB")),
        "no_errors": not any((view_of(k).get("error") or (results.get(k, {}).get("send") or {}).get("error"))
                             for k in ("work", "chatA", "chatB")),
    }
    return {"passed": all(checks.values()), "checks": checks, "results": results,
            "rows": rows, "probes": probe}


# ---------------------------------------------------------------------------
# E8 执行凭据生命周期
# ---------------------------------------------------------------------------

def e8_credentials(eng: Engine, threads: dict) -> dict:
    tA = threads["话题A"]["id"]
    out = {"steps": {}}

    def send(base, tok, session, start, text):
        grant_turn(tA)  # 话题发言位：与凭据校验正交，先排除 409 干扰
        return http("POST", base + "/msg",
                    {"project": P1, "thread_id": tA, "text": text, "need_reply": False,
                     "execution": {"session": session, "wake_started_at": start}},
                    bot_headers(BOT, tok))

    # (1) 活跃执行：写入成功
    session = f"{BOT}-{P1}-chat-{tA}"
    t0 = time.time()
    insert_wake_run(BOT, P1, tA, session, t0)
    ctx0 = engine_ctx(BOT, P1, tA, session, t0)
    tok0 = token_for(BOT, ctx0)
    st, body = send(eng.base, tok0, session, t0, "E8 活跃执行")
    out["steps"]["active_write"] = {"status": st, "message_id": body.get("message_id"),
                                    "detail": body.get("detail")}

    # (2) 执行结束（ended_at 写入，与 wake.py 进程退出同一字段）→ 旧凭据 409
    end_wake_run(session, t0)
    st, body = send(eng.base, tok0, session, t0, "E8 结束后旧凭据")
    out["steps"]["after_end_old_token"] = {"status": st, "detail": body.get("detail")}

    # (3) 同 session 新执行 → 新凭据可写，旧凭据仍被拒
    t1 = time.time() + 0.05
    insert_wake_run(BOT, P1, tA, session, t1)
    ctx1 = engine_ctx(BOT, P1, tA, session, t1)
    tok1 = token_for(BOT, ctx1)
    st, body = send(eng.base, tok1, session, t1, "E8 新执行")
    out["steps"]["new_run_write"] = {"status": st, "message_id": body.get("message_id"),
                                     "detail": body.get("detail")}
    st, body = send(eng.base, tok0, session, t0, "E8 旧执行再试")
    out["steps"]["old_token_after_new_run"] = {"status": st, "detail": body.get("detail")}

    # (4) 被超越（同 session 有更晚的未结束 run）→ 409 superseded
    t2 = time.time() + 0.1
    insert_wake_run(BOT, P1, tA, session, t2)
    tok2 = token_for(BOT, engine_ctx(BOT, P1, tA, session, t2))
    st, body = send(eng.base, tok1, session, t1, "E8 被超越的旧执行")
    out["steps"]["superseded_old_token"] = {"status": st, "detail": body.get("detail")}
    st, body = send(eng.base, tok2, session, t2, "E8 最新执行")
    out["steps"]["latest_run_write"] = {"status": st, "message_id": body.get("message_id"),
                                        "detail": body.get("detail")}

    # (5) 同密钥重启：凭据有效性由 wake_runs 状态决定，不依赖进程内存
    eng.stop()
    eng2 = Engine(KEY, port=free_port())
    restart_ok = False
    try:
        restart_ok = eng2.start()
        out["steps"]["restart_same_key"] = {"started": restart_ok, "port": eng2.port}
        if restart_ok:
            st, body = send(eng2.base, tok0, session, t0, "E8 同密钥重启后旧凭据")
            out["steps"]["after_restart_old_ended_token"] = {"status": st, "detail": body.get("detail")}
            st, body = send(eng2.base, tok2, session, t2, "E8 同密钥重启后最新凭据")
            out["steps"]["after_restart_latest_token"] = {"status": st, "message_id": body.get("message_id"),
                                                          "detail": body.get("detail")}
            out["steps"]["restart_orphan_reaped"] = db_rows(
                "SELECT session,started_at,ended_at,exit_code FROM wake_runs "
                "WHERE session=? AND started_at=?", (session, t2))
            # 重启后重新绑定：新执行 + 新凭据
            t3 = time.time()
            insert_wake_run(BOT, P1, tA, session, t3)
            tok3 = token_for(BOT, engine_ctx(BOT, P1, tA, session, t3))
            st, body = send(eng2.base, tok3, session, t3, "E8 重启后新执行重新绑定")
            out["steps"]["rebind_after_restart"] = {"status": st, "message_id": body.get("message_id"),
                                                    "detail": body.get("detail")}
    finally:
        eng2.stop()

    # (6) 换密钥重启：旧凭据签名失效 401
    eng3 = Engine(KEY2, port=free_port())
    rotated_ok = False
    try:
        rotated_ok = eng3.start()
        out["steps"]["restart_rotated_key"] = {"started": rotated_ok, "port": eng3.port}
        if rotated_ok:
            st, body = send(eng3.base, tok2, session, t2, "E8 换密钥后旧凭据")
            out["steps"]["after_key_rotation_old_token"] = {"status": st, "detail": body.get("detail")}
    finally:
        eng3.stop()
    out["tokens"] = {"old_prefix": tok0[:26] + "…", "new_prefix": tok1[:26] + "…"}

    steps = out["steps"]
    checks = {
        "active_write_ok": steps["active_write"]["status"] == 200,
        "ended_token_409": steps["after_end_old_token"]["status"] == 409,
        "new_run_ok": steps["new_run_write"]["status"] == 200,
        "old_token_still_409_after_new_run": steps["old_token_after_new_run"]["status"] == 409,
        "superseded_409": steps["superseded_old_token"]["status"] == 409,
        "latest_run_ok": steps["latest_run_write"]["status"] == 200,
        "restart_same_key_started": restart_ok,
        "after_restart_old_ended_409": steps.get("after_restart_old_ended_token", {}).get("status") == 409,
        "after_restart_orphan_409": (steps.get("after_restart_latest_token", {}).get("status") == 409
                                     and "ended" in str(steps.get("after_restart_latest_token", {}).get("detail"))),
        "orphan_reaped_on_restart": bool((steps.get("restart_orphan_reaped") or [{}])[0].get("ended_at")),
        "rebind_after_restart_ok": steps.get("rebind_after_restart", {}).get("status") == 200,
        "rotated_key_started": rotated_ok,
        "after_rotation_401": steps.get("after_key_rotation_old_token", {}).get("status") == 401,
    }
    return {"passed": all(checks.values()), "checks": checks, **out,
            "note": "session ticket 一次性属网关侧（#250/#252 已证 403），本项验证的是引擎执行凭据："
                    "wake_runs.ended_at / 更晚 run 存在 / GROUP_ENGINE_KEY 轮换三条真实失效路径。"}


# ---------------------------------------------------------------------------
# E9 真实网关加载引擎生成的 managed overlay（端到端）
# ---------------------------------------------------------------------------

def e9_gateway_managed(eng: Engine, threads: dict, runs: dict) -> dict:
    """用隔离网关显式加载引擎生成的 managed overlay，让真实模型调用 task_queue 工具。

    与 gw_mcp_probe M3 的区别：M3 证明「父环境继承的 HERMES_MANAGED_DIR 被白名单移除」；
    本项反过来，把引擎 `mcp_runtime.hermes_overlay` 生成的目录**显式**注入网关子进程，
    验证网关确实加载 `mcp_servers.task_queue` 并把会话连到隔离引擎（而不是只证明配置生成）。
    """
    import asyncio
    import gw_probe as G
    import gw_mcp_probe as M
    from engine import mcp_runtime

    # chat 发言会推进话题已读水位（E3/E4/E7 之后话题 A 可能已清空），
    # 因此先给 BOT 留一条新的 pending，模型读到的必须是这条真实入库的消息。
    probe_text = "E9 网关回执探针：managed overlay 连通"
    mid = db_exec("INSERT INTO messages(project, ts, sender, sender_type, content, mentions, "
                  "thread_id, parent_id, round, depth) VALUES(?,?,?,?,?,?,?,NULL,0,0)",
                  (P1, iso(), PEER, "bot", probe_text, "[]", threads["话题A"]["id"]))
    db_exec("INSERT INTO inbox(message_id, profile, status) VALUES(?,?, 'pending')", (mid, BOT))

    server = engine_server_config(eng.base, runs["chatA"]["ctx"], chat=True)
    overlay_env = {}
    overlay = mcp_runtime.hermes_overlay(server, overlay_env, profile_home=None)
    mdir = overlay_env.get("HERMES_MANAGED_DIR", "")
    orig_probe_env = G.probe_env
    orig_connect = G.CONNECT_CODE

    def patched_env():
        env = orig_probe_env()
        env["HERMES_MANAGED_DIR"] = mdir  # 显式注入（不是从父环境继承）
        return env

    G.probe_env = patched_env
    # CONNECT_CODE 默认 pop HERMES_MANAGED_DIR（隔离生产 managed scope）；本项改为保留显式值。
    G.CONNECT_CODE = orig_connect.replace('"HERMES_MANAGED_DIR", ', "")
    info = None
    pid = None
    rec = {"overlay": mdir, "engine": eng.base, "thread_id": threads["话题A"]["id"]}
    try:
        rec["config_reset"] = M.reset_config_servers()
        info = G.connect_gateway_info()
        pid = M.gateway_pid(info)
        rec["gateway"] = {"api_origin": info.get("api_origin"), "pid": pid}

        async def run():
            ws = await M._open(info)
            try:
                created = await M.new_session_any(ws, "e9")
                servers = (created.get("info") or {}).get("mcp_servers")
                names = [s.get("name") if isinstance(s, dict) else s for s in (servers or [])]
                prompt = ("请调用工具 mcp__task_queue__group_read_inbox，不要传任何参数，"
                          "把工具返回的 JSON 原文完整贴出来，不要改写。")
                sub = await M.submit_text(ws, created["session_id"], prompt, "e9")
                final = await M.wait_final(ws, created["session_id"],
                                           (sub or {}).get("admission_id") or "", "e9")
                return {"session_mcp_servers": servers, "visible_names": names,
                        "admission": sub, "final": final, "reply": str(final.get("text") or "")}
            finally:
                if M._is_open(ws):
                    await ws.close()

        res = asyncio.run(run())
        rec.update(res)
        text = res.get("reply") or ""
        tq = next((s for s in (res.get("session_mcp_servers") or [])
                   if isinstance(s, dict) and s.get("name") == "task_queue"), {})
        rec["task_queue_connected"] = tq.get("connected") is True
        rec["task_queue_tools"] = tq.get("tools")
        rec["has_topic_a"] = "话题A" in text
        rec["has_probe_text"] = probe_text in text
        rec["has_project_p1"] = P1 in text
        rec["has_production_project"] = "xjk" in text
        rec["passed"] = (bool(res.get("visible_names")) and "task_queue" in res["visible_names"]
                         and rec["task_queue_connected"] and rec["has_probe_text"]
                         and not rec["has_production_project"])
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"{type(exc).__name__}: {exc}"
        rec["passed"] = False
    finally:
        if info is not None:
            try:
                rec["stop"] = G.stop_instance(info, pid, force=True)
            except Exception as exc:  # noqa: BLE001
                rec["stop_error"] = f"{type(exc).__name__}: {exc}"
        overlay.cleanup()
        G.probe_env = orig_probe_env
        G.CONNECT_CODE = orig_connect
    return rec


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def heartbeat() -> dict:
    origin = os.environ.get("GW_OBSERVER_URL", "")
    if not origin:
        return {"status": None, "skipped": True, "reason": "GW_OBSERVER_URL not configured"}
    st, body = http("GET", loopback_url(origin) + "/heartbeat", timeout=3)
    return {"status": st, "body": body if st == 200 else str(body)[:120]}


def main() -> int:
    if os.environ.get("GW_OBSERVER_URL"):
        loopback_url(os.environ["GW_OBSERVER_URL"])
    BASE.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    HOME.mkdir(parents=True, exist_ok=True)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    SCRATCH.mkdir(parents=True, exist_ok=True)
    # --only e2,e3,... 只跑指定场景（E1 数据准备与执行上下文始终执行），便于单项复跑。
    argv = sys.argv[1:]
    only = set()
    if "--only" in argv:
        idx = argv.index("--only")
        if idx + 1 < len(argv):
            only = {x.strip().lower() for x in argv[idx + 1].split(",") if x.strip()}

    def want(name: str) -> bool:
        return not only or name in only
    # 父进程配置必须在 import engine 之前落位：config.data_dir() 会读该目录 .env。
    os.environ["GROUP_DATA_DIR"] = str(DATA)
    os.environ["GROUP_DB"] = str(DB)
    os.environ["GROUP_ENGINE_KEY"] = KEY
    os.environ["GROUP_ADMIN_PASSWORD"] = ADMIN_PW
    sys.path.insert(0, str(ROOT))

    report = {"meta": {"run": RUN, "base": str(BASE), "db": str(DB), "mcp": str(MCP),
                       "pid": os.getpid(), "python": str(PY)},
              "scenarios": {}}
    hb_before = heartbeat()
    report["heartbeat_before"] = hb_before

    eng = Engine()
    try:
        if not eng.start():
            log("ENGINE START FAILED", log=str(eng.log_path))
            report["engine"] = {"started": False, "log": str(eng.log_path)}
            evidence("engine-failed", report)
            return 2
        report["engine"] = {"started": True, "port": eng.port, "base": eng.base,
                            "db": str(DB), "data_dir": str(DATA),
                            "port_basis": "kernel-assigned 127.0.0.1 loopback (bind port 0 then release)",
                            "listen": listening(eng.port), "log": str(eng.log_path)}
        log("engine ready", port=eng.port, pid=eng.proc.pid)

        e1 = prepare(eng)
        report["scenarios"]["E1_prepare"] = e1
        evidence("e1-prepare", e1)
        log("E1 prepare", counts=e1["db_counts"], readiness=e1["readiness"])

        threads = e1["threads"]
        # 三个执行上下文（chat 用真实会话名；work 无 thread）
        started = {name: time.time() + i * 0.01 for i, name in enumerate(("work", "chatA", "chatB"))}
        runs = {}
        for name, tid in (("work", None), ("chatA", threads["话题A"]["id"]), ("chatB", threads["话题B"]["id"])):
            session = f"{BOT}-{P1}-work" if tid is None else f"{BOT}-{P1}-chat-{tid}"
            insert_wake_run(BOT, P1, tid, session, started[name])
            ctx = engine_ctx(BOT, P1, tid, session, started[name])
            runs[name] = {"ctx": ctx, "token": token_for(BOT, ctx), "session": session}
        report["runs"] = {k: {"session": v["session"], "thread_id": v["ctx"]["thread_id"],
                              "token_prefix": v["token"][:26] + "…"} for k, v in runs.items()}

        if want("e2"):
            report["scenarios"]["E2_config"] = e2_config(eng, threads)
            evidence("e2-config", report["scenarios"]["E2_config"])
            log("E2 config", passed=report["scenarios"]["E2_config"]["passed"],
                checks=report["scenarios"]["E2_config"].get("checks"))

        if want("e3"):
            report["scenarios"]["E3_read"] = e3_read_routing(eng, threads, runs)
            evidence("e3-read-routing", report["scenarios"]["E3_read"])
            log("E3 read", passed=report["scenarios"]["E3_read"]["passed"],
                checks=report["scenarios"]["E3_read"]["checks"])

        if want("e4"):
            report["scenarios"]["E4_write"] = e4_write_ownership(eng, threads, runs)
            evidence("e4-write-ownership", report["scenarios"]["E4_write"])
            log("E4 write", passed=report["scenarios"]["E4_write"]["passed"],
                checks=report["scenarios"]["E4_write"]["checks"])

        if want("e5"):
            report["scenarios"]["E5_permissions"] = e5_permissions(eng, threads, runs)
            evidence("e5-permissions", report["scenarios"]["E5_permissions"])
            log("E5 permissions", passed=report["scenarios"]["E5_permissions"]["passed"],
                checks=report["scenarios"]["E5_permissions"]["checks"])

        if want("e6"):
            report["scenarios"]["E6_toolsets"] = e6_toolsets(eng, runs)
            evidence("e6-toolsets", report["scenarios"]["E6_toolsets"])
            log("E6 toolsets", passed=report["scenarios"]["E6_toolsets"]["passed"],
                chat=report["scenarios"]["E6_toolsets"]["chat_count"],
                work=report["scenarios"]["E6_toolsets"]["work_count"])

        if want("e7"):
            report["scenarios"]["E7_concurrency"] = e7_concurrency(eng, threads, runs)
            evidence("e7-concurrency", report["scenarios"]["E7_concurrency"])
            log("E7 concurrency", passed=report["scenarios"]["E7_concurrency"]["passed"],
                checks=report["scenarios"]["E7_concurrency"]["checks"])

        if want("e9"):
            # 宿主网关的 session DB 偶发写入失败（unable to open database file）会让
            # assistant 消息不落库、回执为空；重试并保留每次失败证据。
            attempts = []
            e9 = {}
            for attempt in range(1, 4):
                e9 = e9_gateway_managed(eng, threads, runs)
                attempts.append({k: e9.get(k) for k in
                                 ("passed", "visible_names", "has_topic_a", "error", "final")})
                if e9.get("passed"):
                    break
                evidence(f"e9-attempt{attempt}-failed", e9)
                log("E9 attempt not passed, retrying", attempt=attempt,
                    error=e9.get("error"), final=(e9.get("final") or {}).get("receipt"))
            e9["attempts"] = attempts
            report["scenarios"]["E9_gateway_managed"] = e9
            evidence("e9-gateway-managed", e9)
            log("E9 gateway managed", passed=e9.get("passed"), visible=e9.get("visible_names"),
                has_topic_a=e9.get("has_topic_a"), attempts=len(attempts), error=e9.get("error"))
    finally:
        if eng.proc and eng.proc.poll() is None:
            eng.stop()

    # E8 自行管理引擎生命周期（含重启/换密钥）
    try:
        if not want("e8"):
            report["heartbeat_after"] = heartbeat()
            report["cleanup"] = {
                "engine_port_listening": listening(eng.port),
                "db_size": DB.stat().st_size if DB.exists() else None,
                "mcp_procs": "closed by script",
            }
            summary = {k: v.get("passed") for k, v in report["scenarios"].items()}
            report["summary"] = summary
            report["passed"] = all(summary.values()) if summary else False
            out = SCRATCH / f"ownership253-{RUN}.json"
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            evidence("summary", {"summary": summary, "passed": report["passed"],
                                 "heartbeat_before": hb_before, "heartbeat_after": report["heartbeat_after"],
                                 "cleanup": report["cleanup"], "evidence": EVID})
            log("SUMMARY", **summary, passed=report["passed"], report=str(out))
            return 0 if report["passed"] else 4
        eng8 = Engine()
        if not eng8.start():
            report["scenarios"]["E8_credentials"] = {"passed": False, "error": "engine restart failed"}
        else:
            report["scenarios"]["E8_credentials"] = e8_credentials(eng8, threads)
            evidence("e8-credentials", report["scenarios"]["E8_credentials"])
            log("E8 credentials", passed=report["scenarios"]["E8_credentials"]["passed"],
                checks=report["scenarios"]["E8_credentials"]["checks"])
    finally:
        report["heartbeat_after"] = heartbeat()
        report["cleanup"] = {
            "engine_port_listening": listening(eng.port),
            "db_size": DB.stat().st_size if DB.exists() else None,
            "mcp_procs": "closed by script",
        }
    summary = {k: v.get("passed") for k, v in report["scenarios"].items()}
    report["summary"] = summary
    report["passed"] = all(summary.values()) if summary else False
    out = SCRATCH / f"ownership253-{RUN}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    evidence("summary", {"summary": summary, "passed": report["passed"],
                         "heartbeat_before": hb_before, "heartbeat_after": report["heartbeat_after"],
                         "cleanup": report["cleanup"], "evidence": EVID})
    log("SUMMARY", **summary, passed=report["passed"], report=str(out))
    return 0 if report["passed"] else 4


if __name__ == "__main__":
    sys.exit(main())
