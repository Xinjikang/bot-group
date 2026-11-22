"""req#173 task#324 插件 listen/register 与配对链接隔离测试。

覆盖：配对链接单次有效/重生成旧失效/日志脱敏/0600 文件；listen 绑定校验
（禁通配/回环/非本机）；方向 B 真实监听 + 引擎拨号接入（复用 relay_dialer）
+ hello 握手/帧互通；MCP loopback 隧道 http_request→引擎转发→http_response
回传；CLI/私聊入口身份核验拒绝路径。#316 出站链路回归由既有测试承担。
"""
import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from engine import db, ip_allowlist, relay, relay_dialer, relay_server
from scripts.unified_test_support import isolated_database
from scripts.project_test_support import enable_members

# 插件模块经合成包路径导入（目录名带连字符不能直接 import 包）
PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "hermes" / "bot-group-relay"


def _plugin_modules(slot="bot_group_relay_test", plugin_dir=PLUGIN_DIR):
    """两遍法加载插件模块（相对导入与执行顺序解耦）。"""
    import importlib.util
    import sys as _sys
    import types
    pkg_name = f"hermes_plugins.{slot}"
    parent_name = "hermes_plugins"
    parent = _sys.modules.setdefault(parent_name, types.ModuleType(parent_name))
    parent.__path__ = [str(plugin_dir)]
    parent.__package__ = parent_name
    _sys.modules[parent_name] = parent
    # 相对导入要求子包本身可解析：注册包占位（父包属性 + sys.modules）
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(plugin_dir)]
    pkg.__package__ = pkg_name
    _sys.modules[pkg_name] = pkg
    setattr(parent, slot, pkg)
    modules = {}
    for sub in ("pairing", "config", "mcp_digest", "streams", "listen", "mcpproxy",
                "cli", "tools"):
        full = f"{pkg_name}.{sub}"
        spec = importlib.util.spec_from_file_location(full, plugin_dir / f"{sub}.py")
        mod = importlib.util.module_from_spec(spec)
        mod.__name__ = full
        mod.__package__ = pkg_name
        _sys.modules[full] = mod
        setattr(pkg, sub, mod)
        modules[sub] = mod
    for sub, mod in modules.items():
        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.{sub}", plugin_dir / f"{sub}.py")
        spec.loader.exec_module(mod)
    return modules


_PLUGIN = None


def _plugin() -> dict:
    global _PLUGIN
    if _PLUGIN is None:
        _PLUGIN = _plugin_modules()
    return _PLUGIN


def _lan_ipv4() -> str | None:
    from engine import listen_control
    for entry in listen_control.enumerate_interfaces():
        if entry["family"] == "IPv4":
            return entry["address"]
    return None


REMOTE = "remote1"
CREDENTIAL = "test-relay-credential"


class PairingTests(unittest.TestCase):
    def setUp(self):
        plg = _plugin()
        self.pairing = plg["pairing"]
        # 隔离：临时目录放配对状态（TMPDIR 未设时不得落项目根，
        # 测试产物会污染工作树）
        self._tmpdir = tempfile.TemporaryDirectory(prefix="pairing-test-")
        self.addCleanup(self._tmpdir.cleanup)
        self._state_patch = patch.object(
            self.pairing, "_state_path",
            return_value=Path(self._tmpdir.name) /
            f"pairing-test-{threading.get_ident()}.json")
        self._state_patch.start()
        self.addCleanup(self._state_patch.stop)

    def test_issue_redeem_single_use(self):
        issued = self.pairing.issue_pairing(member=REMOTE, advertise="203.0.113.7",
                                            port=8443, fingerprint="ab" * 32)
        self.assertTrue(self.pairing.redeem_pairing(
            member=REMOTE, token=issued["pairing_token"],
            fingerprint=issued["fingerprint"]))
        self.assertFalse(self.pairing.redeem_pairing(
            member=REMOTE, token=issued["pairing_token"]),
            "配对凭据一次性：二次兑换必须失败")

    def test_reissue_invalidates_old_unused(self):
        first = self.pairing.issue_pairing(member=REMOTE, advertise="203.0.113.7",
                                           port=8443, fingerprint="")
        second = self.pairing.issue_pairing(member=REMOTE, advertise="203.0.113.7",
                                            port=8443, fingerprint="")
        self.assertFalse(self.pairing.redeem_pairing(
            member=REMOTE, token=first["pairing_token"]),
            "重新生成后旧未用链接必须失效")
        self.assertTrue(self.pairing.redeem_pairing(
            member=REMOTE, token=second["pairing_token"]))

    def test_expired_rejected(self):
        with patch.object(self.pairing, "PAIRING_TTL_SECONDS", -1):
            issued = self.pairing.issue_pairing(member=REMOTE, advertise="x",
                                                port=1, fingerprint="")
        self.assertFalse(self.pairing.redeem_pairing(
            member=REMOTE, token=issued["pairing_token"]))

    def test_wrong_member_or_fingerprint_rejected(self):
        issued = self.pairing.issue_pairing(member=REMOTE, advertise="203.0.113.7",
                                            port=8443, fingerprint="ab" * 32)
        self.assertFalse(self.pairing.redeem_pairing(
            member="other", token=issued["pairing_token"]))
        self.assertFalse(self.pairing.redeem_pairing(
            member=REMOTE, token=issued["pairing_token"], fingerprint="cd" * 32))

    def test_logs_never_contain_token(self):
        records = []
        handler = logging.Handler()
        handler.emit = lambda record: records.append(record.getMessage())
        logger = logging.getLogger("hermes_plugins.bot_group_relay_test.pairing")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        issued = self.pairing.issue_pairing(member=REMOTE, advertise="203.0.113.7",
                                            port=8443, fingerprint="")
        self.pairing.redeem_pairing(member=REMOTE, token=issued["pairing_token"])
        for message in records:
            self.assertNotIn(issued["pairing_token"], message,
                             "日志绝不记录配对凭据/链接")

    def test_link_file_0600(self):
        target = Path(os.environ.get("TMPDIR", ".")) / \
            f"pairing-link-{threading.get_ident()}.txt"
        self.addCleanup(target.unlink, missing_ok=True)
        written = self.pairing.write_link_file("https://x/?token=secret", path=str(target))
        self.assertEqual(written, str(target))
        if os.name == "posix":
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)


class ListenBindTests(unittest.TestCase):
    def test_validate_rejects_wildcard_loopback_foreign(self):
        plg = _plugin()
        validate = plg["listen"].validate_bind_address
        for bad in ("0.0.0.0", "::", "127.0.0.1", "::1", "198.51.100.7", "not-an-ip"):
            with self.subTest(address=bad):
                with self.assertRaises(ValueError, msg=f"{bad} 必须被拒绝"):
                    validate(bad)
        lan = _lan_ipv4()
        if lan:
            self.assertEqual(validate(lan), lan)


class DirectionBDialTests(unittest.TestCase):
    """方向 B 真实监听 + 引擎拨号接入 + MCP 隧道（隔离：本机 LAN 地址
    或回环地址，不动主引擎 8180）。"""

    def setUp(self):
        self._ctx = isolated_database()
        self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)
        conn = db.get_connection()
        try:
            from scripts.project_test_support import enable_members
            enable_members(conn, ("alpha",), profiles=[REMOTE])
            relay.register_node(conn, REMOTE, CREDENTIAL, display_name="remote")
        finally:
            conn.close()
        self.plg = _plugin()
        # 隔离：插件配对状态落临时目录（TMPDIR 未设时不得落项目根，
        # 测试产物会污染工作树）
        self._tmpdir = tempfile.TemporaryDirectory(prefix="pairing-dial-")
        self.addCleanup(self._tmpdir.cleanup)
        self._state_patch = patch.object(
            self.plg["pairing"], "_state_path",
            return_value=Path(self._tmpdir.name) /
            f"pairing-dial-{threading.get_ident()}.json")
        self._state_patch.start()
        self.addCleanup(self._state_patch.stop)
        self.addCleanup(relay_server.close_all)
        self.addCleanup(relay_dialer.stop_dial, REMOTE)

    def _wait_connected(self, timeout=10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if relay_dialer._task_state(REMOTE) == "connected":
                return True
            time.sleep(0.05)
        return False

    def _api_client(self):
        """按需建 API TestClient（/switch、/mcp-verify、/relay/nodes 用）。"""
        if getattr(self, "_client", None) is None:
            from fastapi import FastAPI, WebSocket
            from fastapi.testclient import TestClient
            from engine import api
            app = FastAPI()
            app.include_router(api.router_api)

            @app.websocket("/relay/ws")
            async def endpoint(websocket: WebSocket):
                await relay_server.relay_ws(websocket)
            self._client = TestClient(app, client=("127.0.0.1", 4321))
            self.addCleanup(self._client.close)
        return self._client

    def _ensure_grant(self):
        """建成员会话授权（hermes_session_auth 同口径；隔离测试 signing
        keys 未初始化——patch 临时签名代，与 manager_stdio.py 同口径）。"""
        from engine import auth, hermes_session_auth
        import secrets as _secrets
        _keys_patch = patch.object(auth, "signing_keys",
                                   return_value=(1, {1: _secrets.token_bytes(32)}))
        _keys_patch.start()
        self.addCleanup(_keys_patch.stop)
        conn = db.get_connection()
        try:
            bot = __import__("engine.db", fromlist=["db"])._ensure_hermes_bot(conn, REMOTE)
            conn.execute("""INSERT INTO session_bindings(project,bot_id,profile,
                kind,thread_id,session_id,runtime,generation,title,updated_at)
                VALUES('alpha',?,?,'work',0,'isolated-session','hermes',0,
                'fixture',?)""", (bot, REMOTE, __import__("engine.db", fromlist=["db"]).now_iso()))
            hermes_session_auth.grant(
                conn, project="alpha", profile=REMOTE, bot_id=bot, kind="work",
                thread_id=None, session="fixture", home=".", command=["x"],
                workdir=".", model_args=[])
            conn.commit()
        finally:
            conn.close()

    # ---------------- manager node2102：同方向重拨 + MCP 实际验证 ----------------

    def _listen_serve(self, port: int = 0, tmp_key: str = "retry", tmp_dir=None,
                      mcp_entry=None, mcp_python=None):
        """起真实插件监听（隔离证书/配对状态落 TMPDIR），返回 result。
        重启场景复用同一 tmp_dir：证书一致则指纹不变，免重新配对。
        mcp_entry/mcp_python 供 node2106 随包工具调用全链用例指定。"""
        plg_listen = self.plg["listen"]
        if tmp_dir is None:
            tmp_dir = Path(tempfile.mkdtemp(prefix=f"bgrelay-{tmp_key}-"))
            self.addCleanup(lambda: __import__("shutil").rmtree(tmp_dir, ignore_errors=True))
        with patch.object(plg_listen, "STATE_DIR", tmp_dir), \
                patch.object(plg_listen, "validate_bind_address",
                             return_value="127.0.0.1"):
            self._serve_tmpdir = tmp_dir
            return plg_listen.listen_serve(
                address="127.0.0.1", port=port, advertise="127.0.0.1",
                member=REMOTE, home=os.environ.get("TMPDIR", "."),
                mcp_python=mcp_python or "", tls=True,
                mcp_entry=mcp_entry or "")

    def _pair_and_dial(self, result):
        """把真实插件监听写入引擎拨号端点并拨入，返回 connected 是否达成。"""
        from urllib.parse import parse_qs, urlsplit
        pairing_token = parse_qs(urlsplit(result["pairing_url"]).query)[
            "pairing_token"][0]
        conn = db.get_connection()
        try:
            relay.set_dial_endpoint(conn, REMOTE,
                                    {"host": "127.0.0.1", "port": result["port"],
                                     "tls": True,
                                     "fingerprint": result["fingerprint"],
                                     "pairing_token": pairing_token})
            relay.set_direction(conn, REMOTE, "dial")
        finally:
            conn.close()
        relay_dialer.start_dial(REMOTE)
        return self._wait_connected()

    def test_same_direction_retry_after_abandon_actually_dials(self):
        """manager node2102 ②：B 曾在线→意外掉线→退避耗尽 abandoned→点
        同方向重试必须实际发起拨号（恢复连接），不得静默 0 请求。"""
        result = self._listen_serve()
        self.assertTrue(self._pair_and_dial(result), "首次拨入必须连通")
        port = result["port"]
        # 意外掉线：停掉插件监听；退避上限压到 1 快速耗尽
        result["_server"].stop()
        with patch.object(relay_dialer, "MAX_ATTEMPTS", 1):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and \
                    relay_dialer._task_state(REMOTE) != "abandoned":
                time.sleep(0.05)
        self.assertEqual(relay_dialer._task_state(REMOTE), "abandoned",
                         "连续失败耗尽退避后进入 abandoned")
        # 插件同端口重启（同一证书目录：指纹不变；持久身份仍在，免重配对）
        result2 = self._listen_serve(port=port, tmp_key="retry2",
                                     tmp_dir=self._serve_tmpdir)
        self.addCleanup(result2["_server"].stop)
        # 同方向重试：/switch 目标=dial=当前方向，必须实际启动拨号
        response = self._api_client().post(f"/relay/nodes/{REMOTE}/switch",
                                    json={"direction": "dial"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self._wait_connected(15),
                        "同方向离线重试必须实际发起请求并恢复连接")

    def test_same_direction_switch_online_is_idempotent(self):
        """manager node2102 ②：在线重复点击同方向维持幂等——不打断通道、
        不重复拨号。"""
        result = self._listen_serve()
        self.assertTrue(self._pair_and_dial(result))
        link = relay_server.active_link(REMOTE)
        response = self._api_client().post(f"/relay/nodes/{REMOTE}/switch",
                                    json={"direction": "dial"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["switch_state"], "")
        self.assertEqual(response.json()["dial_state"], "connected")
        self.assertIs(relay_server.active_link(REMOTE), link,
                      "在线幂等：同一通道不被替换")

    def test_dial_link_request_mcp_verify_roundtrip(self):
        """manager node2102 ①：引擎→插件 mcp_verify_request 帧（带成员
        受限凭据与项目查询）→ mcp_verify_response 回传等待线程。"""
        fake_ws = _SyncWs()
        link = relay_dialer.DialLink(REMOTE, fake_ws, 1, "203.0.113.9",
                                     db_generation=2)
        holder = {}

        def ask():
            holder["report"] = link.request_mcp_verify("session-key-1", "alpha")

        thread = threading.Thread(target=ask)
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not fake_ws.sent:
            time.sleep(0.02)
        self.assertTrue(fake_ws.sent, "必须发出 mcp_verify_request 帧")
        frame = json.loads(fake_ws.sent[-1])
        self.assertEqual(frame["relay"]["kind"], "mcp_verify_request")
        self.assertEqual(frame["x_group_key"], "session-key-1",
                         "成员受限凭据经已认证通道下发")
        self.assertIn("project=alpha", frame["query"])
        self.assertEqual(frame["path"], "/members")
        # 插件回传状态 → 等待线程拿到
        link._on_verify_response(frame["relay"]["request_id"],
                                 {"status": 200, "body": '{"members": []}'})
        thread.join(timeout=5)
        # 回执带发起时本地状态代（node2106：写库按原代条件，防旧连接点亮新代）
        self.assertEqual(holder["report"]["status"], 200)
        self.assertEqual(holder["report"]["body"], '{"members": []}')
        self.assertEqual(holder["report"]["generation"], 2)

    def test_mcp_verify_endpoint_flow_and_generation_invalidation(self):
        """manager node2102 ①：/mcp-verify 端点——方向 B + 成员会话授权
        签发受限凭据；成功/权限拒绝落库（ok/failed+脱敏原因）；无授权
        拒绝；方向 A 400；连接换代后旧结果作废。"""
        from engine import auth, hermes_session_auth
        from scripts.project_test_support import enable_members
        conn = db.get_connection()
        try:
            relay.set_direction(conn, REMOTE, "dial")
            # 方向 A 拒绝
        finally:
            conn.close()
        conn = db.get_connection()
        try:
            relay.set_direction(conn, REMOTE, "listen")
        finally:
            conn.close()
        response = self._api_client().post(f"/relay/nodes/{REMOTE}/mcp-verify")
        self.assertEqual(response.status_code, 400, "方向 A 不适用隧道验证")
        conn = db.get_connection()
        try:
            relay.set_direction(conn, REMOTE, "dial")
        finally:
            conn.close()
        # 无会话授权 → failed（不签发管理凭据）
        response = self._api_client().post(f"/relay/nodes/{REMOTE}/mcp-verify")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mcp_verify_state"], "failed")
        self.assertIn("会话授权", response.json()["mcp_verify_error"])
        # 建成员会话授权（hermes_session_auth 同口径）
        self._ensure_grant()
        seen = {}
        link_stub = type("LinkStub", (), {
            "member": REMOTE,
            "request_mcp_verify": lambda self, key, project, execution=None: (
                seen.update(key=key, project=project, execution=execution)
                or {"status": 200,
                    "body": '{"members":[{"profile":"remote1"}]}'}),
        })()
        with patch.object(relay_server, "active_link", return_value=link_stub):
            response = self._api_client().post(f"/relay/nodes/{REMOTE}/mcp-verify")
        body = response.json()
        self.assertEqual(body["mcp_verify_state"], "ok")
        self.assertEqual(seen["project"], "alpha", "验证用成员的会话项目")
        self.assertEqual(seen["execution"].get("credential_type"), "session",
                         "执行范围随帧下发（受限会话）")
        # 受限凭据：verify_bot 通过且带会话范围
        self.assertIsNotNone(auth.session_scope(REMOTE, seen["key"],
                                                auth.signing_keys()[1]))
        # 权限拒绝：403 → failed + 脱敏原因
        link_403 = type("LinkStub", (), {
            "request_mcp_verify": lambda self, key, project, execution=None:
                {"status": 403,
                 "body": '{"error": "engine 403: Session project mismatch"}'},
        })()
        with patch.object(relay_server, "active_link", return_value=link_403):
            response = self._api_client().post(f"/relay/nodes/{REMOTE}/mcp-verify")
        body = response.json()
        self.assertEqual(body["mcp_verify_state"], "failed")
        self.assertIn("权限拒绝(403)", body["mcp_verify_error"])
        self.assertIn("Session project mismatch", body["mcp_verify_error"])
        # nodes 视图含 verify 字段
        nodes = self._api_client().get("/relay/nodes").json()
        node = next(n for n in nodes if n["member"] == REMOTE)
        self.assertEqual(node["mcp_verify_state"], "failed")
        self.assertNotIn("secret", (node["mcp_verify_error"] or "").lower())
        # 换代作废：新连接代 +1 后旧验证结果清除（待验）
        conn = db.get_connection()
        try:
            relay.generation_advance(conn, REMOTE)
        finally:
            conn.close()
        nodes = self._api_client().get("/relay/nodes").json()
        node = next(n for n in nodes if n["member"] == REMOTE)
        self.assertEqual(node["mcp_verify_state"], "",
                         "换代后旧验证结果作废（待验）")

    def test_mcp_verify_full_chain_through_proxy_tunnel(self):
        """manager node2102 ① 全链：真实 listen（含代理）→ 引擎拨入 →
        POST /mcp-verify → 引擎发帧 → 插件本机经 loopback 代理走真实
        隧道回引擎（假引擎 API 鉴权响应）→ 状态回传落库。"""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        result = self._listen_serve(
            mcp_entry=str(Path(__file__).resolve().parents[1] / "mcp" / "server.py"),
            mcp_python=sys.executable)
        self.assertTrue(self._pair_and_dial(result))
        self.assertTrue(result["proxy_port"] > 0, "listen 必启代理")
        self._ensure_grant()

        class EngineAPI(BaseHTTPRequestHandler):
            status = 200

            def log_message(self, *a):  # noqa: A003
                pass

            def do_GET(self):  # noqa: N802
                body = json.dumps({"members": [{"profile": REMOTE}],
                                   "count": 1}).encode()
                self.send_response(self.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        api_server = ThreadingHTTPServer(("127.0.0.1", 0), EngineAPI)
        threading.Thread(target=api_server.serve_forever, daemon=True).start()
        self.addCleanup(api_server.shutdown)
        config_mod = __import__("engine.config", fromlist=["config"])
        with patch.object(config_mod, "host", return_value="127.0.0.1"), \
                patch.object(config_mod, "port",
                             return_value=api_server.server_address[1]):
            response = self._api_client().post(
                f"/relay/nodes/{REMOTE}/mcp-verify")
        self.assertEqual(response.json()["mcp_verify_state"], "ok",
                         "真实代理隧道只读验证成功落库 ok")
        # 权限拒绝变体：假 API 回 403 → failed + 脱敏原因
        EngineAPI.status = 403
        with patch.object(config_mod, "host", return_value="127.0.0.1"), \
                patch.object(config_mod, "port",
                             return_value=api_server.server_address[1]):
            response = self._api_client().post(
                f"/relay/nodes/{REMOTE}/mcp-verify")
        body = response.json()
        self.assertEqual(body["mcp_verify_state"], "failed")
        self.assertIn("403", body["mcp_verify_error"])
        # 连接换代：旧验证结果作废
        conn = db.get_connection()
        try:
            relay.generation_advance(conn, REMOTE)
        finally:
            conn.close()
        nodes = self._api_client().get("/relay/nodes").json()
        node = next(n for n in nodes if n["member"] == REMOTE)
        self.assertEqual(node["mcp_verify_state"], "")

    def test_mcp_verify_entry_unavailable_failed(self):
        """manager node2106：随包 MCP 运行入口不可用时验证失败并如实回传
        （不得伪成功，也不得残留旧结果）。"""
        from engine import api as engine_api
        result = self._listen_serve(mcp_entry=str(
            Path(__file__).resolve().parents[1] / "mcp" / "no-such-entry.py"),
            mcp_python=sys.executable)
        self.assertTrue(self._pair_and_dial(result))
        self._ensure_grant()
        # 真实链路：帧到插件，插件如实回传入口不可用
        reply = engine_api.relay_node_mcp_verify(REMOTE, ("fixture", "user"))
        self.assertEqual(reply["mcp_verify_state"], "failed")
        self.assertIn("entry unavailable", reply["mcp_verify_error"])
        # 失败不残留旧成功：换代后仍待验
        conn = db.get_connection()
        try:
            relay.generation_advance(conn, REMOTE)
        finally:
            conn.close()
        nodes = self._api_client().get("/relay/nodes").json()
        node = next(n for n in nodes if n["member"] == REMOTE)
        self.assertEqual(node["mcp_verify_state"], "")

    def test_mcp_verify_stale_reply_does_not_light_new_generation(self):
        """manager node2106 反例：旧连接发起验证途中换代，迟到回执按
        原代条件写不命中——新连接保持待验，不得被旧成功点亮。"""
        from engine import api as engine_api
        conn = db.get_connection()
        try:
            relay.set_direction(conn, REMOTE, "dial")
            relay.generation_advance(conn, REMOTE)  # 换代作废：当前行待验
        finally:
            conn.close()
        self._ensure_grant()

        class DelayedReply:
            db_generation = 1

            def request_mcp_verify(self, key, project, execution=None):
                conn = db.get_connection()
                try:
                    advanced = relay.generation_advance(conn, REMOTE)
                finally:
                    conn.close()
                assert advanced >= 1
                return {"status": 200,
                        "body": '{"members":[{"profile":"%s"}]}' % REMOTE,
                        "generation": 1}

        with patch.object(relay_server, "active_link",
                          return_value=DelayedReply()):
            reply = engine_api.relay_node_mcp_verify(REMOTE, ("fixture", "user"))
        # 端点返回落库后的真实状态：旧代条件写不命中 → 仍待验
        conn = db.get_connection()
        try:
            node = relay.read_node(conn, REMOTE)
        finally:
            conn.close()
        self.assertEqual(reply["mcp_verify_state"], "",
                         "API 返回与库一致：旧回执未点亮新代")
        self.assertEqual(node["mcp_verify_state"], "",
                         "旧连接迟到回执不得点亮新代（结果仍待验）")

    def test_group_send_through_tunnel_lands_once(self):
        """manager 12774（话题10150）：方向 B 随包 group_send 经隧道 POST
        /msg 合法转发——点名→同一 thread_id 回复→消息落库一次；心跳与
        管理路径保持拒绝（边界照旧）。"""
        import asyncio
        import socket
        import uvicorn
        from fastapi import FastAPI
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from engine import api as engine_api, auth, hermes_session_auth
        result = self._listen_serve(
            mcp_entry=str(Path(__file__).resolve().parents[1] / "mcp" / "server.py"),
            mcp_python=sys.executable)
        self.assertTrue(self._pair_and_dial(result))
        self.assertTrue(result["proxy_port"] > 0, "listen 必启代理")
        # chat 会话授权：thread 9001 存在 + 成员会话授权（thread_id=9001）
        import secrets as _secrets
        _keys_patch = patch.object(auth, "signing_keys",
                                   return_value=(1, {1: _secrets.token_bytes(32)}))
        _keys_patch.start()
        self.addCleanup(_keys_patch.stop)
        conn = db.get_connection()
        try:
            now = __import__("engine.db", fromlist=["db"]).now_iso()
            conn.execute("""INSERT INTO threads (project,title,creator,members,
                status,round,turn_profile,created_at,last_active_at)
                VALUES ('alpha','fixture thread',?,'["%s"]','open',0,'%s',?,?)"""
                         % (REMOTE, REMOTE), (REMOTE, now, now))
            thread_id = conn.execute(
                "SELECT id FROM threads WHERE title='fixture thread'").fetchone()["id"]
            bot = __import__("engine.db", fromlist=["db"])._ensure_hermes_bot(conn, REMOTE)
            conn.execute("""INSERT INTO session_bindings(project,bot_id,profile,
                kind,thread_id,session_id,runtime,generation,title,updated_at)
                VALUES('alpha',?,?,'work',?,'isolated-session','hermes',0,
                'fixture',?)""", (bot, REMOTE, thread_id, now))
            ctx = hermes_session_auth.context(hermes_session_auth.grant(
                conn, project="alpha", profile=REMOTE, bot_id=bot, kind="work",
                thread_id=thread_id, session="fixture", home=".",
                command=["x"], workdir=".", model_args=[]))
            # 该会话的活跃执行行（hermes_executions state 非 terminal/非 manual、
            # 无 cancel_reason；身份与会话一致）——message_execution.validate
            # 的「可写活跃执行」依赖；delegation cycle 无行即 owned。
            started_at = time.time()
            conn.execute("""INSERT INTO hermes_executions (
                execution_id, session, started_at, project, profile, kind,
                thread_id, session_id, profile_id, submission_id, state, source,
                python, home, workdir, submit_deadline, reconcile_deadline)
                VALUES ('exec-fixture','fixture',?,?,'%s','work',?,
                'isolated-session','%s','sub-fixture','running','fixture',
                'py','.', '.',?,?)"""
                         % (REMOTE, REMOTE),
                         (started_at, "alpha", thread_id,
                          started_at + 60, started_at + 120))
            # wake_runs 同 started_at（message_execution.validate 查它判活跃）
            conn.execute("""INSERT INTO wake_runs (session, profile, thread_id,
                runtime_session_id, bot_id, runtime, started_at, project)
                VALUES ('fixture','%s',?,'isolated-session',?,'hermes',?,'alpha')"""
                         % REMOTE, (thread_id, bot, started_at))
            conn.commit()
        finally:
            conn.close()
        key = auth.session_token(REMOTE, ctx)
        # 真实隔离引擎 API（uvicorn 回环端口；隧道转发目标=引擎自身）。
        # sock 不能进 with（退出即 close，uvicorn 还在用会 ConnectionRefused）
        app = FastAPI()
        app.include_router(engine_api.router_api)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.addCleanup(sock.close)
        api_port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off",
                                               log_level="error"))
        worker = threading.Thread(target=server.run,
                                  kwargs={"sockets": [sock]}, daemon=True)
        worker.start()

        def _stop_server():
            server.should_exit = True
            worker.join(8)

        self.addCleanup(_stop_server)
        deadline = time.monotonic() + 8
        while not server.started:
            self.assertTrue(worker.is_alive() and time.monotonic() < deadline)
            time.sleep(0.02)
        # 随包 stdio 进程实际调用 group_send 工具（env 白名单）
        entry = str(Path(__file__).resolve().parents[1] / "mcp" / "server.py")
        env = {
            "PATH": os.environ.get("PATH", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", "") or "C:\\Windows",
            "TEMP": os.environ.get("TEMP", ""), "TMP": os.environ.get("TMP", ""),
            "PYTHONUTF8": "1",
            "GROUP_ENGINE_URL": f"http://127.0.0.1:{result['proxy_port']}",
            "XDG_GROUP_ENGINE_URL": "",
            "GROUP_ENGINE_TOKEN": key,
            "XDG_GROUP_ENGINE_EXECUTION": json.dumps(
                dict(ctx, credential_type="session"), ensure_ascii=False),
            "NO_PROXY": "127.0.0.1",
            "HERMES_PROFILE": REMOTE,
        }
        server_params = StdioServerParameters(
            command=sys.executable, args=["-B", entry],
            cwd=str(Path(entry).resolve().parent), env=env)

        async def send_via_tool():
            async with stdio_client(server_params) as (read, write), \
                    ClientSession(read, write) as client:
                await client.initialize()
                return await client.call_tool(
                    "group_send", {"text": "@user 隔离隧道回复",
                                   "need_reply": False})

        config_mod = __import__("engine.config", fromlist=["config"])
        with patch.object(config_mod, "host", return_value="127.0.0.1"), \
                patch.object(config_mod, "port", return_value=api_port):
            reply = asyncio.run(asyncio.wait_for(send_via_tool(), timeout=60))
        self.assertFalse(reply.isError, "工具调用不得报错")
        payload = json.loads(reply.content[0].text)
        self.assertIn("message_id", payload, "group_send 必须取得 message_id")
        # 落库一次：同一 thread_id、同 sender 仅一条
        conn = db.get_connection()
        try:
            rows = conn.execute(
                "SELECT id, thread_id, sender, content FROM messages "
                "WHERE thread_id=? AND sender=?", (thread_id, REMOTE)).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1, "同一回复只落库一次")
        self.assertEqual(rows[0]["content"], "@user 隔离隧道回复")
        # 边界照旧：/admin 管理路径与心跳仍被白名单拒绝
        fake_ws = _SyncWs()
        link = relay_dialer.DialLink(REMOTE, fake_ws, 1, "203.0.113.9",
                                     db_generation=2)
        from engine import relay_server as rs_mod
        with patch.object(rs_mod, "active_link", return_value=link):
            pass  # 白名单常量断言（无需真实往返）
        self.assertNotIn("/admin", rs_mod._HTTP_TUNNEL_PREFIXES)
        self.assertFalse(any("/admin".startswith(p)
                             for p in rs_mod._HTTP_TUNNEL_PREFIXES))
        self.assertFalse(any("/heartbeat".startswith(p)
                             for p in rs_mod._HTTP_TUNNEL_PREFIXES),
                         "心跳不属发送依赖，保持拒绝")
        # manager 12781：写端点精确匹配 method+path，startswith 不得扩大
        self.assertTrue(rs_mod._tunnel_allowed("POST", "/msg"))
        self.assertFalse(rs_mod._tunnel_allowed("GET", "/msg"),
                         "GET /msg 不得放行")
        self.assertFalse(rs_mod._tunnel_allowed("POST", "/msgx"),
                         "POST /msgx 不得放行（前缀扩大误放）")
        # manager 12796：回执查询仅 GET + 单个非空操作号
        self.assertTrue(rs_mod._tunnel_allowed("GET", "/mcp-operations/abc"))
        self.assertFalse(rs_mod._tunnel_allowed("GET", "/mcp-operations/"),
                         "空操作号拒绝")
        self.assertFalse(rs_mod._tunnel_allowed("GET", "/mcp-operations/a/b"),
                         "多段路径拒绝")
        self.assertFalse(rs_mod._tunnel_allowed("POST", "/mcp-operations/abc"),
                         "回执查询只读，POST 拒绝")

    def test_engine_dials_plugin_listen_and_frames(self):
        plg_listen = self.plg["listen"]
        # 隔离：证书生成目录与配对状态都落 TMPDIR（防污染插件真实 state/）
        import tempfile
        tmp_dir = Path(tempfile.mkdtemp(prefix="bgrelay-listen-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp_dir, ignore_errors=True))
        with patch.object(plg_listen, "STATE_DIR", tmp_dir), \
                patch.object(plg_listen, "validate_bind_address",
                             return_value="127.0.0.1"):
            result = plg_listen.listen_serve(
                address="127.0.0.1", port=0, advertise="127.0.0.1",
                member=REMOTE, home=os.environ.get("TMPDIR", "."),
                mcp_python="", tls=True)
        self.assertEqual(result["status"], "listening")
        self.assertTrue(result["fingerprint"], "TLS 监听必须带证书指纹")
        # 配对凭据只出现在配对链接查询参数（结果 dict 不含凭据值）
        from urllib.parse import parse_qs, urlsplit
        pairing_token = parse_qs(urlsplit(result["pairing_url"]).query)[
            "pairing_token"][0]
        # 引擎侧按配对记录拨入（TLS+证书指纹+一次性凭据）
        conn = db.get_connection()
        try:
            relay.set_dial_endpoint(conn, REMOTE,
                                    {"host": "127.0.0.1", "port": result["port"],
                                     "tls": True,
                                     "fingerprint": result["fingerprint"],
                                     "pairing_token": pairing_token})
            relay.set_direction(conn, REMOTE, "dial")
        finally:
            conn.close()
        relay_dialer.start_dial(REMOTE)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and \
                relay_dialer._task_state(REMOTE) != "connected":
            time.sleep(0.05)
        self.assertEqual(relay_dialer._task_state(REMOTE), "connected",
                         "引擎应实际拨通插件监听")
        link = relay_server.active_link(REMOTE)
        self.assertIsInstance(link, relay_dialer.DialLink)
        # 帧互通：引擎开流发帧，插件 ListenServer 转发给本机 gateway 流
        # （隔离环境无 gateway——验证 listen 侧收帧后不炸+回帧通道）
        transport = relay_server.open_transport(REMOTE)
        self.assertIsNotNone(transport)
        # 配对已兑换：二次拨号握手必被拒（单次）
        self.assertFalse(self.plg["pairing"].redeem_pairing(
            member=REMOTE, token=pairing_token), "首次成功后再次兑换必须失败")

    def test_persistent_identity_reconnect_after_pairing(self):
        """manager node2057 ①：首次配对成功后签发持久身份——断线重连用
        持久凭据（一次性配对票已作废），吊销持久身份后重连才被拒。"""
        plg_listen = self.plg["listen"]
        plg_pairing = self.plg["pairing"]
        import tempfile
        tmp_dir = Path(tempfile.mkdtemp(prefix="bgrelay-persist-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp_dir, ignore_errors=True))
        with patch.object(plg_listen, "STATE_DIR", tmp_dir), \
                patch.object(plg_listen, "validate_bind_address",
                             return_value="127.0.0.1"):
            result = plg_listen.listen_serve(
                address="127.0.0.1", port=0, advertise="127.0.0.1",
                member=REMOTE, home=os.environ.get("TMPDIR", "."),
                mcp_python="", tls=True)
        from urllib.parse import parse_qs, urlsplit
        pairing_token = parse_qs(urlsplit(result["pairing_url"]).query)[
            "pairing_token"][0]
        conn = db.get_connection()
        try:
            relay.set_dial_endpoint(conn, REMOTE,
                                    {"host": "127.0.0.1", "port": result["port"],
                                     "tls": True,
                                     "fingerprint": result["fingerprint"],
                                     "pairing_token": pairing_token})
            relay.set_direction(conn, REMOTE, "dial")
        finally:
            conn.close()
        # 首次拨号：兑换一次性配对票 → 插件签发持久凭据下发 → 引擎轮换
        relay_dialer.start_dial(REMOTE)
        self.assertTrue(self._wait_connected(10), "首次配对应拨通")
        conn = db.get_connection()
        try:
            endpoint = relay.read_dial_endpoint(conn, REMOTE)
        finally:
            conn.close()
        persistent_token = endpoint["pairing_token"]
        self.assertNotEqual(persistent_token, pairing_token,
                            "首次成功后凭据轮换为持久身份凭据")
        self.assertTrue(plg_pairing.verify_persistent(REMOTE, persistent_token))
        self.assertFalse(plg_pairing.redeem_pairing(member=REMOTE,
                                                    token=pairing_token),
                         "一次性配对票已作废")
        # 断线重连：hello 出示轮换后的持久凭据 → verify_persistent 通过
        relay_dialer.stop_dial(REMOTE)
        self.assertIsNone(relay_server.active_link(REMOTE))
        relay_dialer.start_dial(REMOTE)
        self.assertTrue(self._wait_connected(10),
                        "断线重连用持久身份认证，无需重新配对")
        # 吊销持久身份：重连被拒（吊销才断）
        relay_dialer.stop_dial(REMOTE)
        plg_pairing.clear_persistent(REMOTE)
        relay_dialer.start_dial(REMOTE)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and \
                relay_dialer._task_state(REMOTE) != "backoff":
            time.sleep(0.05)
        self.assertIsNone(relay_server.active_link(REMOTE),
                          "吊销持久身份后重连必须被拒（须重新配对）")

    def test_mcp_tunnel_http_request_forwarding(self):
        """隧道全链：引擎 DialLink 收到 http_request → 转发引擎回环 API →
        http_response 回传（插件 mcpproxy.deliver 收单）。"""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from engine import auth

        # manager node2084：随包 MCP 自带 hermes_session_auth 注入的受限
        # 会话凭据，隧道核验属于通道成员后原样转发（范围随行）
        credential = auth.session_token(REMOTE, {
            "profile": REMOTE, "project": "alpha", "thread_id": None,
            "session": "sess-1", "session_id": "sid-1", "generation": 1})
        member_headers = {"x-group-key": credential}

        class EngineAPI(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: A003
                pass

            def do_GET(self):  # noqa: N802
                body = json.dumps({"ok": True, "path": self.path}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        api = ThreadingHTTPServer(("127.0.0.1", 0), EngineAPI)
        api_thread = threading.Thread(target=api.serve_forever, daemon=True)
        api_thread.start()
        self.addCleanup(api.shutdown)
        fake_ws = _SyncWs()
        link = relay_dialer.DialLink(REMOTE, fake_ws, 1, "203.0.113.9")
        request_id = "req-1"
        with patch.object(__import__("engine.config", fromlist=["config"]),
                          "host", return_value="127.0.0.1"), \
                patch.object(__import__("engine.config", fromlist=["config"]),
                             "port", return_value=api.server_address[1]):
            relay_server._serve_relay_http_request(link, {
                "relay": {"kind": "http_request", "request_id": request_id,
                          "generation": 1},
                "method": "GET", "path": "/health", "headers": member_headers,
                "body": ""})
        sent = json.loads(fake_ws.sent[0])
        self.assertEqual(sent["relay"]["kind"], "http_response")
        self.assertEqual(sent["status"], 200)
        self.assertEqual(sent["relay"]["request_id"], request_id)
        self.assertIn('"ok": true', sent["body"])
        # 白名单外路径：403 不触达引擎 API
        with patch.object(__import__("engine.config", fromlist=["config"]),
                          "host", return_value="127.0.0.1"), \
                patch.object(__import__("engine.config", fromlist=["config"]),
                             "port", return_value=api.server_address[1]):
            relay_server._serve_relay_http_request(link, {
                "relay": {"kind": "http_request", "request_id": "req-2",
                          "generation": 1},
                "method": "GET", "path": "/admin/secrets",
                "headers": member_headers, "body": ""})
        sent = json.loads(fake_ws.sent[1])
        self.assertEqual(sent["status"], 403, "管理端点绝不隧道转发")

    def test_plugin_proxy_delivers_response(self):
        plg_proxy = self.plg["mcpproxy"]
        import queue as queue_mod
        waiter = queue_mod.Queue(maxsize=1)
        with plg_proxy._PENDING_LOCK:
            plg_proxy._PENDING["req-x"] = waiter
        plg_proxy.deliver_http_response("req-x", 200, {"content-type": "application/json"},
                                        '{"ok": true}')
        status, headers, body = waiter.get(timeout=2)
        self.assertEqual(status, 200)
        self.assertEqual(body, '{"ok": true}')


    def test_db_generation_used_for_state_writes(self):
        """manager node2057 ②：帧代≠本地代时，心跳/离线写入必须用本地
        状态代（帧代 5、本地代 4：心跳后 DB 仍是 online/4 且心跳时间更新，
        断线后 offline 命中本地代条件）。"""
        fake_ws = _SyncWs()
        link = relay_dialer.DialLink(REMOTE, fake_ws, 5, peer_ip="203.0.113.9",
                                     db_generation=4)
        with relay_server._LINKS_LOCK:
            relay_server._LINKS[REMOTE] = link
        conn = db.get_connection()
        try:
            # 造 DB 前提：行代先到 4（generation_advance 3 次），再 mark_online(4)
            for _ in range(3):
                relay.generation_advance(conn, REMOTE)
            relay.mark_online(conn, REMOTE, code_version="", mcp_fingerprint="",
                              generation=4)
            before = relay.read_node(conn, REMOTE)
            # 插件心跳帧（帧代 5）→ 状态写库走本地代 4
            relay_server._route_inbound(link, {
                "relay": {"kind": "heartbeat", "generation": 5}})
            row = relay.read_node(conn, REMOTE)
            self.assertEqual(row["state"], "online")
            self.assertEqual(row["generation"], 4, "本地状态代不被帧代覆盖")
            self.assertGreater(row["last_heartbeat"], before["last_heartbeat"],
                               "心跳必须更新本地代状态（帧代5/本地代4不命中即漏更）")
            # mcp_report 同口径
            relay_server._route_inbound(link, {
                "relay": {"kind": "mcp_report", "generation": 5},
                "mcp_fingerprint": "ab" * 32})
            row = relay.read_node(conn, REMOTE)
            self.assertEqual(row["mcp_fingerprint"], "ab" * 32,
                             "mcp_report 写库用本地状态代")
            # 断线：offline 写库命中本地代
            relay_server._shutdown_link(link, "test down")
            self.assertEqual(relay.read_node(conn, REMOTE)["state"], "offline",
                             "离线写入命中本地状态代条件")
        finally:
            conn.close()


class _SyncWs:
    """DialLink 的 sync ws 桩：记录发送帧。"""

    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, text: str):
        self.sent.append(text)

    def close(self, code=1000, reason=""):
        self.closed = True


class EntryAuthorizationTests(unittest.TestCase):
    """CLI/私聊入口身份核验拒绝路径。"""

    def test_cli_unauthorized_local_user_rejected(self):
        plg = _plugin()
        cli = plg["cli"]
        with patch.object(cli, "_authorized_local_user", return_value=False):
            with self.assertRaises(SystemExit) as caught:
                cli._require_authorized()
            self.assertEqual(caught.exception.code, 1)

    def test_cli_authorization_uses_kernel_identity(self):
        """manager node2074/12711：CLI 以 OS 内核主体核对白名单，不以可
        伪造的 USERNAME/USER 环境变量为准（Windows 走 GetUserNameExW 登录
        会话主体）。"""
        plg = _plugin()
        cli = plg["cli"]
        if os.name != "nt":
            self.skipTest("Windows 内核身份用例")

        def fake_get_name(fmt, buf, size_ptr):  # noqa: ANN001
            buf.value = "DOMAIN\\realuser"
            return 1

        with patch.object(os, "name", "nt"), \
                patch("ctypes.windll.secur32.GetUserNameExW",
                      side_effect=fake_get_name, create=True), \
                patch.dict(os.environ, {
                    "USERNAME": "fake-env-user",
                    "BOT_GROUP_RELAY_ALLOWED_USERS": "DOMAIN\\realuser"}):
            self.assertTrue(cli._authorized_local_user(),
                            "内核主体 DOMAIN\\realuser 命中白名单")
        # 内核 API 失败：返回空 → 即使环境 USERNAME 命中也不放行
        with patch.object(os, "name", "nt"), \
                patch("ctypes.windll.secur32.GetUserNameExW",
                      return_value=0, create=True), \
                patch.dict(os.environ, {
                    "USERNAME": "env-user",
                    "BOT_GROUP_RELAY_ALLOWED_USERS": "env-user"}):
            self.assertFalse(cli._authorized_local_user(),
                             "内核身份取不到时拒绝，环境变量不算数")

    def test_private_chat_requires_admin_allowlist(self):
        plg = _plugin()
        tools = plg["tools"]
        # 未配置授权管理员：一律拒绝（默认拒绝，绝不裸奔）
        with patch.object(tools, "_authorized_profiles", return_value=set()):
            result = json.loads(tools.bot_group_listen(
                {"address": "127.0.0.1", "port": 1}))
            self.assertFalse(result["ok"])
        # 配置白名单但会话上下文不是 dm 私聊：拒绝（正向匹配 dm，
        # manager node2074/12711）
        with patch.object(tools, "_authorized_profiles", return_value={"admin"}):
            with patch.dict(os.environ, {
                    "HERMES_SESSION_CHAT_TYPE": "group",
                    "HERMES_SESSION_PLATFORM": "telegram",
                    "HERMES_SESSION_ACTOR_ID": "admin"}):
                result = json.loads(tools.bot_group_relay_status({}))
                self.assertFalse(result["ok"], "群聊不是私聊，拒绝")
        # dm 私聊 + 渠道与本轮 ACTOR_ID 命中白名单：放行（status 无副作用）
        with patch.object(tools, "_authorized_profiles", return_value={"admin"}), \
                patch.dict(os.environ, {
                    "HERMES_SESSION_CHAT_TYPE": "dm",
                    "HERMES_SESSION_PLATFORM": "telegram",
                    "HERMES_SESSION_ACTOR_ID": "admin"}):
            result = json.loads(tools.bot_group_relay_status({}))
            self.assertTrue(result["ok"])


class EntryIdentityAndProxyTests(unittest.TestCase):
    """manager node2060 ①③：受信调用者身份（不可信 session_id 不绕过）、
    CLI 交互/非交互配对材料语义、listen 必启随包 MCP 代理。"""

    def test_caller_identity_is_trusted_dm_channel_actor_not_session_id(self):
        """私聊入口身份=宿主受信会话字段（manager node2074/12711）：
        正向匹配 chat_type=dm + 渠道与本轮 ACTOR_ID 核对白名单；不可信
        kwargs 的 session_id 命中白名单也绝不放行（会话句柄不是身份）。"""
        plg = _plugin()
        tools = plg["tools"]
        with patch.object(tools, "_authorized_profiles", return_value={"admin"}), \
                patch.dict(os.environ, {
                    "HERMES_SESSION_CHAT_TYPE": "dm",
                    "HERMES_SESSION_PLATFORM": "telegram",
                    "HERMES_SESSION_ACTOR_ID": "mallory"}):
            # kwargs 带 session_id='admin'（不可信注入）→ actor=mallory 未
            # 授权 → 拒绝（session_id 绝不成为授权身份）
            result = json.loads(tools.bot_group_relay_status(
                {}, session_id="admin", task_id="t-1"))
            self.assertFalse(result["ok"], "不可信的 session_id 不得成为授权身份")
        # 渠道限定条目 platform:actor 命中
        with patch.object(tools, "_authorized_profiles",
                          return_value={"telegram:admin"}), \
                patch.dict(os.environ, {
                    "HERMES_SESSION_CHAT_TYPE": "dm",
                    "HERMES_SESSION_PLATFORM": "telegram",
                    "HERMES_SESSION_ACTOR_ID": "admin",
                    "HERMES_SESSION_ACTOR_NAME": "展示名"}):
            result = json.loads(tools.bot_group_relay_status({}))
            self.assertTrue(result["ok"], "渠道+ACTOR_ID 命中白名单放行")
        # 渠道不符：同 actor 在别的渠道不命中限定条目
        with patch.object(tools, "_authorized_profiles",
                          return_value={"telegram:admin"}), \
                patch.dict(os.environ, {
                    "HERMES_SESSION_CHAT_TYPE": "dm",
                    "HERMES_SESSION_PLATFORM": "lark",
                    "HERMES_SESSION_ACTOR_ID": "admin"}):
            result = json.loads(tools.bot_group_relay_status({}))
            self.assertFalse(result["ok"], "渠道不符的限定条目不放行")
        # 缺渠道或 ACTOR：拒绝
        with patch.object(tools, "_authorized_profiles", return_value={"admin"}), \
                patch.dict(os.environ, {
                    "HERMES_SESSION_CHAT_TYPE": "dm",
                    "HERMES_SESSION_PLATFORM": "",
                    "HERMES_SESSION_ACTOR_ID": "admin"}):
            result = json.loads(tools.bot_group_relay_status({}))
            self.assertFalse(result["ok"], "缺渠道拒绝")

    def test_first_register_uses_host_profile_not_manual_override(self):
        from types import SimpleNamespace
        plg = _plugin()
        extra = SimpleNamespace(extra={"engine_url": "http://127.0.0.1:1",
                                       "member": "manual", "invite": "fixture"})
        with patch.dict(os.environ, {"HERMES_PROFILE": "fixture-host",
                                      "HERMES_PROFILE_NAME": "fixture-host",
                                      "BOT_GROUP_RELAY_MEMBER": "old-setting"}):
            self.assertEqual(plg["config"].load(extra).member, "fixture-host")

    def test_cli_interactive_outputs_pairing_material(self):
        """CLI 交互（TTY）：输出含 pairing_url（用户本人在场可取配对材料）；
        非交互：链接写 0600 文件、stdout 不含链接（manager node2060 ①）。"""
        plg = _plugin()
        cli = plg["cli"]
        synthetic = {
            "status": "listening", "address": "127.0.0.1", "port": 18443,
            "advertise": "203.0.113.10", "scheme": "wss",
            "fingerprint_short": "aabbccdd", "expires_in_seconds": 600,
            "proxy_port": 18444, "proxy_url": "http://127.0.0.1:18444",
            "pairing_url": "wss://203.0.113.10:18443/relay/ws?pairing_token=SECRET",
            "link_file": "", "_server": None,
        }
        args = type("Args", (), {
            "command": "listen", "address": "127.0.0.1", "port": 18443,
            "advertise": "203.0.113.10", "member": "remote1",
            "cert": "", "key": "", "no_tls": False, "no_proxy": False,
            "proxy_port": 0})()
        listen_mod = plg["listen"]
        captured = {}

        def fake_foreground(cleanup):
            captured["cleanup"] = cleanup
            return 0

        from io import StringIO
        # 交互：stdout 含配对链接
        interactive_out = StringIO()
        interactive_out.isatty = lambda: True
        with patch.object(listen_mod, "listen_serve", return_value=synthetic), \
                patch.object(cli, "_run_foreground", side_effect=fake_foreground), \
                patch.object(cli.sys, "stdout", new=interactive_out), \
                patch.object(cli, "_require_authorized", return_value=None):
            rc = cli.dispatch(args)
        self.assertEqual(rc, 0)
        self.assertIn("SECRET", interactive_out.getvalue(),
                      "交互终端必须输出配对链接")
        # 非交互：stdout 不含链接（link_to_file=True 走 0600 文件）
        captured.clear()
        noninteractive_out = StringIO()
        noninteractive_out.isatty = lambda: False
        with patch.object(listen_mod, "listen_serve", return_value=synthetic), \
                patch.object(cli, "_run_foreground", side_effect=fake_foreground), \
                patch.object(cli.sys, "stdout", new=noninteractive_out), \
                patch.object(cli, "_require_authorized", return_value=None):
            rc = cli.dispatch(args)
        self.assertEqual(rc, 0)
        self.assertNotIn("SECRET", noninteractive_out.getvalue(),
                         "非交互 stdout 不得含链接")
        self.assertIn("link_file", noninteractive_out.getvalue())

    def test_listen_starts_mcpproxy_by_default(self):
        """listen_serve 正常路径必启动随包 MCP 代理（manager node2060 ②）：
        结果带 proxy_port/proxy_url；no_proxy 时跳过。"""
        plg = _plugin()
        listen_mod = plg["listen"]
        mcpproxy = plg["mcpproxy"]
        import tempfile
        tmp_dir = Path(tempfile.mkdtemp(prefix="bgrelay-proxy-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp_dir, ignore_errors=True))
        self.addCleanup(mcpproxy.stop_proxy)
        with patch.object(listen_mod, "STATE_DIR", tmp_dir), \
                patch.object(listen_mod, "validate_bind_address",
                             return_value="127.0.0.1"):
            result = listen_mod.listen_serve(
                address="127.0.0.1", port=0, advertise="127.0.0.1",
                member=REMOTE, home=os.environ.get("TMPDIR", "."),
                mcp_python="", tls=True)
        self.assertTrue(result["proxy_port"] > 0, "正常路径必须启动 MCP 代理")
        self.assertEqual(result["proxy_url"],
                         f"http://127.0.0.1:{result['proxy_port']}")
        # 真实代理端口已监听（GROUP_ENGINE_URL 指向后即可用）
        import socket
        probe = socket.create_connection(("127.0.0.1", result["proxy_port"]),
                                         timeout=2)
        probe.close()
        # 清理：server+代理停止（资源清理路径）
        result["_server"].stop()
        mcpproxy.stop_proxy()
        # no_proxy 跳过：proxy_port=0
        with patch.object(listen_mod, "STATE_DIR", tmp_dir), \
                patch.object(listen_mod, "validate_bind_address",
                             return_value="127.0.0.1"):
            result2 = listen_mod.listen_serve(
                address="127.0.0.1", port=0, advertise="127.0.0.1",
                member=REMOTE, home=os.environ.get("TMPDIR", "."),
                mcp_python="", tls=True, no_proxy=True)
        self.assertEqual(result2["proxy_port"], 0, "no_proxy 明确跳过代理")
        result2["_server"].stop()

    def test_proxy_handler_reaches_listener_and_instant_reply(self):
        """manager node2092：HTTP handler 经 self.server.listener 取转发
        目标——listener 必须挂在实际 httpd 实例上（否则 AttributeError、
        业务帧发不出）；响应等待队列先于发帧注册，引擎经配对通道的
        快速回帧不丢响应。"""
        import http.client
        plg = _plugin()
        mcpproxy = plg["mcpproxy"]

        class Listener:
            def forward_http_request(self, request_id, method, path, headers, body):
                # 模拟引擎同步快速回帧（发帧后立即回 http_response）
                mcpproxy.deliver_http_response(request_id, 200,
                                               {"content-type": "application/json"},
                                               '{"projects": ["alpha"]}')

        server = mcpproxy.start_proxy(Listener(), port=0)
        self.addCleanup(mcpproxy.stop_proxy)
        self.assertIs(server._httpd.listener, server.listener,
                      "listener 必须挂在实际 HTTP server 上供 handler 取用")
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("GET", "/members?project=alpha")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200, "快速回帧不得丢失")
        self.assertIn("alpha", resp.read().decode())
        conn.close()
        self.assertEqual(mcpproxy._PENDING, {}, "请求完成后无残留等待位")

    def test_cli_mount_contract_parser_add_subparsers(self):
        """manager 12801：宿主 _attach_plugin_cli_command 已建插件专属
        ArgumentParser 再调 setup_fn——register_cli 首步必须
        add_subparsers（把 parser 当 subparsers 会 AttributeError）；子
        命令 dest=command 挂在插件命名空间、独立于宿主顶层 dest。用宿主
        挂载契约解析 listen/register/status 三子命令。"""
        import argparse
        plg = _plugin()
        cli = plg["cli"]
        host = argparse.ArgumentParser(prog="hermes")
        host.add_argument("--debug", action="store_true", help="宿主顶层参数")
        host_sub = host.add_subparsers(dest="top", required=True)
        plugin_parser = host_sub.add_parser("bot-group-relay",
                                            help="bot-group relay 插件控制")
        cli.register_cli(plugin_parser)
        for argv, expect in (
                (["status"], "status"),
                (["register", "--engine-url", "http://127.0.0.1:1",
                  "--member", "m", "--credential", "c"], "register"),
                (["listen", "--address", "127.0.0.1", "--port", "8443"],
                 "listen"),
        ):
            args = host.parse_args(["bot-group-relay"] + argv)
            self.assertEqual(args.top, "bot-group-relay")
            self.assertEqual(args.command, expect,
                             "子命令 dest 独立于宿主顶层参数、dispatch 可读")
        with self.assertRaises(SystemExit):
            host.parse_args(["bot-group-relay"])  # 缺子命令 required 报错

    def test_mcp_python_resolves_plugin_venv_over_host(self):
        """manager 12793：常规 listen 入口（不显式传 mcp_python）必须复用
        mcp_digest 的插件 venv 解析——宿主 Python 没装 mcp 时仍用安装脚本
        生成的解释器，不落回宿主 sys.executable。"""
        plg = _plugin()
        listen_mod = plg["listen"]
        mcp_digest = plg["mcp_digest"]
        relay_config = plg["config"]
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp) / "plugin"
            fake_venv = fake_root / "venv" / "Scripts" / "python.exe"
            fake_venv.parent.mkdir(parents=True)
            fake_venv.write_text("fake", encoding="utf-8")
            server = listen_mod.ListenServer(
                address="127.0.0.1", port=0, cert_path="", key_path="",
                advertise="127.0.0.1", member=REMOTE, home=tmp, mcp_python="")
            try:
                with patch.object(mcp_digest, "PLUGIN_ROOT", fake_root):
                    self.assertEqual(server._mcp_python(), str(fake_venv),
                                     "插件 venv 解释器优先于宿主 sys.executable")
                with patch.object(relay_config, "platform_mcp_python",
                                  return_value=sys.executable):
                    self.assertEqual(Path(server._mcp_python()),
                                     Path(sys.executable),
                                     "PlatformConfig 配置优先于插件 venv")
            finally:
                pass
            explicit = str(Path(tmp) / "explicit.exe")
            Path(explicit).write_text("x", encoding="utf-8")
            server_explicit = listen_mod.ListenServer(
                address="127.0.0.1", port=0, cert_path="", key_path="",
                advertise="127.0.0.1", member=REMOTE, home=tmp,
                mcp_python=explicit)
            self.assertEqual(server_explicit._mcp_python(), explicit,
                             "显式参数优先")
            # 插件 venv 缺失且无配置 → 宿主兜底
            with patch.object(mcp_digest, "PLUGIN_ROOT", Path(tmp) / "none"):
                with patch.object(relay_config, "platform_mcp_python",
                                  return_value=""):
                    self.assertEqual(server._mcp_python(), sys.executable)

    def test_proxy_releases_waiter_when_channel_unavailable(self):
        """manager node2092：转发失败（通道未认证）→ 502 且释放注册位，
        不残留等待项。"""
        import http.client
        plg = _plugin()
        mcpproxy = plg["mcpproxy"]

        class Listener:
            def forward_http_request(self, *args):
                raise ConnectionError("relay channel not authenticated")

        server = mcpproxy.start_proxy(Listener(), port=0)
        self.addCleanup(mcpproxy.stop_proxy)
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("GET", "/members?project=alpha")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 502)
        conn.close()
        self.assertEqual(mcpproxy._PENDING, {}, "失败时释放注册位，无残留")



class EngineTunnelIdentityTests(unittest.TestCase):
    """manager node2060 ②：隧道转发绑定实际通道成员身份——外部身份头
    过滤、注入 X-Group-Profile=link.member、响应脱敏记录。"""

    def setUp(self):
        ctx = isolated_database()
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        conn = db.get_connection()
        enable_members(conn, ("alpha",), profiles=[REMOTE])
        relay.register_node(conn, REMOTE, CREDENTIAL, display_name="r")
        conn.close()

    def _forward_harness(self, envelope):
        """运行一次隧道转发（patch urlopen 记录下游头），返回 (captured, ws_frames)。"""
        fake_ws = _SyncWs()
        link = relay_dialer.DialLink(REMOTE, fake_ws, 1, peer_ip="203.0.113.9")
        captured_headers = {}
        captured_url = {}

        class _Resp:
            status = 200
            headers = {"content-type": "application/json"}

            def read(self):
                return b'{"projects": ["alpha"]}'

            def close(self):
                pass

        def fake_urlopen(request, timeout=120):  # noqa: ARG001
            captured_headers.update({k.lower(): v for k, v in request.headers.items()})
            captured_url["full"] = request.full_url
            captured_url["method"] = request.get_method()
            return _Resp()

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            relay_server._serve_relay_http_request(link, envelope)
        return (captured_headers, captured_url, fake_ws.sent)

    def _session_credential(self, member=REMOTE):
        """构造该成员的受限会话凭据（hermes_session_auth 同口径注入）。"""
        from engine import auth
        return auth.session_token(member, {
            "profile": member, "project": "alpha", "thread_id": None,
            "session": "sess-1", "session_id": "sid-1", "generation": 1})

    def test_forward_requires_member_scoped_credential(self):
        """manager node2084：外部身份头一律过滤；凭据缺失或不属于通道成员
        → 401 拒绝，绝不注入无 scope 的 bot_token 静默降级。"""
        envelope = {
            "relay": {"kind": "http_request", "request_id": "r-1",
                      "generation": 1},
            "method": "GET", "path": "/projects/?read=1",
            "headers": {
                # 外部身份头：必须全部过滤，绝不透传（不得冒充管理员/他人）
                "authorization": "Bearer admin-secret",
                "x-group-key": "external-key",
                "x-group-profile": "someone-else",
                "content-type": "application/json",
            },
            "body": "",
        }
        captured, captured_url, frames = self._forward_harness(envelope)
        self.assertEqual(captured_url, {}, "凭据缺失/外部身份 → 不得转发到引擎")
        self.assertNotIn("authorization", captured)
        response = json.loads(frames[-1])
        self.assertEqual(response["relay"]["kind"], "http_response")
        self.assertEqual(response["status"], 401,
                         "外部身份头全滤后无通道成员凭据：401 拒绝")
        # 其他成员的有效凭据同样拒绝（凭据必须属于通道成员本人）
        from engine import auth
        alien = auth.session_token("someone-else", {
            "profile": "someone-else", "project": "alpha", "thread_id": None,
            "session": "sess-2", "session_id": "sid-2", "generation": 1})
        envelope["headers"] = {"x-group-key": alien}
        captured, captured_url, frames = self._forward_harness(envelope)
        self.assertEqual(captured_url, {}, "非通道成员凭据不得转发")
        self.assertEqual(json.loads(frames[-1])["status"], 401)

    def test_forward_preserves_member_scoped_credential(self):
        """manager node2084：随包 MCP 自带 hermes_session_auth 注入的受限
        会话凭据——核验属于通道成员后原样转发，session/execution 范围
        随行（access 侧照常施加项目/会话/执行限制）；不再替换为 bot_token
        （该类型 session_scope/execution_scope 均为空会丢权限链）。"""
        from engine import auth
        credential = self._session_credential()
        self.assertIsNotNone(auth.session_scope(REMOTE, credential,
                                                auth.signing_keys()[1]),
                             "夹具凭据须带会话范围")
        envelope = {
            "relay": {"kind": "http_request", "request_id": "r-2",
                      "generation": 1},
            "method": "GET", "path": "/members?project=alpha",
            "headers": {
                "x-group-key": credential,
                "x-group-profile": "someone-else",  # 外部身份仍须被覆盖
                "content-type": "application/json",
                "accept": "application/json",
            },
            "body": "",
        }
        captured, captured_url, frames = self._forward_harness(envelope)
        self.assertIn("/members", captured_url["full"],
                     "/members 在白名单内必须实际转发")
        self.assertEqual(captured.get("x-group-profile"), REMOTE,
                         "下游授权上下文必须绑定实际配对通道成员")
        self.assertEqual(captured.get("x-group-key"), credential,
                         "成员受限凭据原样保留（原会话/执行范围随行）")
        self.assertNotEqual(captured.get("x-group-key"), auth.bot_token(REMOTE),
                            "不得降级为无 scope 的通用成员密钥")
        self.assertEqual(captured.get("content-type"), "application/json",
                         "内容头正常透传")
        response = json.loads(frames[-1])
        self.assertEqual(response["relay"]["kind"], "http_response")
        self.assertEqual(response["status"], 200)

    def test_forward_members_path_with_member_scope_key(self):
        """manager node2074/12711 + node2084：随包 group_list_members 实际
        GET /members——路径在白名单（转发而非 403）、下游保留成员作用域
        凭据与成员身份。"""
        credential = self._session_credential()
        envelope = {
            "relay": {"kind": "http_request", "request_id": "r-3",
                      "generation": 1},
            "method": "GET", "path": "/members?project=alpha",
            "headers": {"x-group-key": credential},
            "body": "",
        }
        captured, captured_url, frames = self._forward_harness(envelope)
        self.assertIn("/members", captured_url["full"],
                     "/members 在白名单内必须实际转发")
        self.assertEqual(captured.get("x-group-profile"), REMOTE)
        self.assertEqual(captured.get("x-group-key"), credential,
                         "成员作用域凭据随转发保留（verify_bot 可核验）")
        response = json.loads(frames[-1])
        self.assertEqual(response["relay"]["kind"], "http_response")
        self.assertEqual(response["status"], 200)
        self.assertIn("projects", response["body"])

    def test_proxy_port_flow_renders_loopback_url_for_dial(self):
        """manager node2084：方向 B 随包 MCP 的 GROUP_ENGINE_URL 必须指向
        插件 loopback 代理实际端口——快照 proxy_port 落库（含 0 清零），
        render_mcp_config 按节点方向渲染；方向 A 仍用引擎直连地址。"""
        conn = db.get_connection()
        try:
            relay.register_node(conn, REMOTE, CREDENTIAL, display_name="r")
            conn.execute("UPDATE relay_nodes SET mcp_config=?,direction=? "
                         "WHERE member=?",
                         (json.dumps({"command": "python", "args": [],
                                      "env": {"GROUP_ENGINE_URL": "{engine_url}"}}),
                          "dial", REMOTE))
            conn.commit()
            # 快照带本次代理端口 → 落库
            relay.mcp_report(conn, REMOTE, "fp-1", generation=1, proxy_port=52310)
            rendered = relay.render_mcp_config(conn, REMOTE,
                                               engine_url="http://engine:8201")
            self.assertEqual(rendered["env"]["GROUP_ENGINE_URL"],
                             "http://127.0.0.1:52310",
                             "方向 B 渲染为插件 loopback 代理实际端口")
            # 停代理/换包：proxy_port=0 清零（不得残留旧端口）
            relay.mcp_report(conn, REMOTE, "fp-2", generation=1, proxy_port=0)
            conn.execute("UPDATE relay_nodes SET proxy_port=0 WHERE member=?",
                         (REMOTE,))
            conn.commit()
            rendered = relay.render_mcp_config(conn, REMOTE,
                                               engine_url="http://engine:8201")
            self.assertEqual(rendered["env"]["GROUP_ENGINE_URL"],
                             "http://engine:8201",
                             "代理未启（proxy_port=0）回落引擎直连地址")
            # 方向 A（引擎监听）：始终用引擎直连地址
            conn.execute("UPDATE relay_nodes SET direction='listen',proxy_port=52311 "
                         "WHERE member=?", (REMOTE,))
            conn.commit()
            rendered = relay.render_mcp_config(conn, REMOTE,
                                               engine_url="http://engine:8201")
            self.assertEqual(rendered["env"]["GROUP_ENGINE_URL"],
                             "http://engine:8201",
                             "方向 A 不走插件 loopback 代理")
        finally:
            conn.close()

    def test_merge_mcp_spec_snapshot_proxy_port_writes_and_clears(self):
        """manager node2084：configure 前 fetch_report（mcp_snapshot 同步
        问询）带回的 proxy_port 按代写库，0 值同样生效（旧端口清零）。"""
        conn = db.get_connection()
        try:
            relay.register_node(conn, REMOTE, CREDENTIAL, display_name="r")
            conn.execute("UPDATE relay_nodes SET mcp_config=? WHERE member=?",
                         (json.dumps({"command": "python", "args": [],
                                      "env": {"GROUP_ENGINE_URL": "{engine_url}"}}),
                          REMOTE))
            conn.execute("UPDATE relay_nodes SET proxy_port=52312 WHERE member=?",
                         (REMOTE,))
            conn.commit()
            calls = {"n": 0}

            def fetch_report():
                calls["n"] += 1
                return {"mcp_fingerprint": "fp-snap", "code_version": "v2",
                        "generation": 1, "proxy_port": 52313}

            merged = relay.merge_mcp_spec(conn, REMOTE, {"command": "python"},
                                          engine_url="http://engine:8201",
                                          fetch_report=fetch_report)
            row = relay.read_node(conn, REMOTE)
            self.assertEqual(row["proxy_port"], 52313, "快照代理端口按代写库")
            self.assertIn("env", merged)
            # 第二次问询 proxy_port=0：旧端口必须清零
            def fetch_report_zero():  # noqa: ANN001
                return {"mcp_fingerprint": "fp-snap2", "code_version": "v2",
                        "generation": 1, "proxy_port": 0}

            relay.merge_mcp_spec(conn, REMOTE, {"command": "python"},
                                 engine_url="http://engine:8201",
                                 fetch_report=fetch_report_zero)
            row = relay.read_node(conn, REMOTE)
            self.assertEqual(row["proxy_port"], 0, "停代理后旧端口清零")
        finally:
            conn.close()


    def test_dial_snapshot_generation_uses_local_db_generation(self):
        """manager node2088：首次拨号帧代≠本地状态代（帧 1/库 2）时，快照
        写库代必须用本连接 db_generation——帧代写库（WHERE generation=?）
        不命中，proxy_port 落库 0、GROUP_ENGINE_URL 回落错误地址。帧校验
        仍用插件帧代，旧连接不得借用新代。"""
        import queue as queue_module
        fake_ws = _SyncWs()
        link = relay_dialer.DialLink(REMOTE, fake_ws, generation=1,
                                     peer_ip="203.0.113.9", db_generation=2)
        waiter = queue_module.Queue(maxsize=1)
        link._snapshot_waiters["snap-1"] = waiter
        link._on_snapshot_response("snap-1", {
            "mcp_fingerprint": "fp-dial", "code_version": "v1",
            "proxy_port": 62630})
        report = waiter.get(timeout=2)
        self.assertEqual(report["generation"], 2,
                         "写库代必须用本地状态代 db_generation")
        self.assertEqual(link.generation, 1, "线上帧校验仍用插件帧代")
        # 快照经 merge_mcp_spec 写库：帧代 1 不命中、本地代 2 命中
        conn = db.get_connection()
        try:
            relay.register_node(conn, REMOTE, CREDENTIAL, display_name="r")
            conn.execute("UPDATE relay_nodes SET generation=2,mcp_config=?,"
                         "direction=? WHERE member=?",
                         (json.dumps({"command": "python", "args": [],
                                      "env": {"GROUP_ENGINE_URL": "{engine_url}"}}),
                          "dial", REMOTE))
            conn.commit()
            merged = relay.merge_mcp_spec(conn, REMOTE, {"command": "python"},
                                          engine_url="http://engine:8201",
                                          fetch_report=lambda: report)
            row = relay.read_node(conn, REMOTE)
            self.assertEqual(row["proxy_port"], 62630, "快照代理端口按本地代落库")
            self.assertEqual(merged["env"]["GROUP_ENGINE_URL"],
                             "http://127.0.0.1:62630",
                             "dial 渲染为插件 loopback 代理实际端口")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()


class FirstJoinTests(unittest.TestCase):
    """req#175 task#328 首次接入：配对导入（方向 B）与一次性邀请（方向 A）
    建独立节点——远端成员不在本地名册；同名冲突不覆盖；邀请单次/过期；
    hello 成员上报核验。隔离：临时 SQLite + 本机回环真实 WS/监听。"""

    def setUp(self):
        self._ctx = isolated_database()
        self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)
        conn = db.get_connection()
        try:
            # 本地名册只有 alpha 项目；远端成员 remoteA/remoteB 不注册、
            # 不进名册——零个远程节点起步（req#175 验收 1）
            enable_members(conn, ("alpha",), profiles=[])
        finally:
            conn.close()
        self.plg = _plugin()
        versions = patch.object(self.plg['config'], 'plugin_version',
                                return_value=relay_server._engine_release_version() or '')
        versions.start()
        self.addCleanup(versions.stop)
        self._tmpdir = tempfile.TemporaryDirectory(prefix="first-join-")
        self.addCleanup(self._tmpdir.cleanup)
        self._state_patch = patch.object(
            self.plg["pairing"], "_state_path",
            return_value=Path(self._tmpdir.name) /
            f"first-join-{threading.get_ident()}.json")
        self._state_patch.start()
        self.addCleanup(self._state_patch.stop)
        self.addCleanup(relay_server.close_all)
        for member in ("remote-remoteA", "remote-remoteB", REMOTE):
            self.addCleanup(relay_dialer.stop_dial, member)
        self._client = None
        self._engine = None

    def _api_client(self):
        """懒起真实 uvicorn 隔离引擎（API 路由 + /relay/ws）——邀请方向
        需要真实 WS 端点供插件 RelayClient 连接。"""
        if self._client is None:
            import socket
            import uvicorn
            from fastapi import FastAPI, WebSocket
            from engine import api
            app = FastAPI()
            app.include_router(api.router_api)

            @app.websocket("/relay/ws")
            async def endpoint(websocket: WebSocket):
                await relay_server.relay_ws(websocket)
            self._sock = socket.socket()
            self._sock.bind(("127.0.0.1", 0))
            self.addCleanup(self._sock.close)
            self._api_port = self._sock.getsockname()[1]
            self._server = uvicorn.Server(uvicorn.Config(
                app, lifespan="off", log_level="error"))
            self._worker = threading.Thread(
                target=self._server.run, kwargs={"sockets": [self._sock]},
                daemon=True)
            self._worker.start()

            def _stop():
                self._server.should_exit = True
                self._worker.join(8)

            self.addCleanup(_stop)
            deadline = time.monotonic() + 8
            while not self._server.started:
                self.assertTrue(self._worker.is_alive()
                                and time.monotonic() < deadline)
                time.sleep(0.02)
            self._client = self._api_port
        return self._client

    def _admin_post(self, path, body=None):
        import urllib.request
        import urllib.error
        port = self._api_client()
        data = json.dumps(body or {}).encode() if body is not None else b"{}"
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                     data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def _confirm_invite(self, issued, member):
        import urllib.request
        path = f"/relay/invitations/{issued['pairing_id']}"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with urllib.request.urlopen(f'http://127.0.0.1:{self._api_client()}{path}', timeout=5) as resp:
                view = json.load(resp)
            if view['state'] == 'pending_confirmation':
                self.assertEqual(view['member'], member)
                break
            time.sleep(0.05)
        else:
            self.fail('verified pairing offer did not become visible')
        status, confirmed = self._admin_post(path + '/confirm')
        self.assertEqual((status, confirmed['state']), (200, 'confirmed'))

    def _wait_dial_connected(self, member, timeout=15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if relay_dialer._task_state(member) == 'connected':
                with closing(db.get_connection()) as conn:
                    node = relay.read_node(conn, member)
                if node and node['state'] == 'online':
                    return True
            time.sleep(0.05)
        return False

    # ---------------- 方向 B：配对导入建独立节点（不选本地成员） ----------------

    def test_pairing_import_creates_independent_node(self):
        """req#175 验收 1/6：云端插件 listen（远端成员 remoteA，本地名册
        不存在）→ 配对链接携带成员 → preview 展示身份 → import 建独立节点
        → 拨号认证；hello_ok 上报成员与节点归属一致。同名再导入 409 且
        不覆盖。"""
        plg_listen = self.plg["listen"]
        with tempfile.TemporaryDirectory(prefix="bgrelay-join-") as tmp_dir:
            with patch.object(plg_listen, "STATE_DIR", Path(tmp_dir)), \
                    patch.object(plg_listen, "validate_bind_address",
                                 return_value="127.0.0.1"):
                result = plg_listen.listen_serve(
                    address="127.0.0.1", port=0, advertise="127.0.0.1",
                    member="remoteA", home=os.environ.get("TMPDIR", "."),
                    tls=True, mcp_entry="")
            self.addCleanup(result["_server"].stop)
            pairing_url = result["pairing_url"]
        self.assertIn("member=remoteA", pairing_url,
                      "配对链接必须携带远端实际成员（req#175 验收 6）")
        status, preview = self._admin_post("/relay/pairings/preview",
                                           {"pairing_url": pairing_url})
        self.assertEqual(status, 200)
        self.assertEqual(preview["member"], "remoteA")
        self.assertEqual(preview.get('state'), 'pending_confirmation')
        self.assertFalse(self.plg['pairing'].pairing_metadata()['redeemed'])
        self.assertIsNone(result['_server']._active_ws)
        with closing(db.get_connection()) as conn:
            self.assertEqual(relay.list_nodes(conn), [])
        path = f"/relay/pairings/{preview['pairing_id']}"
        self.assertEqual(preview["fingerprint_short"],
                         result["fingerprint_short"])
        self.assertEqual(self._admin_post('/relay/pairings/import', {'pairing_url': pairing_url})[0], 409)
        status, imported = self._admin_post(path + '/confirm', {'pairing_url': pairing_url})
        self.assertEqual(status, 200, f"导入失败: {imported}")
        self.assertEqual(imported['state'], 'confirmed')
        conn = db.get_connection()
        try:
            row = relay.read_node(conn, "remote-remoteA")
            self.assertIsNotNone(row, "导入必须按远端上报成员建独立节点")
            self.assertEqual(row["direction"], "dial")
            endpoint = relay.read_dial_endpoint(conn, "remote-remoteA")
            self.assertEqual(endpoint["host"], "127.0.0.1")
            self.assertEqual(endpoint["port"], result["port"])
        finally:
            conn.close()
        self.assertTrue(self._wait_dial_connected("remote-remoteA"),
                        "导入后拨号必须认证连通（hello_ok 成员上报一致）")
        self.assertEqual(self._admin_post(path + '/confirm', {'pairing_url': pairing_url}), (200, imported))
        self.assertEqual(self._admin_post(path + '/cancel')[0], 409)
        relay_dialer.stop_dial('remote-remoteA')
        relay_dialer.start_dial('remote-remoteA')
        self.assertTrue(self._wait_dial_connected('remote-remoteA'), '同一凭据必须能重新连接，不二次消费配对链接')
        conn = db.get_connection()
        try:
            self.assertEqual(relay.read_node(conn, "remote-remoteA")["state"], "online")
        finally:
            conn.close()
        # 同名再导入：409 不覆盖
        status, again = self._admin_post("/relay/pairings/import",
                                         {"pairing_url": pairing_url})
        self.assertEqual(status, 409)
        status, preview2 = self._admin_post("/relay/pairings/preview",
                                            {"pairing_url": pairing_url})
        self.assertEqual((status, preview2['reason']), (409, 'name_conflict'))

    def test_outbound_rejects_stale_and_cancelled_previews_without_consuming(self):
        from urllib.parse import urlsplit, parse_qs
        from engine import relay_pairing
        plg_listen = self.plg['listen']
        with patch.object(plg_listen, 'STATE_DIR', Path(self._tmpdir.name)), \
                patch.object(plg_listen, 'validate_bind_address', return_value='127.0.0.1'):
            result = plg_listen.listen_serve(address='127.0.0.1', port=0, advertise='127.0.0.1',
                member='remoteB', home=self._tmpdir.name, tls=True, mcp_entry='')
        self.addCleanup(result['_server'].stop)
        url = result['pairing_url']
        token = parse_qs(urlsplit(url).query)['pairing_token'][0]
        body = {'pairing_url': url}

        def preview():
            status, data = self._admin_post('/relay/pairings/preview', body)
            self.assertEqual((status, data.get('state')), (200, 'pending_confirmation'), data)
            self.assertNotIn(token, json.dumps(data))
            return f"/relay/pairings/{data['pairing_id']}", data

        for modified in (url.replace(result['fingerprint'], 'ab' * 32),
                         url.replace(token, 'invalid-fixture-ticket'), url.replace('member=remoteB', 'member=remoteA')):
            self.assertEqual(self._admin_post('/relay/pairings/preview', {'pairing_url': modified})[0], 502)
        cancelled, _ = preview()
        self.assertEqual(self._admin_post(cancelled + '/confirm', {'pairing_url': url.replace(token, 'other-ticket')})[1]['reason'],
                         'preview_mismatch')
        self.assertEqual(self._admin_post(cancelled + '/cancel')[1]['state'], 'cancelled')
        self.assertEqual(self._admin_post(cancelled + '/confirm', body)[0], 409)
        expired, e = preview()
        with patch.object(relay, '_now', return_value=e['expires_at'] + 1):
            code, stale = self._admin_post(expired + '/confirm', body)
            self.assertEqual((code, stale['state']), (409, 'expired'))
        conflicted, _ = preview()
        self.plg['pairing'].issue_pairing(member='remoteB', advertise='127.0.0.1', port=result['port'],
                                          fingerprint=result['fingerprint'])
        status, failed = self._admin_post(conflicted + '/confirm', body)
        self.assertEqual((status, failed['stage'], failed['reason']), (502, 'auth', 'pairing_refused'))
        with closing(db.get_connection()) as conn:
            self.assertFalse(relay.list_nodes(conn) or ip_allowlist.list_bindings(conn))
            self.assertIsNone(relay_pairing.read(conn, cancelled.rsplit('/', 1)[1])['candidate'])
        self.assertFalse(self.plg['pairing'].pairing_metadata()['redeemed'])
        self.assertFalse(self.plg['pairing']._load_state().get('persistent'))
        self.assertIsNone(result['_server']._active_ws)

    def test_outbound_confirm_expiring_during_verified_probe_allows_immediate_retry(self):
        from engine import relay_pairing
        plg_listen = self.plg['listen']
        with patch.object(plg_listen, 'STATE_DIR', Path(self._tmpdir.name)), \
                patch.object(plg_listen, 'validate_bind_address', return_value='127.0.0.1'):
            result = plg_listen.listen_serve(address='127.0.0.1', port=0, advertise='127.0.0.1',
                member='remoteB', home=self._tmpdir.name, tls=True, mcp_entry='')
        self.addCleanup(result['_server'].stop)
        body = {'pairing_url': result['pairing_url']}

        def prepare():
            status, data = self._admin_post('/relay/pairings/preview', body)
            self.assertEqual((status, data.get('state')), (200, 'pending_confirmation'), data)
            return f"/relay/pairings/{data['pairing_id']}", data

        early_path, early = prepare()
        clock = [early['expires_at'] + 1]
        verify = relay_dialer.verify_first_pairing
        with patch.object(relay, '_now', side_effect=lambda: clock[0]):
            self.assertEqual(self._admin_post(early_path + '/confirm', body)[1]['state'], 'expired')
            with closing(db.get_connection()) as conn:
                early_row = relay_pairing.read(conn, early['pairing_id'])
            late_path, late = prepare()
            clock[0] = late['expires_at'] - 1

            def cross_deadline(*args, **kwargs):
                receipt = verify(*args, **kwargs)
                clock[0] = late['expires_at'] + 1
                return receipt

            with patch.object(relay_dialer, 'verify_first_pairing', side_effect=cross_deadline):
                code, view = self._admin_post(late_path + '/confirm', body)
            self.assertEqual((code, view['state']), (409, 'expired'))
            # Read both stores before any new preview or status poll can hide the bug.
            with closing(db.get_connection()) as conn:
                late_row = relay_pairing.read(conn, late['pairing_id'])
                self.assertFalse(relay.list_nodes(conn) or ip_allowlist.list_bindings(conn))
            remote = self.plg['pairing']._load_state()
            self.assertFalse(remote.get('persistent') or remote['pairing']['redeemed'])
            self.assertIsNone(result['_server']._active_ws)
            self.assertFalse('activation' in remote['pairing'], 'failed confirmation left a live remote reservation')
            for row in (early_row, late_row):
                self.assertEqual((row['state'], row['candidate']), ('expired', None))
            retry_path, _ = prepare()
            code, verified = self._admin_post(retry_path + '/confirm', body)
            self.assertEqual((code, verified.get('state')), (200, 'confirmed'), verified)
            self.assertTrue(self._wait_dial_connected('remote-remoteB'))

    def test_outbound_confirm_losing_local_race_leaves_no_effective_remote_key(self):
        from concurrent.futures import ThreadPoolExecutor
        plg_listen = self.plg['listen']
        with patch.object(plg_listen, 'STATE_DIR', Path(self._tmpdir.name)), \
                patch.object(plg_listen, 'validate_bind_address', return_value='127.0.0.1'):
            result = plg_listen.listen_serve(address='127.0.0.1', port=0, advertise='127.0.0.1',
                member='remoteB', home=self._tmpdir.name, tls=True, mcp_entry='')
        self.addCleanup(result['_server'].stop)
        body = {'pairing_url': result['pairing_url']}
        status, data = self._admin_post('/relay/pairings/preview', body)
        self.assertEqual(status, 200, data)
        path = f"/relay/pairings/{data['pairing_id']}"
        ready, resume = threading.Event(), threading.Event()
        verify = relay_dialer.verify_first_pairing

        from engine import relay_pairing
        def wait_after_verify(*args, **kwargs):
            checked = verify(*args, **kwargs)
            ready.set()
            if not resume.wait(8):
                raise RuntimeError('test did not release the confirmation')
            return checked

        with patch.object(relay_dialer, 'verify_first_pairing', side_effect=wait_after_verify), \
                ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self._admin_post, path + '/confirm', body)
            try:
                self.assertTrue(ready.wait(8))
                self.assertEqual(self._admin_post(path + '/cancel')[1]['state'], 'cancelled')
                with closing(db.get_connection()) as conn:
                    row = relay_pairing.read(conn, data['pairing_id'])
                    self.assertEqual((row['state'], row['candidate']), ('cancelled', None))
                self.assertFalse('activation' in self.plg['pairing']._load_state()['pairing'])
            finally:
                resume.set()
            status, cancelled = pending.result(timeout=8)
        self.assertEqual((status, cancelled['state']), (409, 'cancelled'))
        self.assertFalse(self.plg['pairing']._load_state().get('persistent'))
        self.assertFalse(self.plg['pairing'].pairing_metadata()['redeemed'])
        self.assertIsNone(result['_server']._active_ws)
        with closing(db.get_connection()) as conn:
            self.assertFalse(relay.list_nodes(conn) or ip_allowlist.list_bindings(conn))
        code, fresh = self._admin_post('/relay/pairings/preview', body)
        self.assertEqual((code, fresh.get('state')), (200, 'pending_confirmation'), fresh)
        code, confirmed = self._admin_post(f"/relay/pairings/{fresh['pairing_id']}/confirm", body)
        self.assertEqual((code, confirmed.get('state')), (200, 'confirmed'), confirmed)
        self.assertTrue(self._wait_dial_connected('remote-remoteB'))

    def test_outbound_idle_expiration_and_expired_cancel_clear_old_material(self):
        from engine import relay_pairing, scanner
        member, now = 'remoteB', relay._now()
        endpoint = {'host': '127.0.0.1', 'port': 1, 'tls': True,
                    'fingerprint': 'ab' * 32, 'pairing_token': 'fixture-ticket'}
        with closing(db.get_connection()) as conn:
            idle, cancelled = [relay_pairing.create(conn, member, endpoint, now + 100) for _ in range(2)]
            # Model a database upgraded with old unconfirmed candidate material.
            with conn:
                conn.execute('UPDATE relay_dial_attempts SET candidate=?', ('obsolete-fixture',))
        cancel_path = f"/relay/pairings/{cancelled['pairing_id']}"
        with patch.object(relay, '_now', return_value=now + 101), \
                patch.object(relay_dialer, 'verify_first_pairing') as verify:
            status, response = self._admin_post(cancel_path + '/cancel')
            self.assertEqual((status, response['state']), (200, 'expired'))
            verify.assert_not_called()
            with closing(db.get_connection()) as conn:
                row = relay_pairing.read(conn, cancelled['pairing_id'])
                self.assertEqual((row['state'], row['candidate']), ('expired', None))
                self.assertIsNotNone(relay_pairing.read(conn, idle['pairing_id'])['candidate'])
                scanner.run_once(conn)
            with closing(db.get_connection()) as conn:
                row = relay_pairing.read(conn, idle['pairing_id'])
                self.assertEqual((row['state'], row['candidate']), ('expired', None))
                self.assertFalse(relay.list_nodes(conn) or ip_allowlist.list_bindings(conn))

    def test_expired_activation_ticket_is_cleared_without_first_dial(self):
        from engine import relay_pairing
        now = relay._now()
        endpoint = {'host': 'fixture', 'port': 443, 'tls': True, 'fingerprint': 'ab' * 32,
                    'pairing_token': 'fixture-ticket'}
        with closing(db.get_connection()) as conn:
            attempt = relay_pairing.create(conn, 'remoteB', endpoint, now + 60)
            relay_pairing.decide(conn, attempt['pairing_id'], verified=('remoteB', endpoint, now + 60))
            initial = relay.read_dial_endpoint(conn, 'remote-remoteB')
            self.assertEqual(initial['activation_ticket'], 'fixture-ticket')
            with patch.object(relay, '_now', return_value=now + 61):
                relay_pairing.expire_stale(conn)
                clean = relay.read_dial_endpoint(conn, 'remote-remoteB')
                self.assertNotIn('activation_ticket', clean)
                self.assertNotIn('activation_expires_at', clean)
                self.assertEqual(clean['pairing_token'], initial['pairing_token'])

    def test_outbound_activation_reconnect_after_lost_ack_does_not_redeem_again(self):
        plg_listen, pairing = self.plg['listen'], self.plg['pairing']
        with patch.object(plg_listen, 'STATE_DIR', Path(self._tmpdir.name)), \
                patch.object(plg_listen, 'validate_bind_address', return_value='127.0.0.1'):
            result = plg_listen.listen_serve(address='127.0.0.1', port=0, advertise='127.0.0.1',
                member='remoteB', home=self._tmpdir.name, tls=True, mcp_entry='')
        self.addCleanup(result['_server'].stop)
        authenticate = relay_dialer._authenticate_dial
        redeemed = []

        def lose_first_ack(member, endpoint, payload, **kwargs):
            reply = authenticate(member, endpoint, payload, **kwargs)
            if reply and payload.get('activate_pairing') and not redeemed:
                redeemed.append(pairing._load_state()['pairing']['redeemed_at'])
                reply[0].close()
                return None
            return reply

        body = {'pairing_url': result['pairing_url']}
        code, view = self._admin_post('/relay/pairings/preview', body)
        self.assertEqual(code, 200, view)
        confirm = f"/relay/pairings/{view['pairing_id']}/confirm"
        with patch.object(relay_dialer, '_authenticate_dial', side_effect=lose_first_ack):
            code, confirmed = self._admin_post(confirm, body)
            self.assertEqual((code, confirmed['state']), (200, 'confirmed'))
            self.assertTrue(self._wait_dial_connected('remote-remoteB'))
        self.assertEqual(redeemed, [pairing._load_state()['pairing']['redeemed_at']])
        with closing(db.get_connection()) as conn:
            endpoint = relay.read_dial_endpoint(conn, 'remote-remoteB')
            self.assertFalse('activation_ticket' in endpoint or 'activation_expires_at' in endpoint)
            self.assertEqual(len(relay.list_nodes(conn)), 1)
        self.assertEqual(self._admin_post(confirm, body), (200, confirmed))

    def test_unreachable_same_named_host_still_requires_verification(self):
        """An existing local profile cannot satisfy remote host verification."""
        with closing(db.get_connection()) as conn, conn:
            conn.execute("INSERT INTO members(profile,display_name) VALUES('local-taken','Local')")
        body = {"pairing_url": "wss://127.0.0.1:1?pairing_token=fixture"
                               "&fingerprint=" + "ab" * 32 + "&member=local-taken"}
        status, preview = self._admin_post("/relay/pairings/preview", body)
        self.assertEqual((status, preview['reason']), (502, 'unreachable'))
        self.assertEqual(self._admin_post("/relay/pairings/import", body)[0], 409)
        with closing(db.get_connection()) as conn:
            self.assertIsNone(relay.read_node(conn, "local-taken"))
            self.assertEqual(conn.execute("SELECT display_name FROM members WHERE profile='local-taken'").fetchone()[0],
                             "Local")

    def test_pairing_url_missing_member_rejected(self):
        """配对链接缺 member/凭据/指纹 → 400（远端身份必须取自配对过程）。"""
        status, body = self._admin_post(
            "/relay/pairings/preview",
            {"pairing_url": "wss://127.0.0.1:8443?pairing_token=x&fingerprint=aa"})
        self.assertEqual(status, 400)
        status, body = self._admin_post(
            "/relay/pairings/import",
            {"pairing_url": "not-a-url"})
        self.assertEqual(status, 400)

    # ---------------- 方向 A：一次性邀请兑换建节点 ----------------

    def test_invitation_issue_redeem_single_use(self):
        """邀请兑换原子单次；过期失效；同名冲突不消耗邀请。"""
        from unittest.mock import patch as _patch
        conn = db.get_connection()
        try:
            issued = relay.issue_invitation(conn, ttl_seconds=5)
        finally:
            conn.close()
        token = issued["invite_token"]
        conn = db.get_connection()
        try:
            self.assertTrue(relay.redeem_invitation(conn, token, "remoteB"))
            self.assertFalse(relay.redeem_invitation(conn, token, "remoteB"),
                             "一次性邀请二次兑换必须失败")
            self.assertFalse(relay.redeem_invitation(conn, "nope", "remoteB"))
            expired = relay.issue_invitation(conn, ttl_seconds=0.01)
        finally:
            conn.close()
        time.sleep(0.02)
        conn = db.get_connection()
        try:
            self.assertFalse(relay.redeem_invitation(conn, expired["invite_token"],
                                                     "remoteB"), "过期失效")
        finally:
            conn.close()

    def test_register_remote_node_name_conflict(self):
        """同名冲突：member 已存在返回 None，不覆盖不接管。"""
        conn = db.get_connection()
        try:
            relay.register_node(conn, "taken", "old-credential")
            self.assertIsNone(relay.register_remote_node(
                conn, "taken", "new-credential", direction="listen"))
            self.assertTrue(relay.verify_credential(conn, "taken", "old-credential"),
                            "旧身份必须保持不变")
            self.assertFalse(relay.verify_credential(conn, "taken", "new-credential"))
            row = relay.register_remote_node(conn, "fresh", "c1",
                                             direction="dial")
            self.assertEqual(row["direction"], "dial")
        finally:
            conn.close()

    def test_duplicate_credential_refused_without_consuming(self):
        import urllib.request
        from websockets.sync.client import connect
        from websockets.exceptions import ConnectionClosed
        with closing(db.get_connection()) as conn:
            relay.register_node(conn, 'remoteA', 'existing-isolated-credential')
        _, issued = self._admin_post('/relay/invitations')
        base = f'http://127.0.0.1:{self._api_client()}'
        path = f"/relay/invitations/{issued['pairing_id']}"
        def status():
            with urllib.request.urlopen(base + path, timeout=5) as response:
                return json.load(response)
        self.assertEqual(status()['state'], 'waiting')
        version = relay_server._engine_release_version() or ''
        hello = {'relay': {'kind': 'hello'}, 'member': 'remoteA',
                 'invite': issued['invite_token'], 'protocol_version': 1,
                 'plugin_version': version, 'join_proof': 'ab' * 32,
                 'credential_commitment': relay.hash_credential('existing-isolated-credential')}
        for token in ('invalid-fixture-ticket', issued['invite_token']):
            with connect(base.replace('http:', 'ws:') + '/relay/ws', proxy=None) as ws:
                ws.send(json.dumps({**hello, 'invite': token}))
                with self.assertRaises(ConnectionClosed) as rejection:
                    ws.recv(timeout=5)
                self.assertEqual(rejection.exception.rcvd.code, 4001)
            if token != issued['invite_token']:
                self.assertEqual(status()['state'], 'waiting')
        failed = status()
        self.assertEqual((failed['state'], failed['stage'], failed['reason'], failed['member']),
                         ('failed', 'identity', 'name_conflict', 'remoteA'))
        self.assertFalse({'token_hash', 'proof_hash', 'credential_commitment', 'invite_token'} & failed.keys())
        self.assertEqual(self._admin_post(path + '/confirm')[0], 409)
        self.assertEqual(self._admin_post(path + '/cancel')[1]['state'], 'cancelled')
        with closing(db.get_connection()) as conn:
            self.assertEqual(conn.execute('SELECT redeemed FROM relay_invitations').fetchone()[0], 0)
            self.assertTrue(relay.verify_credential(conn, 'remoteA', 'existing-isolated-credential'))
            self.assertEqual(len(relay.list_nodes(conn)), 1)
            self.assertFalse(ip_allowlist.list_bindings(conn))
        self.assertIsNone(relay_server.active_link('remote-remoteA'))

    def test_invitation_hello_cannot_register_until_admin_confirms(self):
        from websockets.sync.client import connect
        status, issued = self._admin_post('/relay/invitations')
        self.assertEqual(status, 200)
        version = relay_server._engine_release_version() or ''
        credential = 'isolated-specific-credential'
        with connect(f'ws://127.0.0.1:{self._api_client()}/relay/ws', proxy=None) as ws:
            ws.send(json.dumps({'relay': {'kind': 'hello'}, 'member': 'remoteA',
                                'invite': issued['invite_token'], 'protocol_version': 1,
                                'plugin_version': version, 'join_proof': 'ab' * 32,
                                'credential_commitment': relay.hash_credential(credential)}))
            self.assertEqual(json.loads(ws.recv(timeout=5))['relay']['kind'], 'hello_pending')
        with closing(db.get_connection()) as conn:
            self.assertIsNone(relay.read_node(conn, 'remote-remoteA'))
        self.assertIsNone(relay_server.active_link('remote-remoteA'))
        path = f"/relay/invitations/{issued['pairing_id']}"
        status, confirmed = self._admin_post(path + '/confirm')
        self.assertEqual((status, confirmed['state'], confirmed['member']), (200, 'confirmed', 'remoteA'))
        self.assertNotIn('credential', confirmed)
        with closing(db.get_connection()) as conn:
            self.assertTrue(relay.verify_credential(conn, 'remote-remoteA', credential))
            self.assertEqual(relay.read_node(conn, 'remote-remoteA')['state'], 'offline')

    def test_confirmed_offer_survives_lost_receipt_and_plugin_restart(self):
        from websockets.sync.client import connect
        status, issued = self._admin_post('/relay/invitations')
        self.assertEqual(status, 200)
        base = f'http://127.0.0.1:{self._api_client()}'
        member = 'remoteA'
        version = relay_server._engine_release_version() or ''
        proof, candidate = self.plg['pairing'].register_offer(member, base, issued['invite_token'])
        hello = {'relay': {'kind': 'hello'}, 'member': member, 'invite': issued['invite_token'],
                 'join_proof': proof, 'credential_commitment': relay.hash_credential(candidate),
                 'protocol_version': 1, 'plugin_version': version}
        uri = base.replace('http:', 'ws:') + '/relay/ws'
        with connect(uri, proxy=None) as ws:
            ws.send(json.dumps(hello))
            self.assertEqual(json.loads(ws.recv(timeout=5))['relay']['kind'], 'hello_pending')
        self._confirm_invite(issued, member)
        # Confirmation commits, but the first response is never read by the plugin.
        with connect(uri, proxy=None) as lost:
            lost.send(json.dumps(hello))
        self.assertFalse(self.plg['pairing'].load_register_credential(member))
        from websockets.exceptions import ConnectionClosed
        with connect(uri, proxy=None) as replay:
            replay.send(json.dumps({**hello, 'join_proof': 'de' * 32}))
            with self.assertRaises(ConnectionClosed) as error:
                replay.recv(timeout=5)
            self.assertEqual(error.exception.rcvd.code, 4001)
        cfg = self.plg['config'].RelayConfig(engine_url=base, member=member, credential='',
            invite=issued['invite_token'], mcp_python='', home=self._tmpdir.name)
        with patch.object(self.plg['config'], 'plugin_version', return_value=version):
            client = self._load_relay_module().RelayClient(cfg)
            client.start()
            try:
                deadline = time.monotonic() + 10
                while not client.generation and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(client.generation)
                self.assertTrue(self.plg['pairing'].load_register_credential(member) == candidate)
            finally:
                client.stop()
                client._thread.join(timeout=5)
                self.assertFalse(client._thread.is_alive())
        with closing(db.get_connection()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM relay_nodes').fetchone()[0], 1)
            self.assertFalse(relay.redeem_invitation(conn, issued['invite_token'], member))

    def test_cancel_expiry_and_parallel_confirm_cannot_leave_extra_nodes(self):
        from concurrent.futures import ThreadPoolExecutor
        from engine import ip_allowlist
        proof, commitment = 'cd' * 32, relay.hash_credential('isolated-driven-key')
        with closing(db.get_connection()) as conn:
            cancelled = relay.issue_invitation(conn)
            expired = relay.issue_invitation(conn, ttl_seconds=60)
            live = [relay.issue_invitation(conn) for _ in range(2)]
            for issued, member in zip((cancelled, *live, expired), ('remoteCancel', 'remoteA', 'remoteA', 'remoteExpired')):
                offer = relay.offer_invitation(conn, issued['invite_token'], member, '192.0.2.27', proof, commitment)
                self.assertEqual(offer['state'], 'pending_confirmation')
                self.assertIsNone(relay.offer_invitation(conn, issued['invite_token'], member, '192.0.2.28', proof, commitment))
        path = lambda issued: f"/relay/invitations/{issued['pairing_id']}"
        self.assertEqual(self._admin_post(path(cancelled) + '/cancel')[1]['state'], 'cancelled')
        self.assertEqual(self._admin_post(path(cancelled) + '/cancel')[0], 200)
        self.assertEqual(self._admin_post(path(cancelled) + '/confirm')[0], 409)
        with patch.object(relay, '_now', return_value=expired['expires_at'] + 1):
            self.assertEqual(self._admin_post(path(expired) + '/confirm')[0], 409)
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda issued: self._admin_post(path(issued) + '/confirm'), live))
        self.assertEqual(sorted(status for status, _ in replies), [200, 409])
        refused = next(body for status, body in replies if status == 409)
        self.assertEqual((refused['state'], refused['stage'], refused['reason']),
                         ('failed', 'confirmation', 'name_conflict'))
        confirmed = next(body for status, body in replies if status == 200)
        self.assertEqual(self._admin_post(path(confirmed) + '/confirm')[0], 200)
        with closing(db.get_connection()) as conn:
            self.assertEqual([node['member'] for node in relay.list_nodes(conn)], ['remote-remoteA'])
            self.assertEqual(ip_allowlist.list_bindings(conn), [{'member': 'remote-remoteA', 'ips': ['192.0.2.27']}])
            self.assertEqual(conn.execute('SELECT SUM(redeemed) FROM relay_invitations').fetchone()[0], 1)

    def test_new_invitation_with_stale_environment_credential(self):
        from websockets.sync.client import connect

        status, issued = self._admin_post('/relay/invitations')
        self.assertEqual(status, 200)
        config = self.plg['config']
        pairing = self.plg['pairing']
        base = f'http://127.0.0.1:{self._api_client()}'
        member = 'remoteB'
        pairing.save_register_credential(member, 'previous-saved-key',
                                         engine_url=base, invite='previous-invite')
        with patch.dict(os.environ, {
            'BOT_GROUP_ENGINE_URL': base,
            'BOT_GROUP_RELAY_CREDENTIAL': 'previous-env-key',
            'BOT_GROUP_RELAY_INVITE': issued['invite_token'],
            'HERMES_HOME': self._tmpdir.name,
        }), patch.object(config, 'host_member', return_value=member):
            cfg = config.load()

        client_type = self._load_relay_module().RelayClient
        client = client_type(cfg)
        with connect(config.ws_url(base), proxy=None) as ws:
            self.assertIsNone(client._handshake(ws))
        self.assertEqual(pairing.load_register_credential(member), 'previous-saved-key')
        self._confirm_invite(issued, member)
        with connect(config.ws_url(base), proxy=None) as ws:
            self.assertIsNone(client._handshake(ws))
        with connect(config.ws_url(base), proxy=None) as ws:
            self.assertTrue(client_type(cfg)._handshake(ws))

    def test_cli_exits_after_pairing_then_gateway_connects_from_disk(self):
        from argparse import Namespace
        from concurrent.futures import ThreadPoolExecutor
        from websockets.sync.client import connect
        config, cli = self.plg['config'], self.plg['cli']
        client_type = self._load_relay_module().RelayClient
        _, issued = self._admin_post('/relay/invitations')
        url = f'http://127.0.0.1:{self._api_client()}'
        args = Namespace(command='register', engine_url=url, member='', credential='', invite=issued['invite_token'])
        with patch.object(cli, '_require_authorized'), patch.object(config, 'host_member', return_value='remoteB'), \
                patch.dict(os.environ, {'BOT_GROUP_ENGINE_URL': '', 'BOT_GROUP_RELAY_INVITE': '',
                                        'BOT_GROUP_RELAY_CREDENTIAL': '', 'BOT_GROUP_RELAY_MEMBER': ''}):
            with ThreadPoolExecutor(1) as pool:
                registration = pool.submit(cli.dispatch, args)
                self._confirm_invite(issued, 'remoteB')
                self.assertEqual(registration.result(timeout=8), 0)
            self.assertIsNone(relay_server.active_link('remote-remoteB'))
            cfg = config.load()
            self.assertEqual((cfg.engine_url, cfg.member, cfg.invite), (url, 'remoteB', ''))
            with connect(config.ws_url(url), proxy=None) as ws:
                self.assertTrue(client_type(cfg)._handshake(ws))
                link = relay_server.active_link('remote-remoteB')
                self.assertIsNotNone(link)
                self.assertEqual(cli.dispatch(args), 0)
                self.assertIs(relay_server.active_link('remote-remoteB'), link)
            self.assertFalse(self.plg['pairing'].load_register_credential('remoteB', engine_url=url + '-other'))
            pairing = self.plg['pairing']
            record = pairing._load_state()
            self.assertEqual(record['register'].pop('engine_url'), url)
            pairing._save_state(record)
            self.assertEqual(config.load(), cfg)

    def test_renewal_command_updates_existing_node_and_waiting_gateway(self):
        from argparse import Namespace
        url = f'http://127.0.0.1:{self._api_client()}'
        config, cli, pairing = self.plg['config'], self.plg['cli'], self.plg['pairing']
        profile, target = 'default', 'remote-default'
        with closing(db.get_connection()) as conn, conn:
            relay.register_paired_node(conn, 'original', profile, relay.hash_credential('old-key'),
                                       device_key='original-device', direction='listen')
            relay.revoke_credential(conn, target)
        pairing.save_register_credential(profile, 'old-key', engine_url=url)
        offline, online = threading.Event(), threading.Event()
        def status(state):
            if state == 'offline':
                offline.set()
            if state == 'online':
                online.set()
        gateway = self._load_relay_module().RelayClient(
            config.RelayConfig(url, profile, 'old-key', '', self._tmpdir.name), on_status=status)
        gateway.start()
        try:
            self.assertTrue(offline.wait(6))
            code, issued = self._admin_post(f'/relay/nodes/{target}/renewal')
            self.assertEqual(code, 200)
            args = Namespace(command='register', engine_url=url, member='', credential='', invite=issued['invite_token'])
            with patch.object(cli, '_require_authorized'), patch.object(config, 'host_member', return_value=profile):
                self.assertEqual(cli.dispatch(args), 0)
                self.assertEqual(cli.dispatch(args), 0)
            self.assertTrue(online.wait(6))
            with closing(db.get_connection()) as conn:
                saved_url, key = pairing.saved_register(profile)
                self.assertEqual(saved_url, url)
                self.assertEqual(relay.authenticated_member(conn, profile, key), target)
                self.assertFalse(relay.verify_credential(conn, target, 'old-key'))
                self.assertEqual([n['member'] for n in relay.list_nodes(conn)], [target])
                self.assertEqual(relay.read_invitation(conn, issued['pairing_id'])['node_member'], target)
        finally:
            gateway.stop()
            gateway._thread.join(5)

    def test_renewal_is_scoped_and_cancellable(self):
        target, peer = 'remote-test', '192.0.2.22'
        with closing(db.get_connection()) as conn:
            relay.register_remote_node(conn, target, 'old', direction='listen', peer_ip=peer)
            def issue():
                return relay.issue_invitation(conn, renew_member=target)
            def offer(ticket, member=target, source=peer):
                return relay.offer_invitation(conn, ticket['invite_token'], member, source, 'ab'*32,
                                              relay.hash_credential('replacement'))
            first, second = issue(), issue()
            self.assertFalse(relay.invitation_open(conn))
            self.assertIsNone(offer(first))
            self.assertIsNone(offer(second, member='another'))
            self.assertIsNone(offer(second, source='192.0.2.23'))
            relay.decide_invitation(conn, second['pairing_id'], confirm=False)
            self.assertIsNone(offer(second))
            self.assertTrue(relay.verify_credential(conn, target, 'old'))
            third = issue()
            relay.revoke_credential(conn, target)
            self.assertIsNone(offer(third))
            final = issue()
            self.assertEqual(offer(final)['state'], 'confirmed')
            self.assertEqual(offer(final)['node_member'], target)
            self.assertFalse(relay.verify_credential(conn, target, 'old'))

    def test_invitation_ws_first_join_full_chain(self):
        """方向 A 真实全链：引擎签发邀请 → 插件 RelayClient(invite) 出站
        连真实 /relay/ws → 兑换建节点（remoteB，名册外）→ hello_ok 下发
        持久凭据 → 插件落盘；节点 online。重连不再用邀请（凭据认证）。"""
        plg_relay = self._load_relay_module()
        status, issued = self._admin_post("/relay/invitations")
        self.assertEqual(status, 200)
        token = issued["invite_token"]
        self.assertTrue(issued["engine_url"].startswith("http://"))
        engine_url = f"http://127.0.0.1:{self._api_client()}"
        relay_config = self.plg["config"]
        # 合成包无版本文件：plugin_version 对齐引擎（同 tag 配套契约）
        engine_version = relay_server._engine_release_version() or ""
        self._version_patch = patch.object(
            relay_config, "plugin_version", return_value=engine_version)
        self._version_patch.start()
        self.addCleanup(self._version_patch.stop)
        client = plg_relay.RelayClient(relay_config.RelayConfig(
            engine_url=engine_url, member="remoteB", credential="",
            invite=token, mcp_python="",
            home=os.environ.get("TMPDIR", ".")))
        client.start()
        self.addCleanup(client.stop)
        self._confirm_invite(issued, 'remoteB')
        deadline = time.monotonic() + 15
        node_ok = False
        while time.monotonic() < deadline:
            conn = db.get_connection()
            try:
                row = relay.read_node(conn, "remote-remoteB")
                node_ok = row is not None and row["state"] == "online"
            finally:
                conn.close()
            if node_ok:
                break
            time.sleep(0.1)
        self.assertTrue(node_ok, "邀请兑换后节点必须建行并在线")
        conn = db.get_connection()
        try:
            row = relay.read_node(conn, "remote-remoteB")
            self.assertEqual(row["direction"], "listen")
        finally:
            conn.close()
        saved = self.plg["pairing"].load_register_credential("remoteB")
        self.assertTrue(saved, "hello_ok 下发的持久凭据必须落盘")
        # 用持久凭据重连：断开现连接后新客户端认证通过（不再依赖邀请）
        client.stop()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and \
                relay_server.active_link("remote-remoteB") is not None:
            time.sleep(0.05)
        client3 = plg_relay.RelayClient(relay_config.RelayConfig(
            engine_url=engine_url, member="remoteB", credential=saved,
            mcp_python="", home=os.environ.get("TMPDIR", ".")))
        client3.start()
        self.addCleanup(client3.stop)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and \
                relay_server.active_link("remote-remoteB") is None:
            time.sleep(0.05)
        self.assertIsNotNone(relay_server.active_link("remote-remoteB"),
                             "持久凭据重连必须认证通过")

    # ---------------- req#175 node2118 ③：登记提交时点=验证成功之后 ----------------

    def test_import_failure_leaves_no_registration_or_binding(self):
        """不可达端点返回连接错误，不写入节点、凭据或来源绑定。"""
        # 不可达端点（未监听端口）——证书指纹用 64 位假值通过格式校验
        status, imported = self._admin_post(
            "/relay/pairings/preview",
            {"pairing_url": "wss://127.0.0.1:1?pairing_token=deadbeef-"
                            + "a" * 24 + "&fingerprint=" + "ab" * 32
                            + "&member=remoteFail"})
        self.assertEqual(status, 502, imported)
        conn = db.get_connection()
        try:
            row = relay.read_node(conn, "remoteFail")
            self.assertIsNone(row, "验证失败不能残留待接入节点或凭据")
            bindings = ip_allowlist.list_bindings(conn)
            self.assertFalse(any(b["member"] == "remoteFail" for b in bindings),
                             "验证未成功不得建立来源绑定")
        finally:
            conn.close()

    def test_invite_validation_failure_keeps_invite_unused(self):
        """方向 A 登记提交时点：邀请握手带错误协议版本——兑换前校验失败，
        邀请不消耗、无节点、无绑定（失败一致）；同一邀请随后用正确版本
        兑换成功（邀请未被误消耗的证据）。"""
        status, issued = self._admin_post("/relay/invitations")
        self.assertEqual(status, 200)
        token = issued["invite_token"]
        import websockets.sync.client as ws_sync
        engine_version = relay_server._engine_release_version() or ""
        bad_hello = {"relay": {"kind": "hello"}, "member": "remoteFail2",
                     "credential": "", "invite": token,
                     "code_version": engine_version,
                     "plugin_version": engine_version,
                     "mcp_fingerprint": "",
                     "protocol_version": 999}
        from websockets.exceptions import ConnectionClosed as _WSClosed
        with ws_sync.connect(
                f"ws://127.0.0.1:{self._api_client()}/relay/ws",
                proxy=None, open_timeout=5) as ws:
            ws.send(json.dumps(bad_hello))
            try:
                ws.recv(timeout=5)
                self.fail("错误协议版本必须被拒")
            except _WSClosed as exc:
                self.assertEqual(exc.rcvd.code, 4003)
        conn = db.get_connection()
        try:
            self.assertIsNone(relay.read_node(conn, "remoteFail2"),
                              "验证失败不得残留节点")
            bindings = ip_allowlist.list_bindings(conn)
            self.assertFalse(any(b["member"] == "remoteFail2" for b in bindings),
                             "验证失败不得残留来源绑定")
            # 邀请未被消耗：正确版本可再次兑换（条件更新行数语义）
            self.assertTrue(relay.redeem_invitation(conn, token, "remoteFail2"),
                            "验证失败不得消耗一次性邀请")
        finally:
            conn.close()

    def test_invite_atomic_single_transaction(self):
        """另一连接只能同时看到已兑换的邀请、节点及来源绑定。"""
        conn = db.get_connection()
        try:
            issued = relay.issue_invitation(conn)
            self.assertTrue(relay.redeem_invitation(conn, issued["invite_token"],
                                                    "remoteTx", commit=False),
                            "首次条件更新必须生效（rowcount>0）")
            # 同事务内二次兑换：redeemed=0 条件不命中（行数=0）——单次语义
            self.assertFalse(relay.redeem_invitation(conn, issued["invite_token"],
                                                     "remoteTx", commit=False),
                             "重复兑换条件更新行数必须为 0")
            node = relay.register_remote_node(conn, "remoteTx", "cred-1",
                                              direction="listen", peer_ip="192.0.2.2",
                                              commit=False)
            self.assertEqual(node["state"], "offline")
            with closing(db.get_connection()) as observer:
                self.assertIsNone(relay.read_node(observer, "remoteTx"))
                self.assertFalse(ip_allowlist.member_allowed(observer, "remoteTx", "192.0.2.2"))
                self.assertTrue(relay.invitation_open(observer))
            conn.commit()
            row = relay.read_node(conn, "remoteTx")
            self.assertEqual(row["state"], "offline")
            self.assertTrue(ip_allowlist.member_allowed(conn, "remoteTx", "192.0.2.2"))
            self.assertFalse(relay.invitation_open(conn))
            self.assertEqual(row["credential_hash"],
                             relay.hash_credential("cred-1"))
        finally:
            conn.close()

    # ---------------- req#175 验收 4：双节点同时在线，切换 A 后 B 不受影响 ----------------

    def test_dual_online_and_switch_a_keeps_b(self):
        """req#175 验收 2/4：同一空列表接入两个独立远端——A（remoteA）
        主动连电脑（邀请兑换，direction=listen）、电脑主动连 B（remoteB，
        配对导入，direction=dial）同时在线；切换 A 方向后 B 的方向/连接/
        在线状态不受影响。"""
        plg_relay = self._load_relay_module()
        relay_config = self.plg["config"]
        # --- A：邀请方向 ---
        status, issued = self._admin_post("/relay/invitations")
        self.assertEqual(status, 200)
        engine_version = relay_server._engine_release_version() or ""
        self._version_patch = patch.object(
            relay_config, "plugin_version", return_value=engine_version)
        self._version_patch.start()
        self.addCleanup(self._version_patch.stop)
        client_a = plg_relay.RelayClient(relay_config.RelayConfig(
            engine_url=f"http://127.0.0.1:{self._api_client()}",
            member="remoteA", credential="", invite=issued["invite_token"],
            mcp_python="", home=os.environ.get("TMPDIR", ".")))
        client_a.start()
        self.addCleanup(client_a.stop)
        self._confirm_invite(issued, 'remoteA')
        # --- B：配对导入方向 ---
        plg_listen = self.plg["listen"]
        with tempfile.TemporaryDirectory(prefix="bgrelay-dual-") as tmp_dir:
            with patch.object(plg_listen, "STATE_DIR", Path(tmp_dir)), \
                    patch.object(plg_listen, "validate_bind_address",
                                 return_value="127.0.0.1"):
                result = plg_listen.listen_serve(
                    address="127.0.0.1", port=0, advertise="127.0.0.1",
                    member="remoteB", home=os.environ.get("TMPDIR", "."),
                    tls=True, mcp_entry="")
            self.addCleanup(result["_server"].stop)
            body = {'pairing_url': result['pairing_url']}
            status, preview = self._admin_post('/relay/pairings/preview', body)
            self.assertEqual(status, 200, preview)
            status, imported = self._admin_post(f"/relay/pairings/{preview['pairing_id']}/confirm", body)
            self.assertEqual(status, 200, f"B 确认失败: {imported}")
        # --- 双在线 ---
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            conn = db.get_connection()
            try:
                row_a = relay.read_node(conn, "remote-remoteA")
                row_b = relay.read_node(conn, "remote-remoteB")
                both = (row_a and row_a["state"] == "online"
                        and row_b and row_b["state"] == "online")
            finally:
                conn.close()
            if both:
                break
            time.sleep(0.1)
        self.assertTrue(both, "A、B 必须同时在线")
        self.assertIsNotNone(relay_server.active_link("remote-remoteA"))
        self.assertIsNotNone(relay_server.active_link("remote-remoteB"))
        # --- 切换 A 到 dial（给 A 一个不可达端点→进入切换态）--- 
        conn = db.get_connection()
        try:
            relay.set_dial_endpoint(conn, "remote-remoteA",
                                    {"host": "127.0.0.1", "port": 1,
                                     "tls": True, "fingerprint": "aa" * 32,
                                     "pairing_token": ""})
        finally:
            conn.close()
        status, switched = self._admin_post(
            "/relay/nodes/remote-remoteA/switch",
            {"direction": "dial"})
        self.assertEqual(status, 200, f"A 切换失败: {switched}")
        # --- B 不受影响 ---
        time.sleep(0.5)
        conn = db.get_connection()
        try:
            row_b = relay.read_node(conn, "remote-remoteB")
            self.assertEqual(row_b["direction"], "dial", "B 方向不被 A 的切换改变")
            self.assertEqual(row_b["state"], "online", "B 在线状态不被 A 的切换改变")
            self.assertEqual(row_b["switch_state"], "", "B 不得进入切换态")
        finally:
            conn.close()
        self.assertIsNotNone(relay_server.active_link("remote-remoteB"),
                             "B 的连接不被 A 的切换断开")
        conn = db.get_connection()
        try:
            row_a = relay.read_node(conn, "remote-remoteA")
            self.assertEqual(row_a["switch_target"], "dial",
                             "A 自身进入目标切换态（先断后建）")
        finally:
            conn.close()

    def _load_relay_module(self):
        """加载插件 relay 模块（合成包内）。"""
        import importlib.util
        import types as _types
        import sys as _sys
        name = "hermes_plugins.bot_group_relay_test.relay"
        if name in _sys.modules:
            return _sys.modules[name]
        pkg = _sys.modules["hermes_plugins.bot_group_relay_test"]
        spec = importlib.util.spec_from_file_location(name, PLUGIN_DIR / "relay.py")
        mod = importlib.util.module_from_spec(spec)
        mod.__name__ = name
        mod.__package__ = "hermes_plugins.bot_group_relay_test"
        _sys.modules[name] = mod
        setattr(pkg, "relay", mod)
        spec.loader.exec_module(mod)
        return mod

    # ---------------- 成员上报核验 ----------------

    def test_dial_member_report_mismatch_rejected(self):
        """req#175 验收 6：插件 hello_ok 上报成员与 dial_endpoint 归属
        不一致 → 拨号失败、不轮换凭据、不建 link（防旧连接冒名点亮）。"""
        import socket
        import ssl
        import websockets.sync.server as ws_server
        import shutil
        plg_listen = self.plg["listen"]
        tmp_dir = Path(tempfile.mkdtemp(prefix="bgrelay-mm-"))
        self.addCleanup(shutil.rmtree, tmp_dir, True)
        self._mm_state_patch = patch.object(plg_listen, "STATE_DIR", tmp_dir)
        self._mm_state_patch.start()
        self.addCleanup(self._mm_state_patch.stop)
        with patch.object(plg_listen, "validate_bind_address",
                          return_value="127.0.0.1"):
            cert, key = plg_listen._ensure_cert("", "")
            fingerprint = plg_listen.certificate_fingerprint(cert)

        def handler(conn):
            conn.recv()
            # 回 hello_ok 上报错误成员（与 dial_endpoint 归属不符）
            conn.send(json.dumps(
                {"relay": {"kind": "hello_ok", "generation": 7,
                           "member": "somebody-else"}}))

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        server = ws_server.serve(handler, "127.0.0.1", 0,
                                 ssl_context=ctx)
        port = server.socket.getsockname()[1]
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(worker.join, 2)
        conn = db.get_connection()
        try:
            relay.register_node(conn, REMOTE, CREDENTIAL)
            relay.set_dial_endpoint(conn, REMOTE,
                                    {"host": "127.0.0.1", "port": port,
                                     "tls": True, "fingerprint": fingerprint,
                                     "pairing_token": ""})
            relay.set_direction(conn, REMOTE, "dial")
        finally:
            conn.close()
        relay_dialer.start_dial(REMOTE)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and \
                relay_dialer._task_state(REMOTE) not in ("failed", "abandoned"):
            time.sleep(0.05)
        self.assertNotEqual(relay_dialer._task_state(REMOTE), "connected",
                            "成员上报不一致不得建立连接")
        self.assertIsNone(relay_server.active_link(REMOTE))
