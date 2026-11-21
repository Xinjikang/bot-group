"""req#168 task#315 远程 relay 隔离测试（引擎侧验收 1-6）。

覆盖：节点注册/凭据/吊销、WS 握手认证、generation fencing、relay 帧路由与
GatewayClient RPC、心跳 90s TTL、断线离线与 transport 哨兵、派发闸门（hold
绝不 discard）、offline 点名三态状态机、MCP 渲染与指纹复核、connection 分流。

隔离夹具：isolated_database + 真实 uvicorn 起 /relay/ws（无引擎 lifespan，
不启动 queue/scanner）；客户端用 websockets.sync（与插件同栈）。
真机联调在插件任务完成后另行执行。
"""
import json
import socket
import threading
import time
import unittest

import uvicorn
from fastapi import FastAPI
from websockets.sync.client import connect

from engine import db, relay, relay_server
from engine.hermes_gateway_client import GatewayClient
from scripts.unified_test_support import isolated_database

REMOTE = "remote1"
CREDENTIAL = "test-relay-credential"


from fastapi import WebSocket

RELEASE_VERSION = "v0.2.16"  # 测试固定引擎发布版本（hello 配套校验口径）


def _relay_app():
    app = FastAPI()

    @app.websocket("/relay/ws")
    async def endpoint(websocket: WebSocket):
        await relay_server.relay_ws(websocket)
    return app


class RelayBase(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        self._ctx = isolated_database()
        self._db_path = self._ctx.__enter__()
        conn = db.get_connection()
        try:
            from scripts.project_test_support import enable_members
            enable_members(conn, ("alpha",), profiles=[REMOTE])
            relay.register_node(conn, REMOTE, CREDENTIAL,
                                display_name="remote node", mcp_config={})
        finally:
            conn.close()
        # 引擎发布版本固定：hello 必须带配套 code_version（manager node1981 ③）
        self._version_patch = patch.object(relay_server, "_engine_release_version",
                                           return_value=RELEASE_VERSION)
        self._version_patch.start()
        self.app = _relay_app()
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._server = uvicorn.Server(uvicorn.Config(self.app, log_level="error"))
        self._thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [self._sock]}, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("relay 测试服务启动失败")
            time.sleep(0.01)
        self.ws_url = f"ws://127.0.0.1:{self._sock.getsockname()[1]}/relay/ws"

    def tearDown(self):
        relay_server.close_all()
        self._server.should_exit = True
        self._thread.join(timeout=10)
        self._sock.close()
        if self._thread.is_alive():
            raise RuntimeError("relay 测试服务未停止")
        self._ctx.__exit__(None, None, None)
        self._version_patch.stop()

    def conn(self):
        return db.get_connection()

    def _connect(self):
        return connect(self.ws_url, close_timeout=1, open_timeout=5)

    def _hello(self, ws, *, credential=CREDENTIAL, member=REMOTE,
               code_version=RELEASE_VERSION, mcp_fingerprint="", protocol_version=1):
        ws.send(json.dumps({"relay": {"kind": "hello"}, "member": member,
                            "credential": credential, "code_version": code_version,
                            "mcp_fingerprint": mcp_fingerprint,
                            "protocol_version": protocol_version}))
        return json.loads(ws.recv())

    def _closed_code(self, ws) -> int | None:
        try:
            ws.recv()
        except Exception as exc:  # noqa: BLE001 websockets 关链异常
            return getattr(getattr(exc, "rcvd", None), "code", None)
        return None


class RelayCredentialTests(RelayBase):
    def test_register_stores_hash_not_plaintext(self):
        conn = self.conn()
        try:
            row = relay.read_node(conn, REMOTE)
            self.assertNotEqual(row["credential_hash"], CREDENTIAL)
            self.assertEqual(row["credential_hash"], relay.hash_credential(CREDENTIAL))
            self.assertEqual(row["state"], "offline")
        finally:
            conn.close()

    def test_verify_and_revoke(self):
        conn = self.conn()
        try:
            self.assertTrue(relay.verify_credential(conn, REMOTE, CREDENTIAL))
            self.assertFalse(relay.verify_credential(conn, REMOTE, "wrong"))
            relay.revoke_credential(conn, REMOTE)
            self.assertFalse(relay.verify_credential(conn, REMOTE, CREDENTIAL))
        finally:
            conn.close()


class RelayHandshakeTests(RelayBase):
    def test_handshake_ok_and_generation(self):
        ws = self._connect()
        try:
            reply = self._hello(ws)
            self.assertEqual(reply["relay"]["kind"], "hello_ok")
            self.assertGreaterEqual(reply["relay"]["generation"], 1)
            self.assertTrue(reply.get("engine_url"))
            conn = self.conn()
            try:
                self.assertTrue(relay.is_online(conn, REMOTE))
                self.assertEqual(relay.read_node(conn, REMOTE)["code_version"],
                                 RELEASE_VERSION)
            finally:
                conn.close()
        finally:
            ws.close()

    def test_plugin_version_mismatch_rejected(self):
        # manager node1981 ③：插件包版本必须与引擎同 tag 配套，不符按版本拒绝
        ws = self._connect()
        try:
            ws.send(json.dumps({"relay": {"kind": "hello"}, "member": REMOTE,
                                "credential": CREDENTIAL, "code_version": "v9.9.9",
                                "protocol_version": 1}))
            self.assertEqual(self._closed_code(ws), relay_server.CLOSE_BAD_PROTOCOL)
            conn = self.conn()
            try:
                self.assertFalse(relay.is_online(conn, REMOTE))
            finally:
                conn.close()
        finally:
            ws.close()

    def test_bad_credential_rejected(self):
        ws = self._connect()
        try:
            ws.send(json.dumps({"relay": {"kind": "hello"}, "member": REMOTE,
                                "credential": "wrong"}))
            self.assertEqual(self._closed_code(ws), relay_server.CLOSE_BAD_CREDENTIAL)
            conn = self.conn()
            try:
                self.assertFalse(relay.is_online(conn, REMOTE))
            finally:
                conn.close()
        finally:
            ws.close()

    def test_revoked_credential_rejected(self):
        conn = self.conn()
        try:
            relay.revoke_credential(conn, REMOTE)
        finally:
            conn.close()
        ws = self._connect()
        try:
            ws.send(json.dumps({"relay": {"kind": "hello"}, "member": REMOTE,
                                "credential": CREDENTIAL}))
            self.assertEqual(self._closed_code(ws), relay_server.CLOSE_BAD_CREDENTIAL)
            conn = self.conn()
            try:
                self.assertFalse(relay.is_online(conn, REMOTE))
            finally:
                conn.close()
        finally:
            ws.close()

    def test_mcp_fingerprint_mismatch_rejected(self):
        conn = self.conn()
        try:
            relay.register_node(conn, REMOTE, CREDENTIAL, rotate=True, mcp_config={
                "command": ["python"], "env": {}, "_package_sha": "expected-pkg-sha"})
        finally:
            conn.close()
        ws = self._connect()
        try:
            ws.send(json.dumps({"relay": {"kind": "hello"}, "member": REMOTE,
                                "credential": CREDENTIAL,
                                "code_version": RELEASE_VERSION,
                                "mcp_fingerprint": "other-pkg-sha",
                                "protocol_version": 1}))
            self.assertEqual(self._closed_code(ws), relay_server.CLOSE_MCP_INCOMPATIBLE)
        finally:
            ws.close()

    def test_mcp_fingerprint_match_accepted(self):
        conn = self.conn()
        try:
            relay.register_node(conn, REMOTE, CREDENTIAL, rotate=True, mcp_config={
                "command": ["python"], "env": {}, "_package_sha": "expected-pkg-sha"})
        finally:
            conn.close()
        ws = self._connect()
        try:
            reply = self._hello(ws, mcp_fingerprint="expected-pkg-sha")
            self.assertEqual(reply["relay"]["kind"], "hello_ok")
        finally:
            ws.close()

    def test_reconnect_advances_generation(self):
        ws = self._connect()
        first = self._hello(ws)["relay"]["generation"]
        ws.close()
        ws = self._connect()
        try:
            second = self._hello(ws)["relay"]["generation"]
            self.assertGreater(second, first, "重连连接代必须 +1")
        finally:
            ws.close()

    def test_protocol_version_mismatch_rejected(self):
        ws = self._connect()
        try:
            ws.send(json.dumps({"relay": {"kind": "hello"}, "member": REMOTE,
                                "credential": CREDENTIAL, "protocol_version": 99}))
            self.assertEqual(self._closed_code(ws), relay_server.CLOSE_BAD_PROTOCOL)
        finally:
            ws.close()

    def test_overlapping_takeover_replaces_old_link(self):
        # manager node1977 ①：旧 WS 未关闭时新连接接替——不得死锁、新连接成为
        # 当前代（generation+1）、旧代清理不覆盖新连接的 online 状态、旧代流关闭。
        old_ws = self._connect()
        first_gen = self._hello(old_ws)["relay"]["generation"]
        old_transport = relay_server.open_transport(REMOTE)
        self.assertIsNotNone(old_transport)
        # 重叠接替：旧连接仍开，直接建新连接
        new_ws = self._connect()
        try:
            reply = self._hello(new_ws)
            self.assertGreater(reply["relay"]["generation"], first_gen,
                               "接替连接代必须 +1")
            new_gen = reply["relay"]["generation"]
            conn = self.conn()
            try:
                self.assertTrue(relay.is_online(conn, REMOTE),
                                "旧代清理不得覆盖新连接的 online 状态")
                self.assertEqual(relay.read_node(conn, REMOTE)["generation"], new_gen)
            finally:
                conn.close()
            # 旧代流必须随接替关闭（哨兵）——旧连接的流不再可用
            deadline = time.monotonic() + 5
            sentinel = False
            while time.monotonic() < deadline:
                item = old_transport._inbound.get_nowait() if not old_transport._inbound.empty() else None
                if item is relay_server._CLOSED_SENTINEL:
                    sentinel = True
                    break
                time.sleep(0.05)
            self.assertTrue(sentinel, "旧代流必须随接替关闭（哨兵）")
            # 新代流可用：open_transport 拿到当前连接的新流
            new_transport = relay_server.open_transport(REMOTE)
            self.assertIsNotNone(new_transport)
        finally:
            new_ws.close()
            try:
                old_ws.close()
            except Exception:  # noqa: BLE001 接替时引擎已主动关闭旧连接
                pass

    def test_frame_without_generation_dropped(self):
        # manager node1977 ①：普通帧缺 generation 一律拒收（不再放行）
        link = relay_server.NodeLink(REMOTE, None, generation=3)
        transport = link.open_transport()
        routed = relay_server._route_inbound(link, {
            "relay": {"kind": "frame", "stream": transport.stream},
            "frame": {"method": "x"}})
        self.assertTrue(routed)
        self.assertTrue(transport._inbound.empty(), "缺代帧不得进入流队列")

    def test_stale_generation_heartbeat_dropped(self):
        # 旧代心跳不得把节点标 online（只判存活，且以当前代为准）
        conn = self.conn()
        try:
            relay.mark_offline(conn, REMOTE)
        finally:
            conn.close()
        link = relay_server.NodeLink(REMOTE, None, generation=3)
        relay_server._route_inbound(link, {"relay": {"kind": "heartbeat",
                                                    "generation": 2}})
        conn = self.conn()
        try:
            self.assertFalse(relay.is_online(conn, REMOTE), "旧代心跳不得标 online")
        finally:
            conn.close()


class RelayFrameTests(RelayBase):
    def _link(self):
        ws = self._connect()
        hello = self._hello(ws)
        return ws, hello["relay"]["generation"]

    def _remote_client(self, on_event=None):
        transport = relay_server.open_transport(REMOTE)
        self.assertIsNotNone(transport)
        gc = GatewayClient(
            {"profile_id": REMOTE, "instance_id": "relay-" + REMOTE,
             "runtime_protocol": 1, "api_origin": "", "control_home": "",
             "code_sha": "", "authority_epoch": 1},
            on_event=on_event, transport=transport)
        return gc, transport

    def test_rpc_roundtrip_over_relay(self):
        ws, generation = self._link()
        try:
            gc, transport = self._remote_client()
            result_holder = {}

            def run_rpc():
                result_holder["value"] = gc.rpc("session.create", title="t")
            thread = threading.Thread(target=run_rpc, daemon=True)
            thread.start()
            # 引擎→插件的 relay 帧（带 stream/generation，内容仍是 JSON-RPC）
            envelope = json.loads(ws.recv())
            self.assertEqual(envelope["relay"]["kind"], "frame")
            self.assertEqual(envelope["relay"]["generation"], generation)
            self.assertTrue(envelope["relay"]["stream"].startswith(REMOTE))
            frame = envelope["frame"]
            self.assertEqual(frame["method"], "session.create")
            # 插件转发响应帧（同 stream、JSON-RPC id 一一对应）
            ws.send(json.dumps({"relay": {"kind": "frame",
                                          "stream": envelope["relay"]["stream"],
                                          "generation": generation},
                                "frame": {"jsonrpc": "2.0", "id": frame["id"],
                                          "result": {"session_id": "s1"}}}))
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "rpc 未在时限内返回")
            self.assertEqual(result_holder["value"], {"session_id": "s1"})
            gc.close()
        finally:
            ws.close()

    def test_event_frame_routes_to_callback(self):
        ws, generation = self._link()
        try:
            events = []
            gc, transport = self._remote_client(on_event=events.append)
            # 事件帧无 id，靠 stream 标识归属
            ws.send(json.dumps({"relay": {"kind": "frame", "stream": transport.stream,
                                          "generation": generation},
                                "frame": {"method": "session.event",
                                          "params": {"text": "hi"}}}))
            deadline = time.monotonic() + 5
            while not events and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(events[0]["method"], "session.event")
            gc.close()
        finally:
            ws.close()

    def test_stale_generation_frame_dropped(self):
        # 旧代帧（generation 落后）必须被拒收：不入任何流队列
        link = relay_server.NodeLink(REMOTE, None, generation=5)
        transport = link.open_transport()
        routed = relay_server._route_inbound(link, {
            "relay": {"kind": "frame", "stream": transport.stream, "generation": 4},
            "frame": {"method": "x"}})
        self.assertTrue(routed)
        self.assertTrue(transport._inbound.empty(), "旧代帧不得进入流队列")

    def test_heartbeat_updates_liveness_only(self):
        ws, generation = self._link()
        try:
            conn = self.conn()
            try:
                relay.mark_offline(conn, REMOTE)
            finally:
                conn.close()
            ws.send(json.dumps({"relay": {"kind": "heartbeat",
                                          "generation": generation}}))
            deadline = time.monotonic() + 3
            conn = self.conn()
            try:
                while not relay.is_online(conn, REMOTE) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(relay.is_online(conn, REMOTE), "心跳应恢复在线（只判存活）")
            finally:
                conn.close()
        finally:
            ws.close()

    def test_heartbeat_ttl_marks_offline(self):
        conn = self.conn()
        try:
            relay.mark_online(conn, REMOTE, code_version="v", mcp_fingerprint="",
                              generation=1, now=time.time() - 200)
            self.assertFalse(relay.is_online(conn, REMOTE), "90s 无心跳必须判离线")
            self.assertEqual(relay.read_node(conn, REMOTE)["state"], "offline")
        finally:
            conn.close()

    def test_link_close_sentinel_and_offline(self):
        ws, _ = self._link()
        gc, _ = self._remote_client()
        ws.close()
        deadline = time.monotonic() + 3
        conn = self.conn()
        try:
            while relay.is_online(conn, REMOTE) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(relay.is_online(conn, REMOTE), "断链必须标离线")
        finally:
            conn.close()
        deadline = time.monotonic() + 5
        while not gc.closed and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(gc.closed, "流哨兵应使远端 client 关闭")

    def test_stale_link_heartbeat_sql_generation_condition(self):
        # manager node1981 ②：旧链接带自身合法代次的迟到心跳——内存层与
        # link.generation 比较通过，但写库 SQL 以当前连接代为条件，不覆盖新代。
        conn = self.conn()
        try:
            # 当前连接代=5（新连接已注册），旧 link 代=4
            conn.execute("UPDATE relay_nodes SET generation=5,state='offline',"
                         "last_heartbeat=?,mcp_fingerprint='' WHERE member=?",
                         (time.time(), REMOTE))
            conn.commit()
            old_link = relay_server.NodeLink(REMOTE, None, generation=4)
            relay_server._route_inbound(old_link, {"relay": {"kind": "heartbeat",
                                                             "generation": 4}})
            row = relay.read_node(conn, REMOTE)
            self.assertEqual(row["state"], "offline",
                             "旧代心跳不得把当前代节点标 online（SQL 代条件）")
            self.assertEqual(row["generation"], 5)
        finally:
            conn.close()

    def test_stale_link_mcp_report_sql_generation_condition(self):
        # 旧链接带自身合法代次的迟到摘要上报：SQL 代条件不覆盖新代摘要
        conn = self.conn()
        try:
            conn.execute("UPDATE relay_nodes SET generation=5,state='online',"
                         "mcp_fingerprint='new',last_heartbeat=? WHERE member=?",
                         (time.time(), REMOTE))
            conn.commit()
            old_link = relay_server.NodeLink(REMOTE, None, generation=4)
            relay_server._route_inbound(old_link, {"relay": {"kind": "mcp_report",
                                                             "generation": 4},
                                                   "mcp_fingerprint": "stale"})
            self.assertEqual(relay.read_node(conn, REMOTE)["mcp_fingerprint"], "new",
                             "旧代摘要不得覆盖当前代")
        finally:
            conn.close()

    def test_shutdown_stale_link_keeps_current_generation(self):
        # 旧代清理锁外写库带代条件：不覆盖新连接状态（manager node1981 ②）
        conn = self.conn()
        try:
            conn.execute("UPDATE relay_nodes SET generation=5,state='online',"
                         "last_heartbeat=? WHERE member=?", (time.time(), REMOTE))
            conn.commit()
            stale = relay_server.NodeLink(REMOTE, None, generation=4)
            stale.ws = None
            stale.loop = None
            relay_server._shutdown_link(stale, "stale cleanup")
            self.assertTrue(relay.is_online(conn, REMOTE),
                            "旧代清理不得把当前代标 offline")
        finally:
            conn.close()

    def test_mcp_snapshot_request_gets_fresh_report(self):
        # manager node1981 ③：configure 前同步问询——同连接原地换包、配置早于
        # 异步上报，问询取到本次实际摘要。
        ws = self._connect()
        try:
            reply = self._hello(ws)
            link = relay_server.active_link(REMOTE)
            self.assertIsNotNone(link)
            result = {}

            def ask():
                result["report"] = link.request_mcp_snapshot(timeout=5)

            thread = threading.Thread(target=ask, daemon=True)
            thread.start()
            # 模拟插件：收到 mcp_snapshot_request → 回复当前实际摘要
            request = json.loads(ws.recv())
            self.assertEqual(request["relay"]["kind"], "mcp_snapshot_request")
            request_id = request["relay"]["request_id"]
            ws.send(json.dumps({"relay": {"kind": "mcp_report",
                                          "generation": reply["relay"]["generation"],
                                          "request_id": request_id},
                                "mcp_fingerprint": "fresh-sha",
                                "code_version": RELEASE_VERSION}))
            thread.join(timeout=6)
            # 响应附本连接代：configure 写库按代条件（reviewer 返修①）；
            # proxy_port 为插件 loopback 代理端口（manager node2084，0=未启）
            self.assertEqual(result.get("report"), {
                "mcp_fingerprint": "fresh-sha", "code_version": RELEASE_VERSION,
                "generation": reply["relay"]["generation"], "proxy_port": 0})
        finally:
            ws.close()

    def test_mcp_snapshot_timeout_returns_none(self):
        # 插件不回响应：超时返回 None（configure 走缓存复核，不误通过）
        ws = self._connect()
        try:
            self._hello(ws)
            link = relay_server.active_link(REMOTE)
            report = link.request_mcp_snapshot(timeout=0.3)
            self.assertIsNone(report)
        finally:
            ws.close()

    def test_merge_mcp_spec_fetch_report_uses_fresh_digest(self):
        # 旧摘要曾匹配→同连接原地换包→配置早于异步上报：fetch_report 同步
        # 问询取到本次实际摘要（旧缓存不匹配即拒绝，绝不误通过）。
        from engine.hermes_gateway_client import GatewayRefusal
        conn = self.conn()
        try:
            relay.register_node(conn, REMOTE, CREDENTIAL, rotate=True, mcp_config={
                "command": ["python"], "env": {}, "_package_sha": "pkg-v2"})
            relay.mark_online(conn, REMOTE, code_version=RELEASE_VERSION,
                              mcp_fingerprint="pkg-v1", generation=1)
            # 旧缓存 pkg-v1：不带 fetch 时与期望 pkg-v2 不符 → 拒绝
            with self.assertRaises(GatewayRefusal):
                relay.merge_mcp_spec(conn, REMOTE, {"command": ["x"], "args": [],
                                                    "env": {}}, engine_url="http://e")
            # 带 fetch：同步问询返回本次实际摘要 pkg-v2 → 复核通过
            merged = relay.merge_mcp_spec(
                conn, REMOTE, {"command": ["local"], "args": [], "env": {"T": "1"}},
                engine_url="http://e",
                fetch_report=lambda: {"mcp_fingerprint": "pkg-v2",
                                      "code_version": RELEASE_VERSION})
            self.assertEqual(merged["command"], ["python"])
            self.assertIn("T", merged["env"])
            # fetch 返回旧摘要（换包未完成/回滚）：拒绝
            with self.assertRaises(GatewayRefusal):
                relay.merge_mcp_spec(conn, REMOTE, {"command": ["x"], "args": [],
                                                    "env": {}}, engine_url="http://e",
                                     fetch_report=lambda: {"mcp_fingerprint": "pkg-v1"})
        finally:
            conn.close()

    def test_first_join_mcp_uses_current_authenticated_snapshot_only(self):
        from engine.hermes_gateway_client import GatewayRefusal
        conn = self.conn()
        try:
            row = relay.register_remote_node(conn, 'fresh-remote', 'fixture-node-key', direction='dial')
            relay.mark_online(conn, row['member'], code_version=RELEASE_VERSION,
                              mcp_fingerprint='fresh-sha', generation=row['generation'])
            report = {'mcp_fingerprint': 'fresh-sha', 'generation': row['generation'], 'proxy_port': 12345,
                      'mcp_python': '/isolated/bin/python', 'mcp_entry': '/isolated/mcp/server.py'}
            spec = {'command': '/local/python', 'env': {'GROUP_ENGINE_URL': 'http://ignored'}}
            configured = relay.merge_mcp_spec(conn, row['member'], spec, fetch_report=lambda: report)
            self.assertEqual(configured, {'command': report['mcp_python'], 'args': [report['mcp_entry']],
                                          'env': {'GROUP_ENGINE_URL': 'http://127.0.0.1:12345'}})
            for update in ({'generation': row['generation'] - 1}, {'proxy_port': 0}, {'mcp_entry': 'server.py'}):
                with self.subTest(update=update), self.assertRaises(GatewayRefusal):
                    relay.merge_mcp_spec(conn, row['member'], spec, fetch_report=lambda: {**report, **update})
        finally:
            conn.close()

    def test_merge_mcp_spec_fetch_failure_refused(self):
        # reviewer 返修②：问询超时/无响应（fetch_report 返回 None）必须拒绝，
        # 不得清空库内摘要后按旧缓存放行。
        from engine.hermes_gateway_client import GatewayRefusal
        conn = self.conn()
        try:
            relay.register_node(conn, REMOTE, CREDENTIAL, rotate=True, mcp_config={
                "command": ["python"], "env": {}, "_package_sha": "pkg-v2"})
            relay.mark_online(conn, REMOTE, code_version=RELEASE_VERSION,
                              mcp_fingerprint="pkg-v2", generation=1)
            with self.assertRaises(GatewayRefusal) as ctx:
                relay.merge_mcp_spec(conn, REMOTE, {"command": ["x"], "args": [],
                                                    "env": {}}, engine_url="http://e",
                                     fetch_report=lambda: None)
            self.assertEqual(ctx.exception.reason, "mcp_snapshot_unavailable")
            # 库内摘要保持原值，未被清空
            self.assertEqual(relay.read_node(conn, REMOTE)["mcp_fingerprint"], "pkg-v2")
            # 空报告同样拒绝（无有效摘要）
            with self.assertRaises(GatewayRefusal):
                relay.merge_mcp_spec(conn, REMOTE, {"command": ["x"], "args": [],
                                                    "env": {}}, engine_url="http://e",
                                     fetch_report=lambda: {})
        finally:
            conn.close()

    def test_merge_mcp_spec_stale_generation_report_not_written(self):
        # reviewer 返修①：问询响应带旧代（接替后迟到的旧连接响应）——
        # 写库按代条件不匹配，不得覆盖新代摘要。
        conn = self.conn()
        try:
            relay.register_node(conn, REMOTE, CREDENTIAL, rotate=True, mcp_config={
                "command": ["python"], "env": {}, "_package_sha": "pkg-v2"})
            # 新代连接握手：generation_advance 推进到 2 后 mark_online 写入新摘要
            relay.generation_advance(conn, REMOTE)
            relay.mark_online(conn, REMOTE, code_version=RELEASE_VERSION,
                              mcp_fingerprint="pkg-v2", generation=2)
            # 旧代（gen=1）迟到的问询响应：写库 WHERE generation=1 不匹配
            merged = relay.merge_mcp_spec(
                conn, REMOTE, {"command": ["local"], "args": [], "env": {"T": "1"}},
                engine_url="http://e",
                fetch_report=lambda: {"mcp_fingerprint": "pkg-v1",
                                      "code_version": RELEASE_VERSION,
                                      "generation": 1})
            # 新代摘要未被覆盖，复核仍用 pkg-v2 通过
            node = relay.read_node(conn, REMOTE)
            self.assertEqual(node["mcp_fingerprint"], "pkg-v2",
                             "旧代问询响应不得覆盖新代摘要")
            self.assertEqual(node["generation"], 2)
            self.assertEqual(merged["command"], ["python"])
        finally:
            conn.close()


class RelayGateTests(RelayBase):
    """派发闸门与 offline 点名三态状态机（验收 3/5）。"""

    def _chat_job(self, conn, thread_id, status="queued"):
        return conn.execute(
            "INSERT INTO wake_jobs(profile,project,kind,trigger,thread_id,status,enqueued_at) "
            "VALUES(?,?,?,?,?,?,?)", (REMOTE, "alpha", "chat", "thread@msg", thread_id,
                                      status, db.now_iso())).lastrowid

    def _thread_with_mention(self, conn, turn_profile=REMOTE):
        conn.execute(
            "INSERT INTO messages(project,ts,sender,sender_type,content,mentions,thread_id) "
            "VALUES('alpha',?,'user','user',?,?,?)",
            (db.now_iso(), "@" + REMOTE + " hi", json.dumps([REMOTE]), None))
        message_id = conn.execute("SELECT MAX(id) FROM messages").fetchone()[0]
        thread_id = conn.execute(
            "INSERT INTO threads(project,title,creator,members,status,round,turn_profile,"
            "turn_parent_id,turn_skipped,created_at,last_active_at) "
            "VALUES('alpha','t','user',?,'open',0,?,NULL,'[]',?,?)",
            (json.dumps(["user", REMOTE]), turn_profile, db.now_iso(), db.now_iso())).lastrowid
        conn.execute("UPDATE messages SET thread_id=? WHERE id=?", (thread_id, message_id))
        conn.execute(
            "INSERT INTO inbox(profile,message_id,status) VALUES(?,?,?)",
            (REMOTE, message_id, "pending"))
        conn.commit()
        return thread_id, message_id

    def test_gate_hold_when_offline_and_mention_upsert(self):
        from engine import queue
        conn = self.conn()
        try:
            thread_id, message_id = self._thread_with_mention(conn)
            job_id = self._chat_job(conn, thread_id)
            relay.mark_offline(conn, REMOTE)
            gate, ids = queue._relay_node_gate(conn, conn.execute(
                "SELECT * FROM wake_jobs WHERE id=?", (job_id,)).fetchone())
            self.assertEqual(gate, "hold", "离线节点必须 hold")
            self.assertIn(message_id, ids)
            mention = relay.read_offline_mention(conn, message_id, REMOTE)
            self.assertEqual(mention["state"], "offline_waiting")
            # 闸门反复检查 upsert 不新增：仍一条、状态不变
            queue._relay_node_gate(conn, conn.execute(
                "SELECT * FROM wake_jobs WHERE id=?", (job_id,)).fetchone())
            count = conn.execute(
                "SELECT COUNT(*) FROM relay_offline_mentions WHERE message_id=? AND member=?",
                (message_id, REMOTE)).fetchone()[0]
            self.assertEqual(count, 1, "唯一键幂等：反复检查只显示一条")
            # process_one 绝不 discard：job 仍 queued
            queue.process_one(conn)
            status = conn.execute("SELECT status FROM wake_jobs WHERE id=?",
                                  (job_id,)).fetchone()[0]
            self.assertEqual(status, "queued", "hold 分支绝不能把 job 置 done")
        finally:
            conn.close()

    def test_gate_go_when_online(self):
        from engine import queue
        conn = self.conn()
        try:
            thread_id, _ = self._thread_with_mention(conn)
            job_id = self._chat_job(conn, thread_id)
            relay.mark_online(conn, REMOTE, code_version="v", mcp_fingerprint="",
                              generation=1)
            gate, _ = queue._relay_node_gate(conn, conn.execute(
                "SELECT * FROM wake_jobs WHERE id=?", (job_id,)).fetchone())
            self.assertEqual(gate, "go")
        finally:
            conn.close()

    def test_gate_hold_when_at_capacity_and_receipt_releases(self):
        from engine import queue
        from engine import hermes_gateway_ledger as ledger
        conn = self.conn()
        try:
            relay.register_node(conn, REMOTE, CREDENTIAL, rotate=True,
                                mcp_config={"_concurrency": 1})
            relay.mark_online(conn, REMOTE, code_version="v", mcp_fingerprint="",
                              generation=1)
            ledger.prepare(conn, session="s", project="alpha", profile=REMOTE, kind="work",
                           thread_id=None, task_id=None, job_id=None, event_floor=0,
                           source="", python="[]", home="", workdir="", session_id="s1",
                           profile_id=REMOTE)
            job_id = conn.execute(
                "INSERT INTO wake_jobs(profile,project,kind,trigger,status,enqueued_at) "
                "VALUES(?,?,?,?,?,?)", (REMOTE, "alpha", "work", "scan:task",
                                        "queued", db.now_iso())).lastrowid
            conn.commit()
            gate, _ = queue._relay_node_gate(conn, conn.execute(
                "SELECT * FROM wake_jobs WHERE id=?", (job_id,)).fetchone())
            self.assertEqual(gate, "hold", "达并发上限必须 hold 保留 queued")
            row = conn.execute(
                "SELECT execution_id FROM hermes_executions WHERE profile=?",
                (REMOTE,)).fetchone()
            ledger.apply_receipt(conn, row["execution_id"], {
                "admission_id": "a1", "status": "terminal", "outcome": "done",
                "ref": {"session_id": "s1", "profile_id": REMOTE},
                "execution_generation": None, "authority_epoch": 1})
            self.assertEqual(relay.capacity_used(conn, REMOTE), 0,
                             "receipt 终态必须释放容量")
            gate, _ = queue._relay_node_gate(conn, conn.execute(
                "SELECT * FROM wake_jobs WHERE id=?", (job_id,)).fetchone())
            self.assertEqual(gate, "go", "容量释放后恢复派发")
        finally:
            conn.close()

    def test_gate_expire_when_turn_lost(self):
        from engine import queue
        conn = self.conn()
        try:
            thread_id, message_id = self._thread_with_mention(conn, turn_profile="other")
            job_id = self._chat_job(conn, thread_id)
            relay.mark_offline(conn, REMOTE)
            relay.upsert_offline_mention(conn, message_id=message_id, member=REMOTE,
                                         project="alpha", thread_id=thread_id)
            gate, _ = queue._relay_node_gate(conn, conn.execute(
                "SELECT * FROM wake_jobs WHERE id=?", (job_id,)).fetchone())
            self.assertEqual(gate, "expire")
            self.assertEqual(relay.read_offline_mention(conn, message_id, REMOTE)["state"],
                             "offline_expired", "发言位到期且从未提交必须标 expired")
        finally:
            conn.close()

    def test_mention_submitted_batch(self):
        conn = self.conn()
        try:
            for message_id in (10, 11, 12):
                relay.upsert_offline_mention(conn, message_id=message_id, member=REMOTE,
                                             project="alpha", thread_id=None)
            changed = relay.mark_submitted(conn, message_ids=[10, 11, 12], member=REMOTE,
                                          submission_id="sub-1")
            self.assertEqual(changed, 3, "本次注入快照内所有点名一起标 submitted")
            for message_id in (10, 11, 12):
                row = relay.read_offline_mention(conn, message_id, REMOTE)
                self.assertEqual(row["state"], "submitted")
                self.assertEqual(row["submission_id"], "sub-1")
        finally:
            conn.close()

    def test_mention_expire_skips_submitted(self):
        conn = self.conn()
        try:
            relay.upsert_offline_mention(conn, message_id=20, member=REMOTE,
                                         project="alpha", thread_id=None)
            relay.mark_submitted(conn, message_ids=[20], member=REMOTE, submission_id="s")
            changed = relay.expire_mentions(conn, message_ids=[20])
            self.assertEqual(changed, 0, "已提交的点名不得被 expire")
            self.assertEqual(relay.read_offline_mention(conn, 20, REMOTE)["state"],
                             "submitted")
        finally:
            conn.close()

    def test_settle_thread_expires_waiting_mentions(self):
        from engine import router
        conn = self.conn()
        try:
            thread_id, message_id = self._thread_with_mention(conn)
            relay.upsert_offline_mention(conn, message_id=message_id, member=REMOTE,
                                         project="alpha", thread_id=thread_id)
            router.settle_thread(conn, thread_id)
            self.assertEqual(relay.read_offline_mention(conn, message_id, REMOTE)["state"],
                             "offline_expired", "话题收口：从未提交的离线点名标未投递")
        finally:
            conn.close()

    def test_settlement_uses_injected_ids_and_receipt(self):
        # manager node1977 ②：按「实际注入的目标点名」+「同一 submission 的确认
        # 回执」结算——deliver 返回的 injected/admitted/submission_id 为准；
        # 非 admitted（pending reconciliation）保留 waiting 核对依据。
        from unittest.mock import patch
        from engine import queue
        conn = self.conn()
        try:
            thread_id, message_id = self._thread_with_mention(conn)
            job_id = self._chat_job(conn, thread_id)
            relay.upsert_offline_mention(conn, message_id=message_id, member=REMOTE,
                                         project="alpha", thread_id=thread_id)
            with patch.object(queue, "_relay_node_gate", return_value=("go", [])), \
                    patch.object(queue.wake, "session_busy", return_value=False), \
                    patch.object(queue.wake, "topic_busy", return_value=False):
                # 确认回执已受理：实际注入 ids + submission_id → submitted
                with patch.object(queue.wake, "deliver", return_value={
                        "ok": True, "admitted": True, "submission_id": "sub-9",
                        "injected_message_ids": [message_id]}):
                    queue.process_one(conn)
                row = relay.read_offline_mention(conn, message_id, REMOTE)
                self.assertEqual(row["state"], "submitted")
                self.assertEqual(row["submission_id"], "sub-9")
            # pending reconciliation（不确定）：不标 submitted，保留 waiting
            relay.upsert_offline_mention(conn, message_id=message_id, member=REMOTE,
                                         project="alpha", thread_id=thread_id)
            conn.execute("UPDATE relay_offline_mentions SET state='offline_waiting',"
                         "submission_id=NULL WHERE message_id=? AND member=?",
                         (message_id, REMOTE))
            conn.commit()
            job2 = self._chat_job(conn, thread_id)
            with patch.object(queue, "_relay_node_gate", return_value=("go", [])), \
                    patch.object(queue.wake, "session_busy", return_value=False), \
                    patch.object(queue.wake, "topic_busy", return_value=False), \
                    patch.object(queue.wake, "deliver", return_value={
                        "ok": True, "admitted": False, "submission_id": "sub-10",
                        "injected_message_ids": [message_id]}):
                queue.process_one(conn)
            row = relay.read_offline_mention(conn, message_id, REMOTE)
            self.assertEqual(row["state"], "offline_waiting",
                             "不确定回执不得标 submitted（保留核对依据）")
            self.assertEqual(row["submission_id"], "sub-10",
                             "未决期间持久关联 submission 供权威回执结算 "
                             "(manager node1981 ①)")
            # 无 submission_id 也不标（不得回退空串）
            job3 = self._chat_job(conn, thread_id)
            with patch.object(queue, "_relay_node_gate", return_value=("go", [])), \
                    patch.object(queue.wake, "session_busy", return_value=False), \
                    patch.object(queue.wake, "topic_busy", return_value=False), \
                    patch.object(queue.wake, "deliver", return_value={
                        "ok": True, "admitted": True, "submission_id": None,
                        "injected_message_ids": [message_id]}):
                queue.process_one(conn)
            row = relay.read_offline_mention(conn, message_id, REMOTE)
            self.assertEqual(row["state"], "offline_waiting",
                             "无 submission_id 不得标 submitted")
        finally:
            conn.close()

    def test_settle_skips_attempted_unsettled_and_receipt_settles(self):
        # manager node1981 ①：已尝试未决（waiting+submission_id）收口不标
        # expired；后续权威回执（apply_receipt）结算为 submitted。两种顺序。
        from engine import router
        conn = self.conn()
        try:
            thread_id, message_id = self._thread_with_mention(conn)
            relay.upsert_offline_mention(conn, message_id=message_id, member=REMOTE,
                                         project="alpha", thread_id=thread_id)
            # 顺序 A：先收口后对账——未决保留，权威回执补记 submitted
            relay.attach_submission(conn, message_ids=[message_id], member=REMOTE,
                                    submission_id="sub-a")
            router.settle_thread(conn, thread_id)
            row = relay.read_offline_mention(conn, message_id, REMOTE)
            self.assertEqual(row["state"], "offline_waiting",
                             "收口跳过已尝试但未决者（manager node1981 ①）")
            relay.settle_by_submission(conn, member=REMOTE, submission_id="sub-a")
            row = relay.read_offline_mention(conn, message_id, REMOTE)
            self.assertEqual(row["state"], "submitted",
                             "权威回执补记已尝试点名")
            # 顺序 B：先对账后收口——已 submitted 收口不动
            thread_id2, message_id2 = self._thread_with_mention(conn)
            relay.upsert_offline_mention(conn, message_id=message_id2, member=REMOTE,
                                         project="alpha", thread_id=thread_id2)
            relay.attach_submission(conn, message_ids=[message_id2], member=REMOTE,
                                    submission_id="sub-b")
            relay.settle_by_submission(conn, member=REMOTE, submission_id="sub-b")
            router.settle_thread(conn, thread_id2)
            row = relay.read_offline_mention(conn, message_id2, REMOTE)
            self.assertEqual(row["state"], "submitted", "先对账后收口：submitted 保持")
        finally:
            conn.close()

    def test_authoritative_receipt_settles_through_apply_receipt(self):
        # manager node1981 ①：恢复回执有入口补记——apply_receipt 受理回执后
        # 该 submission 关联的 waiting 点名标 submitted（真实链路接线）。
        from engine import hermes_gateway_ledger as ledger
        conn = self.conn()
        try:
            thread_id, message_id = self._thread_with_mention(conn)
            relay.upsert_offline_mention(conn, message_id=message_id, member=REMOTE,
                                         project="alpha", thread_id=thread_id)
            row = ledger.prepare(conn, session="sess-1", project="alpha", profile=REMOTE,
                                 kind="chat", thread_id=thread_id, task_id=None, job_id=None,
                                 event_floor=0, source="", python="py", home="home",
                                 workdir="wd", session_id="sid-1", profile_id=REMOTE)
            eid, submission_id = row["execution_id"], row["submission_id"]
            relay.attach_submission(conn, message_ids=[message_id], member=REMOTE,
                                    submission_id=submission_id)
            receipt = {"ref": {"session_id": "sid-1", "profile_id": REMOTE},
                       "admission_id": "adm-1", "status": "started",
                       "execution_generation": None, "authority_epoch": 1}
            ledger.apply_receipt(conn, eid, receipt)
            row = relay.read_offline_mention(conn, message_id, REMOTE)
            self.assertEqual(row["state"], "submitted",
                             "权威回执（apply_receipt）结算 waiting 点名")
            self.assertEqual(row["submission_id"], submission_id)
            # 幂等：重复回执不重复改
            ledger.apply_receipt(conn, eid, {"ref": {"session_id": "sid-1",
                                                     "profile_id": REMOTE},
                                             "admission_id": "adm-1",
                                             "status": "started",
                                             "execution_generation": None,
                                             "authority_epoch": 1})
            row = relay.read_offline_mention(conn, message_id, REMOTE)
            self.assertEqual(row["state"], "submitted")
        finally:
            conn.close()


class RelayMcpTests(RelayBase):
    def test_render_mcp_config_substitution(self):
        conn = self.conn()
        try:
            relay.register_node(conn, REMOTE, CREDENTIAL, rotate=True, mcp_config={
                "command": ["python", "{engine_url}"],
                "args": ["--url", "{engine_url}"],
                "env": {"GROUP_ENGINE_URL": "{engine_url}", "KEEP": "1"},
                "_package_sha": "pkg-1",
            })
            rendered = relay.render_mcp_config(conn, REMOTE, engine_url="http://e:1")
            self.assertEqual(rendered["command"], ["python", "http://e:1"])
            self.assertEqual(rendered["env"]["GROUP_ENGINE_URL"], "http://e:1")
            self.assertEqual(rendered["env"]["KEEP"], "1")
            # 控制字段不下发（_package_sha 只参与握手比对）
            self.assertNotIn("_package_sha", rendered)
            # 引擎期望指纹=管理员配置的随包 MCP 入口内容摘要
            self.assertEqual(relay.read_node(conn, REMOTE)["expected_mcp_sha"], "pkg-1")
        finally:
            conn.close()

    def test_mcp_report_updates_live_fingerprint(self):
        # manager node1977 ③：连接存续中换包 → mcp_report 更新远端实际摘要 →
        # configure 前复核用最新值（旧握手缓存不作数）。
        from engine.hermes_gateway_client import GatewayRefusal
        conn = self.conn()
        try:
            relay.register_node(conn, REMOTE, CREDENTIAL, rotate=True, mcp_config={
                "command": ["python"], "env": {}, "_package_sha": "pkg-v2"})
            relay.mark_online(conn, REMOTE, code_version="v", mcp_fingerprint="pkg-v1",
                              generation=1)
            # 握手缓存 v1：期望 v2 不符 → configure 拒绝
            with self.assertRaises(GatewayRefusal) as ctx:
                relay.merge_mcp_spec(conn, REMOTE, {"command": ["x"], "args": [], "env": {}},
                                     engine_url="http://e:1")
            self.assertEqual(ctx.exception.reason, "mcp_package_mismatch")
            # 插件换包上报新摘要 v2 → 复核通过
            relay.mcp_report(conn, REMOTE, "pkg-v2")
            merged = relay.merge_mcp_spec(conn, REMOTE, {"command": ["local"], "args": [],
                                                         "env": {"GROUP_ENGINE_TOKEN": "t"}},
                                          engine_url="http://e:1")
            self.assertEqual(merged["command"], ["python"], "远端渲染命令覆盖本机")
            self.assertIn("GROUP_ENGINE_TOKEN", merged["env"], "身份令牌继续由引擎注入")
        finally:
            conn.close()

    def test_merge_mcp_spec_missing_command_refused(self):
        # 远端渲染缺 command：拒绝（绝不回落本机配置，manager node1977 ③）
        from engine.hermes_gateway_client import GatewayRefusal
        conn = self.conn()
        try:
            relay.register_node(conn, REMOTE, CREDENTIAL, rotate=True, mcp_config={"env": {}})
            relay.mark_online(conn, REMOTE, code_version="v", mcp_fingerprint="",
                              generation=1)
            with self.assertRaises(GatewayRefusal) as ctx:
                relay.merge_mcp_spec(conn, REMOTE, {"command": ["local"], "args": [],
                                                    "env": {}}, engine_url="http://e:1")
            self.assertEqual(ctx.exception.reason, "mcp_config_missing")
        finally:
            conn.close()

    def test_merge_mcp_spec_engine_url_remote_wins(self):
        # GROUP_ENGINE_URL 以远端渲染值为准，本机会话 env 不得覆盖（manager node1977 ③）
        conn = self.conn()
        try:
            relay.register_node(conn, REMOTE, CREDENTIAL, rotate=True, mcp_config={
                "command": ["python"], "env": {"GROUP_ENGINE_URL": "{engine_url}"}})
            relay.mark_online(conn, REMOTE, code_version="v", mcp_fingerprint="",
                              generation=1)
            merged = relay.merge_mcp_spec(conn, REMOTE, {
                "command": ["local"], "args": [],
                "env": {"GROUP_ENGINE_URL": "http://local-wrong:1", "TOKEN": "t"}},
                engine_url="http://engine:8180")
            self.assertEqual(merged["env"]["GROUP_ENGINE_URL"], "http://engine:8180",
                             "可达 URL 以远端渲染为准")
            self.assertEqual(merged["env"]["TOKEN"], "t", "其余会话 env 由引擎注入")
        finally:
            conn.close()

    def test_merge_mcp_spec_local_member_untouched(self):
        # 本机成员（无 relay 节点）：spec 原样返回（本机路径不动）
        conn = self.conn()
        try:
            spec = {"command": ["local"], "args": [], "env": {}}
            self.assertEqual(relay.merge_mcp_spec(conn, "user", spec, engine_url="x"),
                             spec)
        finally:
            conn.close()


class RelayConnectionSplitTests(RelayBase):
    def test_project_members_refresh_queries_remote_work_session(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from engine import api, hermes_gateway, wake
        from scripts.project_test_support import enable_members

        conn = self.conn()
        try:
            enable_members(conn, ('remote-project',), profiles=[REMOTE])
            conn.commit()
        finally:
            conn.close()
        app = FastAPI()
        app.include_router(api.router_api)
        app.dependency_overrides[api._resolve_identity] = lambda: ('user', 'user')
        with self._connect() as ws, TestClient(app) as http, ThreadPoolExecutor(1) as pool, \
                patch.object(hermes_gateway, 'enabled', return_value=True):
            self._hello(ws)
            for sessions in ([], [{'session_id': 'work-session'}]):
                request = pool.submit(http.get, '/projects/remote-project/workspaces')
                envelope = json.loads(ws.recv(timeout=5))
                frame = envelope['frame']
                self.assertEqual(frame['method'], 'session.list')
                self.assertEqual(frame['params']['title'], wake.session_name(REMOTE, 'work', None, 'remote-project'))
                ws.send(json.dumps({'relay': envelope['relay'], 'frame': {
                    'jsonrpc': '2.0', 'id': frame['id'], 'result': {'sessions': sessions}}}))
                response = request.result(timeout=5)
                self.assertEqual(response.status_code, 200)
                member, = response.json()['members']
                self.assertEqual(member['profile'], REMOTE)
                self.assertEqual(member['work_session_exists'], bool(sessions))

    def test_connection_relay_when_node_registered(self):
        from engine import hermes_gateway
        ws = self._connect()
        try:
            self._hello(ws)
            lease, home, command = hermes_gateway.connection(REMOTE)
            self.assertEqual(home, REMOTE)
            self.assertEqual(command, [])
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                with lease as client:
                    self.assertIs(client, lease)
                    raise RuntimeError('cancelled')
            self.assertTrue(lease.client.closed)
            lease.close()
        finally:
            ws.close()

    def test_connection_unavailable_when_offline(self):
        from engine import hermes_gateway
        from engine.hermes_gateway_client import GatewayUnavailable
        with self.assertRaises(GatewayUnavailable):
            hermes_gateway.connection(REMOTE)


class RelaySpawnRaceTests(RelayBase):
    """manager node1985：提交与点名关联的竞态——生产派发路径受控网关测试。"""

    def _injected_mention(self, conn):
        """造一条带 @REMOTE 的 pending inbox 消息（真实注入快照会包含它）。"""
        conn.execute(
            "INSERT INTO messages(project,ts,sender,sender_type,content,mentions,thread_id) "
            "VALUES('alpha',?,'user','user',?,?,?)",
            (db.now_iso(), "@" + REMOTE + " hi", json.dumps([REMOTE]), None))
        message_id = conn.execute("SELECT MAX(id) FROM messages").fetchone()[0]
        thread_id = conn.execute(
            "INSERT INTO threads(project,title,creator,members,status,round,turn_profile,"
            "turn_parent_id,turn_skipped,created_at,last_active_at) "
            "VALUES('alpha','t','user',?,'open',0,?,NULL,'[]',?,?)",
            (json.dumps(["user", REMOTE]), REMOTE, db.now_iso(), db.now_iso())).lastrowid
        conn.execute("UPDATE messages SET thread_id=? WHERE id=?", (thread_id, message_id))
        conn.execute("INSERT INTO inbox(profile,message_id,status) VALUES(?,?,?)",
                     (REMOTE, message_id, "pending"))
        conn.commit()
        return thread_id, message_id

    def test_attach_persists_before_submit_and_settle_keeps_waiting(self):
        # manager node1985 竞态：submit 期间收口不得误标 expired——attach 须
        # 在 prompt.submit 到达网关前落盘（与 prepare 同一事务边界）。
        from unittest.mock import patch
        from engine import hermes_gateway as gateway, wake
        from engine import router
        conn = self.conn()
        try:
            thread_id, message_id = self._injected_mention(conn)
            relay.upsert_offline_mention(conn, message_id=message_id, member=REMOTE,
                                         project="alpha", thread_id=thread_id)
            # fake submit：收口发生在 submit 期间，收口后立即断言点名行未 expired
            # 且已带 submission_id
            settle_seen = {}

            def rpc(method, **kwargs):
                if method == 'runtime.describe':
                    return dict(profile_id='fixture',
                                capabilities=['durable-admission-v1', 'acp-session-mcp-v1'])
                if method == 'prompt.submit':
                    c = self.conn()
                    try:
                        router.settle_thread(c, thread_id)
                        row = relay.read_offline_mention(c, message_id, REMOTE)
                        settle_seen.update(state=row["state"],
                                           submission_id=row["submission_id"])
                    finally:
                        c.close()
                    return dict(status='queued', admission_id='adm-1',
                                authority_epoch=1, execution_generation=None,
                                ref={'profile_id': 'fixture', 'session_id': 'sid-1'})
                if method == 'session.resume':
                    return dict(session_id='sid-1', running=False, pending=[])
                raise AssertionError(method)

            from unittest.mock import Mock
            remote = Mock()
            remote.rpc.side_effect = rpc
            snapshot = dict(session_id='sid-1', running=False, pending=[])
            from engine import browser_resources, hermes_session_auth
            with patch.object(gateway, 'connection', return_value=(remote, '/isolated', ['hermes'])), \
                    patch.object(gateway.sessions, 'initialization_rpc', return_value={
                        'profile_id': 'fixture', 'capabilities': ['durable-admission-v1', 'acp-session-mcp-v1']}), \
                    patch.object(gateway, '_binding', return_value=snapshot), \
                    patch.object(hermes_session_auth, 'grant', return_value={
                        'project': 'alpha', 'profile': REMOTE, 'kind': 'chat',
                        'thread_id': thread_id, 'session': 's', 'session_id': 'sid-1',
                        'signing_generation': 1, 'home': '/isolated',
                        'command': ['hermes'], 'workdir': '/w', 'model_args': [],
                        'bot_id': 1}), \
                    patch.object(hermes_session_auth, 'configure', return_value=snapshot), \
                    patch.object(hermes_session_auth, 'context', return_value={}), \
                    patch.object(browser_resources, 'register'), \
                    patch.object(gateway, '_start_worker'), patch.object(gateway, '_publish'), \
                    patch.object(wake, 'wake_system_prompt', return_value='fixture'):
                result = gateway.spawn(REMOTE, 'chat', None, thread_id, 'alpha',
                                       bot={'id': 1}, workdir='/w', model_args=[])
            # 收口瞬间：未 expired、已带 submission_id（attach 先于 submit 落盘）
            self.assertEqual(settle_seen["state"], "offline_waiting",
                             "submit 期间收口不得误标 expired")
            self.assertTrue(settle_seen["submission_id"],
                            "收口瞬间点名必须已关联 submission")
            # spawn 返回 admitted，权威回执已按同一 submission 结算
            self.assertTrue(result.get("admitted"))
            self.assertEqual(result.get("injected_message_ids"), [message_id])
            row = relay.read_offline_mention(conn, message_id, REMOTE)
            self.assertEqual(row["state"], "submitted",
                             "权威回执按同一 submission 结算为 submitted")
            self.assertEqual(row["submission_id"], result["submission_id"])
            # 无重复投递
            submits = [c for c in remote.rpc.call_args_list if c.args[0] == 'prompt.submit']
            self.assertEqual(len(submits), 1, "同一派发最多投递一次")
        finally:
            conn.close()

    def test_never_submitted_mention_still_expires(self):
        # 对照：确实未提交（无注入、无 submission 关联）的点名照常过期。
        from unittest.mock import patch
        from engine import hermes_gateway as gateway, wake
        from engine import router
        from engine import browser_resources, hermes_session_auth
        conn = self.conn()
        try:
            thread_id, message_id = self._injected_mention(conn)
            # 点名行存在，但本次 spawn 传显式 signal（无注入 ids）→ 未关联 submission
            relay.upsert_offline_mention(conn, message_id=message_id, member=REMOTE,
                                         project="alpha", thread_id=thread_id)
            # 显式 signal：spawn 不再注入快照（injected_ids 空）
            # 但 inbox 里消息仍在——用 signal 传入时 injected_ids=[]，attach 0 行
            def rpc(method, **kwargs):
                if method == 'runtime.describe':
                    return dict(profile_id='fixture',
                                capabilities=['durable-admission-v1', 'acp-session-mcp-v1'])
                if method == 'prompt.submit':
                    return dict(status='queued', admission_id='adm-2',
                                authority_epoch=1, execution_generation=None,
                                ref={'profile_id': 'fixture', 'session_id': 'sid-1'})
                if method == 'session.resume':
                    return dict(session_id='sid-1', running=False, pending=[])
                raise AssertionError(method)

            from unittest.mock import Mock
            remote = Mock()
            remote.rpc.side_effect = rpc
            snapshot = dict(session_id='sid-1', running=False, pending=[])
            with patch.object(gateway, 'connection', return_value=(remote, '/isolated', ['hermes'])), \
                    patch.object(gateway.sessions, 'initialization_rpc', return_value={
                        'profile_id': 'fixture', 'capabilities': ['durable-admission-v1', 'acp-session-mcp-v1']}), \
                    patch.object(gateway, '_binding', return_value=snapshot), \
                    patch.object(hermes_session_auth, 'grant', return_value={
                        'project': 'alpha', 'profile': REMOTE, 'kind': 'chat',
                        'thread_id': thread_id, 'session': 's', 'session_id': 'sid-1',
                        'signing_generation': 1, 'home': '/isolated',
                        'command': ['hermes'], 'workdir': '/w', 'model_args': [],
                        'bot_id': 1}), \
                    patch.object(hermes_session_auth, 'configure', return_value=snapshot), \
                    patch.object(hermes_session_auth, 'context', return_value={}), \
                    patch.object(browser_resources, 'register'), \
                    patch.object(gateway, '_start_worker'), patch.object(gateway, '_publish'), \
                    patch.object(wake, 'wake_system_prompt', return_value='fixture'):
                gateway.spawn(REMOTE, 'chat', 'explicit-signal', thread_id, 'alpha',
                              bot={'id': 1}, workdir='/w', model_args=[])
            # 显式信号路径未注入该点名：未关联 submission，收口照常标 expired
            router.settle_thread(conn, thread_id)
            row = relay.read_offline_mention(conn, message_id, REMOTE)
            self.assertEqual(row["state"], "offline_expired",
                             "确实未提交的点名照常过期（对照）")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
