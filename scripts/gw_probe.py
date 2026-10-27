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
脚本**自动清理本次实例**：正常完成与可捕获异常都在 finally 里执行 gateway stop，并落
`stop-after` 证据（pid 存活 / `gateway.pid` / 端口 LISTENING 三方核验）；清理失败以退出码 9
体现。启停统一走 pm 环境 `python -m hermes_cli.main`，不依赖任何 launcher。
真实网关证据：所有回执原样落盘 evidence 目录，禁止 mock。

隔离门禁（任一不满足即失败，不得假绿）：
  preflight  子进程环境白名单、config 顶层 mcp_servers:{}（YAML 解析，拒重复键）、先停残留网关
  create/resume 后  info 必须为 dict 且显式含 mcp_servers，值为 list 且空
  运行前后     mcp-stderr 的 /heartbeat 计数必须可信且增量为 0（空 home 按 0 基线）

退出码：
  0=terminal/completed 且全部门禁通过
  2=authority 检查失败（非 gateway runner 路径）
  3=轮询超时未达终态
  4=terminal 但 outcome 非 completed
  5=隔离未知或失守（info/mcp_servers 缺失、类型异常或非空）
  6=preflight 拒绝启动（环境/配置/残留网关）
  7=session.resume 返回的 session_id 与 create 不一致
  8=心跳门禁失败（mcp-stderr 不可读/不一致或 /heartbeat 有新增）
  9=清理失败（网关资源未释放）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
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


class ConfigError(ValueError):
    """探针 home config.yaml 不满足隔离要求。"""


def _strict_yaml_load(text: str) -> dict:
    """解析 config.yaml 并拒绝重复键（ruamel 优先，退化 PyYAML 严格 loader）。

    重复键是绕过配置门禁的常见手法：逐行扫描时只看第一条 `mcp_servers:`，
    第二条非空仍能带着注入跑起来；解析后只有最后一条生效，必须显式拒绝。
    """
    try:
        from ruamel.yaml import YAML
        from ruamel.yaml.constructor import DuplicateKeyError
    except ImportError:
        import yaml as _pyyaml

        class _StrictLoader(_pyyaml.SafeLoader):
            pass

        def _no_dups(loader, node, deep=False):
            mapping = {}
            for key_node, value_node in node.value:
                key = loader.construct_object(key_node, deep=deep)
                if key in mapping:
                    raise ConfigError(f"duplicate key: {key!r}")
                mapping[key] = loader.construct_object(value_node, deep=deep)
            return mapping

        _StrictLoader.add_constructor(
            _pyyaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dups)
        try:
            data = _pyyaml.load(text, Loader=_StrictLoader)
        except ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - YAML 语法错误一律视为配置非法
            raise ConfigError(f"invalid yaml: {type(exc).__name__}: {exc}") from exc
    else:
        yaml = YAML(typ="safe")
        yaml.allow_duplicate_keys = False
        try:
            data = yaml.load(text)
        except DuplicateKeyError as exc:
            raise ConfigError(f"duplicate key: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(f"invalid yaml: {type(exc).__name__}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"top-level not a mapping (type={type(data).__name__})")
    return data


def config_mcp_issue() -> str:
    """preflight：探针 home 必须显式声明顶层 `mcp_servers: {}`（空映射）。

    按 YAML 语义判定而不是逐行匹配：重复键、null、[]、块条目、非空映射一律拒绝。
    返回问题描述（空串=通过）。
    """
    cfg = HERMES_HOME / "config.yaml"
    if not cfg.exists():
        return f"config.yaml missing at {cfg}"
    text = cfg.read_text(encoding="utf-8", errors="replace")
    try:
        data = _strict_yaml_load(text)
    except ConfigError as exc:
        return f"config invalid: {exc}"
    if "mcp_servers" not in data:
        return "mcp_servers key missing (probe home must declare 'mcp_servers: {}')"
    servers = data["mcp_servers"]
    if not isinstance(servers, dict):
        return f"mcp_servers must be top-level empty mapping '{{}}', got: {servers!r}"
    if servers:
        return f"mcp_servers must be empty, got: {servers!r}"
    return ""
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

    三态：`exists=False`（空 home，从没有 MCP 日志，等价 0 基线）；
    `exists=True, readable=False`（存在但读不了，属**未知**）；
    `readable=True`（可读，计数可信）。
    """
    p = HERMES_HOME / "logs" / "mcp-stderr.log"
    if not p.exists():
        return {"exists": False, "readable": False, "heartbeat_count": 0,
                "last_heartbeat": "", "baseline": "no-mcp-log",
                "error": "mcp-stderr.log missing"}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"exists": True, "readable": False, "heartbeat_count": 0,
                "last_heartbeat": "", "baseline": "unreadable",
                "error": f"{type(exc).__name__}: {exc}"}
    beats = [ln.strip() for ln in text.splitlines() if "/heartbeat" in ln]
    return {"exists": True, "readable": True, "heartbeat_count": len(beats),
            "last_heartbeat": beats[-1] if beats else "", "baseline": "readable"}


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

    统一走 pm 环境 venv 的 `python -m hermes_cli.main`（architect 8373 拍板口径）：
    不依赖 `.hermes/bin` / `<home>/bin` 的 launcher——launcher 内嵌生成时的绝对路径，
    home 迁移或重装后可能失效且静默吞错。同时避免 attach 旧实例。
    """
    if not PROBE_PYTHON.exists():
        return {"cli": f"{PROBE_PYTHON} -m hermes_cli.main", "cli_exists": False,
                "rc": None, "stopped": False,
                "output": "probe venv python missing under HERMES_HOME/installs"}
    record: dict = {"cli": f"{PROBE_PYTHON.name} -m hermes_cli.main", "cli_exists": True}
    try:
        r = subprocess.run([str(PROBE_PYTHON), "-m", "hermes_cli.main", "gateway", "stop"],
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
    # stop 之后等状态收敛：ensure 读到 draining/starting 过渡态会误报未就绪
    settled = wait_gateway_settled()
    save_evidence("preflight-gateway-settled", settled)
    log("preflight gateway settled", state=settled["state"], settled=settled["settled"],
        waited_s=settled["waited_s"])
    pid_file = HERMES_HOME / "gateway.pid"
    if pid_file.exists():
        log("PREFLIGHT FAILED: gateway.pid still present after stop", path=str(pid_file))
        sys.exit(6)

    baseline = mcp_log_snapshot()
    if baseline.get("exists") and not baseline["readable"]:
        log("PREFLIGHT FAILED: mcp-stderr unreadable, cannot gate heartbeats",
            error=baseline.get("error"))
        sys.exit(8)
    log("preflight ok", config_mcp="explicit {}",
        heartbeat_baseline=baseline["heartbeat_count"],
        baseline=baseline.get("baseline"),
        last_heartbeat=baseline["last_heartbeat"])
    return {"env": snap, "stop": stop_rec, "settled": settled,
            "heartbeat_baseline": baseline}


def wait_gateway_settled(timeout: float = 20.0) -> dict:
    """stop 之后等 `gateway_state.json` 收敛，避免 ensure 读到 draining/starting 过渡态。"""
    p = HERMES_HOME / "gateway_state.json"
    t0 = time.monotonic()
    last: str | None = None
    while True:
        if not p.exists():
            return {"exists": False, "state": "absent", "settled": True,
                    "waited_s": round(time.monotonic() - t0, 1)}
        try:
            last = (json.loads(p.read_text(encoding="utf-8", errors="replace")) or {}).get(
                "gateway_state")
        except (json.JSONDecodeError, OSError):
            last = None
        if last in (None, "stopped"):
            return {"exists": True, "state": last, "settled": True,
                    "waited_s": round(time.monotonic() - t0, 1)}
        if time.monotonic() - t0 >= timeout:
            return {"exists": True, "state": last, "settled": False,
                    "waited_s": round(time.monotonic() - t0, 1)}
        time.sleep(0.5)


def _ensure_once() -> dict:
    """跑一次官方连接三步（子进程），返回 (info, stderr)；不判成败。"""
    r = subprocess.run([str(PROBE_PYTHON), "-X", "utf8", "-c", CONNECT_CODE,
                        str(HERMES_HOME), str(ENSURE_GATEWAY_TIMEOUT)],
                       capture_output=True, text=True, env=probe_env(), timeout=600,
                       cwd=str(WORKTREE_SRC))
    out = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "{}"
    try:
        info = json.loads(out)
    except json.JSONDecodeError:
        info = {"ok": False, "state": "unparsable", "detail": out[:300]}
    return {"info": info, "stderr": (r.stderr or "")[-500:]}


def connect_gateway_info(max_attempts: int = 3, delay: float = 5.0) -> dict:
    """在 v0.22.5 依赖环境（pm store python）里跑官方连接三步，回传 ws url + subprotocols。

    ensure 可能读到网关自身的过渡态（draining/starting，例如上一轮 stop 后马上重跑），
    此时既不是拒绝也不是就绪；重试有限次数后仍不 ready 才判失败，避免把过渡态当故障。
    """
    last: dict = {}
    for attempt in range(1, max_attempts + 1):
        res = _ensure_once()
        info = res["info"]
        if info.get("ok"):
            log("ensure_gateway OK", api_origin=info.get("api_origin"),
                instance_id=str(info.get("instance_id", ""))[:12] + "…", attempt=attempt)
            return info
        last = res
        log("ensure_gateway not ready", attempt=attempt, state=info.get("state"),
            reason=info.get("reason"), detail=str(info.get("detail", ""))[:200])
        if attempt < max_attempts:
            time.sleep(delay)
    info = last.get("info") or {}
    log("ensure_gateway FAILED", state=info.get("state"), reason=info.get("reason"),
        detail=str(info.get("detail", ""))[:300], stderr=last.get("stderr", ""))
    pid_file = HERMES_HOME / "gateway.pid"
    if pid_file.exists():
        log("cleanup required: leftover gateway", pid_file=str(pid_file),
            hint="run gateway stop with probe HERMES_HOME/HERMES_RUNTIME_DIR "
                 "(pm venv python -m hermes_cli.main gateway stop)")
    sys.exit(1)


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
    """心跳门禁：证据可信且增量为 0 才算通过。

    空 home（两侧都没有 mcp-stderr）不是「未知」——没有 MCP 日志就是没有 MCP，
    按 0 基线放行并标 `no-mcp-log`（否则清理后首次运行永远起不来）。
    只有「存在但读不了」或「一侧有一侧无」才是未知，按 rc=8 处理。
    rc 已非 0 时保留原码（不掩盖根因），但判定仍记录为失败。
    """
    b_exists = bool(before.get("exists"))
    a_exists = bool(after.get("exists"))
    if not b_exists and not a_exists:
        return rc, "no-mcp-log", 0
    if b_exists != a_exists:
        return (8 if rc == 0 else rc), "unknown", None
    if not (before.get("readable") and after.get("readable")):
        return (8 if rc == 0 else rc), "unknown", None
    delta = after["heartbeat_count"] - before["heartbeat_count"]
    if delta != 0:
        return (8 if rc == 0 else rc), "violation", delta
    return rc, "pass", delta


def _pid_alive(pid: int) -> bool | None:
    """进程是否存活；查询本身失败返回 None（未知，不当作已退出）。"""
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return str(pid) in (r.stdout or "")


def _port_listening(port: int) -> bool | None:
    """端口是否有 LISTENING；netstat 查询失败返回 None。"""
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    needle = f":{port} "
    return any(needle in ln and "LISTENING" in ln
               for ln in (r.stdout or "").splitlines())


def _origin_port(origin: str) -> int | None:
    """从 api_origin 取端口：兼容 `host:port` 与 `scheme://host:port`（urlparse 会把裸 host 当 scheme）。"""
    if not origin:
        return None
    text = origin.split("//", 1)[-1] if "//" in origin else origin
    host_port = text.split("/", 1)[0].strip()
    if ":" not in host_port:
        return None
    tail = host_port.rsplit(":", 1)[1]
    return int(tail) if tail.isdigit() else None


def stop_instance(info: dict, pid: int | None) -> dict:
    """停掉本轮拉起的网关并核验释放（stop-after 证据：端口 / pid / gateway.pid）。

    验收要求：正常完成与可捕获异常均清理，清理失败要体现在退出状态与报告里。
    三方核验（pid 存活 / gateway.pid / 端口 LISTENING）任一不干净即判未清理成功。
    """
    origin = str(info.get("api_origin") or "")
    port = _origin_port(origin)
    rec: dict = {"api_origin": origin or None, "port": port, "pid": pid}
    rec["stop"] = stop_existing_gateway()
    rec["pid_alive_after"] = _pid_alive(pid) if pid else None
    rec["gateway_pid_exists"] = (HERMES_HOME / "gateway.pid").exists()
    rec["port_listening"] = _port_listening(port) if port else None
    rec["clean"] = bool(rec["pid_alive_after"] is False
                        and rec["gateway_pid_exists"] is False
                        and (rec["port_listening"] is False or port is None))
    return rec


def _parse_pid_start_time(value) -> datetime | None:
    """解析 gateway.pid 的 start_time。

    Windows 上该值是 psutil `create_time()` 量化后的**厘秒**（见 gateway/status.py
    `_get_process_start_time`：epoch 秒 × 100），Linux 是 `/proc/<pid>/stat` 的时钟滴答数
    （跨平台不可比，仅同源比较）。这里按数量级识别秒/厘秒/毫秒/微秒/FILETIME，
    识别不了就返回 None，不猜。
    """
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num <= 0:
        return None
    try:
        if num > 1e16:      # FILETIME：100ns since 1601-01-01
            return datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=num / 10)
        if num > 1e14:      # 微秒 epoch
            return datetime.fromtimestamp(num / 1e6, tz=timezone.utc)
        if num > 1e12:      # 毫秒 epoch
            return datetime.fromtimestamp(num / 1e3, tz=timezone.utc)
        if num > 1e10:      # 厘秒 epoch（Windows gateway.pid 实测单位）
            return datetime.fromtimestamp(num / 100, tz=timezone.utc)
        return datetime.fromtimestamp(num, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_win32_creation(value: str) -> datetime | None:
    """解析 wmic CreationDate `20261027214403.396228+480`（本地时间 + 时区偏移，单位分钟）。"""
    if not value:
        return None
    m = re.match(r"^(\d{14})\.(\d+)([+-]\d{3})$", value.strip())
    if not m:
        return None
    stamp, frac, tzmin = m.groups()
    try:
        dt = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(
            microsecond=int(frac.ljust(6, "0")[:6]))
        sign = 1 if tzmin[0] == "+" else -1
        return dt.replace(tzinfo=timezone(timedelta(minutes=sign * int(tzmin[1:]))))
    except ValueError:
        return None


def alignment_verdict(pid_meta: dict, win32_output: str, probe_home: Path) -> dict:
    """实例归属三方对齐判定（PID 不单独作准）。

    三方 = gateway.pid 记录（pid/hermes_home/argv/start_time）+ Win32_Process
    （CommandLine/CreationDate）+ 探针 home 路径。只采集不作判定等于没核验，
    这里显式给出每项比较结果与总判定（aligned / mismatched / partial）。
    """
    win_cmd = ""
    win_created = None
    for line in (win32_output or "").splitlines():
        if line.startswith("CommandLine="):
            win_cmd = line.split("=", 1)[1].strip()
        elif line.startswith("CreationDate="):
            win_created = _parse_win32_creation(line.split("=", 1)[1])

    pid_start = _parse_pid_start_time(pid_meta.get("start_time"))
    delta = None
    if pid_start and win_created:
        delta = round(abs((win_created - pid_start).total_seconds()), 3)

    home_ok = None
    home_raw = pid_meta.get("hermes_home")
    if home_raw:
        try:
            home_ok = (str(Path(str(home_raw)).resolve()).lower()
                       == str(Path(probe_home).resolve()).lower())
        except OSError:
            home_ok = False

    argv = pid_meta.get("argv")
    argv_text = " ".join(argv) if isinstance(argv, list) else str(argv or "")
    argv_norm = argv_text.replace("\\", "/")
    entry = "main.py" if "hermes_cli/main.py" in argv_norm else (
        "hermes_cli.main" if "hermes_cli.main" in argv_text else "")
    win_entry = "hermes_cli.main" if "-m hermes_cli.main" in win_cmd else (
        "main.py" if "hermes_cli/main.py" in win_cmd.replace("\\", "/") else "")
    # 两种入口写法指向同一 CLI：入口语义等价即可，字符串不同不算冲突
    entry_equivalent = bool(entry and win_entry
                            and (entry == win_entry
                                 or {entry, win_entry} == {"main.py", "hermes_cli.main"}))

    checks = {
        "start_time_aligned": (delta is not None and delta <= 5.0),
        "hermes_home_matches": home_ok,
        "argv_entry_equivalent": entry_equivalent if (entry and win_entry) else None,
    }
    if all(v is True for v in checks.values()):
        verdict = "aligned"
    elif any(v is False for v in checks.values()):
        verdict = "mismatched"
    else:
        verdict = "partial"
    return {
        "pid_file_pid": pid_meta.get("pid"),
        "pid_start_time_parsed": pid_start.isoformat() if pid_start else None,
        "win32_creation_parsed": win_created.isoformat() if win_created else None,
        "start_time_delta_s": delta,
        "pid_file_hermes_home": home_raw,
        "win32_commandline": win_cmd[:400],
        "argv_entry": entry,
        "win32_entry": win_entry,
        "argv_note": "gateway.pid 记录 CLI 入口脚本，Win32 CommandLine 记录解释器+模块入口；"
                     "按入口语义比较，字符串差异本身不构成冲突",
        "checks": checks,
        "verdict": verdict,
    }


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
                               ("pid", "kind", "hermes_home", "api_origin", "started_at",
                                "start_time", "gateway_state", "exit_reason", "argv")}
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
    # 采集之外必须判定：三方逐项比较 + 总判定，避免「只采集即宣称一致」
    rec["alignment"] = alignment_verdict(
        rec.get("pid_file") or {},
        (rec.get("win32_process") or {}).get("output") or "",
        HERMES_HOME)
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
    info: dict = {}
    pid = None
    rc = 0
    try:
        info = connect_gateway_info()
        forensics = instance_forensics(info)
        save_evidence("instance-forensics", forensics)
        pid = (forensics.get("pid_file") or {}).get("pid")
        log("instance forensics", pid=pid,
            win32=bool(forensics.get("win32_process", {}).get("output")),
            log_lines=forensics.get("gateway_log", {}).get("total_lines"),
            alignment=forensics.get("alignment", {}).get("verdict"),
            start_delta_s=forensics.get("alignment", {}).get("start_time_delta_s"))
        if forensics.get("alignment", {}).get("verdict") == "mismatched":
            log("INSTANCE MISMATCH: gateway.pid / Win32 / probe home 不一致",
                checks=forensics["alignment"].get("checks"))
        rc = asyncio.run(run(info, args.prompt))
    except SystemExit as exc:
        # 内层 sys.exit（如 ensure 失败）不直接终止：先走 finally 清理可能半启动的实例
        rc = rc or int(exc.code or 1)
    except Exception as exc:  # noqa: BLE001 - 可捕获异常也必须清理
        log("PROBE EXCEPTION", error=f"{type(exc).__name__}: {exc}")
        rc = rc or 1
    finally:
        # 心跳门禁：证据可信且本次运行前后 /heartbeat 计数差为 0。
        # 空 home（无 mcp-stderr）按 0 基线放行并标 no-mcp-log；存在但读不了才算未知。
        beats_after = mcp_log_snapshot()
        rc, verdict, delta = heartbeat_gate(rc, beats_before, beats_after)
        save_evidence("heartbeat-diff", {
            "before": beats_before, "after": beats_after,
            "baseline": beats_after.get("baseline"),
            "delta": delta, "gate": verdict})
        log("heartbeat check", before=beats_before["heartbeat_count"],
            after=beats_after["heartbeat_count"], delta=delta, gate=verdict)
        if verdict == "no-mcp-log":
            log("no MCP log (empty home): 0 baseline, heartbeat gate skipped")
        elif verdict == "unknown":
            log("ISOLATION UNKNOWN (heartbeat): mcp-stderr unreadable/inconsistent")
        elif verdict == "violation":
            log("ISOLATION VIOLATION (heartbeat): /heartbeat increased", delta=delta)

        # 资源清理：正常完成与可捕获异常都停掉本次实例，并留 stop-after 证据。
        # 清理失败单独体现（rc=9），不能把未释放的资源算成成功。
        try:
            cleanup = stop_instance(info, pid)
        except Exception as exc:  # noqa: BLE001 - 清理自身的异常同样要如实反映
            cleanup = {"clean": False, "error": f"{type(exc).__name__}: {exc}"}
        save_evidence("stop-after", cleanup)
        log("stop-after", clean=cleanup.get("clean"), port=cleanup.get("port"),
            pid_alive_after=cleanup.get("pid_alive_after"),
            gateway_pid_exists=cleanup.get("gateway_pid_exists"),
            port_listening=cleanup.get("port_listening"))
        if not cleanup.get("clean"):
            log("CLEANUP FAILED: gateway resources not released", detail=cleanup)
            if rc == 0:
                rc = 9
    log("PROBE DONE", rc=rc, elapsed_s=round(time.time() - t0, 1))
    return rc


if __name__ == "__main__":
    sys.exit(main())