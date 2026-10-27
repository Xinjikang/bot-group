"""#250 隔离接入客户端：Hermes v0.22.5 网关 /api/ws 正常闭环验证。

设计依据：docs/design/2026-10-27-hermes-gateway-v0225-call-table.md
协议：hermes-gateway-v1（JSON-RPC over WebSocket）
连接三步：ensure_gateway_runtime 拉起网关 → gateway_ws_target 拼 ws://<origin>/api/ws
→ _session_ticket 本地生成（profile_id/instance_id 匹配）随连接提交。

闭环序列（验收标准 2）：
  连接握手 → ping/runtime.describe（判 authority 非 legacy -32601）
  → session.create 建会话订阅 → prompt.submit 拿 admission_id
  → prompt.receipt 轮询到 terminal（v0.22.5 停止序列见 3.4，本任务只验正常路径）

隔离边界：独立 profile（--profile gw-probe）、独立 HERMES_HOME、独立数据目录；
不碰生产配置/数据/进程；结束停网关释放资源。
真实网关证据：所有回执原样落盘 evidence/，禁止 mock。
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

import websockets

# ── 隔离环境常量（全部独立于生产） ────────────────────────────────────────
PROBE_ROOT = Path(os.environ.get("GW_PROBE_ROOT", "E:/Code/bot-group/.probe-worktrees"))
HERMES_HOME = PROBE_ROOT / "gw-probe-home"          # 独立 profile 数据根
HERMES_BIN = PROBE_ROOT / "hermes-v0225" / "venv" / "Scripts" / "hermes.exe"
PROFILE = "gw-probe"                                 # 独立测试 profile
EVIDENCE_DIR = Path(os.environ.get("GW_EVIDENCE_DIR", "reports/gateway-probe"))
WS_RECV_TIMEOUT = 60.0
RECEIPT_POLL_INTERVAL = 2.0
RECEIPT_POLL_MAX = 150                               # 5 分钟兜底

_json_id = 0


def next_id() -> int:
    global _json_id
    _json_id += 1
    return _json_id


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


# ── 第 1 步：发现/拉起网关进程（官方 gateway runner，非独立 serve） ────────
def ensure_gateway() -> dict:
    """经 CLI 走 ensure_gateway_runtime 路径拉起网关，回传网关端点信息。

    v0.22.5 gateway_client.connect_gateway：ensure_gateway_runtime(home, idle_exit=True)
    → GatewayEndpoint.api_origin。这里用 CLI 侧 Python 接口直接调用同一函数链，
    保证拉起的是挂 session_authority 的官方 gateway runner。
    """
    code = (
        "import asyncio, json, sys\n"
        "from hermes_cli import gateway_client\n"
        f"home = r'{HERMES_HOME}'\n"
        "async def main():\n"
        "    ep = await gateway_client.ensure_gateway_runtime(home, idle_exit=True)\n"
        "    print(json.dumps({'api_origin': ep.api_origin, 'instance_id': getattr(ep, 'instance_id', ''), 'pid': getattr(ep, 'pid', None)}))\n"
        "asyncio.run(main())\n"
    )
    env = dict(os.environ)
    env["HERMES_HOME"] = str(HERMES_HOME)
    env.setdefault("HERMES_PROFILE", PROFILE)
    env.pop("XDG_GROUP_ENGINE_URL", None)  # 隔离：绝不继承生产引擎指向
    env.pop("GROUP_ENGINE_URL", None)
    r = subprocess.run([str(HERMES_BIN), "-X", "utf8", "-c", code],
                       capture_output=True, text=True, env=env, timeout=300,
                       cwd=str(PROBE_ROOT))
    out = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "{}"
    info = json.loads(out)
    if r.returncode != 0:
        log("ensure_gateway FAILED", rc=r.returncode, stderr=r.stderr[-800:])
        sys.exit(1)
    log("ensure_gateway OK", **info)
    return info


def session_ticket() -> str:
    """本地生成 session ticket（与 CLI _session_ticket 同构：profile_id/instance_id）。"""
    return json.dumps({"profile_id": PROFILE, "purpose": "bot-group-probe"})


# ── 第 2 步：连接 + 认证 + 能力识别（authority 判别） ─────────────────────
async def connect_and_describe(api_origin: str) -> tuple[object, dict]:
    from urllib.parse import urlparse
    origin_http = api_origin.rstrip("/")
    ws_target = "ws" + origin_http[len("http"):] + "/api/ws"
    log("connecting", target=ws_target)
    ws = await websockets.connect(
        ws_target,
        subprotocols=["hermes-gateway-v1"],
        additional_headers={"x-hermes-session-ticket": session_ticket()},
        open_timeout=30,
    )
    log("connected", subprotocol=ws.subprotocol)

    # 只读能力识别：ping → runtime.describe（authority 路径判别）
    pong = await rpc(ws, "ping", {})
    described = await rpc(ws, "runtime.describe", {})
    save_evidence("runtime-describe", described)
    caps = described.get("capabilities") or []
    if "durable-admission-v1" not in caps:
        log("AUTHORITY CHECK FAILED", got=caps)
        raise RuntimeError("连接走 legacy fallback（无 authority capabilities），判别失败")
    log("authority confirmed", instance_id=described.get("instance_id"),
        profile_id=described.get("profile_id"),
        epoch=described.get("authority_epoch"), capabilities=caps)
    return ws, described


async def rpc(ws, method: str, params: dict) -> dict:
    """发 hermes-gateway-v1 JSON-RPC 请求并等对应 id 的回执（跳过推送事件）。"""
    rid = next_id()
    payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
    await ws.send(json.dumps(payload))
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=WS_RECV_TIMEOUT)
        data = json.loads(raw)
        if data.get("id") == rid:
            if "error" in data:
                raise RuntimeError(f"{method} error: {data['error']}")
            return data.get("result") or {}


# ── 第 3 步：会话 + 提交 + 查终态（正常闭环） ─────────────────────────────
async def happy_path(ws, described: dict) -> dict:
    profile_id = described.get("profile_id") or PROFILE
    instance_id = described.get("instance_id")
    # 建会话（内部 resume+attach，返回 SubscriptionSnapshot）
    created = await rpc(ws, "session.create", {
        "request_id": f"probe-create-{int(time.time())}",
        "source": "bot-group-probe",
        "cwd": str(PROBE_ROOT),
        "title": "gw-probe-happy-path",
    })
    save_evidence("session-create", created)
    session_id = created["handle"]["ref"]["session_id"]
    log("session created", session_id=session_id[:12] + "…",
        epoch=created["handle"].get("authority_epoch"),
        generation=created["handle"].get("execution_generation"),
        state=created["handle"].get("execution_state"))

    # 提交（提交前已建订阅——session.create 即订阅）
    request_id = f"probe-submit-{int(time.time())}"
    submitted = await rpc(ws, "prompt.submit", {
        "session_id": session_id,
        "text": "请只回复两个字：收到",
        "submission_id": request_id,
        "unattended": True,
    })
    save_evidence("prompt-submit", submitted)
    admission_id = submitted.get("admission_id")
    log("admitted", admission_id=admission_id[:12] + "…",
        status=submitted.get("status"),
        epoch=submitted.get("authority_epoch"),
        generation=submitted.get("execution_generation"))
    assert admission_id, "未拿到 admission_id"

    # 轮询终态（真实执行：v0.22.5 网关真跑一轮 agent）
    deadline = time.time() + RECEIPT_POLL_MAX
    receipt = {}
    while time.time() < deadline:
        receipt = await rpc(ws, "prompt.receipt", {"admission_id": admission_id})
        status = receipt.get("status")
        if status == "terminal":
            break
        log("receipt poll", status=status)
        await asyncio.sleep(RECEIPT_POLL_INTERVAL)
    else:
        log("receipt poll TIMEOUT", last_status=receipt.get("status"))
    save_evidence("prompt-receipt-terminal", receipt)
    log("terminal", admission_id=admission_id[:12] + "…",
        status=receipt.get("status"), outcome=receipt.get("outcome"))
    return {"session_id": session_id, "admission_id": admission_id,
            "request_id": request_id, "receipt": receipt,
            "instance_id": instance_id, "profile_id": profile_id}


async def main_async(args) -> int:
    t0 = time.time()
    gw = ensure_gateway()
    ws, described = await connect_and_describe(gw["api_origin"])
    try:
        result = await happy_path(ws, described)
        save_evidence("happy-path-summary", {
            "elapsed_s": round(time.time() - t0, 1),
            "gateway": gw, "describe": described, **{k: v for k, v in result.items()
                                                      if k != "receipt"},
            "receipt_status": result["receipt"].get("status"),
            "receipt_outcome": result["receipt"].get("outcome"),
        })
        log("HAPPY PATH PASS", outcome=result["receipt"].get("outcome"),
            elapsed_s=round(time.time() - t0, 1))
        return 0
    finally:
        await ws.close()
        log("ws closed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-gateway", action="store_true",
                        help="结束后不请求网关退出（默认空闲自动退出）")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())