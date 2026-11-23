"""Verify slow remote history through the same HTTP routes used by the dashboard."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine import api, db, hermes_gateway, relay, remote_reasoning
from scripts.unified_test_support import isolated_database


class RemotePollingTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_database())
        self.enterContext(patch.object(remote_reasoning, "_reads", {}))
        pool = self.enterContext(ThreadPoolExecutor(max_workers=2))
        self.enterContext(patch.object(remote_reasoning, "_workers", pool))
        app = FastAPI()
        app.include_router(api.router_api)
        self.http = self.enterContext(TestClient(app, client=("127.0.0.1", 1234)))
        self.client = MagicMock()
        self.enterContext(patch.object(hermes_gateway, "connection", return_value=(self.client, None, [])))
        with closing(db.get_connection()) as conn:
            relay.register_node(conn, "remote-a", "fixture-key")
            generation = relay.generation_advance(conn, "remote-a")
            relay.mark_online(conn, "remote-a", code_version="", mcp_fingerprint="", generation=generation)
            for start in (100, 200):
                conn.execute("INSERT INTO wake_runs(session,profile,runtime_session_id,started_at,ended_at) "
                             "VALUES('work','remote-a','base',?,?)", (start, start + 50))
            conn.commit()

    def poll(self, start, cursor=0, **bounds):
        return self.http.get("/reasoning", params={"profile": "remote-a", "session": "work",
                             "wake_started_at": start, "after_id": cursor, **bounds})

    def test_blocked_gateway_keeps_local_routes_available_and_shares_windows(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def rpc(method, **params):
            if method == "session.resume":
                entered.set()
                if not release.wait(8):
                    raise TimeoutError
                return {"session_id": "continued"}
            self.assertEqual(params["session_id"], "continued")
            return {"content": json.dumps({"messages": [
                {"id": i, "role": "assistant", "timestamp": t, "reasoning": str(t)}
                for i, t in [(1, 110), (2, 210)]]})}
        self.client.rpc.side_effect = rpc
        self.assertEqual(self.poll(100).status_code, 202)
        self.assertTrue(entered.wait(1))
        for start in (100, 200, 100):
            self.assertEqual(self.poll(start).status_code, 202)
        nodes = self.http.get("/relay/nodes")
        self.assertEqual(nodes.json()[0]["member"], "remote-a")
        self.assertFalse(release.is_set())
        self.assertEqual(self.client.rpc.call_count, 1)
        release.set()
        with remote_reasoning._lock:
            future = next(iter(remote_reasoning._reads.values()))
        future.result(2)
        for start, expected in [(100, 1), (200, 2)]:
            response = self.poll(start)
            self.assertEqual(response.status_code, 200)
            self.assertEqual([row["id"] for row in response.json()["entries"]], [expected])
            self.assertEqual(response.json()["process_ended_at"], start + 50)
        self.assertEqual(self.poll(200, 2).json()["entries"], [])
        self.assertEqual(self.client.rpc.call_count, 2)

    def test_ended_run_gets_snapshot_after_final_flush(self):
        def rpc(method, **params):
            if method == "session.resume":
                return {"session_id": "continued"}
            return {"content": json.dumps({"messages": list(messages)})}
        messages = []
        self.client.rpc.side_effect = rpc
        self.assertEqual(self.poll(100).status_code, 202)
        with remote_reasoning._lock:
            first = next(iter(remote_reasoning._reads.values()))
        _, captured, _ = first.result(2)
        messages.append({"id": 3, "timestamp": 130, "role": "assistant", "content": "done"})
        # Mark completion after the first export, even with a coarse system clock.
        ended = captured + 0.001
        while time.time() < ended:
            time.sleep(0.002)
        with closing(db.get_connection()) as conn, conn:
            conn.execute("UPDATE wake_runs SET ended_at=? WHERE started_at=100", (ended,))
        self.assertEqual(self.poll(100, wake_ended_at=150).status_code, 202)
        with remote_reasoning._lock:
            final = next(iter(remote_reasoning._reads.values()))
        final.result(2)
        response = self.poll(100).json()
        self.assertTrue(response["entries"][0]["finished"])
        self.assertEqual(response["process_ended_at"], ended)

    def test_timeout_backs_off_without_advancing_or_finishing(self):
        self.client.rpc.side_effect = TimeoutError
        self.assertEqual(self.poll(100).status_code, 202)
        with remote_reasoning._lock:
            future = next(iter(remote_reasoning._reads.values()))
        future.result(2)
        for _ in range(4):
            response = self.poll(100, 7)
            self.assertEqual(response.status_code, 202)
            self.assertGreater(response.json()["retry_after_ms"], 1000)
            self.assertNotIn("process_ended_at", response.json())
        self.assertEqual(self.client.rpc.call_count, 1)
