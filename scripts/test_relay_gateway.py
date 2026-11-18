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

def _relay_app():
    app = FastAPI()

    @app.websocket("/relay/ws")
    async def endpoint(websocket: WebSocket):
        await relay_server.relay_ws(websocket)
    return app


class RelayBase(unittest.TestCase):
    def setUp(self):
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

    def conn(self):
        return db.get_connection()

    def _connect(self):
        return connect(self.ws_url, close_timeout=1, open_timeout=5)

    def _hello(self, ws, *, credential=CREDENTIAL, member=REMOTE,
               code_version="v1", mcp_fingerprint=""):
        ws.send(json.dumps({"relay": {"kind": "hello"}, "member": member,
                            "credential": credential, "code_version": code_version,
                            "mcp_fingerprint": mcp_fingerprint}))
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
                self.assertEqual(relay.read_node(conn, REMOTE)["code_version"], "v1")
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
            relay.register_node(conn, REMOTE, CREDENTIAL, mcp_config={
                "command": ["python"], "env": {}, "_package_sha": "expected-pkg-sha"})
        finally:
            conn.close()
        ws = self._connect()
        try:
            ws.send(json.dumps({"relay": {"kind": "hello"}, "member": REMOTE,
                                "credential": CREDENTIAL,
                                "mcp_fingerprint": "other-pkg-sha"}))
            self.assertEqual(self._closed_code(ws), relay_server.CLOSE_MCP_INCOMPATIBLE)
        finally:
            ws.close()

    def test_mcp_fingerprint_match_accepted(self):
        conn = self.conn()
        try:
            relay.register_node(conn, REMOTE, CREDENTIAL, mcp_config={
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
        ws, _ = self._link()
        try:
            conn = self.conn()
            try:
                relay.mark_offline(conn, REMOTE)
            finally:
                conn.close()
            ws.send(json.dumps({"relay": {"kind": "heartbeat"}}))
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
            relay.register_node(conn, REMOTE, CREDENTIAL,
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


class RelayMcpTests(RelayBase):
    def test_render_mcp_config_substitution(self):
        conn = self.conn()
        try:
            relay.register_node(conn, REMOTE, CREDENTIAL, mcp_config={
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


class RelayConnectionSplitTests(RelayBase):
    def test_connection_relay_when_node_registered(self):
        from engine import hermes_gateway
        ws = self._connect()
        try:
            self._hello(ws)
            lease, home, command = hermes_gateway.connection(REMOTE)
            self.assertEqual(home, REMOTE)
            self.assertEqual(command, [])
            lease.close()
        finally:
            ws.close()

    def test_connection_unavailable_when_offline(self):
        from engine import hermes_gateway
        from engine.hermes_gateway_client import GatewayUnavailable
        with self.assertRaises(GatewayUnavailable):
            hermes_gateway.connection(REMOTE)


if __name__ == "__main__":
    unittest.main()
