"""Isolated outbound retry windows with a deterministic clock and synthetic sockets."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from engine import db, relay, relay_dialer, relay_retry
from scripts import test_relay_plugin as plugin_tests
from scripts.test_relay_dialer import DialerBase, REMOTE


DELAYS = [1, 2, 4, 8, 16] + [30] * 19


def refused(code):
    return ConnectionClosedError(Close(code, ""), None)


class EngineWindowTests(DialerBase):
    def setUp(self):
        super().setUp()
        self._set_endpoint(1, fingerprint=hashlib.sha256(b"test cert").hexdigest())
        self.worker = {"stop": Mock(wraps=threading.Event()), "abandoned": False}
        self.delays = []
        self.worker["stop"].wait.side_effect = self.delays.append

    def run_window(self, failure):
        ws = MagicMock()
        ws.socket.getpeercert.return_value = b"test cert"
        ws.recv.side_effect = failure
        with patch("websockets.sync.client.connect", return_value=ws) as dial, \
                patch.object(relay_retry, "time") as clock, \
                self.assertLogs(relay_dialer.logger, "INFO") as logs:
            clock.monotonic.side_effect = lambda: sum(self.delays)
            relay_dialer.dial_loop(REMOTE, self.worker)
        return dial.call_count, " ".join(logs.output)

    def test_auth_timeouts_use_full_budget_and_remain_stopped(self):
        attempts, logs = self.run_window(TimeoutError())
        self.assertEqual((attempts, self.delays), (25, DELAYS))
        self.assertIn("attempts=25 offline_seconds=601.0", logs)
        self.assertIn("'等待认证回执超时': 25", logs)
        self.assertEqual(logs.count("dial unavailable"), 1)
        with patch.object(relay_dialer, "start_dial") as dial:
            relay_dialer.restore_on_startup()
            dial.assert_not_called()

    def test_online_disconnect_logged_before_next_dial_then_summarized(self):
        # A real handshake receipt followed by loss of the established transport.
        socket = MagicMock()
        socket.socket.getpeercert.return_value = b"test cert"
        socket.recv.side_effect = [json.dumps({"relay": {"kind": "hello_ok", "generation": 1}}),
                                   ConnectionResetError()]
        def connect(*args, **kwargs):
            if dial.call_count == 1:
                return socket
            self.assertEqual(sum("dial unavailable" in line for line in logs.output), 1)
            raise TimeoutError()
        with self.assertLogs("engine", "INFO") as logs, \
                patch.object(relay_dialer, "MAX_ATTEMPTS", 1), \
                patch("websockets.sync.client.connect", side_effect=connect) as dial:
            relay_dialer.dial_loop(REMOTE, self.worker)
        outage = [line for line in logs.output if "dial unavailable" in line or "relay offline member=" in line]
        self.assertEqual(len(outage), 1)
        self.assertIn("ConnectionResetError", outage[0])
        summary, = [line for line in logs.output if "dial abandoned" in line]
        self.assertIn("attempts=2", summary)
        self.assertIn("'dial receive failed: ConnectionResetError': 1", summary)

    def test_heartbeat_send_failure_preserves_first_cause(self):
        socket = MagicMock()
        socket.socket.getpeercert.return_value = b"test cert"
        socket.recv.side_effect = [json.dumps({"relay": {"kind": "hello_ok", "generation": 1}}),
                                   TimeoutError()]
        socket.send.side_effect = [None, BrokenPipeError()]
        with self.assertLogs("engine", "INFO") as logs, \
                patch.object(relay_dialer, "MAX_ATTEMPTS", 0), \
                patch.object(relay_dialer, "HEARTBEAT_INTERVAL", 0), \
                patch("websockets.sync.client.connect", side_effect=[socket, TimeoutError()]):
            relay_dialer.dial_loop(REMOTE, self.worker)
        outage = [line for line in logs.output if "dial unavailable" in line or "relay offline member=" in line]
        self.assertEqual(len(outage), 1)
        self.assertIn("send failed: BrokenPipeError", outage[0])

    def test_explicit_handshake_refusals_stop_at_once(self):
        for code, stage in [(4001, "auth"), (4002, "mcp"), (4003, "protocol"),
                            (4004, "auth"), (4005, "direction")]:
            with self.subTest(code=code):
                attempts, logs = self.run_window(refused(code))
                self.assertEqual((attempts, self.delays), (1, []))
                self.assertEqual(relay_dialer.dial_failure(REMOTE)["stage"], stage)
                self.assertIn("result=rejected attempts=1", logs)

    def test_direction_close_after_switch_returns_to_listening(self):
        def switch_to_listen(**kwargs):
            with closing(db.get_connection()) as conn:
                relay.set_direction(conn, REMOTE, "listen")
            raise refused(4005)
        ws = MagicMock()
        ws.socket.getpeercert.return_value = b"test cert"
        ws.recv.side_effect = switch_to_listen
        with patch("websockets.sync.client.connect", return_value=ws) as dial:
            relay_dialer.dial_loop(REMOTE, self.worker)
            dial.assert_called_once()
        self.assertFalse(self.worker["abandoned"])
        self.assertEqual(self.delays, [])

    def test_listen_target_does_not_attempt_a_connection(self):
        with closing(db.get_connection()) as conn:
            relay.set_direction(conn, REMOTE, "listen")
        with patch("websockets.sync.client.connect") as dial:
            relay_dialer.dial_loop(REMOTE, self.worker)
            dial.assert_not_called()


class PluginWindowTests(unittest.TestCase):
    def setUp(self):
        self.plugin = plugin_tests._plugin
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.enterContext(patch.object(self.plugin.pairing, "_state_path",
                                      return_value=Path(directory.name) / "pairing.json"))
        self.cfg = plugin_tests.make_cfg("ws://example.invalid")
        self.plugin.pairing.save_register_credential(self.cfg.member, self.cfg.credential,
                                                     engine_url=self.cfg.engine_url)
        self.client = self.plugin.relay.RelayClient(self.cfg)
        self.delays = []
        self.enterContext(patch.dict(os.environ, {
            "HERMES_PROFILE_NAME": self.cfg.member,
            "HERMES_SESSION_CHAT_TYPE": "dm", "HERMES_SESSION_PLATFORM": "chat",
            "HERMES_SESSION_ACTOR_ID": "operator"}))

    def run_window(self, failure):
        ws = MagicMock()
        ws.__enter__.return_value = ws
        ws.recv.side_effect = failure

        def wait(timeout):
            if self.plugin.pairing.outbound_state()["state"] == "stopped":
                self.client._stop.set()
            else:
                self.delays.append(timeout)

        with patch("websockets.sync.client.connect", return_value=ws) as dial, \
                patch.object(self.client._retry, "wait", side_effect=wait), \
                patch.object(relay_retry, "time") as clock, \
                self.assertLogs(self.plugin.relay.logger, "INFO") as logs:
            clock.monotonic.side_effect = lambda: sum(self.delays)
            self.client._run()
        return dial.call_count, " ".join(logs.output)

    def test_timeout_stop_survives_restart_and_dm_rearms_once(self):
        attempts, logs = self.run_window(TimeoutError())
        self.assertEqual((attempts, self.delays), (25, DELAYS))
        self.assertEqual(logs.count("link down"), 1)
        self.assertIn("result=exhausted attempts=25 offline_seconds=601.0", logs)
        self.assertIn("'等待认证回执超时': 25", logs)
        stopped = self.plugin.pairing.status()["engines"][0]["outbound"]
        self.assertEqual(stopped["state"], "stopped")
        self.client = self.plugin.relay.RelayClient(self.cfg)
        with patch("websockets.sync.client.connect") as dial, \
                patch.object(self.client._retry, "wait", side_effect=lambda **_: self.client._stop.set()):
            self.client._run()
            dial.assert_not_called()
        tool = self.plugin.tools.bot_group_reconnect
        with patch.dict(os.environ, {"HERMES_SESSION_CHAT_TYPE": "group"}):
            self.assertFalse(json.loads(tool({}))["ok"])
            self.assertEqual(self.plugin.pairing.outbound_state(), stopped)
        for _ in range(2):
            self.assertEqual(json.loads(tool({}))["state"], "requested")
        self.client = self.plugin.relay.RelayClient(self.cfg)
        ws = MagicMock()
        ws.__enter__.return_value = ws
        ws.recv.return_value = json.dumps({"relay": {"kind": "hello_ok", "generation": 7}})
        def online(*args):
            for _ in range(2):
                self.assertEqual(json.loads(tool({}))["state"], "online")
            self.client._stop.set()
        with patch("websockets.sync.client.connect", return_value=ws) as dial, \
                patch.object(self.client, "_recv_loop", side_effect=online):
            self.client._run()
            dial.assert_called_once()
        self.assertEqual(self.client.generation, 7)

    def test_explicit_refusals_stop_at_once(self):
        for code in (4001, 4002, 4003, 4004, 4005):
            with self.subTest(code=code):
                self.plugin.pairing.resume_outbound()
                self.client = self.plugin.relay.RelayClient(self.cfg)
                attempts, logs = self.run_window(refused(code))
                self.assertEqual((attempts, self.delays), (1, []))
                self.assertIn("result=rejected attempts=1", logs)

    def test_direction_close_adopts_selected_listener(self):
        selected = threading.Event()
        def switch_to_listen(**kwargs):
            selected.set()
            raise refused(4005)
        ws = MagicMock()
        ws.__enter__.return_value = ws
        ws.recv.side_effect = switch_to_listen
        with patch("websockets.sync.client.connect", return_value=ws) as dial, \
                patch.object(self.plugin.pairing, "listener_selected", side_effect=lambda *a, **k: selected.is_set()), \
                patch.object(self.client, "_open_listener") as listen, \
                patch.object(self.client._retry, "wait", side_effect=lambda **_: self.client._stop.set()):
            self.client._run()
            dial.assert_called_once()
            listen.assert_called_once()
            response = json.loads(self.plugin.tools.bot_group_reconnect({}))
            self.assertFalse(response["ok"])
            self.assertEqual(response["state"], "listening")

    def test_dm_selects_only_the_named_paired_engine(self):
        pairing = self.plugin.pairing
        pairing.set_outbound_state("stopped", reason="timeout")
        other = "https://other.example.invalid"
        peer = pairing.select_peer(self.cfg.member, engine_url=other)
        with pairing.using_peer(peer):
            pairing.save_register_credential(self.cfg.member, "other-test-key", engine_url=other)
            pairing.set_outbound_state("stopped", reason="timeout")
        tool = self.plugin.tools.bot_group_reconnect
        self.assertFalse(json.loads(tool({}))["ok"])
        self.assertFalse(json.loads(tool({"engine_url": "http://unknown.invalid"}))["ok"])
        self.assertTrue(json.loads(tool({"engine_url": other}))["ok"])
        self.assertEqual(pairing.outbound_state()["state"], "stopped")
        with pairing.using_peer(peer):
            self.assertEqual(pairing.outbound_state()["state"], "requested")

    def test_crashed_worker_cannot_confirm_online(self):
        pairing = self.plugin.pairing
        script = """from pathlib import Path
from unittest.mock import patch
import os, sys
from scripts.test_relay_plugin import _plugin
with patch.object(_plugin.pairing, '_state_path', return_value=Path(sys.argv[1])):
    with _plugin.pairing.outbound_worker() as owner:
        _plugin.pairing.set_outbound_state('online', worker=owner)
        os._exit(0)
"""
        subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", script, str(pairing._state_path())],
                       cwd=Path(__file__).resolve().parents[1], timeout=15, check=True)
        self.assertEqual(pairing.outbound_state()["state"], "offline")
        self.assertEqual(json.loads(self.plugin.tools.bot_group_reconnect({}))["state"], "requested")

    def test_stopped_gateway_cross_process_request_and_restart(self):
        engine = plugin_tests.FakeEngine().start()
        self.addCleanup(engine.stop)
        cfg = plugin_tests.make_cfg(engine.url)
        pairing = self.plugin.pairing
        pairing.save_register_credential(cfg.member, cfg.credential, engine_url=cfg.engine_url)

        def dm_from_other_process():
            script = """from pathlib import Path
from unittest.mock import patch
import sys
from scripts.test_relay_plugin import _plugin
with patch.object(_plugin.pairing, '_state_path', return_value=Path(sys.argv[1])):
    print(_plugin.tools.bot_group_reconnect({}))
"""
            output = subprocess.check_output([sys.executable, "-X", "utf8", "-B", "-c", script,
                                              str(pairing._state_path())], encoding="utf-8",
                                             cwd=Path(__file__).resolve().parents[1], timeout=15)
            return json.loads(output)["state"]

        def wait_online():
            deadline = time.monotonic() + 5
            while pairing.outbound_state().get("state") != "online":
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)

        client = self.plugin.relay.gateway_client(cfg, "")
        try:
            wait_online()
            self.assertEqual(dm_from_other_process(), "online")
        finally:
            self.plugin.relay.stop_gateway_client(client)
        self.assertFalse(client._thread.is_alive())
        self.assertNotIn(client, self.plugin.relay._GATEWAY_CLIENTS.values())
        self.assertEqual(pairing.status()["engines"][0]["outbound"]["state"], "offline")
        # Records from previous gateways lack a live worker identity.
        pairing.set_outbound_state("online")
        self.assertEqual(pairing.outbound_state()["state"], "offline")
        self.assertEqual(dm_from_other_process(), "requested")
        self.assertEqual(json.loads(self.plugin.tools.bot_group_reconnect({}))["state"], "requested")
        self.assertEqual(len(engine._clients), 1)
        client = self.plugin.relay.gateway_client(cfg, "")
        try:
            wait_online()
            self.assertEqual(dm_from_other_process(), "online")
            self.assertEqual(len(engine._clients), 2)
        finally:
            self.plugin.relay.stop_gateway_client(client)

    def test_live_gateway_worker_resumes_once_from_admin_dm(self):
        engine = plugin_tests.FakeEngine().start()
        self.addCleanup(engine.stop)
        cfg = plugin_tests.make_cfg(engine.url)
        self.plugin.pairing.save_register_credential(cfg.member, cfg.credential, engine_url=cfg.engine_url)
        calls = []
        def connect(uri, **kwargs):
            calls.append(uri)
            if len(calls) <= 25:
                raise TimeoutError()
            return plugin_tests.ws_connect(uri, **kwargs)

        def wait_for(state):
            deadline = time.monotonic() + 5
            while self.plugin.pairing.outbound_state().get("state") != state:
                self.assertLess(time.monotonic(), deadline, state)
                time.sleep(0.01)

        tool = self.plugin.tools.bot_group_reconnect
        with patch.object(self.plugin.config, "host_home", return_value=cfg.home), \
                patch.object(relay_retry, "DELAYS", (0,) * 24), \
                patch("websockets.sync.client.connect", side_effect=connect):
            client = self.plugin.relay.gateway_client(cfg, "")
            try:
                wait_for("stopped")
                self.assertEqual(len(calls), 25)
                for _ in range(2):
                    self.assertTrue(json.loads(tool({}))["ok"])
                wait_for("online")
                self.assertEqual(json.loads(tool({}))["state"], "online")
                self.assertIs(self.plugin.relay.gateway_client(cfg, ""), client)
                self.assertEqual((len(calls), len(engine._clients)), (26, 1))
            finally:
                self.plugin.relay.stop_gateway_client(client)
