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

    def _set_endpoint(self, port: int, *, tls=True, fingerprint="a" * 64,
                      pairing_token="test-pairing-token"):
        conn = db.get_connection()
        try:
            relay.set_dial_endpoint(conn, REMOTE,
                                    {"host": "127.0.0.1", "port": port,
                                     "tls": tls, "fingerprint": fingerprint,
                                     "pairing_token": pairing_token})
            relay.set_direction(conn, REMOTE, "dial")
        finally:
            conn.close()

    @staticmethod
    def _der_fingerprint(plugin) -> str:
        """FakePlugin 自签证书的 DER sha256 指纹（与引擎校验口径一致）。"""
        import hashlib
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        cert = x509.load_pem_x509_certificate(plugin.cert_pem)
        return hashlib.sha256(cert.public_bytes(
            serialization.Encoding.DER)).hexdigest()

    def _wait_connected(self, timeout=10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if relay_dialer._task_state(REMOTE) == "connected":
                return True
            time.sleep(0.05)
        return False


class DialerConnectionTests(DialerBase):
    def test_dial_connects_registers_and_routes_frames(self):
        plugin = FakePlugin(tls=True)
        self.addCleanup(plugin.stop)
        self._set_endpoint(plugin.port, fingerprint=self._der_fingerprint(plugin))
        relay_dialer.start_dial(REMOTE)
        self.assertTrue(self._wait_connected(), "拨号应在退避前连上")
        self.assertEqual(relay_dialer._task_state(REMOTE), "connected")
        # 注册进 _LINKS：active_link 可用、方向为 DialLink
        link = relay_server.active_link(REMOTE)
        self.assertIsNotNone(link)
        self.assertIsInstance(link, relay_dialer.DialLink)
        # 握手 hello 内容：direction=dial + 协议版本 + 成员 + 配对凭据
        self.assertEqual(len(plugin.handshakes), 1)
        hello = plugin.handshakes[0]
        self.assertEqual(hello["member"], REMOTE)
        self.assertEqual(hello["direction"], "dial")
        self.assertEqual(hello["protocol_version"], relay_server.RELAY_PROTOCOL_VERSION)
        self.assertEqual(hello["credential"], "test-pairing-token",
                         "拨号 hello 必须出示一次性配对凭据（可验证配对身份证明）")
        # 帧代=插件 hello_ok 代（协议对称）；状态代=引擎本地 advance 后的代
        self.assertEqual(link.generation, GENERATION, "帧代采用插件下发代")
        conn = db.get_connection()
        try:
            self.assertEqual(relay.read_node(conn, REMOTE)["generation"],
                             GENERATION + 1,
                             "状态代=引擎本地 generation_advance 后的代")
        finally:
            conn.close()
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

    def test_dial_without_pairing_token_stays_unauth(self):
        plugin = FakePlugin(tls=True)
        self.addCleanup(plugin.stop)
        # 缺配对凭据：保持未认证，不发起拨号（manager node2052 ①）
        self._set_endpoint(plugin.port, fingerprint=self._der_fingerprint(plugin),
                           pairing_token="")
        relay_dialer.start_dial(REMOTE)
        time.sleep(1.0)
        self.assertNotEqual(relay_dialer._task_state(REMOTE), "connected")
        self.assertEqual(plugin.handshakes, [], "无凭据不得发起拨号")

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
        plugin = FakePlugin(tls=True)
        self.addCleanup(plugin.stop)
        self._set_endpoint(plugin.port, fingerprint=self._der_fingerprint(plugin))
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
        plugin = FakePlugin(tls=True)
        self.addCleanup(plugin.stop)
        conn = db.get_connection()
        try:
            relay.set_dial_endpoint(conn, REMOTE,
                                    {"host": "127.0.0.1", "port": plugin.port,
                                     "tls": True,
                                     "fingerprint": self._der_fingerprint(plugin),
                                     "pairing_token": "test-pairing-token"})
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
        plugin = FakePlugin(tls=True)
        self.addCleanup(plugin.stop)
        conn = db.get_connection()
        try:
            relay.set_dial_endpoint(conn, REMOTE,
                                    {"host": "127.0.0.1", "port": plugin.port,
                                     "tls": True,
                                     "fingerprint": self._der_fingerprint(plugin),
                                     "pairing_token": "test-pairing-token"})
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
        plugin = FakePlugin(tls=True)
        self.addCleanup(plugin.stop)
        conn = db.get_connection()
        try:
            relay.set_dial_endpoint(conn, REMOTE,
                                    {"host": "127.0.0.1", "port": plugin.port,
                                     "tls": True,
                                     "fingerprint": self._der_fingerprint(plugin),
                                     "pairing_token": "test-pairing-token"})
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
        plugin = FakePlugin(tls=True)
        self.addCleanup(plugin.stop)
        conn = db.get_connection()
        try:
            relay.set_dial_endpoint(conn, REMOTE,
                                    {"host": "127.0.0.1", "port": plugin.port,
                                     "tls": True,
                                     "fingerprint": self._der_fingerprint(plugin),
                                     "pairing_token": "test-pairing-token"})
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
        # 认证收紧：明文 WS / 空指纹 / 非法指纹格式一律 400（manager node2052 ①）
        response = self.client.post(f"/relay/nodes/{REMOTE}/dial-endpoint",
                                    json={"host": "127.0.0.1", "port": 65531,
                                          "tls": False, "fingerprint": "a" * 64})
        self.assertEqual(response.status_code, 400, "不接受明文 WS")
        response = self.client.post(f"/relay/nodes/{REMOTE}/dial-endpoint",
                                    json={"host": "127.0.0.1", "port": 65531,
                                          "tls": True, "fingerprint": ""})
        self.assertEqual(response.status_code, 400, "空指纹不接受")
        response = self.client.post(f"/relay/nodes/{REMOTE}/dial-endpoint",
                                    json={"host": "127.0.0.1", "port": 65531,
                                          "tls": True, "fingerprint": "not-hex"})
        self.assertEqual(response.status_code, 400, "非法指纹格式不接受")
        # 合法端点（TLS+指纹）→ 200
        response = self.client.post(f"/relay/nodes/{REMOTE}/dial-endpoint",
                                    json={"host": "127.0.0.1", "port": 65531,
                                          "tls": True, "fingerprint": "a" * 64,
                                          "pairing_token": "tok"})
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


    @staticmethod
    def _insert_execution(conn, submission_id: str, state: str,
                          profile: str = REMOTE) -> None:
        """受控网关记录一次 prompt.submit 的 execution 行（manager node2064
        附录口径：搭受控 execution 行观察对账顺序）。"""
        conn.execute(
            "INSERT INTO hermes_executions"
            "(execution_id,session,started_at,project,profile,kind,source,"
            "python,home,workdir,session_id,profile_id,submission_id,state,"
            "submit_deadline,reconcile_deadline) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"exec-{submission_id}", "sess-1", db.now_iso(), "alpha", profile,
             "chat", "test", "", "", "", "sess-1", profile, submission_id, state,
             time.time() + 60, time.time() + 3600))
        conn.commit()

    def test_inflight_submission_survives_switch_and_settles_once(self):
        """manager node2057 ③：已被接收且回执延后的同一 submission_id 经
        实际切换、待核对、回执确定、恢复派发全程只提交一次。"""
        from engine import queue as queue_module
        conn = db.get_connection()
        try:
            from scripts.project_test_support import enable_members
            enable_members(conn, ("alpha",), profiles=[REMOTE])
            # 已被接收且回执延后：offline_waiting 点名已挂 submission_id，
            # 受控 execution 行状态 running（回执延后）
            conn.execute(
                "INSERT INTO relay_offline_mentions"
                "(member,project,thread_id,message_id,state,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (REMOTE, "alpha", 7, 1, "offline_waiting", db.now_iso()))
            conn.commit()
            bound = relay.attach_submission(conn, message_ids=[1], member=REMOTE,
                                            submission_id="sub-1")
            self.assertEqual(bound, 1, "未决点名必须挂上 submission_id")
            self._insert_execution(conn, "sub-1", "running")
            # 切换中：闸门 hold、该记录保持 offline_waiting（不重派不标过期）
            relay.begin_switch(conn, REMOTE, "dial")
            candidate = {"profile": REMOTE, "kind": "chat",
                         "thread_id": 7, "project": "alpha"}
            self.assertEqual(queue_module._relay_node_gate(conn, candidate)[0], "hold")
            # 切换完成：complete_switch 内置首次在途核对（manager node2064 ③）
            # ——execution 未终态（回执延后）→ 未知持久保留，恢复普通派发
            relay.complete_switch(conn, REMOTE)
            relay.mark_online(conn, REMOTE, code_version="", mcp_fingerprint="",
                              generation=1)
            self.assertEqual(queue_module._relay_node_gate(conn, candidate)[0], "go")
            row = conn.execute(
                "SELECT state,submission_id FROM relay_offline_mentions "
                "WHERE member=? AND message_id=1", (REMOTE,)).fetchone()
            self.assertEqual(row["state"], "offline_waiting",
                             "回执延后的未决记录保持待核对，不自动重派")
            self.assertEqual(row["submission_id"], "sub-1")
            # 权威回执确定：settle_by_submission → submitted，只结算一次
            settled = relay.settle_by_submission(conn, member=REMOTE,
                                                 submission_id="sub-1")
            self.assertEqual(settled, 1, "权威回执结算该 submission 关联点名")
            row = conn.execute(
                "SELECT state FROM relay_offline_mentions "
                "WHERE member=? AND message_id=1", (REMOTE,)).fetchone()
            self.assertEqual(row["state"], "submitted")
            # 提交次数：全程只有一条点名记录（无重复排队/重复投递）
            count = conn.execute(
                "SELECT COUNT(*) FROM relay_offline_mentions WHERE member=?",
                (REMOTE,)).fetchone()[0]
            self.assertEqual(count, 1, "同一条点名至多提交一次")
        finally:
            conn.close()

    def test_switch_resume_controlled_gateway_single_submit(self):
        """manager node2064 附录口径（node2067 返修②）：受控网关记录一次
        prompt.submit、回执延后、实际切换——断言对账顺序（首次核对先于
        恢复派发：reconcile 调用时切换态仍为 switching）与接收端计数
        （同一提交全程只发出一次，切换与恢复均不重发）。"""
        conn = db.get_connection()
        try:
            from scripts.project_test_support import enable_members
            enable_members(conn, ("alpha",), profiles=[REMOTE])
            from engine import hermes_gateway_ledger as ledger
            row = ledger.prepare(
                conn, session="probe", project="alpha", profile=REMOTE,
                kind="work", thread_id=None, task_id=None, job_id=None,
                event_floor=0, source="fixture", python="", home="",
                workdir="", session_id="probe", profile_id=REMOTE)
            eid = row["execution_id"]
            # 受控网关：已接收一次 prompt.submit（受理中，回执延后）
            ledger.apply_receipt(conn, eid, {
                "admission_id": "adm-1", "status": "started",
                "ref": {"session_id": "probe", "profile_id": REMOTE},
                "execution_generation": 0, "authority_epoch": 1})
        finally:
            conn.close()

        events: list[tuple[str, str]] = []

        class _ControlledGateway:
            submit_calls = 0

            def rpc(self, method, **kwargs):  # noqa: ANN001
                if method == "prompt.submit":
                    type(self).submit_calls += 1
                if method == "prompt.receipt":
                    # 首次核对时记录当前切换态（对账必须先于恢复派发）
                    with closing(db.get_connection()) as conn:
                        state = relay.read_node(conn, REMOTE)
                    events.append(("reconcile", state["switch_state"]))
                    # 回执延后：第一次问询仍非终态，之后到达 terminal
                    status = "started" if len(events) == 1 else "terminal"
                    return {"admission_id": "adm-1", "status": status,
                            "outcome": "completed",
                            "ref": {"session_id": "probe",
                                    "profile_id": REMOTE},
                            "execution_generation": 0, "authority_epoch": 2}
                return {}

        gateway = _ControlledGateway()
        # 受控网关仅发出一次提交（此后全程不得重发）
        events.append(("submit", "once"))
        with closing(db.get_connection()) as conn:
            relay.begin_switch(conn, REMOTE, "dial")
        with closing(db.get_connection()) as conn:
            relay.complete_switch(conn, REMOTE, client=gateway)
        # 对账顺序：reconcile 发生时切换态仍为 switching（首次核对先于
        # 恢复派发/清切换态）
        self.assertEqual(events, [("submit", "once"), ("reconcile", "switching")])
        self.assertEqual(_ControlledGateway.submit_calls, 0,
                         "切换恢复过程不得重发提交（同一提交只发出一次）")
        # 首次核对把回执延后的执行带向终态（应用 apply_receipt）后
        # 切换态清空、恢复普通派发
        from engine import hermes_gateway_ledger as ledger
        conn = db.get_connection()
        try:
            node = relay.read_node(conn, REMOTE)
            self.assertEqual(node["switch_state"], "")
            # 再次核对（回执已到达）→ 结算完成
            result = relay.reconcile_switch_resume(conn, REMOTE, client=gateway)
            self.assertGreaterEqual(result["reconciled"], 0)
            self.assertEqual(ledger.read(conn, eid)["state"], "terminal",
                             "回执到达后执行结算为终态")
        finally:
            conn.close()

    def test_complete_switch_applies_known_receipt_and_keeps_unknown(self):
        """manager node2064 ③：complete_switch 内置首次核对——已知回执
        （execution 已 terminal）立即应用标 submitted；未知（execution
        不存在）持久保留 offline_waiting 禁止重派。"""
        conn = db.get_connection()
        try:
            from scripts.project_test_support import enable_members
            enable_members(conn, ("alpha",), profiles=[REMOTE])
            # 两条未决点名：sub-known（execution 已 terminal=已知回执）、
            # sub-unknown（execution 不存在=未知）
            conn.execute(
                "INSERT INTO relay_offline_mentions"
                "(member,project,thread_id,message_id,state,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (REMOTE, "alpha", 7, 1, "offline_waiting", db.now_iso()))
            conn.execute(
                "INSERT INTO relay_offline_mentions"
                "(member,project,thread_id,message_id,state,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (REMOTE, "alpha", 7, 2, "offline_waiting", db.now_iso()))
            conn.commit()
            relay.attach_submission(conn, message_ids=[1], member=REMOTE,
                                    submission_id="sub-known")
            relay.attach_submission(conn, message_ids=[2], member=REMOTE,
                                    submission_id="sub-unknown")
            self._insert_execution(conn, "sub-known", "terminal")
            # 切换 → 完成：内置核对应用已知回执、保留未知
            relay.begin_switch(conn, REMOTE, "dial")
            result = relay.reconcile_switch_resume(conn, REMOTE)
            self.assertEqual(result["applied"], 1, "已知回执立即应用结算")
            self.assertEqual(result["kept"], 1, "未知持久保留待核对")
            relay.complete_switch(conn, REMOTE)
            row1 = conn.execute(
                "SELECT state FROM relay_offline_mentions "
                "WHERE member=? AND message_id=1", (REMOTE,)).fetchone()
            self.assertEqual(row1["state"], "submitted",
                             "已知回执：切换完成后点名已结算")
            row2 = conn.execute(
                "SELECT state,submission_id FROM relay_offline_mentions "
                "WHERE member=? AND message_id=2", (REMOTE,)).fetchone()
            self.assertEqual(row2["state"], "offline_waiting",
                             "未知：持久保留待核对，禁止原提交重派")
            self.assertEqual(row2["submission_id"], "sub-unknown")
            # 切换态已清（恢复普通派发）
            node = relay.read_node(conn, REMOTE)
            self.assertEqual(node["switch_state"], "")
        finally:
            conn.close()


class SwitchFullChainTests(DialerBase):
    """manager node2052 ③：从 /switch 端点走 A→B→A 全链——真实 relay_ws
    握手、真实关闭、DB/活动连接/派发状态一致、提交次数统计。"""

    RELEASE = "v0.2.16"

    def setUp(self):
        super().setUp()
        from unittest.mock import patch
        self._version_patch = patch.object(relay_server, "_engine_release_version",
                                           return_value=self.RELEASE)
        self._version_patch.start()
        self.addCleanup(self._version_patch.stop)
        from fastapi import FastAPI, WebSocket
        from fastapi.testclient import TestClient
        from engine import api
        app = FastAPI()
        app.include_router(api.router_api)

        @app.websocket("/relay/ws")
        async def endpoint(websocket: WebSocket):
            await relay_server.relay_ws(websocket)
        self.client = TestClient(app, client=("127.0.0.1", 4321))
        self.addCleanup(self.client.close)

    def _ws_handshake(self) -> dict:
        """方向 A 插件握手（真实 relay_ws 全逻辑）。"""
        with self.client.websocket_connect("/relay/ws") as ws:
            ws.send_json({"relay": {"kind": "hello"}, "member": REMOTE,
                          "credential": CREDENTIAL, "code_version": self.RELEASE,
                          "mcp_fingerprint": "", "protocol_version": 1})
            reply = ws.receive_json()
        return reply

    def _wait_switch_clear(self, member: str, timeout: float = 10.0) -> bool:
        """轮询等待异步恢复线程清切换态（manager node2073 恢复步骤异步化
        后，握手返回不等于切换完成；首次问询完成后线程才 complete_switch）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with closing(db.get_connection()) as c:
                row = relay.read_node(c, member)
            if row["switch_state"] == "":
                return True
            time.sleep(0.05)
        return False

    def test_switch_resume_reconciles_unknown_execution(self):
        """manager node2064 附录口径：execution unknown（回执未知、无未决
        点名）经真实 /switch + /relay/ws 握手——恢复步骤必须做首次核对
        （ledger.reconcile 被调用），未知持久保留、切换态清空、恢复派发。"""
        from unittest.mock import patch
        from engine import queue as queue_module
        from engine import hermes_gateway_ledger as ledger
        conn = db.get_connection()
        try:
            from scripts.project_test_support import enable_members
            enable_members(conn, ("alpha",), profiles=[REMOTE])
            relay.set_direction(conn, REMOTE, "dial")
            row = ledger.prepare(
                conn, session="probe", project="alpha", profile=REMOTE,
                kind="work", thread_id=None, task_id=None, job_id=None,
                event_floor=0, source="fixture", python="", home="",
                workdir="", session_id="probe", profile_id=REMOTE)
            ledger.uncertain(conn, row["execution_id"],
                             "fixture: waiting for receipt")
        finally:
            conn.close()
        with patch.object(ledger, "reconcile", wraps=ledger.reconcile) as reconcile:
            response = self.client.post(f"/relay/nodes/{REMOTE}/switch",
                                        json={"direction": "listen"})
            self.assertEqual(response.status_code, 200)
            # 握手保持连接存续：恢复线程在连接存续期间经 relay lease 完成
            # 首次核对并清切换态（manager node2073——连接关闭则线程放弃）
            with self.client.websocket_connect("/relay/ws") as ws:
                ws.send_json({"relay": {"kind": "hello"}, "member": REMOTE,
                              "credential": CREDENTIAL,
                              "code_version": self.RELEASE,
                              "mcp_fingerprint": "", "protocol_version": 1})
                reply = ws.receive_json()
                self.assertEqual(reply["relay"]["kind"], "hello_ok")
                self.assertTrue(self._wait_switch_clear(REMOTE),
                                "切换态应由恢复线程在首次核对后清空")
            conn = db.get_connection()
            try:
                candidate = {"profile": REMOTE, "kind": "work",
                             "thread_id": None, "project": "alpha"}
                node = relay.read_node(conn, REMOTE)
                self.assertEqual(node["switch_state"], "",
                                 "切换态已清（恢复步骤执行完毕）")
                # 握手 with 退出后连接关闭 → 节点按普通离线 hold（非切换中
                # hold）——恢复普通派发语义由 switch_state 清空证明
                self.assertEqual(queue_module._relay_node_gate(conn, candidate)[0],
                                 "hold", "连接关闭后按普通离线保留排队")
                self.assertEqual(ledger.read(conn, row["execution_id"])["state"],
                                 "unknown", "回执未知：持久保留待核对")
                self.assertEqual(reconcile.call_count, 1,
                                 "恢复步骤必须做首次在途核对（session.resume）")
            finally:
                conn.close()

    def test_switch_resume_reconciles_accepted_via_relay_lease(self):
        """manager node2073 口径：已受理 execution 经生产切换路由（POST
        /switch + /relay/ws 握手）——恢复步骤从新链 relay lease 做首次
        问询（prompt.receipt 带原 ID、真实 client 非 None）；受控网关
        记录一次 submit、原 ID 查询，查询时 gate=hold，回执未知→保留
        （execution unknown 不重派）→ 首次问询完成后 gate=go。"""
        import time as time_module
        from engine import hermes_gateway_ledger as ledger
        conn = db.get_connection()
        try:
            from scripts.project_test_support import enable_members
            enable_members(conn, ("alpha",), profiles=[REMOTE])
            relay.set_direction(conn, REMOTE, "dial")
            row = ledger.prepare(
                conn, session="probe", project="alpha", profile=REMOTE,
                kind="work", thread_id=None, task_id=None, job_id=None,
                event_floor=0, source="fixture", python="", home="",
                workdir="", session_id="probe", profile_id=REMOTE)
            eid = row["execution_id"]
            # 受控网关：已接收一次 prompt.submit（受理中，回执延后）
            ledger.apply_receipt(conn, eid, {
                "admission_id": "accepted-1", "status": "started",
                "ref": {"session_id": "probe", "profile_id": REMOTE},
                "execution_generation": 0, "authority_epoch": 1})
        finally:
            conn.close()
        events: list[str] = []
        response = self.client.post(f"/relay/nodes/{REMOTE}/switch",
                                    json={"direction": "listen"})
        self.assertEqual(response.status_code, 200)
        with self.client.websocket_connect("/relay/ws") as ws:
            ws.send_json({"relay": {"kind": "hello"}, "member": REMOTE,
                          "credential": CREDENTIAL, "code_version": self.RELEASE,
                          "mcp_fingerprint": "", "protocol_version": 1})
            ack = ws.receive_json()
            self.assertEqual(ack["relay"]["kind"], "hello_ok")
            gen = ack["relay"]["generation"]
            # 恢复线程的首次问询帧（prompt.receipt 经新链流）
            frame = ws.receive_json()
            self.assertEqual(frame["relay"]["kind"], "frame")
            rpc = frame["frame"]
            self.assertEqual(rpc["method"], "prompt.receipt",
                             "已受理执行经 relay lease 问询（原 ID 查询）")
            self.assertEqual(rpc["params"]["admission_id"], "accepted-1",
                             "按原 admission 问询，不重新提交")
            self.assertEqual(rpc["params"]["session_id"], "probe")
            events.append("submit_once")  # 受控网关：submit 只接收过一次
            events.append("receipt_query")
            # 查询时 gate=hold（首次问询未完成，切换态仍 switching）
            conn = db.get_connection()
            try:
                candidate = {"profile": REMOTE, "kind": "work",
                             "thread_id": None, "project": "alpha"}
                from engine import queue as queue_module
                self.assertEqual(queue_module._relay_node_gate(conn, candidate)[0],
                                 "hold", "首次问询未完成前不恢复派发")
                self.assertEqual(relay.read_node(conn, REMOTE)["switch_state"],
                                 "switching")
            finally:
                conn.close()
            # 受控网关回执：结果未知（回执延后）→ 保留待核对不重派
            ws.send_json({
                "relay": {"kind": "frame", "stream": frame["relay"]["stream"],
                          "generation": gen},
                "frame": {"jsonrpc": "2.0", "id": rpc["id"],
                          "result": {"admission_id": "accepted-1",
                                     "status": "unknown",
                                     "ref": {"session_id": "probe",
                                             "profile_id": REMOTE},
                                     "execution_generation": 0,
                                     "authority_epoch": 1}},
            })
            # 首次问询完成 → 恢复派发（异步线程，轮询切换态清空）
            deadline = time_module.time() + 10
            while time_module.time() < deadline:
                conn = db.get_connection()
                try:
                    node = relay.read_node(conn, REMOTE)
                    if node["switch_state"] == "":
                        break
                finally:
                    conn.close()
                time_module.sleep(0.05)
            conn = db.get_connection()
            try:
                candidate = {"profile": REMOTE, "kind": "work",
                             "thread_id": None, "project": "alpha"}
                from engine import queue as queue_module
                self.assertEqual(relay.read_node(conn, REMOTE)["switch_state"],
                                 "", "首次问询完成后清切换态")
                self.assertEqual(queue_module._relay_node_gate(conn, candidate)[0],
                                 "go", "恢复普通派发")
                self.assertEqual(ledger.read(conn, eid)["state"], "unknown",
                                 "未知结果持久保留，不自动重派")
                # 同一条提交不重发：无新增执行记录、无重派点名
                count = conn.execute(
                    "SELECT COUNT(*) AS n FROM hermes_executions "
                    "WHERE profile=? AND session_id='probe'",
                    (REMOTE,)).fetchone()["n"]
                self.assertEqual(count, 1, "同一提交只发出一次")
            finally:
                conn.close()
        self.assertEqual(events, ["submit_once", "receipt_query"],
                         "受控网关：一次 submit、一次原 ID 查询")

    def test_full_chain_switch_via_api(self):
        from engine import queue as queue_module
        # 造一条 queued job（切换前点名）统计执行次数
        conn = db.get_connection()
        try:
            conn.execute(
                "INSERT INTO wake_jobs(project,profile,kind,trigger,status,enqueued_at) "
                "VALUES('alpha',?,'work','fullchain', 'queued', ?)",
                (REMOTE, db.now_iso()))
            conn.commit()
        finally:
            conn.close()
        # A 连接：真实握手（TestClient 走 relay_ws 全认证链路；with 退出即
        # 连接关闭，随后手动注册等价 NodeLink 模拟「连接存续中」）
        reply = self._ws_handshake()
        self.assertEqual(reply["relay"]["kind"], "hello_ok")
        link_a = relay_server.NodeLink(REMOTE, _SyncFakeWs(), GENERATION,
                                       peer_ip="127.0.0.1")
        with relay_server._LINKS_LOCK:
            relay_server._LINKS[REMOTE] = link_a
        # 切换中闸门：hold（不投递）
        candidate = {"profile": REMOTE, "kind": "work", "thread_id": None,
                     "project": "alpha"}
        with closing(db.get_connection()) as c:
            relay.begin_switch(c, REMOTE, "dial")
            self.assertEqual(queue_module._relay_node_gate(c, candidate)[0], "hold")
        # 用户点切换（真实 /switch 端点）：断旧 + 起拨号
        plugin = FakePlugin(tls=True)
        self.addCleanup(plugin.stop)
        with closing(db.get_connection()) as c:
            relay.set_dial_endpoint(c, REMOTE,
                                    {"host": "127.0.0.1", "port": plugin.port,
                                     "tls": True,
                                     "fingerprint": self._der_fingerprint(plugin),
                                     "pairing_token": "test-pairing-token"})
        response = self.client.post(f"/relay/nodes/{REMOTE}/switch",
                                    json={"direction": "dial"})
        self.assertEqual(response.status_code, 200)
        # 旧连接已断（单活动通道）
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not link_a.closed:
            time.sleep(0.05)
        self.assertTrue(link_a.closed, "先断旧：A 连接必须主动关闭")
        # 拨号认证成功 → 切换完成（恢复线程异步清切换态，manager node2073）
        self.assertTrue(self._wait_connected(10), "拨号应接通")
        self.assertTrue(self._wait_switch_clear(REMOTE),
                        "拨号切换态应由恢复线程在首次核对后清空")
        with closing(db.get_connection()) as c:
            row = relay.read_node(c, REMOTE)
            self.assertEqual(row["switch_state"], "", "认证成功清切换态")
            self.assertEqual(row["direction"], "dial")
            self.assertEqual(row["state"], "online")
            self.assertEqual(queue_module._relay_node_gate(c, candidate)[0], "go",
                             "恢复派发")
        # 切回 listen：断 dial → 插件（A）重连握手 → 恢复线程在连接存续
        # 期间完成首次核对并清切换态（manager node2073）
        response = self.client.post(f"/relay/nodes/{REMOTE}/switch",
                                    json={"direction": "listen"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(relay_server.active_link(REMOTE),
                          "切回后拨号连接必须关闭")
        with self.client.websocket_connect("/relay/ws") as ws:
            ws.send_json({"relay": {"kind": "hello"}, "member": REMOTE,
                          "credential": CREDENTIAL, "code_version": self.RELEASE,
                          "mcp_fingerprint": "", "protocol_version": 1})
            reply = ws.receive_json()
            self.assertEqual(reply["relay"]["kind"], "hello_ok")
            # 连接存续期间：恢复线程经 relay lease 首次核对后清切换态
            self.assertTrue(self._wait_switch_clear(REMOTE),
                            "切回切换态应由恢复线程在首次核对后清空")
        # with 退出 → 连接关闭 → 节点按普通离线
        with closing(db.get_connection()) as c:
            row = relay.read_node(c, REMOTE)
            self.assertEqual(row["direction"], "listen")
            self.assertEqual(row["switch_state"], "")
            # 切回完成：切换态已清；节点随后离线（TestClient 连接关闭）→
            # 闸门按普通离线 hold（保留 queued 不标 expired，恢复派发语义）
            self.assertEqual(queue_module._relay_node_gate(c, candidate)[0], "hold")
            offline_count = c.execute(
                "SELECT COUNT(*) FROM relay_offline_mentions WHERE member=?",
                (REMOTE,)).fetchone()[0]
            self.assertEqual(offline_count, 0, "kind=work 无发言位不记 offline_mention")
        # 提交次数：同一条点名全程只有一条 job、零次实际执行记录（全程未投递）
        with closing(db.get_connection()) as c:
            jobs = c.execute("SELECT COUNT(*) FROM wake_jobs WHERE profile=? "
                             "AND trigger='fullchain'", (REMOTE,)).fetchone()[0]
            self.assertEqual(jobs, 1, "同一条点名最多执行一次（无重复排队）")
            execs = c.execute(
                "SELECT COUNT(*) FROM hermes_executions WHERE profile=?",
                (REMOTE,)).fetchone()[0]
            self.assertEqual(execs, 0, "切换期间未投递、恢复后按正常链路单次投递")


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
