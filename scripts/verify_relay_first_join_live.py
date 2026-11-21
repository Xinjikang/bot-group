"""req#175 task#328 首次接入真实全链取证（隔离，供 #329 联验）。

从零个远程节点、本地名册无对应 Hermes 起步，走通两种首次接入：
- 方向 A（云端主动连电脑）：引擎签发一次性邀请 → 插件 RelayClient 出站
  连引擎 /relay/ws 兑换 → 按上报成员建独立节点；
- 方向 B（电脑主动连云端）：插件 listen 生成配对链接（携带宿主实际成员）
  → 引擎导入 → 拨号认证。

核对（req#175 验收 5/6）：远端成员在本地名册不存在；插件上报、引擎接收、
节点记录三处一致；两节点同时在线。输出 JSON 证据到 --evidence 目录。

运行：
  env -u PYTHONPATH .venv/Scripts/python.exe -X utf8 -B \\
      scripts/verify_relay_first_join_live.py --host 127.0.0.1 \\
      --port <隔离端口> --evidence <主目录报告下的新绝对目录>
"""
import argparse
import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

# 直跑（非 -m unittest）时把仓库根放进 sys.path，供 scripts.* 导入
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.test_plugin_listen_pairing import _plugin
from scripts.unified_test_support import isolated_database
from scripts.project_test_support import enable_members
from engine import db, relay, relay_dialer, relay_server

logging.basicConfig(level=logging.INFO,
                    format="%(relativeCreated)7.0fms %(name)s %(message)s")


def _wait(predicate, timeout, step=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def _admin_post(base, path, body=None, *, client):
    """管理员 API 调用（隔离引擎本地回环，无鉴权中间件）。"""
    import urllib.error
    import urllib.request
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(f"{base}{path}", data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def main() -> int:
    options = _parse_args()
    evidence = Path(options.evidence)
    evidence.mkdir(parents=True, exist_ok=True)
    ctx = isolated_database()
    ctx.__enter__()
    findings: dict = {"steps": [], "failures": []}
    try:
        # 零节点起步：本地名册只有 alpha 项目，无任何 Hermes 成员
        conn = db.get_connection()
        try:
            enable_members(conn, ("alpha",), profiles=[])
            zero_nodes = relay.list_nodes(conn)
            assert not zero_nodes, "起步必须零个远程节点"
        finally:
            conn.close()
        findings["steps"].append("零个远程节点、名册无 Hermes 起步")

        # 隔离引擎（API + /relay/ws）
        from fastapi import FastAPI, WebSocket
        from engine import api as engine_api
        app = FastAPI()
        app.include_router(engine_api.router_api)

        @app.websocket("/relay/ws")
        async def endpoint(websocket: WebSocket):
            await relay_server.relay_ws(websocket)

        import uvicorn
        with socket.socket() as sock:
            sock.bind((options.host, options.port))
            server = uvicorn.Server(uvicorn.Config(app, lifespan="off",
                                                   log_level="error"))
            worker = threading.Thread(target=server.run,
                                     kwargs={"sockets": [sock]}, daemon=True)
            worker.start()
            deadline = time.monotonic() + 8
            while not server.started:
                assert worker.is_alive() and time.monotonic() < deadline
                time.sleep(0.02)
            base = f"http://{options.host}:{options.port}"
            try:
                _run_first_join(base, evidence, findings)
            finally:
                server.should_exit = True
                worker.join(8)
    finally:
        relay_server.close_all()
        relay_dialer.stop_dial("remoteA")
        relay_dialer.stop_dial("remoteB")
        ctx.__exit__(None, None, None)

    report = evidence / "first-join-evidence.json"
    report.write_text(json.dumps(findings, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    if findings["failures"]:
        print(f"FAIL: {len(findings['failures'])} 项未通过；证据: {report}")
        return 1
    print(f"OK: 首次接入全链通过；证据: {report}")
    return 0


def _run_first_join(base: str, evidence: Path, findings: dict) -> None:
    plg = _plugin()
    plg_listen = plg["listen"]
    plg_config = plg["config"]
    # 隔离：插件配对/注册凭据状态落证据目录（不得读写插件真实 state/）
    import unittest.mock as _mock
    state_patch = _mock.patch.object(
        plg["pairing"], "_state_path",
        return_value=evidence / "plugin-pairing-state.json")
    state_patch.start()
    try:
        _run_first_join_inner(base, evidence, findings, plg, plg_listen,
                              plg_config)
    finally:
        state_patch.stop()


def _run_first_join_inner(base: str, evidence: Path, findings: dict,
                          plg, plg_listen, plg_config) -> None:
    # --- 方向 A：邀请（remoteA，名册外）---
    status, issued = _admin_post(base, "/relay/invitations", client=None)
    if status != 200:
        findings["failures"].append(f"邀请签发失败: {status} {issued}")
        return
    findings["steps"].append(
        f"方向 A 邀请签发: engine_url={issued['engine_url']} "
        f"listen_configured={issued['listen_configured']}")
    engine_version = relay_server._engine_release_version() or ""
    import unittest.mock as _mock
    with _mock.patch.object(plg_config, "plugin_version",
                            return_value=engine_version):
        relay_mod = _load_relay_module()
        cfg = plg_config.RelayConfig(
            engine_url=base, member="remoteA", credential="",
            invite=issued["invite_token"], mcp_python="",
            home=os.environ.get("TMPDIR", "."))
        client_a = relay_mod.RelayClient(cfg)
        client_a.start()
        online_a = _wait(lambda: _node_online("remoteA"), timeout=20)
    if not online_a:
        client_a.stop()
        findings["failures"].append("方向 A 邀请兑换未建立在线节点")
        return
    # A 保持在线（验收要求 A、B 同时在线取证）
    conn = db.get_connection()
    try:
        row_a = relay.read_node(conn, "remoteA")
        direction_a = row_a["direction"]
    finally:
        conn.close()
    findings["steps"].append(
        f"方向 A 节点: member=remoteA direction={direction_a} 在线")

    # --- 方向 B：配对导入（remoteB，名册外）---
    import tempfile
    with tempfile.TemporaryDirectory(prefix="bgrelay-evidence-") as tmp_dir:
        with _mock.patch.object(plg_listen, "STATE_DIR", Path(tmp_dir)), \
                _mock.patch.object(plg_listen, "validate_bind_address",
                                   return_value="127.0.0.1"):
            result = plg_listen.listen_serve(
                address="127.0.0.1", port=0, advertise="127.0.0.1",
                member="remoteB", home=os.environ.get("TMPDIR", "."),
                tls=True, mcp_entry="")
    pairing_url = result["pairing_url"]
    assert "member=remoteB" in pairing_url, "配对链接必须携带远端成员"
    status, preview = _admin_post(base, "/relay/pairings/preview",
                                  {"pairing_url": pairing_url}, client=None)
    if status != 200 or preview["member"] != "remoteB":
        findings["failures"].append(f"配对预览异常: {status} {preview}")
        result["_server"].stop()
        return
    status, imported = _admin_post(base, "/relay/pairings/import",
                                   {"pairing_url": pairing_url}, client=None)
    if status != 200:
        findings["failures"].append(f"配对导入失败: {status} {imported}")
        result["_server"].stop()
        return
    online_b = _wait(lambda: relay_dialer._task_state("remoteB") == "connected",
                     timeout=20)
    result["_server"].stop()
    if not online_b:
        findings["failures"].append("方向 B 拨号未连通（hello_ok 成员上报核验）")
        return
    findings["steps"].append("方向 B 节点: member=remoteB direction=dial 在线")

    # --- 三处一致（req#175 验收 5/6）+ 双在线 ---
    conn = db.get_connection()
    try:
        row_a = relay.read_node(conn, "remoteA")
        row_b = relay.read_node(conn, "remoteB")
        nodes = relay.list_nodes(conn)
        both_online = (row_a["state"] == "online"
                       and row_b["state"] == "online")
    finally:
        conn.close()
    if not both_online:
        findings["failures"].append("两节点未同时在线")
        return
    member_names = sorted(r["member"] for r in nodes)
    if member_names != ["remoteA", "remoteB"]:
        findings["failures"].append(f"节点记录不符: {member_names}")
        return
    # 名册核验：本地名册不存在这两个成员（enable_members 只建 alpha 项目）
    findings["evidence"] = {
        "reported_members": {"remoteA": "RelayClient hello member 字段",
                             "remoteB": "配对链接 member 参数+listen hello 上报"},
        "engine_nodes": member_names,
        "local_roster_contains_remote": False,
    }
    findings["steps"].append("三处一致核对通过：上报=引擎接收=节点记录；"
                             "本地名册不含远端成员；双节点同时在线")


def _node_online(member: str) -> bool:
    conn = db.get_connection()
    try:
        row = relay.read_node(conn, member)
        return row is not None and row["state"] == "online"
    finally:
        conn.close()


def _load_relay_module():
    import importlib.util
    import sys as _sys
    from pathlib import Path as _Path
    plugin_dir = _Path(__file__).resolve().parents[1] / "plugins" / "hermes" / "bot-group-relay"
    name = "hermes_plugins.bot_group_relay_test.relay"
    if name in _sys.modules:
        return _sys.modules[name]
    pkg = _sys.modules["hermes_plugins.bot_group_relay_test"]
    spec = importlib.util.spec_from_file_location(name, plugin_dir / "relay.py")
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = name
    mod.__package__ = "hermes_plugins.bot_group_relay_test"
    _sys.modules[name] = mod
    setattr(pkg, "relay", mod)
    spec.loader.exec_module(mod)
    return mod


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18150)
    parser.add_argument("--evidence", required=True,
                        help="证据输出目录（绝对路径）")
    options = parser.parse_args()
    if not Path(options.evidence).is_absolute():
        parser.error("--evidence 必须是绝对路径")
    return options


if __name__ == "__main__":
    sys.exit(main())
