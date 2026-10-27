"""#250 隔离接入客户端：Hermes v0.22.5 网关 /api/ws 正常闭环验证。

设计依据：docs/design/2026-10-27-hermes-gateway-v0225-call-table.md
协议：hermes-gateway-v1（JSON-RPC over WebSocket）

连接三步（全部走 v0.22.5 官方接口链，不自己拼 ticket）：
  ensure_gateway_runtime(home, idle_exit=True) 拉起官方 gateway runner
  → _session_ticket 向网关 control socket 请求授权（含 profile_id/instance_id）
  → gateway_ws_target 拼 ws://<origin>/api/ws（ticket 走 subprotocol，非 header）

闭环序列（验收标准 2）：
  连接握手 → ping/runtime.describe（判 authority 非 legacy -32601）
  → session.create 建会话订阅 → prompt.submit 拿 admission_id
  → prompt.receipt 轮询到 terminal

隔离边界：代码在独立 worktree（`.probe-worktrees/hermes-v0225`），
持久测试数据在引擎分配的 test-data 下（`<test-data>/task-250/gw-probe-home` 与
`hermes-pm-store`）；不碰生产配置/数据/进程。网关由 ensure_gateway_runtime 拉起（非独立 serve），
脚本**不自动停止**网关：结束时须显式执行 gateway stop 子命令（本 home 的
`bin/hermes.exe`，由 setup 在该 home 重新安装发布；HERMES_HOME/HERMES_RUNTIME_DIR
指向探针目录），并核对目标端口无 LISTENING、gateway.pid 不存在。
真实网关证据：所有回执原样落盘 evidence 目录，禁止 mock。

隔离门禁（任一不满足即失败，不得假绿）：
  preflight  子进程环境白名单、config 显式 mcp_servers:{}、先停残留网关（强制 fresh）
  create/resume 后  info 必须为 dict 且显式含 mcp_servers，值为 list 且空
  运行前后     mcp-stderr 的 /heartbeat 计数必须可读且增量为 0

退出码：
  0=terminal/completed 且全部门禁通过
  2=authority 检查失败（非 gateway runner 路径）
  3=轮询超时未达终态
  4=terminal 但 outcome 非 completed
  5=隔离未知或失守（info/mcp_servers 缺失、类型异常或非空）
  6=preflight 拒绝启动（环境/配置/残留网关）
  7=session.resume 返回的 session_id 与 create 不一致
  8=心跳门禁失败（mcp-stderr 不可读或 /heartbeat 有新增）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ── 隔离环境常量 ───────────────────────────────────────────────────────────
# 代码/源码隔离：git worktree + uv 包，可用 GW_PROBE_ROOT 覆盖；
# 默认取仓库根下的 .probe-worktrees（脚本位于 <repo>/scripts/）。
PROBE_ROOT = Path(os.environ.get("GW_PROBE_ROOT")
                  or Path(__file__).resolve().parents[1] / ".probe-worktrees")
# 持久测试数据根：HERMES_HOME 属测试持久数据，必须落在引擎分配的 test-data 下
# （不用 .probe-worktrees 代替）；GW_PROBE_DATA 可覆盖以在其他工作区复跑。
PROBE_DATA = Path(os.environ.get("GW_PROBE_DATA")
                  or r"E:\Code\.bot-group-resources\dcc5f7fd43510c8f"
                     r"\ws_6043478a1e82665dc05e9604\test-data")
HERMES_HOME = PROBE_DATA / "task-250" / "gw-probe-home"      # 独立 profile 数据根（绝不指向生产）
HERMES_RUNTIME = PROBE_DATA / "task-250" / "hermes-pm-store"  # pm 依赖存储
HERMES_CLI = HERMES_HOME / "bin" / "hermes.exe"  # setup 在本 home 发布的官方入口
# 依赖环境：hermes pm 装在 HERMES_HOME/installs/<install>/environments/<env>/venv
# （pm repair 产物，ruamel/fastapi/websockets 全在此解释器）；PYTHONPATH 指向 worktree 源码根。
import glob as _glob  # noqa: E402
_env_venvs = sorted(_glob.glob(str(HERMES_HOME / "installs" / "*" / "environments" / "*"
                                   / "venv" / "Scripts" / "python.exe")),
                    key=lambda p: Path(p).stat().st_mtime)
assert _env_venvs, "probe dependency venv python.exe not found under gw-probe-home/installs"
PROBE_PYTHON = Path(_env_venvs[-1])                  # 按 mtime 取最近安装的环境（非名称排序）
WORKTREE_SRC = PROBE_ROOT / "hermes-v0225"
EVIDENCE_DIR = Path(os.environ.get(
    "GW_EVIDENCE_DIR",
    Path(__file__).resolve().parents[1] / "reports" / "research" / "hermes-gateway"))
RECEIPT_POLL_INTERVAL = 3.0
RECEIPT_POLL_MAX = 900                              # 15 分钟兜底（真实 agent 一轮可能慢）
ENSURE_GATEWAY_TIMEOUT = 120.0                      # 冷启动实测 >60s（reviewer 21:04 遗留端口事件）

_json_id = 0
_evidence_seq = 0


def log(step: str, **kv) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {step}"
    if kv:
        line += " " + json.dumps(kv, ensure_ascii=False)
    print(line, flush=True)


def save_evidence(name: str, payload) -> Path:
    """落盘原始回执。文件名带序号，避免同秒同步骤相互覆盖。"""
    global _evidence_seq
    _evidence_seq += 1
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    p = EVIDENCE_DIR / f"{time.strftime('%H%M%S')}-{_evidence_seq:02d}-{name}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ── 连接三步：官方接口链（在 v0.22.5 安装的 Python 里跑） ──────────────────
CONNECT_CODE = r"""
import asyncio, json, sys, os
# 隔离：绝不继承生产引擎指向、managed scope 或执行凭据。
# managed scope 经 HERMES_MANAGED_DIR 覆盖层注入 mcp_servers（含 GROUP_ENGINE_URL=生产 8180
# 与成员凭据），v0.22.5 会在网关启动时合并并拉起 MCP server 周期心跳——必须一并清除。
for k in ("XDG_GROUP_ENGINE_URL", "GROUP_ENGINE_URL", "XDG_GROUP_ENGINE_EXECUTION",
          "HERMES_MANAGED_DIR", "HERMES_CHAT_MODE", "TASK_QUEUE_ROLE"):
    os.environ.pop(k, None)
os.environ["HERMES_HOME"] = sys.argv[1]
ENSURE_TIMEOUT = float(sys.argv[2]) if len(sys.argv) > 2 else 120.0

from hermes_cli import gateway_client
from hermes_cli.gateway_runtime import attachable, ensure_gateway_runtime, gateway_profile_home
from hermes_constants import get_hermes_home

async def main():
    home = gateway_profile_home(get_hermes_home())
    result = await asyncio.to_thread(ensure_gateway_runtime, home,
                                     timeout=ENSURE_TIMEOUT, idle_exit=True)
    if not attachable(result):
        print(json.dumps({"ok": False, "state": result.state,
                         "reason": result.reason_code, "detail": getattr(result, "detail", "")}))
        return
    endpoint = result.endpoint
    ticket = await asyncio.to_thread(
        gateway_client._session_ticket, home, endpoint, purpose="interactive")
    url, protocols = gateway_client.gateway_ws_target(endpoint, ticket)
    print(json.dumps({"ok": True, "api_origin": endpoint.api_origin,
                      "profile_id": endpoint.profile_id,
                      "instance_id": endpoint.instance_id,
                      "ws_url": url, "protocols": protocols}))

asyncio.run(main())
"""


# 子进程环境白名单：只放行 Windows/Python 运行必需的基础变量。
# 绝不继承 HERMES_*、GROUP_ENGINE*、XDG_GROUP_ENGINE*、TASK_QUEUE*（managed scope 会经
# HERMES_MANAGED_DIR 注入 mcp_servers，含生产 URL 与成员凭据）；模型凭据由隔离 home 的 .env 提供。
_ENV_ALLOW = {
    "PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "SystemRoot", "WINDIR",
    "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMW6432", "PROGRAMDATA",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "OS",
    "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONHOME", "PYTHONPATH",
}

# 显式排除清单（安全审查要求；白名单已从机制上排除，此处用于自检与审计留痕）。
_FORBIDDEN_ENV = (
    "HERMES_MANAGED_DIR", "HERMES_PROFILE", "HERMES_CHAT_MODE",
    "GROUP_ENGINE_URL", "GROUP_ENGINE_HOST", "GROUP_ENGINE_PORT", "GROUP_ENGINE_TOKEN",
    "XDG_GROUP_ENGINE_URL", "XDG_GROUP_ENGINE_TOKEN", "XDG_GROUP_ENGINE_EXECUTION",
    "TASK_QUEUE_ROLE",
)
_SENSITIVE_PREFIX = ("HERMES_", "GROUP_ENGINE", "XDG_GROUP_ENGINE", "TASK_QUEUE")


def probe_env() -> dict:
    """构造探针子进程环境：白名单基础变量 + 隔离 home/runtime，其他一律不继承。"""
    env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOW}
    env["HERMES_HOME"] = str(HERMES_HOME)
    env["HERMES_RUNTIME_DIR"] = str(HERMES_RUNTIME)
    env["PYTHONPATH"] = str(WORKTREE_SRC)
    for k in _FORBIDDEN_ENV:
        env.pop(k, None)
    return env


def env_snapshot() -> dict:
    """脱敏环境快照：只记父进程/子进程环境里敏感变量键名，不落值（URL 与凭据不外泄）。"""
    parent = sorted(k for k in os.environ if k.startswith(_SENSITIVE_PREFIX))
    child = sorted(k for k in probe_env() if k.startswith(_SENSITIVE_PREFIX))
    return {
        "parent_sensitive_keys": parent,
        "child_sensitive_keys": child,
        "child_isolation_ok": all(k in ("HERMES_HOME", "HERMES_RUNTIME_DIR") for k in child),
        "forbidden_in_child": [k for k in _FORBIDDEN_ENV if k in probe_env()],
    }


def config_mcp_issue() -> str:
    """探针 home 配置校验：必须显式写 `mcp_servers: {}`（空映射）。

    缺失、null、[]、非空内联值、缩进块条目一律不通过——「键不存在」等于配置失控权
    仍在外部（managed scope / profile 覆盖层可注入），不能当作已关闭 MCP。
    返回问题描述（空串=通过）。
    """
    cfg = HERMES_HOME / "config.yaml"
    if not cfg.exists():
        return f"config.yaml missing at {cfg}"
    lines = cfg.read_text(encoding="utf-8", errors="replace").splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        if not s.startswith("mcp_servers:"):
            continue
        rest = s.split(":", 1)[1].split("#", 1)[0].strip()
        if rest.replace(" ", "") != "{}":
            return f"mcp_servers must be exactly '{{}}', got: {rest or '<empty>'}"
        base_indent = len(line) - len(line.lstrip())
        for nxt in lines[i + 1:]:
            if not nxt.strip() or nxt.strip().startswith("#"):
                continue
            indent = len(nxt) - len(nxt.lstrip())
            if indent <= base_indent:
                break
            return f"mcp_servers block entry not allowed: {nxt.strip()}"
        return ""
    return "mcp_servers key missing (probe home must declare 'mcp_servers: {}')"


def check_mcp_empty(snapshot, stage: str) -> str:
    """严格校验会话快照里没有 MCP：必须 info 为 dict、显式含 mcp_servers、值为 list 且空。

    「字段缺失 / None / 类型异常」是**未知**而非「空」——后者会让注入被静默放过，
    所以一律判不通过。返回问题描述（空串=通过）。
    """
    if not isinstance(snapshot, dict):
        return f"{stage}: snapshot not a dict (type={type(snapshot).__name__})"
    if "info" not in snapshot:
        return f"{stage}: 'info' key missing"
    info = snapshot["info"]
    if not isinstance(info, dict):
        return f"{stage}: info not a dict (type={type(info).__name__})"
    if "mcp_servers" not in info:
        return f"{stage}: info.mcp_servers key missing"
    servers = info["mcp_servers"]
    if not isinstance(servers, list):
        return f"{stage}: info.mcp_servers not a list (type={type(servers).__name__})"
    if servers:
        return f"{stage}: info.mcp_servers not empty ({len(servers)} entries)"
    return ""


def mcp_log_snapshot() -> dict:
    """记录探针 home 的 mcp-stderr 日志里 /heartbeat 计数与末条。

    readable=False 表示文件不存在或读取失败——该情形是**未知**，不得当作「0 次心跳」。
    """
    p = HERMES_HOME / "logs" / "mcp-stderr.log"
    if not p.exists():
        return {"exists": False, "readable": False, "heartbeat_count": 0,
                "last_heartbeat": "", "error": "mcp-stderr.log missing"}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"exists": True, "readable": False, "heartbeat_count": 0,
                "last_heartbeat": "", "error": f"{type(exc).__name__}: {exc}"}
    beats = [ln.strip() for ln in text.splitlines() if "/heartbeat" in ln]
    return {"exists": True, "readable": True, "heartbeat_count": len(beats),
            "last_heartbeat": beats[-1] if beats else ""}


_NO_GATEWAY_MARKERS = ("no gateway", "not running", "no running", "gateway not running")


def stop_verdict(rc: int | None, output: str) -> tuple[bool, str]:
    """判定 gateway stop 是否把环境留成干净起点。

    「无网关运行」对 stop 而言就是幂等成功——不能因为 rc 非 0 就当成隔离失败，
    否则干净环境下探针反而起不来。只有真正未知的失败（超时/报错且无此语义）才阻断。
    """
    if rc == 0:
        return True, "rc0"
    low = (output or "").lower()
    if any(marker in low for marker in _NO_GATEWAY_MARKERS):
        return True, "no-gateway"
    return False, "failed"


def stop_existing_gateway() -> dict:
    """强制干净起点：停掉探针 home 下可能残留的网关。

    ensure_gateway_runtime 会 attach 已存在的网关，而旧实例的 env/config 可能与本轮
    preflight 不同（正是第二次隔离事故的成因）。停止后再启动，保证校验对象与被测对象一致。

    经本 home 的官方 CLI（`<HERMES_HOME>/bin/hermes.exe`）执行：home 采用重新安装
    而非移动，CLI 元数据天然绑定当前路径；同时避免 attach 旧实例。
    """
    if not HERMES_CLI.exists():
        return {"cli": str(HERMES_CLI), "cli_exists": False, "rc": None,
                "stopped": False, "output": "hermes CLI missing under HERMES_HOME/bin"}
    record: dict = {"cli": str(HERMES_CLI), "cli_exists": True}
    try:
        r = subprocess.run([str(HERMES_CLI), "gateway", "stop"],
                           capture_output=True, text=True, env=probe_env(),
                           timeout=180, cwd=str(WORKTREE_SRC))
    except subprocess.TimeoutExpired:
        record.update({"rc": None, "stopped": False, "output": "gateway stop timeout(180s)"})
        return record
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    record.update({"rc": r.returncode, "stopped": "Stopped gateway" in out,
                   "output": out[-400:]})
    return record


def preflight() -> dict:
    """启动网关前的隔离校验。任一不通过即终止，绝不启动真实网关。

    校验项：①子进程环境白名单；②探针 home 显式 `mcp_servers: {}`；
    ③停止残留网关（保证被测实例由本轮按白名单环境启动，而非 attach 旧实例）。
    """
    snap = env_snapshot()
    save_evidence("env-snapshot", snap)
    log("preflight env", child_sensitive_keys=snap["child_sensitive_keys"],
        forbidden_in_child=snap["forbidden_in_child"])
    if snap["forbidden_in_child"]:
        log("PREFLIGHT FAILED: forbidden env in child", keys=snap["forbidden_in_child"])
        sys.exit(6)

    issue = config_mcp_issue()
    if issue:
        log("PREFLIGHT FAILED: probe home config", issue=issue)
        sys.exit(6)

    stop_rec = stop_existing_gateway()
    clean, verdict = stop_verdict(stop_rec.get("rc"), stop_rec.get("output", ""))
    stop_rec["verdict"] = verdict
    save_evidence("preflight-gateway-stop", stop_rec)
    log("preflight gateway stop", rc=stop_rec.get("rc"), stopped=stop_rec.get("stopped"),
        verdict=verdict)
    if not clean:
        # 既非成功也非「无网关运行」：不能假定环境干净
        log("PREFLIGHT FAILED: gateway stop failed", output=stop_rec.get("output"))
        sys.exit(6)
    pid_file = HERMES_HOME / "gateway.pid"
    if pid_file.exists():
        log("PREFLIGHT FAILED: gateway.pid still present after stop", path=str(pid_file))
        sys.exit(6)

    baseline = mcp_log_snapshot()
    if not baseline["readable"]:
        log("PREFLIGHT FAILED: mcp-stderr unreadable, cannot gate heartbeats",
            error=baseline.get("error"))
        sys.exit(8)
    log("preflight ok", config_mcp="explicit {}",
        heartbeat_baseline=baseline["heartbeat_count"],
        last_heartbeat=baseline["last_heartbeat"])
    return {"env": snap, "stop": stop_rec, "heartbeat_baseline": baseline}


def connect_gateway_info() -> dict:
    """在 v0.22.5 依赖环境（pm store python）里跑官方连接三步，回传 ws url + subprotocols。"""
    env = probe_env()
    r = subprocess.run([str(PROBE_PYTHON), "-X", "utf8", "-c", CONNECT_CODE,
                        str(HERMES_HOME), str(ENSURE_GATEWAY_TIMEOUT)],
                       capture_output=True, text=True, env=env, timeout=600,
                       cwd=str(WORKTREE_SRC))
    out = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "{}"
    info = json.loads(out)
    if not info.get("ok"):
        log("ensure_gateway FAILED", state=info.get("state"), reason=info.get("reason"),
            detail=str(info.get("detail", ""))[:300], stderr=r.stderr[-500:])
        pid_file = HERMES_HOME / "gateway.pid"
        if pid_file.exists():
            log("cleanup required: leftover gateway", pid_file=str(pid_file),
                hint="run gateway stop with probe HERMES_HOME/HERMES_RUNTIME_DIR "
                     "(CLI: <HERMES_HOME>/bin/hermes.exe)")
        sys.exit(1)
    log("ensure_gateway OK", api_origin=info["api_origin"],
        instance_id=info["instance_id"][:12] + "…")
    return info


# ── WebSocket 客户端（websockets 库，与官方 gateway_client 同参数） ────────
async def rpc(ws, method: str, params: dict) -> dict:
    global _json_id
    _json_id += 1
    rid = _json_id
    payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
    await ws.send(json.dumps(payload))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=120)
        data = json.loads(raw)
        if data.get("id") == rid:
            if "error" in data:
                raise RuntimeError(f"{method} error: {json.dumps(data['error'], ensure_ascii=False)}")
            return data.get("result") or {}


async def run(info: dict, prompt_text: str) -> int:
    import websockets

    url, protocols = info["ws_url"], info["protocols"]
    log("connecting", target=url)
    ws = await websockets.connect(url, subprotocols=protocols, open_timeout=30,
                                  max_size=8 * 1024 * 1024, proxy=None)
    try:
        log("connected", subprotocol=ws.subprotocol)
        # 能力识别：ping → runtime.describe（authority 判别）
        pong = await rpc(ws, "ping", {})
        described = await rpc(ws, "runtime.describe", {})
        save_evidence("runtime-describe", described)
        caps = described.get("capabilities") or []
        if "durable-admission-v1" not in caps:
            log("AUTHORITY CHECK FAILED", got=caps)
            return 2
        log("authority confirmed", instance_id=described.get("instance_id"),
            profile_id=described.get("profile_id"),
            epoch=described.get("authority_epoch"), capabilities=caps)

        # 建会话（内部 resume+attach，返回 CanonicalSnapshot：顶层 session_id，无 handle）
        created = await rpc(ws, "session.create", {
            "request_id": f"probe-create-{int(time.time())}",
            "source": "tool",  # v0.22.5 SURFACES 白名单：cli/tui/gui/acp/tool/oneshot
            "cwd": str(PROBE_ROOT),
        })
        save_evidence("session-create", created)
        session_id = created.get("session_id")
        log("session created", session_id=str(session_id)[:20] + "…",
            epoch=created.get("authority_epoch"),
            replay_epoch=created.get("replay_epoch"),
            generation=created.get("execution_generation"),
            revision=created.get("revision"))
        assert session_id, "未拿到 session_id"

        # 隔离硬断言（隔离失守防线）：会话快照必须显式证明 MCP 为空。
        # info 缺失 / mcp_servers 键缺失 / None / 类型异常都属「未知」，不得视为空——
        # 否则 managed scope 或 profile 注入可被静默放过。任一异常立即中止，不提交 prompt。
        create_issue = check_mcp_empty(created, "create")
        save_evidence("session-mcp-check", {"stage": "create", "issue": create_issue,
                                            "info": created.get("info")})
        if create_issue:
            log("ISOLATION UNKNOWN/VIOLATION (create)", issue=create_issue)
            return 5
        log("isolation confirmed", mcp_servers=[], stage="create")

        # session.resume 独立路径（验收 2：须独立取得 resume 回执，非仅 create 内部 resume+attach）。
        # 先 detach 断开本连接订阅，再 resume 重新挂载。
        subscription_id = created.get("subscription_id")
        await rpc(ws, "session.detach", {
            "session_id": session_id, "subscription_id": subscription_id})
        resumed = await rpc(ws, "session.resume", {"session_id": session_id})
        save_evidence("session-resume", resumed)
        log("session resumed", session_id=str(resumed.get("session_id"))[:20] + "…",
            epoch=resumed.get("authority_epoch"),
            message_count=resumed.get("message_count"),
            subscription_id=str(resumed.get("subscription_id"))[:12] + "…")
        if resumed.get("session_id") != session_id:
            log("RESUME ID MISMATCH", expected=session_id, got=resumed.get("session_id"))
            return 7
        resume_issue = check_mcp_empty(resumed, "resume")
        save_evidence("session-resume-mcp-check", {"stage": "resume", "issue": resume_issue,
                                                   "info": resumed.get("info")})
        if resume_issue:
            log("ISOLATION UNKNOWN/VIOLATION (resume)", issue=resume_issue)
            return 5
        log("isolation confirmed", mcp_servers=[], stage="resume")

        # 提交（session.create 已建订阅）。
        # v0.22.5 实测语义（session_finite.py:47/_may_run_unattended）：unattended=true
        # 必须 finite=true 同传，且仅 cli 平台会话 + 本地 creator 允许——tool 会话
        # 传 unattended 会 invalid_params。正常闭环不需要自动审批，去掉该参数。
        request_id = f"probe-submit-{int(time.time())}"
        submitted = await rpc(ws, "prompt.submit", {
            "session_id": session_id,
            "text": prompt_text,
            "submission_id": request_id,
        })
        save_evidence("prompt-submit", submitted)
        admission_id = submitted.get("admission_id")
        log("admitted", admission_id=str(admission_id)[:12] + "…",
            status=submitted.get("status"),
            epoch=submitted.get("authority_epoch"),
            generation=submitted.get("execution_generation"))
        assert admission_id, "未拿到 admission_id"

        # 轮询终态（真实执行：网关真跑一轮 agent）。
        # receipt 契约 = CanonicalSessionParams：session_id 必填（call-table 3.3 未记录，实测补充）
        deadline = time.time() + RECEIPT_POLL_MAX
        receipt = {}
        while time.time() < deadline:
            receipt = await rpc(ws, "prompt.receipt",
                                {"session_id": session_id, "admission_id": admission_id})
            status = receipt.get("status")
            if status == "terminal":
                break
            log("receipt poll", status=status)
            await asyncio.sleep(RECEIPT_POLL_INTERVAL)
        else:
            log("receipt poll TIMEOUT", last_status=receipt.get("status"))
        save_evidence("prompt-receipt-terminal", receipt)
        # 终态后带 include_result 取回真实执行结果（闭环证据）
        final = await rpc(ws, "prompt.receipt",
                          {"session_id": session_id, "admission_id": admission_id,
                           "include_result": True})
        save_evidence("prompt-receipt-with-result", final)
        result = final.get("result") or ""
        status = final.get("status")
        outcome = final.get("outcome")
        log("terminal", admission_id=admission_id[:12] + "…",
            status=status, outcome=outcome, result=str(result)[:120])
        # 退出码三态（失败不得假绿）：0=终态成功；3=轮询超时未达终态；4=终态失败。
        # 仅 status=terminal 且 outcome=completed 才算成功——terminal/failed
        # （如模型名错误导致的执行失败）返回 4，证据见 202118 outcome=failed。
        if status != "terminal":
            return 3
        if outcome == "completed":
            return 0
        log("PROBE TERMINAL NOT COMPLETED", status=status, outcome=outcome)
        return 4
    finally:
        await ws.close()
        log("ws closed")


def heartbeat_gate(rc: int, before: dict, after: dict) -> tuple[int, str, int | None]:
    """心跳门禁：证据可读且增量为 0 才算通过。

    返回 (退出码, 判定, 增量)。rc 已非 0 时保留原码（不掩盖根因），但判定仍记录为失败。
    """
    readable = bool(before.get("readable")) and bool(after.get("readable"))
    if not readable:
        return (8 if rc == 0 else rc), "unknown", None
    delta = after["heartbeat_count"] - before["heartbeat_count"]
    if delta != 0:
        return (8 if rc == 0 else rc), "violation", delta
    return rc, "pass", delta


def instance_forensics(info: dict) -> dict:
    """实例归属三方对齐（PID 不单独作准）。

    三方 = gateway.pid 记录（argv/hermes_home/start） + Win32_Process 的
    CommandLine/CreationDate + 日志 inbound/stop 时间。任一方缺失即如实留空，
    不推断归属。
    """
    rec: dict = {"instance_id": info.get("instance_id"), "api_origin": info.get("api_origin")}
    pid_file = HERMES_HOME / "gateway.pid"
    pid = None
    if pid_file.exists():
        raw = pid_file.read_text(encoding="utf-8", errors="replace")
        rec["pid_file_raw"] = raw[:800]
        try:
            meta = json.loads(raw)
            pid = meta.get("pid")
            rec["pid_file"] = {k: meta.get(k) for k in
                               ("pid", "hermes_home", "api_origin", "started_at", "argv")}
        except json.JSONDecodeError:
            rec["pid_file_parse"] = "not json"
    if pid:
        try:
            r = subprocess.run(["wmic", "process", "where", f"processid={pid}",
                                "get", "CommandLine,CreationDate", "/format:list"],
                               capture_output=True, text=True, timeout=30)
            rec["win32_process"] = {"pid": pid, "rc": r.returncode,
                                    "output": (r.stdout or "").strip()[:1200]}
        except (OSError, subprocess.SubprocessError) as exc:
            rec["win32_process"] = {"pid": pid, "error": f"{type(exc).__name__}: {exc}"}
    log_file = HERMES_HOME / "logs" / "gateway.log"
    if log_file.exists():
        try:
            lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
            hits = [ln for ln in lines
                    if "gateway" in ln.lower()
                    and any(k in ln.lower() for k in ("start", "stop", "inbound"))]
            rec["gateway_log"] = {"total_lines": len(lines), "recent": hits[-6:]}
        except OSError as exc:
            rec["gateway_log"] = {"error": str(exc)}
    return rec


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", default="请只回复两个字：收到")
    parser.add_argument("--preflight-only", action="store_true",
                        help="只跑启动前隔离校验并退出（门禁负向测试用，不启动网关）")
    args = parser.parse_args()
    t0 = time.time()
    # 启动前隔离校验：环境白名单 + 显式空 MCP 配置 + 停止残留网关；不通过即终止，绝不拉起网关。
    pre = preflight()
    if args.preflight_only:
        log("PREFLIGHT ONLY: all static gates passed")
        return 0
    beats_before = pre["heartbeat_baseline"]
    info = connect_gateway_info()
    forensics = instance_forensics(info)
    save_evidence("instance-forensics", forensics)
    log("instance forensics", pid=(forensics.get("pid_file") or {}).get("pid"),
        win32=bool(forensics.get("win32_process", {}).get("output")),
        log_lines=forensics.get("gateway_log", {}).get("total_lines"))
    rc = asyncio.run(run(info, args.prompt))

    # 心跳门禁：证据必须可读，且本次运行前后 /heartbeat 计数差为 0。
    # 增量非 0 说明探针仍连到了某个心跳端点（隔离失守）；证据不可读则属未知，都不是成功。
    beats_after = mcp_log_snapshot()
    rc, verdict, delta = heartbeat_gate(rc, beats_before, beats_after)
    save_evidence("heartbeat-diff", {
        "before": beats_before, "after": beats_after,
        "readable": verdict != "unknown", "delta": delta, "gate": verdict})
    log("heartbeat check", before=beats_before["heartbeat_count"],
        after=beats_after["heartbeat_count"], delta=delta, gate=verdict)
    if verdict == "unknown":
        log("ISOLATION UNKNOWN (heartbeat): mcp-stderr unreadable")
    elif verdict == "violation":
        log("ISOLATION VIOLATION (heartbeat): /heartbeat increased", delta=delta)
    log("PROBE DONE", rc=rc, elapsed_s=round(time.time() - t0, 1))
    return rc


if __name__ == "__main__":
    sys.exit(main())