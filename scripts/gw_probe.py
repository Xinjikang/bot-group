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

隔离边界：独立 HERMES_HOME（gw-probe-home）、独立安装 worktree、分配资源；
不碰生产配置/数据/进程；结束网关空闲自动退出。
真实网关证据：所有回执原样落盘 evidence 目录，禁止 mock。
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
PROBE_ROOT = Path("E:/Code/bot-group/.probe-worktrees")
HERMES_HOME = PROBE_ROOT / "gw-probe-home"          # 独立 profile 数据根（绝不指向生产）
HERMES_EXE = PROBE_ROOT / "hermes-v0225" / ".hermes" / "bin" / "hermes.exe"
# 依赖环境：hermes pm 装在 HERMES_HOME/installs/<install>/environments/<env>/venv
# （pm repair 产物，ruamel/fastapi/websockets 全在此解释器）；PYTHONPATH 指向 worktree 源码根。
import glob as _glob  # noqa: E402
_env_venvs = _glob.glob(str(HERMES_HOME / "installs" / "*" / "environments" / "*"
                            / "venv" / "Scripts" / "python.exe"))
assert _env_venvs, "probe dependency venv python.exe not found under gw-probe-home/installs"
PROBE_PYTHON = Path(_env_venvs[0])
WORKTREE_SRC = PROBE_ROOT / "hermes-v0225"
EVIDENCE_DIR = Path(os.environ.get("GW_EVIDENCE_DIR",
                                   "E:/Code/bot-group/reports/gateway-probe"))
RECEIPT_POLL_INTERVAL = 3.0
RECEIPT_POLL_MAX = 900                              # 15 分钟兜底（真实 agent 一轮可能慢）

_json_id = 0


def log(step: str, **kv) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {step}"
    if kv:
        line += " " + json.dumps(kv, ensure_ascii=False)
    print(line, flush=True)


def save_evidence(name: str, payload) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    p = EVIDENCE_DIR / f"{time.strftime('%H%M%S')}-{name}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ── 连接三步：官方接口链（在 v0.22.5 安装的 Python 里跑） ──────────────────
CONNECT_CODE = r"""
import asyncio, json, sys, os
# 隔离：绝不继承生产引擎指向
for k in ("XDG_GROUP_ENGINE_URL", "GROUP_ENGINE_URL"):
    os.environ.pop(k, None)
os.environ["HERMES_HOME"] = sys.argv[1]

from hermes_cli import gateway_client
from hermes_cli.gateway_runtime import attachable, ensure_gateway_runtime, gateway_profile_home
from hermes_constants import get_hermes_home

async def main():
    home = gateway_profile_home(get_hermes_home())
    result = await asyncio.to_thread(ensure_gateway_runtime, home, idle_exit=True)
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


def connect_gateway_info() -> dict:
    """在 v0.22.5 依赖环境（pm store python）里跑官方连接三步，回传 ws url + subprotocols。"""
    env = dict(os.environ)
    env["HERMES_HOME"] = str(HERMES_HOME)
    env["HERMES_RUNTIME_DIR"] = str(PROBE_ROOT / "hermes-pm-store")
    env["PYTHONPATH"] = str(WORKTREE_SRC)
    for k in ("XDG_GROUP_ENGINE_URL", "GROUP_ENGINE_URL"):
        env.pop(k, None)
    r = subprocess.run([str(PROBE_PYTHON), "-X", "utf8", "-c", CONNECT_CODE, str(HERMES_HOME)],
                       capture_output=True, text=True, env=env, timeout=600,
                       cwd=str(WORKTREE_SRC))
    out = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "{}"
    info = json.loads(out)
    if not info.get("ok"):
        log("ensure_gateway FAILED", stderr=r.stderr[-500:])
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

        # 建会话（内部 resume+attach，返回 SubscriptionSnapshot）
        created = await rpc(ws, "session.create", {
            "request_id": f"probe-create-{int(time.time())}",
            "source": "tool",  # v0.22.5 SURFACES 白名单：cli/tui/gui/acp/tool/oneshot
            "cwd": str(PROBE_ROOT),
        })
        save_evidence("session-create", created)
        # v0.22.5 实际响应为扁平 SubscriptionSnapshot：session_id 在顶层
        #（call-table 描述的 handle.ref 包装在实测响应中不存在——以运行证据为准）
        session_id = created.get("session_id")
        log("session created", session_id=str(session_id)[:20] + "…",
            epoch=created.get("authority_epoch"),
            replay_epoch=created.get("replay_epoch"),
            generation=created.get("execution_generation"),
            revision=created.get("revision"))
        assert session_id, "未拿到 session_id"

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
        log("terminal", admission_id=admission_id[:12] + "…",
            status=final.get("status"), outcome=final.get("outcome"),
            result=str(result)[:120])
        return 0 if receipt.get("status") == "terminal" else 3
    finally:
        await ws.close()
        log("ws closed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", default="请只回复两个字：收到")
    args = parser.parse_args()
    t0 = time.time()
    info = connect_gateway_info()
    rc = asyncio.run(run(info, args.prompt))
    log("PROBE DONE", rc=rc, elapsed_s=round(time.time() - t0, 1))
    return rc


if __name__ == "__main__":
    sys.exit(main())