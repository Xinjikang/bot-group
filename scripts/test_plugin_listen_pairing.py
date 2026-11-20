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

from engine import db, relay, relay_dialer, relay_server
from scripts.unified_test_support import isolated_database
from scripts.project_test_support import enable_members

# 插件模块经合成包路径导入（目录名带连字符不能直接 import 包）
PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "hermes" / "bot-group-relay"


def _plugin_modules():
    """两遍法加载插件模块（相对导入与执行顺序解耦）。"""
    import importlib.util
    import sys as _sys
    import types
    pkg_name = "hermes_plugins.bot_group_relay_test"
    parent_name = "hermes_plugins"
    parent = types.ModuleType(parent_name)
    parent.__path__ = [str(PLUGIN_DIR)]
    parent.__package__ = parent_name
    _sys.modules[parent_name] = parent
    # 相对导入要求子包本身可解析：注册包占位（父包属性 + sys.modules）
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(PLUGIN_DIR)]
    pkg.__package__ = pkg_name
    _sys.modules[pkg_name] = pkg
    setattr(parent, "bot_group_relay_test", pkg)
    modules = {}
    for sub in ("pairing", "config", "mcp_digest", "streams", "listen", "mcpproxy",
                "cli", "tools"):
        full = f"{pkg_name}.{sub}"
        spec = importlib.util.spec_from_file_location(full, PLUGIN_DIR / f"{sub}.py")
        mod = importlib.util.module_from_spec(spec)
        mod.__name__ = full
        mod.__package__ = pkg_name
        _sys.modules[full] = mod
        setattr(pkg, sub, mod)
        modules[sub] = mod
    for sub, mod in modules.items():
        spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.{sub}", PLUGIN_DIR / f"{sub}.py")
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

    def _listen_serve(self, port: int = 0, tmp_key: str = "retry", tmp_dir=None):
        """起真实插件监听（隔离证书/配对状态落 TMPDIR），返回 result。
        重启场景复用同一 tmp_dir：证书一致则指纹不变，免重新配对。"""
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
                mcp_python="", tls=True)

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
        self.assertEqual(holder["report"],
                         {"status": 200, "body": '{"members": []}'})

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
            "request_mcp_verify": lambda self, key, project: (
                seen.update(key=key, project=project) or {"status": 200, "body": "{}"}),
        })()
        with patch.object(relay_server, "active_link", return_value=link_stub):
            response = self._api_client().post(f"/relay/nodes/{REMOTE}/mcp-verify")
        body = response.json()
        self.assertEqual(body["mcp_verify_state"], "ok")
        self.assertEqual(seen["project"], "alpha", "验证用成员的会话项目")
        # 受限凭据：verify_bot 通过且带会话范围
        self.assertIsNotNone(auth.session_scope(REMOTE, seen["key"],
                                                auth.signing_keys()[1]))
        # 权限拒绝：403 → failed + 脱敏原因
        link_403 = type("LinkStub", (), {
            "request_mcp_verify": lambda self, key, project:
                {"status": 403, "body": '{"detail": "Session project mismatch"}'},
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
        result = self._listen_serve()
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
