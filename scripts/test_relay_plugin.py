"""req#168 task#316 插件侧隔离测试（bot-group-relay）。

覆盖（纯 unittest + 本地 websockets 模拟引擎/网关，无 ECS 真机）：
- 握手 hello 字段与 hello_ok 代次、版本不匹配被拒后不重试
- generation fencing（旧代帧拒收）+ 心跳带当前代
- 帧路由：引擎 frame → 本机 gateway（取票协议校验 + 转发原样）；本地事件回传
- mcp_snapshot_request 实时重算摘要（换包后摘要变化）
- 摘要口径：确定性 + 文件变化即变；只覆盖 MCP 包与依赖清单
- 断线重连：退避重连、不重投
真机（ECS 端到端/首装拉起 MCP）另行执行（验收 6/9 真机层）。
"""

import asyncio
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from websockets.sync.client import connect as ws_connect

_PLUGIN_DIR = Path(__file__).resolve().parents[1] / "plugins" / "hermes" / "bot-group-relay"
_PLUGIN_MODULE = "hermes_plugins.bot_group_relay"


def _load_plugin(plugin_dir: Path | None = None,
                 module_name: str | None = None):
    """按 Hermes plugin_loader 的合成包方式加载插件（相对导入可用）。

    plugin_dir：插件目录（默认工作树）；解包目录加载时传入并给独立模块名，
    避免与工作树模块混用。"""
    plugin_dir = plugin_dir or _PLUGIN_DIR
    module_name = module_name or _PLUGIN_MODULE
    parent = types.ModuleType(module_name.split(".")[0])
    parent.__path__ = [str(plugin_dir.parent)]
    sys.modules[parent.__name__] = parent
    spec = importlib.util.spec_from_file_location(
        module_name, plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    # 与 Hermes plugin_loader 一致：先注册全部子模块壳，再逐个 exec
    #（相对导入按需命中已注册对象，与执行顺序无关）。
    sub_specs = []
    for sub_file in sorted(plugin_dir.glob("*.py")):
        if sub_file.name == "__init__.py":
            continue
        sub_spec = importlib.util.spec_from_file_location(
            f"{module_name}.{sub_file.stem}", sub_file)
        sub_mod = importlib.util.module_from_spec(sub_spec)
        sys.modules[f"{module_name}.{sub_file.stem}"] = sub_mod
        setattr(mod, sub_file.stem, sub_mod)
        sub_specs.append((sub_file.stem, sub_spec, sub_mod))
    for _stem, sub_spec, sub_mod in sub_specs:
        sub_spec.loader.exec_module(sub_mod)
    return mod


_plugin = _load_plugin()
PACKAGE_VERSION = _plugin.config.PACKAGE_VERSION
RELAY_PROTOCOL_VERSION = _plugin.config.RELAY_PROTOCOL_VERSION
RelayConfig = _plugin.config.RelayConfig
mcp_digest = _plugin.mcp_digest
relay_mod = _plugin.relay
streams_mod = _plugin.streams
tickets = _plugin.tickets

RELEASE_VERSION = "v0.2.16"
MEMBER = "remote1"
CREDENTIAL = "test-relay-credential"


class FakeEngine:
    """本地 websockets 模拟引擎 /relay/ws 服务端（可向插件主动发帧）。"""

    def __init__(self, *, reject_code=None):
        import websockets.asyncio.server as ws_async
        self.received = []
        self.reject_code = reject_code
        self._outbound = asyncio.Queue()
        self._clients = []
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._ready = threading.Event()
        self.host = "127.0.0.1"
        self.port = 0

        async def handler(ws):
            self._clients.append(ws)
            if self.reject_code is not None:
                await ws.close(code=self.reject_code)
                return
            hello = json.loads(await ws.recv())
            self.received.append(hello)
            await ws.send(json.dumps({"relay": {"kind": "hello_ok", "generation": 7},
                                      "engine_url": "http://engine:8180"}))

            async def sender():
                while True:
                    envelope = await self._outbound.get()
                    await ws.send(envelope)

            async def receiver():
                while True:
                    raw = await ws.recv()
                    self.received.append(json.loads(raw))

            sender_task = asyncio.create_task(sender())
            receiver_task = asyncio.create_task(receiver())
            try:
                await asyncio.gather(sender_task, receiver_task)
            except Exception:
                sender_task.cancel()
                receiver_task.cancel()

        async def main():
            async with ws_async.serve(handler, self.host, 0) as server:
                self.port = server.sockets[0].getsockname()[1]
                self._ready.set()
                await self._stop_event.wait()

        self._stop_event = asyncio.Event()
        self._main = main

    def _serve(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    def start(self):
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("fake engine failed to start")
        return self

    def stop(self):
        for client in list(self._clients):
            try:
                asyncio.run_coroutine_threadsafe(client.close(), self._loop)
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._stop_event.set)
        self._thread.join(timeout=10)

    def send(self, envelope: dict) -> None:
        """引擎向插件发帧。"""
        self._loop.call_soon_threadsafe(
            self._outbound.put_nowait, json.dumps(envelope, ensure_ascii=False))

    @property
    def url(self):
        return f"ws://{self.host}:{self.port}/relay/ws"


class FakeGateway:
    """本地 websockets 模拟本机 gateway /api/ws（subprotocol 票据校验）。"""

    def __init__(self):
        import websockets.asyncio.server as ws_async
        self.frames = []
        self.subprotocols = []
        self._outbound = asyncio.Queue()
        self._clients = []
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._ready = threading.Event()
        self.host = "127.0.0.1"
        self.port = 0

        async def handler(ws):
            self._clients.append(ws)
            self.subprotocols.append(ws.subprotocol)

            async def sender():
                while True:
                    frame = await self._outbound.get()
                    await ws.send(frame)

            async def receiver():
                while True:
                    raw = await ws.recv()
                    self.frames.append(json.loads(raw))

            sender_task = asyncio.create_task(sender())
            receiver_task = asyncio.create_task(receiver())
            try:
                await asyncio.gather(sender_task, receiver_task)
            except Exception:
                sender_task.cancel()
                receiver_task.cancel()

        async def main():
            async with ws_async.serve(handler, self.host, 0,
                                      subprotocols=["hermes-gateway-v1"]) as server:
                self.port = server.sockets[0].getsockname()[1]
                self._ready.set()
                await self._stop_event.wait()

        self._stop_event = asyncio.Event()
        self._main = main

    def _serve(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    def start(self):
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("fake gateway failed to start")
        return self

    def stop(self):
        self._loop.call_soon_threadsafe(self._stop_event.set)
        self._thread.join(timeout=10)

    def send_event(self, frame: dict) -> None:
        """本机 gateway 向插件主动发事件帧。"""
        self._loop.call_soon_threadsafe(
            self._outbound.put_nowait, json.dumps(frame, ensure_ascii=False))

    @property
    def url(self):
        return f"ws://{self.host}:{self.port}/api/ws"


def make_cfg(engine_url: str, *, mcp_python: str = "", home: str = "/home/bot/.hermes") -> RelayConfig:
    return RelayConfig(engine_url=engine_url, member=MEMBER, credential=CREDENTIAL,
                       mcp_python=mcp_python, home=home)


def check_streamed_history(case, link, tickets_module):
    from engine.hermes_gateway_client import GatewayClient, GatewayTransportError
    from engine.relay_server import RelayTransport
    from websockets.sync.server import serve
    from websockets.exceptions import ConnectionClosed

    history = {"messages": [{"content": "记录🌙" * 1024, "index": i} for i in range(880)]}
    first_part = threading.Event()
    released = threading.Event()
    original = RelayTransport.push_part

    def observe(transport, envelope):
        original(transport, envelope)
        if envelope.get("data"):
            first_part.set()

    def gateway(ws):
        try:
            for raw in ws:
                request = json.loads(raw)
                rid, method = request["id"], request["method"]
                if method in ("session.resume", "session.export", "test.disconnect"):
                    data = json.dumps({"id": rid, "result": history}, ensure_ascii=False)
                if method == "session.resume":
                    def fragments():
                        yield data[:800]
                        if not first_part.wait(5):
                            raise AssertionError("first fragment wasn't forwarded immediately")
                        yield data[800:]
                    ws.send(fragments())
                elif method == "session.export":
                    ws.send(data)
                elif method == "test.disconnect":
                    def broken():
                        yield data[:200]
                        ws.close(1011, "fixture closed")
                        yield data[200:]
                    try:
                        ws.send(broken())
                    except ConnectionClosed:
                        pass
                    return
                else:
                    ws.send(json.dumps({"id": rid, "result": {"accepted": True}}))
        finally:
            released.set()

    with serve(gateway, "127.0.0.1", 0, subprotocols=["hermes-gateway-v1"]) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        endpoint = {"api_origin": f"http://127.0.0.1:{server.socket.getsockname()[1]}"}
        with mock.patch.object(tickets_module, "fetch_ticket", return_value=(endpoint, "fixture")), \
                mock.patch.object(RelayTransport, "push_part", observe):
            with GatewayClient({}, transport=link.open_transport()) as client:
                case.assertEqual(client.rpc("session.resume", timeout=10), history)
                case.assertEqual(client.rpc("session.export", timeout=10), history)
                case.assertEqual(client.rpc("prompt.submit", timeout=3), {"accepted": True})
            case.assertTrue(released.wait(3), "closing the lease must release its gateway socket")
            with GatewayClient({}, transport=link.open_transport()) as client:
                with case.assertRaisesRegex(GatewayTransportError, r"gateway stream closed \(WebSocket \d+\)"):
                    client.rpc("test.disconnect", timeout=5)
            with GatewayClient({}, transport=link.open_transport()) as client:
                case.assertTrue(client.rpc("prompt.submit", timeout=3)["accepted"])
    worker.join(3)


class GatewayInvitationConfigTests(unittest.TestCase):
    def test_invite_only_profile_is_enabled_for_gateway_startup(self):
        cfg = types.SimpleNamespace(extra={"invite": "test-invite", "engine_url": "http://fixture.invalid"})
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(_PLUGIN_DIR)}, clear=True), mock.patch.object(
            _plugin.config, "host_member", return_value="remote"
        ):
            self.assertTrue(_plugin.is_connected(cfg))
            cfg.extra.pop('invite')
            self.assertFalse(_plugin.is_connected(cfg))


class TicketTests(unittest.TestCase):
    def test_identify_alignment_and_loopback(self):
        home = "/home/bot/.hermes"
        payload = {"state": "ready",
                   "served_profiles": [{"home": home, "profile_id": home}],
                   "instance_id": "inst-1", "authority_epoch": 3,
                   "runtime_protocol": 1, "api_origin": "http://127.0.0.1:18789"}
        with unittest.mock.patch.object(tickets, "_socket_exchange",
                                        return_value=json.dumps({"ok": True, "protocol": 1, "id": 1,
                                                                 "result": payload})) as ex:
            endpoint = tickets.identify(home)
            self.assertEqual(endpoint["profile_id"], home)
            self.assertEqual(endpoint["instance_id"], "inst-1")
            # authority_epoch 正整数语义检查
            self.assertEqual(endpoint["authority_epoch"], 3)
        # 非 loopback api_origin：端点发现时拒绝
        bad = dict(payload, api_origin="http://evil.example.com:80")
        with unittest.mock.patch.object(tickets, "_socket_exchange",
                                        return_value=json.dumps({"ok": True, "protocol": 1, "id": 1,
                                                                 "result": bad})):
            with self.assertRaises(tickets.TicketError):
                tickets.identify(home)
        # authority_epoch 非正整数：拒绝
        bad_epoch = dict(payload, authority_epoch=0)
        with unittest.mock.patch.object(tickets, "_socket_exchange",
                                        return_value=json.dumps({"ok": True, "protocol": 1, "id": 1,
                                                                 "result": bad_epoch})):
            with self.assertRaises(tickets.TicketError):
                tickets.identify(home)

    def test_fetch_ticket_aligns_and_returns_ticket(self):
        home = "/home/bot/.hermes"
        payload = {"state": "ready",
                   "served_profiles": [{"home": home, "profile_id": home}],
                   "instance_id": "inst-1", "authority_epoch": 3,
                   "runtime_protocol": 1, "api_origin": "http://127.0.0.1:18789"}
        responses = [
            json.dumps({"ok": True, "protocol": 1, "id": 1, "result": payload}),
            json.dumps({"ok": True, "protocol": 1, "id": 1, "result": {
                "profile_id": home, "instance_id": "inst-1", "runtime_protocol": 1,
                "ticket": "tkt-1"}}),
        ]
        with unittest.mock.patch.object(tickets, "_socket_exchange", side_effect=responses) as ex:
            endpoint, ticket = tickets.fetch_ticket(home)
            self.assertEqual(ticket, "tkt-1")
            self.assertEqual(ex.call_count, 2, "每次取票都是新的 identify+ticket 交换（不缓存）")
            self.assertIn(b"session-ticket", ex.call_args_list[1].args[1])
            self.assertEqual(json.loads(ex.call_args_list[1].args[1])['params']['purpose'], 'interactive')
        # ticket 响应身份不一致（profile_id 被篡改）：拒绝
        tampered = [
            json.dumps({"ok": True, "protocol": 1, "id": 1, "result": payload}),
            json.dumps({"ok": True, "protocol": 1, "id": 1, "result": {
                "profile_id": "/other", "instance_id": "inst-1", "runtime_protocol": 1,
                "ticket": "tkt-1"}}),
        ]
        with unittest.mock.patch.object(tickets, "_socket_exchange", side_effect=tampered):
            with self.assertRaises(tickets.TicketError):
                tickets.fetch_ticket(home)


class DigestTests(unittest.TestCase):
    def test_fingerprint_deterministic_and_tracks_package(self):
        first = mcp_digest.mcp_fingerprint()
        self.assertTrue(first)
        second = mcp_digest.mcp_fingerprint()
        self.assertEqual(first, second, "摘要口径确定性")

    def test_snapshot_has_interpreter_entry_version(self):
        snapshot = mcp_digest.snapshot("", code_version=PACKAGE_VERSION)
        self.assertEqual(snapshot["code_version"], PACKAGE_VERSION)
        self.assertIn("mcp_fingerprint", snapshot)
        self.assertIn("mcp_python", snapshot)
        self.assertIn("mcp_entry", snapshot)


class VersionTests(unittest.TestCase):
    """req#168 返修（manager 12223）：插件包版本单一事实源=随包 VERSION 文件。

    打包注入实际发布版本 → 解包后 hello 上报的 code_version/plugin_version 与
    同批引擎发布版本一致（引擎按此同 tag 配套校验，不复用源码常量 1.0.0）。"""

    def test_plugin_version_sources_priority(self):
        config_mod = _plugin.config
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            with mock.patch.object(config_mod, "PLUGIN_ROOT", tmp):
                # 无 VERSION 无 env → 源码常量兜底（开发模式）
                self.assertEqual(config_mod.plugin_version(), "1.0.0")
                # VERSION 文件优先（打包注入）
                (tmp / "VERSION").write_text("v0.2.16\n", encoding="utf-8")
                self.assertEqual(config_mod.plugin_version(), "v0.2.16")
            # 环境变量次之
            with mock.patch.dict(os.environ,
                                 {"BOT_GROUP_RELAY_VERSION": "v0.9.9"}, clear=False):
                self.assertEqual(config_mod.plugin_version(), "v0.9.9")

    def test_release_archive_hello_version_matches_engine_version(self):
        # 真实打包（engine/maintenance/make_release.py _make_relay_asset）→ 解包
        # → 插件跑 hello：上报版本 == 打包版本 == 归档 VERSION（同批引擎同 tag）。
        from engine.maintenance import make_release as make_release_mod
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "out"
            archive, _sha = make_release_mod._make_relay_asset(
                project_root, out_dir, RELEASE_VERSION)
            unpack_dir = Path(td) / "unpacked"
            unpack_dir.mkdir()
            import tarfile as _tarfile
            with _tarfile.open(archive, "r:gz") as tar:
                tar.extractall(unpack_dir)
            # 归档内 VERSION 与 manifest 同源
            self.assertEqual((unpack_dir / "VERSION").read_text(encoding="utf-8"),
                             RELEASE_VERSION + "\n")
            with _tarfile.open(archive, "r:gz") as tar:
                manifest = json.loads(tar.extractfile("manifest.json").read())
            self.assertEqual(manifest["version"], RELEASE_VERSION)
            self.assertEqual(manifest["files"]["VERSION"]["sha256"],
                             hashlib.sha256((RELEASE_VERSION + "\n").encode("utf-8")).hexdigest())
            # 解包目录加载插件（独立模块名，不混用工作树模块）
            unpacked = _load_plugin(unpack_dir, "unpacked_plugins.bot_group_relay")
            self.assertEqual(unpacked.config.plugin_version(), RELEASE_VERSION,
                             "解包插件版本=归档 VERSION（不回落源码常量）")
            # 解包插件跑真实握手：FakeEngine 收到 hello 版本与打包版本一致
            engine = FakeEngine().start()
            self.addCleanup(engine.stop)
            cfg = unpacked.config.RelayConfig(
                engine_url=engine.url.replace("ws://", "http://"),
                member=MEMBER, credential=CREDENTIAL, mcp_python="", home="/home/bot/.hermes")
            client = unpacked.relay.RelayClient(cfg)
            client.start()
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and client.generation != 7:
                    time.sleep(0.02)
                hello = engine.received[0]
                self.assertEqual(hello["code_version"], RELEASE_VERSION)
                self.assertEqual(hello["plugin_version"], RELEASE_VERSION)
                self.assertNotEqual(hello["plugin_version"], "1.0.0",
                                    "发布包绝不报源码常量版本")
            finally:
                client.stop()


class RelayClientTests(unittest.TestCase):
    def setUp(self):
        state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(state_dir.cleanup)
        state = mock.patch.object(_plugin.pairing, "_state_path",
                                  return_value=Path(state_dir.name) / "pairing.json")
        state.start()
        self.addCleanup(state.stop)
        self.engine = FakeEngine().start()
        self.addCleanup(self.engine.stop)

    def _client(self, *, reject_code=None, mcp_python=""):
        engine = self.engine
        if reject_code is not None:
            engine = FakeEngine(reject_code=reject_code).start()
            self.addCleanup(engine.stop)
        client = relay_mod.RelayClient(make_cfg(engine.url, mcp_python=mcp_python))
        client.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if engine.received and engine.received[0].get("relay", {}).get("kind") == "hello":
                break
            time.sleep(0.02)
        return client, engine

    def test_hello_fields_and_generation(self):
        client, engine = self._client()
        try:
            hello = engine.received[0]
            self.assertEqual(hello["member"], MEMBER)
            self.assertEqual(hello["credential"], CREDENTIAL)
            self.assertEqual(hello["plugin_version"], PACKAGE_VERSION)
            self.assertEqual(hello["code_version"], PACKAGE_VERSION)
            self.assertIn(RELAY_PROTOCOL_VERSION, hello["protocol_versions"])
            self.assertTrue(hello["mcp_fingerprint"])
            self.assertIn("mcp_python", hello)
            self.assertIn("mcp_entry", hello)
            deadline = time.monotonic() + 5
            while client.generation != 7 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(client.generation, 7, "hello_ok 代次下发")
            self.assertEqual(client.engine_url, "http://engine:8180")
        finally:
            client.stop()

    def test_version_mismatch_rejected_no_retry(self):
        engine = FakeEngine(reject_code=4003).start()
        self.addCleanup(engine.stop)
        client = relay_mod.RelayClient(make_cfg(engine.url))
        client.start()
        client._thread.join(timeout=3)
        self.assertFalse(client._thread.is_alive(), "版本不匹配被拒后不重试")

    def test_stale_generation_frame_dropped(self):
        client, engine = self._client()
        try:
            deadline = time.monotonic() + 5
            while client.generation != 7 and time.monotonic() < deadline:
                time.sleep(0.02)
            # 引擎发旧代帧：插件拒收（不发回、不崩溃）
            engine.send({"relay": {"kind": "frame", "stream": "s1", "generation": 6},
                         "frame": {"method": "x"}})
            time.sleep(0.3)
            self.assertEqual(client.generation, 7)
        finally:
            client.stop()

    def test_heartbeat_carries_generation(self):
        client, engine = self._client()
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                beats = [e for e in engine.received
                         if e.get("relay", {}).get("kind") == "heartbeat"]
                if beats:
                    self.assertEqual(beats[0]["relay"]["generation"], 7)
                    return
                time.sleep(0.2)
            self.fail("未收到带代次的心跳")
        finally:
            client.stop()

    def test_frame_routes_to_local_gateway_with_ticket(self):
        # 引擎 frame → 本机 gateway：取票（mock）+ /api/ws 连接 + 原样转发
        gateway = FakeGateway().start()
        self.addCleanup(gateway.stop)
        payload = {"state": "ready",
                   "served_profiles": [{"home": "/home/bot/.hermes", "profile_id": "/home/bot/.hermes"}],
                   "instance_id": "inst-1", "authority_epoch": 3,
                   "runtime_protocol": 1, "api_origin": gateway.url.replace("ws://", "http://").replace("/api/ws", "")}
        responses = {
            b"identify": json.dumps({"ok": True, "protocol": 1, "id": 1, "result": payload}),
            b"session-ticket": json.dumps({"ok": True, "protocol": 1, "id": 1, "result": {
                "profile_id": "/home/bot/.hermes", "instance_id": "inst-1",
                "runtime_protocol": 1, "ticket": "tkt-9"}}),
        }

        def _exchange(home, data, timeout):
            request = json.loads(data)
            if request['verb'] == 'session-ticket':
                self.assertEqual(request['params']['purpose'], 'interactive')
            for verb, response in responses.items():
                if verb in data:
                    return response
            raise AssertionError(data)

        client, engine = self._client()
        try:
            deadline = time.monotonic() + 5
            while client.generation != 7 and time.monotonic() < deadline:
                time.sleep(0.02)
            rpc = {"jsonrpc": "2.0", "id": 1, "method": "session.resume",
                   "params": {"session_id": "sid"}}
            # patch 覆盖流线程的取票时刻（流线程异步启动，晚于 send 调用）
            with mock.patch.object(tickets, "_socket_exchange", side_effect=_exchange):
                engine.send({"relay": {"kind": "frame", "stream": "s9",
                                       "generation": 7}, "frame": rpc})
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and not gateway.frames:
                    time.sleep(0.05)
            self.assertEqual(gateway.frames, [rpc], "引擎帧原样转发到本机 gateway")
            self.assertEqual(gateway.subprotocols, ["hermes-gateway-v1"],
                             "取票票据经 subprotocol 认证")
        finally:
            client.stop()

    def test_local_event_relayed_back_to_engine(self):
        # 本机 gateway 事件帧 → 回传引擎（带 stream + 当前代）
        gateway = FakeGateway().start()
        self.addCleanup(gateway.stop)
        payload = {"state": "ready",
                   "served_profiles": [{"home": "/home/bot/.hermes", "profile_id": "/home/bot/.hermes"}],
                   "instance_id": "inst-1", "authority_epoch": 3,
                   "runtime_protocol": 1, "api_origin": gateway.url.replace("ws://", "http://").replace("/api/ws", "")}
        responses = {
            b"identify": json.dumps({"ok": True, "protocol": 1, "id": 1, "result": payload}),
            b"session-ticket": json.dumps({"ok": True, "protocol": 1, "id": 1, "result": {
                "profile_id": "/home/bot/.hermes", "instance_id": "inst-1",
                "runtime_protocol": 1, "ticket": "tkt-9"}}),
        }

        def _exchange(home, data, timeout):
            for verb, response in responses.items():
                if verb in data:
                    return response
            raise AssertionError(data)

        client, engine = self._client()
        try:
            deadline = time.monotonic() + 5
            while client.generation != 7 and time.monotonic() < deadline:
                time.sleep(0.02)
            with mock.patch.object(tickets, "_socket_exchange", side_effect=_exchange):
                engine.send({"relay": {"kind": "frame", "stream": "s9",
                                       "generation": 7},
                             "frame": {"jsonrpc": "2.0", "id": 1,
                                       "method": "session.resume", "params": {}}})
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline and not gateway.frames:
                    time.sleep(0.05)
                # 本机 gateway 主动发事件帧（无 method 的响应/事件）→ 插件回传引擎
                gateway.send_event({"jsonrpc": "2.0", "id": 2, "result": {"status": "started"}})
                deadline = time.monotonic() + 8
                echoed = None
                while time.monotonic() < deadline:
                    for e in engine.received:
                        if (e.get("relay", {}).get("kind") == "frame"
                                and e.get("relay", {}).get("stream") == "s9"):
                            echoed = e
                            break
                    if echoed:
                        break
                    time.sleep(0.05)
            self.assertIsNotNone(echoed, "本机事件帧应回传引擎")
            self.assertEqual(echoed["relay"]["generation"], 7)
            self.assertEqual(echoed["frame"]["id"], 2, "事件帧靠流标识归属、id 原样")
            self.assertEqual(echoed["frame"]["result"], {"status": "started"})
        finally:
            client.stop()

    def test_mcp_snapshot_request_recomputes_live(self):
        client, engine = self._client()
        try:
            deadline = time.monotonic() + 5
            while client.generation != 7 and time.monotonic() < deadline:
                time.sleep(0.02)
            engine.send({"relay": {"kind": "mcp_snapshot_request",
                                   "request_id": "snap-1", "generation": 7}})
            deadline = time.monotonic() + 8
            reply = None
            while time.monotonic() < deadline:
                for e in engine.received:
                    if e.get("relay", {}).get("kind") == "mcp_report":
                        reply = e
                        break
                if reply:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(reply, "应回 mcp_report")
            self.assertEqual(reply["relay"]["request_id"], "snap-1")
            self.assertEqual(reply["relay"]["generation"], 7)
            self.assertTrue(reply["mcp_fingerprint"])
            self.assertEqual(reply["code_version"], PACKAGE_VERSION)
            # 实时重算：与握手时一致（未换包）
            self.assertEqual(reply["mcp_fingerprint"],
                             engine.received[0]["mcp_fingerprint"])
        finally:
            client.stop()

    def test_reconnect_after_drop_without_resubmit(self):
        client, engine = self._client()
        try:
            deadline = time.monotonic() + 5
            while client.generation != 7 and time.monotonic() < deadline:
                time.sleep(0.02)
            # 引擎断链：插件进入离线（连接代清零，退避重连中），绝不重投
            engine.stop()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and client.generation != 0:
                time.sleep(0.05)
            self.assertEqual(client.generation, 0, "断链后连接代清零（等待重连）")
        finally:
            client.stop()


if __name__ == "__main__":
    unittest.main()
