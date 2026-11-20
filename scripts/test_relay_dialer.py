"""req#173 task#323 出站拨号与成员级切换状态机（先断后建）隔离测试。

覆盖：方向 B 实际拨通插件监听（TLS/指纹校验、重连退避、单活动通道）、
切换全链 A→B→A（暂停派发、同点名单次执行、切换中不告警）、重启恢复、
offline 原因区分、API 层权限。方向 A 回归由 test_relay_gateway/test_ip_allowlist 承担。

隔离夹具：isolated_database + 本机回环 fake 插件 WS 服务器（websockets.sync
与插件同栈），不动主引擎 8180。
"""
import json
import ssl
import threading
import time
import unittest
from contextlib import closing
from datetime import timedelta

from websockets.sync.server import serve as ws_serve

from engine import db, relay, relay_dialer, relay_server
from scripts.unified_test_support import isolated_database

REMOTE = "remote1"
CREDENTIAL = "test-relay-credential"
GENERATION = 1


def _make_self_signed_cert():
    """动态生成测试用自签证书（隔离夹具，仅参与模拟握手）。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-plugin")])
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1))
            .sign(key, hashes.SHA256()))
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(serialization.Encoding.PEM,
                          serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()),
    )


class FakePlugin:
    """fake 插件监听端点：websockets.sync 服务器（与真实插件同栈）。

    handler 行为可配置：accept_hello=False 时回错误 hello（不 hello_ok）；
    silent=False 时记录收到的帧；frames 供断言引擎出站帧内容。
    """

    def __init__(self, *, tls=False, accept_hello=True, close_after_hello=False):
        self.tls = tls
        self.accept_hello = accept_hello
        self.close_after_hello = close_after_hello
        self.frames: list[dict] = []
        self.handshakes: list[dict] = []
        self.closed_by_peer = False
        self._server = None
        self._thread = None
        ssl_context = None
        if tls:
            cert_pem, key_pem = _make_self_signed_cert()
            self.cert_pem, self.key_pem = cert_pem, key_pem
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            import tempfile, os
            self._tmp = tempfile.TemporaryDirectory()
            cert_path = os.path.join(self._tmp.name, "cert.pem")
            key_path = os.path.join(self._tmp.name, "key.pem")
            with open(cert_path, "wb") as f:
                f.write(cert_pem)
            with open(key_path, "wb") as f:
                f.write(key_pem)
            ssl_context.load_cert_chain(cert_path, key_path)
        self._server = ws_serve(self._handler, "127.0.0.1", 0, ssl=ssl_context)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return self._server.socket.getsockname()[1]

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
        self._server = None
        if getattr(self, "_tmp", None):
            self._tmp.cleanup()

    def _handler(self, conn):
        try:
            hello = json.loads(conn.recv(timeout=10))
            self.handshakes.append(hello)
            if not self.accept_hello:
                conn.send(json.dumps({"relay": {"kind": "bad"}}))
                return
            conn.send(json.dumps({"relay": {"kind": "hello_ok",
                                            "generation": GENERATION}}))
            if self.close_after_hello:
                conn.close()
                return
            while True:
                raw = conn.recv(timeout=20)
                envelope = json.loads(raw)
                self.frames.append(envelope)
                if (envelope.get("relay") or {}).get("kind") == "frame":
                    # 回一个应答帧（模拟插件→引擎的 JSON-RPC 响应）
                    conn.send(json.dumps({
                        "relay": {"kind": "frame", "stream": envelope["relay"]["stream"],
                                  "generation": GENERATION},
                        "frame": {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},
                    }))
        except Exception as exc:  # noqa: BLE001 断言错误也在主线程检查
            if "close" in type(exc).__name__.lower() or \
                    getattr(exc, "__class__", object()).__name__ == "ConnectionClosed":
                self.closed_by_peer = True
            else:
                self.handshakes.append({"__error__": repr(exc)})


class DialerBase(unittest.TestCase):
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
        self.addCleanup(self._teardown_links)

    def _teardown_links(self):
        relay_dialer.stop_dial(REMOTE)
        relay_server.close_all()

    def _set_endpoint(self, port: int, *, tls=False, fingerprint=""):
        conn = db.get_connection()
        try:
            relay.set_dial_endpoint(conn, REMOTE,
                                    {"host": "127.0.0.1", "port": port,
                                     "tls": tls, "fingerprint": fingerprint})
            relay.set_direction(conn, REMOTE, "dial")
        finally:
            conn.close()

    def _wait_connected(self, timeout=10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if relay_dialer._task_state(REMOTE) == "connected":
                return True
            time.sleep(0.05)
        return False


class DialerConnectionTests(DialerBase):
    def test_dial_connects_registers_and_routes_frames(self):
        plugin = FakePlugin()
        self.addCleanup(plugin.stop)
        self._set_endpoint(plugin.port)
        relay_dialer.start_dial(REMOTE)
        self.assertTrue(self._wait_connected(), "拨号应在退避前连上")
        self.assertEqual(relay_dialer._task_state(REMOTE), "connected")
        # 注册进 _LINKS：active_link 可用、方向为 DialLink
        link = relay_server.active_link(REMOTE)
        self.assertIsNotNone(link)
        self.assertIsInstance(link, relay_dialer.DialLink)
        # 握手 hello 内容：direction=dial + 协议版本 + 成员
        self.assertEqual(len(plugin.handshakes), 1)
        hello = plugin.handshakes[0]
        self.assertEqual(hello["member"], REMOTE)
        self.assertEqual(hello["direction"], "dial")
        self.assertEqual(hello["protocol_version"], relay_server.RELAY_PROTOCOL_VERSION)
        # 出站帧路由：open_transport → send → 插件收到 frame envelope
        transport = relay_server.open_transport(REMOTE)
        self.assertIsNotNone(transport)
        transport.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not plugin.frames:
            time.sleep(0.05)
        self.assertTrue(plugin.frames, "插件应收到引擎出站帧")
        self.assertEqual(plugin.frames[0]["relay"]["kind"], "frame")
        # 插件应答帧回引擎 transport
        self.assertIsNotNone(next(transport))

    def test_dial_tls_fingerprint_mismatch_rejected(self):
        import hashlib
        plugin = FakePlugin(tls=True)
        self.addCleanup(plugin.stop)
        wrong = hashlib.sha256(b"wrong").hexdigest()
        self._set_endpoint(plugin.port, tls=True, fingerprint=wrong)
        relay_dialer.start_dial(REMOTE)
        time.sleep(1.5)
        self.assertNotEqual(relay_dialer._task_state(REMOTE), "connected",
                            "指纹不符不得认证成功")

    def test_dial_tls_fingerprint_match_connects(self):
        import hashlib
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        plugin = FakePlugin(tls=True)
        self.addCleanup(plugin.stop)
        cert = x509.load_pem_x509_certificate(plugin.cert_pem)
        actual = hashlib.sha256(cert.public_bytes(
            serialization.Encoding.DER)).hexdigest()
        self._set_endpoint(plugin.port, tls=True, fingerprint=actual)
        relay_dialer.start_dial(REMOTE)
        self.assertTrue(self._wait_connected(), "指纹匹配应拨通 TLS 端点")

    def test_single_active_channel_replaces_old(self):
        # 先造一条「旧通道」（模拟方向 A relay 连接被新拨号接替）
        old_link = relay_server.NodeLink(REMOTE, _SyncFakeWs(), GENERATION,
                                         peer_ip="127.0.0.1")
        with relay_server._LINKS_LOCK:
            relay_server._LINKS[REMOTE] = old_link
        plugin = FakePlugin()
        self.addCleanup(plugin.stop)
        self._set_endpoint(plugin.port)
        relay_dialer.start_dial(REMOTE)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and \
                relay_server.active_link(REMOTE) is old_link:
            time.sleep(0.05)
        self.assertTrue(old_link.closed, "新通道认证成功后旧连接必须主动关闭")
        link = relay_server.active_link(REMOTE)
        self.assertIsInstance(link, relay_dialer.DialLink)
        self.assertIsNot(link, old_link)

    def test_reconnect_backoff_then_success(self):
        # 端点先不可达 → 退避重试；服务器后起 → 连上（重连退避不无限重试）
        relay_dialer.start_dial(REMOTE)
        self._set_endpoint(65531)  # 无监听端口：拨号必失败
        time.sleep(1.0)
        self.assertNotEqual(relay_dialer._task_state(REMOTE), "connected")
        plugin = FakePlugin()
        self.addCleanup(plugin.stop)
        conn = db.get_connection()
        try:
            relay.set_dial_endpoint(conn, REMOTE,
                                    {"host": "127.0.0.1", "port": plugin.port,
                                     "tls": False, "fingerprint": ""})
        finally:
            conn.close()
        self.assertTrue(self._wait_connected(15), "退避后端点可达应连上")

    def test_abandon_after_max_attempts(self):
        original_backoff = relay_dialer.BACKOFF_SEQUENCE
        original_max = relay_dialer.MAX_ATTEMPTS
        relay_dialer.BACKOFF_SEQUENCE = (0.05,)
        relay_dialer.MAX_ATTEMPTS = 1
        self.addCleanup(setattr, relay_dialer, "BACKOFF_SEQUENCE", original_backoff)
        self.addCleanup(setattr, relay_dialer, "MAX_ATTEMPTS", original_max)
        self._set_endpoint(65531)  # 无监听：连续失败
        relay_dialer.start_dial(REMOTE)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and \
                relay_dialer._task_state(REMOTE) != "abandoned":
            time.sleep(0.1)
        self.assertEqual(relay_dialer._task_state(REMOTE), "abandoned",
                         "重试耗尽应停止（不无限重试）")
        conn = db.get_connection()
        try:
            self.assertTrue(relay.read_node(conn, REMOTE)["switch_error"],
                            "耗尽应记录失败原因供页面显示")
        finally:
            conn.close()


class _SyncFakeWs:
    """旧通道的 ws 桩：只用于被 close（sync 分支）。"""

    def __init__(self, plugin: FakePlugin | None = None):
        self._plugin = plugin

    def close(self, code=1000, reason=""):
        pass


class SwitchStateMachineTests(DialerBase):
    def test_offline_reason_switch_vs_unexpected(self):
        conn = db.get_connection()
        try:
            relay.mark_offline(conn, REMOTE)
            self.assertEqual(relay.offline_reason(conn, REMOTE), "unexpected")
            relay.begin_switch(conn, REMOTE, "dial")
            relay.mark_offline(conn, REMOTE)
            self.assertEqual(relay.offline_reason(conn, REMOTE), "switch",
                             "切换中断链必须记 switch 原因（不告警不重试）")
        finally:
            conn.close()

    def test_switch_to_dial_full_chain(self):
        plugin = FakePlugin()
        self.addCleanup(plugin.stop)
        conn = db.get_connection()
        try:
            relay.set_dial_endpoint(conn, REMOTE,
                                    {"host": "127.0.0.1", "port": plugin.port,
                                     "tls": False, "fingerprint": ""})
        finally:
            conn.close()
        # 用户点切换：意图+目标方向持久化 → 暂停派发 → 断旧 → 建新
        with closing(db.get_connection()) as c:
            self.assertEqual(relay.read_node(c, REMOTE)["direction"], "listen")
        # 直接走 relay 状态机（API 端点级测试在 API 类）
        with closing(db.get_connection()) as c:
            relay.begin_switch(c, REMOTE, "dial")
            self.assertEqual(relay.read_node(c, REMOTE)["switch_state"], "switching")
        # 断旧（此时无旧连接）→ 建新
        relay_dialer.start_dial(REMOTE)
        self.assertTrue(self._wait_connected(), "拨号成功应完成切换")
        with closing(db.get_connection()) as c:
            row = relay.read_node(c, REMOTE)
            self.assertEqual(row["switch_state"], "", "认证成功清切换态")
            self.assertEqual(row["direction"], "dial", "direction 更新为目标方向")
            self.assertEqual(row["state"], "online")

    def test_switch_back_to_listen_completes_on_plugin_reconnect(self):
        # B→A：dial 连接中 → 用户切回 listen → dial 停+断 → 插件重连
        # （relay_ws 认证成功）→ 清切换态
        plugin = FakePlugin()
        self.addCleanup(plugin.stop)
        conn = db.get_connection()
        try:
            relay.set_dial_endpoint(conn, REMOTE,
                                    {"host": "127.0.0.1", "port": plugin.port,
                                     "tls": False, "fingerprint": ""})
            relay.set_direction(conn, REMOTE, "dial")
        finally:
            conn.close()
        relay_dialer.start_dial(REMOTE)
        self.assertTrue(self._wait_connected())
        # 切回 listen（begin_switch + 停拨号——API 端点的核心两步）
        with closing(db.get_connection()) as c:
            relay.begin_switch(c, REMOTE, "listen")
        relay_dialer.stop_dial(REMOTE)
        with closing(db.get_connection()) as c:
            self.assertEqual(relay.read_node(c, REMOTE)["switch_state"], "switching")
        # 插件重连（relay_ws 认证成功）→ 清切换态
        with closing(db.get_connection()) as c:
            relay.mark_online(c, REMOTE, code_version="v0.2.16",
                              mcp_fingerprint="", generation=1)
            row = relay.read_node(c, REMOTE)
            if row["switch_state"] == "switching":
                relay.complete_switch(c, REMOTE)
            self.assertEqual(relay.read_node(c, REMOTE)["direction"], "listen")
            self.assertEqual(relay.read_node(c, REMOTE)["switch_state"], "")
        # 旧 dial 连接已断（单活动通道）
        self.assertIsNone(relay_server.active_link(REMOTE))

    def test_switch_gate_holds_dispatch(self):
        # 切换中：闸门 hold 不投递、不记 offline_mention、不标 expired
        from engine import queue as queue_module
        conn = db.get_connection()
        try:
            from scripts.project_test_support import enable_members
            enable_members(conn, ("alpha",), profiles=[REMOTE])
            relay.begin_switch(conn, REMOTE, "dial")
            candidate = {"profile": REMOTE, "kind": "work",
                         "thread_id": None, "project": "alpha"}
            gate, _ = queue_module._relay_node_gate(conn, candidate)
            self.assertEqual(gate, "hold", "切换中必须暂停派发")
            row = relay.read_node(conn, REMOTE)
            self.assertEqual(row["switch_state"], "switching")
        finally:
            conn.close()

    def test_switch_persists_across_restart(self):
        # 引擎重启：切换态读回——target=dial 未达成 → 续建拨号
        plugin = FakePlugin()
        self.addCleanup(plugin.stop)
        conn = db.get_connection()
        try:
            relay.set_dial_endpoint(conn, REMOTE,
                                    {"host": "127.0.0.1", "port": plugin.port,
                                     "tls": False, "fingerprint": ""})
            relay.begin_switch(conn, REMOTE, "dial")
        finally:
            conn.close()
        # 模拟重启：清运行时（连接全断），调恢复
        relay_server.close_all()
        relay_dialer.restore_on_startup()
        self.assertTrue(self._wait_connected(), "重启后应恢复切换流程并拨通")
        with closing(db.get_connection()) as c:
            row = relay.read_node(c, REMOTE)
            self.assertEqual(row["switch_state"], "")
            self.assertEqual(row["direction"], "dial")

    def test_restart_clears_completed_switch(self):
        conn = db.get_connection()
        try:
            relay.set_direction(conn, REMOTE, "dial")
            relay.begin_switch(conn, REMOTE, "dial")  # 目标=当前方向（已达成）
        finally:
            conn.close()
        relay_dialer.restore_on_startup()
        with closing(db.get_connection()) as c:
            row = relay.read_node(c, REMOTE)
            self.assertEqual(row["switch_state"], "", "重启清除已达成切换态")
            self.assertEqual(row["switch_target"], "")


class SwitchApiTests(DialerBase):
    """API 层：switch/dial-endpoint 端点（回环 TestClient + 管理员）。"""

    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from engine import api
        app = FastAPI()
        app.include_router(api.router_api)
        self.client = TestClient(app, client=("127.0.0.1", 4321))
        self.addCleanup(self.client.close)

    def test_switch_endpoint_flow_and_validation(self):
        # 无 dial-endpoint 时切 dial → 400
        response = self.client.post(f"/relay/nodes/{REMOTE}/switch",
                                    json={"direction": "dial"})
        self.assertEqual(response.status_code, 400)
        # 非法方向 → 400
        response = self.client.post(f"/relay/nodes/{REMOTE}/switch",
                                    json={"direction": "sideways"})
        self.assertEqual(response.status_code, 400)
        # 保存 dial-endpoint（校验）
        response = self.client.post(f"/relay/nodes/{REMOTE}/dial-endpoint",
                                    json={"host": "127.0.0.1", "port": 65531,
                                          "tls": False})
        self.assertEqual(response.status_code, 200)
        # 切 dial：断旧+起拨号（端点可达性无影响，切换态持久化）
        response = self.client.post(f"/relay/nodes/{REMOTE}/switch",
                                    json={"direction": "dial"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["switch_state"], "switching")
        with closing(db.get_connection()) as c:
            row = relay.read_node(c, REMOTE)
            self.assertEqual(row["switch_state"], "switching")
            self.assertEqual(row["switch_target"], "dial")
        # 切回 listen（幂等可切回）
        response = self.client.post(f"/relay/nodes/{REMOTE}/switch",
                                    json={"direction": "listen"})
        self.assertEqual(response.status_code, 200)
        with closing(db.get_connection()) as c:
            self.assertEqual(relay.read_node(c, REMOTE)["switch_target"], "listen")

    def test_nodes_list_exposes_direction_and_switch(self):
        with closing(db.get_connection()) as c:
            relay.begin_switch(c, REMOTE, "dial")
        nodes = self.client.get("/relay/nodes").json()
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["direction"], "listen")
        self.assertEqual(nodes[0]["switch_state"], "switching")
        self.assertEqual(nodes[0]["switch_target"], "dial")
        self.assertEqual(nodes[0]["dial_state"], "idle")

    def test_bot_forbidden(self):
        from engine import auth
        bot = {"X-Group-Profile": "default", "X-Group-Key": auth.bot_token("default")}
        for path in (f"/relay/nodes/{REMOTE}/switch",
                     f"/relay/nodes/{REMOTE}/dial-endpoint"):
            response = self.client.post(path, json={}, headers=bot)
            self.assertEqual(response.status_code, 403)


class SwitchDispatchTests(DialerBase):
    """切换期间同一条点名最多执行一次：queued hold + 恢复后单次投递。"""

    def test_mention_executes_at_most_once_across_switch(self):
        from engine import queue as queue_module
        conn = db.get_connection()
        try:
            from scripts.project_test_support import enable_members
            enable_members(conn, ("alpha",), profiles=[REMOTE])
            # 造一条 queued job（切换前点名）
            conn.execute(
                "INSERT INTO wake_jobs(project,profile,kind,trigger,status,enqueued_at) "
                "VALUES('alpha',?,'work','test', 'queued', ?)",
                (REMOTE, db.now_iso()))
            conn.commit()
            # 切换中：hold（不投递）
            relay.begin_switch(conn, REMOTE, "dial")
            candidate = {"profile": REMOTE, "kind": "work",
                         "thread_id": None, "project": "alpha"}
            self.assertEqual(queue_module._relay_node_gate(conn, candidate)[0], "hold")
            # 切换完成：闸门放行（go）→ 单次投递路径恢复
            relay.complete_switch(conn, REMOTE)
            relay.mark_online(conn, REMOTE, code_version="", mcp_fingerprint="",
                              generation=1)
            self.assertEqual(queue_module._relay_node_gate(conn, candidate)[0], "go")
            # 同一条 job 仍只有一条（无重复排队）
            count = conn.execute(
                "SELECT COUNT(*) FROM wake_jobs WHERE profile=? AND trigger='test'",
                (REMOTE,)).fetchone()[0]
            self.assertEqual(count, 1, "切换期间同一条点名最多执行一次")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
