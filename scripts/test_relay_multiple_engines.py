"""One gateway profile, independent WS peers, and one shared Hermes session store."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import http.client
import io
import json
import multiprocessing
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from websockets.sync.client import connect
from websockets.sync.server import serve

from engine import relay_protocol
from scripts.test_relay_gateway import RelayBase
from scripts.test_relay_plugin import _load_plugin, _plugin


def listen(stack, handler, **options):
    server = serve(handler, "127.0.0.1", 0, **options)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    stack.callback(worker.join, 3)
    stack.callback(server.shutdown)
    return server.socket.getsockname()[1]


def receive(ws, kind):
    while True:
        packet = json.loads(ws.recv(timeout=5))
        if packet["relay"]["kind"] == kind:
            return packet


def run_engine(control):
    """Run a production relay endpoint and direction coordinator with an isolated DB."""
    from contextlib import closing
    from engine import db, hermes_gateway, relay, relay_dialer, relay_server, relay_switch
    from scripts.test_relay_gateway import RelayBase, REMOTE
    instance = RelayBase()
    instance.setUp()
    try:
        control.send(instance.ws_url.removesuffix("/relay/ws").replace("ws://", "http://"))
        while True:
            command, payload = control.recv()
            if command == "stop":
                return
            if command == "revoke":
                with closing(db.get_connection()) as conn:
                    relay.revoke_credential(conn, REMOTE)
                relay_server.close_all()
                control.send(True)
                continue
            if command == "switch":
                target, endpoint = payload
                if endpoint:
                    with closing(db.get_connection()) as conn:
                        relay.set_dial_endpoint(conn, REMOTE, endpoint)
                relay_switch.switch(REMOTE, target)
            else:
                target = payload
            until = time.monotonic() + 12
            while time.monotonic() < until:
                link = relay_server.active_link(REMOTE)
                with closing(db.get_connection()) as conn:
                    row = relay.read_node(conn, REMOTE)
                    if (link and isinstance(link, relay_dialer.DialLink) == (target == "dial")
                            and row["direction"] == target and not row["switch_state"]
                            and row["generation"] == relay_server._db_generation(link)
                            and relay.is_online(conn, REMOTE)):
                        break
                time.sleep(0.05)
            else:
                raise TimeoutError(target)
            with hermes_gateway._relay_lease(REMOTE) as lease:
                result = lease.rpc("session.create", title="same", request_id="same", timeout=5)
            control.send((link.protocol_version, result))
    finally:
        relay_dialer.stop_dial(REMOTE)
        instance.tearDown()
        control.close()


class MultiEngineTests(unittest.TestCase):
    def setUp(self):
        self.stack = self.enterContext(ExitStack())
        tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(_plugin.pairing, "_state_path", return_value=tmp / "pairing.json"))
        self.stack.enter_context(patch.object(_plugin.config, "host_member", return_value="default"))
        self.stack.enter_context(patch.object(_plugin.config, "host_home", return_value=str(tmp)))
        self.stack.enter_context(patch.object(_plugin.listen, "STATE_DIR", tmp))
        self.stack.enter_context(patch.dict(os.environ, {k: "" for k in os.environ if k.startswith("BOT_GROUP_")}))

    def test_two_engines_share_profile_without_sharing_sessions(self):
        sessions = {}

        def gateway(ws):
            for raw in ws:
                frame = json.loads(raw)
                params = frame["params"]
                if frame["method"] == "session.create":
                    sessions[params["title"]] = params["request_id"]
                ws.send(json.dumps({"id": frame["id"], "result": sessions[params["title"]]}))

        port = listen(self.stack, gateway, subprotocols=["hermes-gateway-v1"])
        self.stack.enter_context(patch.object(_plugin.tickets, "fetch_ticket",
            return_value=({"api_origin": f"http://127.0.0.1:{port}"}, "test")))

        class Host:
            def __init__(self, *_): pass
            def _mark_connected(self): pass
            def _mark_disconnected(self): pass

        class Adapter(_plugin.adapter.RelayAdapter, Host):
            pass

        def engine(version, credential):
            connections = queue.Queue()
            def handle(ws):
                hello = receive(ws, "hello")
                if hello["credential"] != credential or version not in hello["protocol_versions"]:
                    ws.close(4001)
                    return
                ws.send(json.dumps({"relay": {"kind": "hello_ok", "generation": 1, "protocol_version": version}}))
                connections.put(ws)
                ws.wait_closed()
            port = listen(self.stack, handle)
            url = f"http://127.0.0.1:{port}"
            peer = _plugin.pairing.select_peer("default", engine_url=url)
            with _plugin.pairing.using_peer(peer):
                _plugin.pairing.save_register_credential("default", credential, engine_url=url)
            return connections

        def rpc(ws, method):
            params = {"title": "same-project-same-thread"}
            if method == "session.create":
                params["request_id"] = "identical-request-id"
            ws.send(json.dumps({"relay": {"kind": "frame", "stream": "same-stream", "generation": 1},
                                "frame": {"id": 1, "method": method, "params": params}}))
            return receive(ws, "frame")["frame"]["result"]

        async def run():
            home = engine(1, "home-test-key")
            adapter = Adapter(None)
            await adapter.connect()
            try:
                a = await asyncio.to_thread(home.get, True, 5)
                office = engine(2, "office-test-key")
                b = await asyncio.to_thread(office.get, True, 7)
                ids = await asyncio.gather(*(asyncio.to_thread(rpc, ws, "session.create") for ws in (a, b)))
                self.assertNotEqual(*ids)
                a.close()
                self.assertEqual(await asyncio.to_thread(rpc, b, "session.list"), ids[1])
                a = await asyncio.to_thread(home.get, True, 6)
                self.assertEqual(await asyncio.to_thread(rpc, a, "session.list"), ids[0])
                await adapter.disconnect()
                await adapter.connect()
                for connections, expected in zip((home, office), ids):
                    ws = await asyncio.to_thread(connections.get, True, 5)
                    self.assertEqual(await asyncio.to_thread(rpc, ws, "session.list"), expected)
            finally:
                await adapter.disconnect()
        asyncio.run(run())

    def test_two_listeners_route_group_http_to_their_own_engine(self):
        self.stack.enter_context(patch.object(_plugin.listen, "validate_bind_address", return_value="127.0.0.1"))
        def pair():
            result = _plugin.listen.listen_serve(address="127.0.0.1", port=0, tls=False)
            self.stack.callback(result["_server"].stop)
            url = urlsplit(result["pairing_url"])
            ws = self.stack.enter_context(connect(f"ws://{url.netloc}", proxy=None))
            ticket = parse_qs(url.query)["pairing_token"][0]
            ws.send(json.dumps({"relay": {"kind": "hello"}, "member": "default", "direction": "dial",
                                "credential": ticket, **relay_protocol.offer()}))
            ack = receive(ws, "hello_ok")["relay"]
            self.assertEqual(ack["protocol_version"], relay_protocol.CURRENT)
            return ws, result["proxy_port"], ack["generation"]

        def group_send(port, message):
            client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                client.request("POST", "/msg", json.dumps({"content": message}))
                return client.getresponse().read().decode()
            finally:
                client.close()

        pairs = [pair(), pair()]
        with ThreadPoolExecutor(2) as pool:
            pending = [pool.submit(group_send, port, str(i)) for i, (_, port, _) in enumerate(pairs)]
            for i, (ws, _, generation) in enumerate(pairs):
                packet = receive(ws, "http_request")
                self.assertEqual(json.loads(packet["body"])["content"], str(i))
                ws.send(json.dumps({"relay": {"kind": "http_response", "generation": generation,
                                              "request_id": packet["relay"]["request_id"]},
                                    "status": 200, "body": f"receipt-{i}"}))
            self.assertEqual([f.result() for f in pending], ["receipt-0", "receipt-1"])

    def test_version_intersection_and_malformed_offers(self):
        self.assertEqual(relay_protocol.negotiate({"protocol_version": 1}), 1)
        self.assertEqual(relay_protocol.negotiate({"protocol_version": 1, "protocol_versions": [3, 2, 1]}), 3)
        self.assertEqual(relay_protocol.negotiate({"protocol_version": 1, "protocol_versions": [2, 1]}), 2)
        for hello in ({}, {"protocol_version": True}, {"protocol_version": 9},
                      {"protocol_version": 1, "protocol_versions": [2]}):
            self.assertIsNone(relay_protocol.negotiate(hello))

    def test_real_engines_switch_independently(self):
        from scripts.test_relay_gateway import CREDENTIAL, REMOTE
        def gateway(ws):
            for raw in ws:
                frame = json.loads(raw)
                ws.send(json.dumps({"id": frame["id"], "result": frame["params"]["request_id"]}))

        port = listen(self.stack, gateway, subprotocols=["hermes-gateway-v1"])
        self.stack.enter_context(patch.object(_plugin.tickets, "fetch_ticket",
            return_value=({"api_origin": f"http://127.0.0.1:{port}"}, "test")))
        self.stack.enter_context(patch.object(_plugin.listen, "validate_bind_address", return_value="127.0.0.1"))
        self.stack.enter_context(patch.object(_plugin.config, "host_member", return_value=REMOTE))
        context = multiprocessing.get_context("spawn")

        def start():
            parent, child = context.Pipe()
            process = context.Process(target=run_engine, args=(child,))
            process.start()
            child.close()
            def stop():
                try:
                    if process.is_alive():
                        parent.send(("stop", None))
                        process.join(8)
                finally:
                    parent.close()
                    if process.is_alive():
                        process.terminate()
                        process.join(3)
            self.stack.callback(stop)
            self.assertTrue(parent.poll(25))
            url = parent.recv()
            peer = _plugin.pairing.select_peer(REMOTE, engine_url=url)
            with _plugin.pairing.using_peer(peer):
                _plugin.pairing.save_register_credential(REMOTE, CREDENTIAL, engine_url=url)
            cfg = _plugin.config.peer_configs()[peer]
            client = _plugin.relay.RelayClient(cfg, peer=peer)
            client.start()
            self.stack.callback(client.stop)
            setup = _plugin.listen.listen_serve(address="127.0.0.1", port=0, member=REMOTE, engine_url=url)
            setup["_server"].stop()
            with _plugin.pairing.using_peer(peer):
                key = _plugin.pairing.issue_persistent(REMOTE)
            return parent, client, {"host": "127.0.0.1", "port": setup["port"], "tls": True,
                            "fingerprint": setup["fingerprint"], "pairing_token": key}

        def response(channel):
            self.assertTrue(channel.poll(20))
            version, result = channel.recv()
            self.assertEqual(version, relay_protocol.CURRENT)
            return result

        a, client_a, endpoint_a = start()
        b, client_b, endpoint_b = start()
        a.send(("rpc", "listen"))
        b.send(("rpc", "listen"))
        id_a, id_b = response(a), response(b)
        self.assertNotEqual(id_a, id_b)
        for moving, other, target, endpoint, other_direction, own_id, other_id in (
            (b, a, "dial", endpoint_b, "listen", id_b, id_a),
            (a, b, "dial", endpoint_a, "dial", id_a, id_b),
            (b, a, "listen", None, "dial", id_b, id_a),
            (a, b, "listen", None, "listen", id_a, id_b),
        ):
            moving.send(("switch", (target, endpoint)))
            other.send(("rpc", other_direction))
            self.assertEqual(response(other), other_id)
            self.assertEqual(response(moving), own_id)
            if target == other_direction == "dial":
                for client in (client_a, client_b):
                    client.stop()
                configs = _plugin.config.peer_configs()
                self.assertEqual(len(configs), 2)
                for peer, cfg in configs.items():
                    restored = _plugin.relay.RelayClient(cfg, peer=peer)
                    self.stack.callback(restored.stop)
                    restored.start()
                for channel, expected in ((a, id_a), (b, id_b)):
                    channel.send(("rpc", "dial"))
                    self.assertEqual(response(channel), expected)
        a.send(("revoke", None))
        self.assertTrue(a.poll(5) and a.recv())
        b.send(("rpc", "listen"))
        self.assertEqual(response(b), id_b)


class HistoricalPluginTests(RelayBase):
    def test_legacy_plugins_connect_to_distinct_engine_release(self):
        from engine import hermes_gateway, relay_server
        from scripts.test_relay_gateway import REMOTE, CREDENTIAL
        root = Path(__file__).resolve().parents[1]
        plugin_path = "plugins/hermes/bot-group-relay"
        with ExitStack() as stack:
            def gateway(ws):
                for raw in ws:
                    message = json.loads(raw)
                    ws.send(json.dumps({"id": message["id"], "result": {"sessions": []}}))
            port = listen(stack, gateway, subprotocols=["hermes-gateway-v1"])
            for tag in ("v0.3.3", "v0.3.4"):
                with self.subTest(tag=tag), ExitStack() as current:
                    archive = subprocess.check_output(["git", "archive", "--format=zip", tag, plugin_path],
                                                      cwd=root, env=dict(os.environ))
                    directory = Path(current.enter_context(tempfile.TemporaryDirectory()))
                    with zipfile.ZipFile(io.BytesIO(archive)) as src:
                        src.extractall(directory)
                    (directory / plugin_path / "VERSION").write_text(tag, encoding="utf-8")
                    plugin = _load_plugin(directory / plugin_path, "historical_plugins.relay_" + tag.replace(".", "_"))
                    current.enter_context(patch.object(plugin.tickets, "fetch_ticket",
                        return_value=({"api_origin": f"http://127.0.0.1:{port}"}, "fixture")))
                    url = self.ws_url.removesuffix("/relay/ws").replace("ws://", "http://")
                    client = plugin.relay.RelayClient(plugin.config.RelayConfig(url, REMOTE, CREDENTIAL, "", str(directory)))
                    client.start()
                    try:
                        until = time.monotonic() + 5
                        while not client.generation and time.monotonic() < until:
                            time.sleep(0.05)
                        self.assertTrue(client.generation)
                        self.assertEqual(relay_server.active_link(REMOTE).protocol_version, 1)
                        with hermes_gateway._relay_lease(REMOTE) as lease:
                            self.assertEqual(lease.request_mcp_snapshot(3)["code_version"], tag)
                            self.assertEqual(lease.rpc("session.list", title="test", timeout=4), {"sessions": []})
                    finally:
                        client.stop()
                        client._thread.join(5)
