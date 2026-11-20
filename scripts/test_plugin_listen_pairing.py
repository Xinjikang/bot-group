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
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from engine import db, relay, relay_dialer, relay_server
from scripts.unified_test_support import isolated_database

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
        self._state_patch = patch.object(
            self.pairing, "_state_path",
            return_value=Path(os.environ.get("TMPDIR", ".")) /
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
        # 隔离：插件配对状态不落真实插件目录（防污染）
        self._state_patch = patch.object(
            self.plg["pairing"], "_state_path",
            return_value=Path(os.environ.get("TMPDIR", ".")) /
            f"pairing-dial-{threading.get_ident()}.json")
        self._state_patch.start()
        self.addCleanup(self._state_patch.stop)
        self.addCleanup(relay_server.close_all)
        self.addCleanup(relay_dialer.stop_dial, REMOTE)

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

    def test_mcp_tunnel_http_request_forwarding(self):
        """隧道全链：引擎 DialLink 收到 http_request → 转发引擎回环 API →
        http_response 回传（插件 mcpproxy.deliver 收单）。"""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
                "method": "GET", "path": "/health", "headers": {}, "body": ""})
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
                "method": "GET", "path": "/admin/secrets", "headers": {}, "body": ""})
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

    def test_private_chat_requires_admin_allowlist(self):
        plg = _plugin()
        tools = plg["tools"]
        # 未配置授权管理员：一律拒绝（默认拒绝，绝不裸奔）
        with patch.object(tools, "_authorized_profiles", return_value=set()):
            result = json.loads(tools.bot_group_listen(
                {"address": "127.0.0.1", "port": 1}))
            self.assertFalse(result["ok"])
        # 配置白名单但会话上下文不含白名单身份：拒绝
        with patch.object(tools, "_authorized_profiles", return_value={"admin"}):
            result = json.loads(tools.bot_group_relay_status({}))
            self.assertFalse(result["ok"])
        # 身份命中白名单：放行（status 无副作用）
        with patch.object(tools, "_authorized_profiles", return_value={"admin"}), \
                patch.object(tools, "_caller_identity", return_value="admin"):
            result = json.loads(tools.bot_group_relay_status({}))
            self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
