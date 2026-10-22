"""Isolated HTTP authentication, authorization and credential transport regressions."""

import base64
import os
import threading
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine import access, api, auth, db, wake
from scripts.unified_test_support import isolated_database


class AccessContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = isolated_database()
        self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        self.env = patch.dict(os.environ, {"GROUP_ADMIN_PASSWORD": "测试密码"})
        self.env.start()
        self.addCleanup(self.env.stop)
        auth._LOGIN_ATTEMPTS.clear()
        self.app = FastAPI()
        self.app.include_router(api.router_api)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.admin = {"Authorization": "Bearer " + auth.login("测试密码")["token"]}
        self.bot = {"X-Group-Profile": "default", "X-Group-Key": auth.bot_token("default")}

    def test_all_api_routes_reject_anonymous_requests(self):
        import re
        for path_template, operations in self.app.openapi()["paths"].items():
            for method in operations:
                method = method.upper()
                if (method, path_template) in access.PUBLIC:
                    continue
                path = re.sub(r"\{[^}]+\}", "1", path_template)
                with self.subTest(method=method, path=path):
                    response = self.client.request(method, path, json={})
                    self.assertEqual(response.status_code, 401, response.text)

    def test_member_key_is_bound_to_profile_and_has_no_admin_rights(self):
        for headers in (
            {"X-Group-Profile": "default"},
            {**self.bot, "X-Group-Profile": "architect"},
            {**self.bot, "X-Group-Key": "invalid"},
            {**self.bot, "X-Group-Profile": "user", "X-Group-Key": auth.bot_token("user")},
        ):
            self.assertEqual(self.client.get("/commands", headers=headers).status_code, 401)
        for path in ("/discovery/scan", "/messages", "/thinking", "/tasks", "/events"):
            self.assertEqual(self.client.get(path, headers=self.bot).status_code, 403)
        for path in ("/members", "/thread/1/close", "/thread/1/members", "/approval/1/resolve"):
            self.assertEqual(self.client.post(path, json={}, headers=self.bot).status_code, 403)
        self.assertEqual(self.client.get("/commands", headers=self.bot).status_code, 200)
        self.assertEqual(self.client.get("/members", headers=self.admin).status_code, 200)

    def test_login_unicode_types_rate_limit_and_expiry(self):
        for value in ("错误", None, 123, {}, []):
            self.assertEqual(self.client.post("/auth/login", json={"password": value}).status_code, 401)
        result = self.client.post("/auth/login", json={"password": "测试密码"})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(auth.verify_token(result.json()["token"]))
        auth._LOGIN_ATTEMPTS.clear()
        with patch.object(auth.time, "monotonic", return_value=100):
            for _ in range(auth.LOGIN_LIMIT):
                self.assertEqual(self.client.post("/auth/login", json={"password": "wrong"}).status_code, 401)
            denied = self.client.post("/auth/login", json={"password": "测试密码"})
            self.assertEqual(denied.status_code, 429)
            self.assertEqual(denied.headers["retry-after"], "60")
        with patch.object(auth.time, "monotonic", return_value=161):
            self.assertEqual(self.client.post("/auth/login", json={"password": "测试密码"}).status_code, 200)
        malformed = base64.urlsafe_b64encode("admin:9999999999.中文".encode()).decode()
        self.assertFalse(auth.verify_token(malformed))
        self.assertIn("error", auth.login("\ud800"))

    def test_development_mode_requires_loopback_and_same_origin(self):
        with patch.dict(os.environ, {"GROUP_ADMIN_PASSWORD": "", "GROUP_SECRET": ""}):
            self.assertEqual(self.client.get("/commands").status_code, 401)
            payload = "admin:9999999999"
            forged = base64.urlsafe_b64encode(
                f"{payload}.{auth._sign(payload)}".encode()).decode()
            self.assertFalse(auth.verify_token(forged))
            self.assertEqual(self.client.get("/commands", headers={
                "Authorization": "Bearer " + forged}).status_code, 401)
            with TestClient(self.app, client=("127.0.0.1", 4321)) as local:
                self.assertEqual(local.get("/commands").status_code, 200)
                self.assertEqual(local.get("/commands", headers={"Origin": "https://evil.test"}).status_code, 403)
                self.assertEqual(local.get("/commands", headers={"Authorization": "Bearer bad"}).status_code, 401)
                self.assertEqual(local.get("/commands", headers={"X-Group-Profile": "default"}).status_code, 401)

    def test_runtime_inbox_and_project_scopes(self):
        self.assertEqual(self.client.get("/inbox?profile=architect", headers=self.bot).status_code, 403)
        self.assertEqual(self.client.get("/wake/check?profile=architect", headers=self.bot).status_code, 403)
        self.assertEqual(self.client.get("/members", headers=self.bot).status_code, 403)
        self.assertEqual(self.client.get("/members?project=alpha", headers=self.bot).status_code, 200)
        conn = db.get_connection()
        conn.execute("UPDATE project_members SET active=0 WHERE project='beta' AND profile='default'")
        conn.commit()
        conn.close()
        self.assertEqual(self.client.get("/members?project=beta", headers=self.bot).status_code, 403)
        response = self.client.post("/task-tools/project_list", json={"arguments": {}}, headers=self.bot)
        self.assertEqual([p["name"] for p in response.json()["projects"]], ["alpha"])
        with patch("engine.heartbeat.beat") as beat:
            self.assertEqual(self.client.post("/heartbeat", json={"profile": "architect"}, headers=self.bot).status_code, 403)
            beat.assert_not_called()
            self.assertEqual(self.client.post("/heartbeat", json={}, headers=self.bot).status_code, 200)
            self.assertEqual(beat.call_args.args[0], "default")
            self.assertEqual(self.client.post("/heartbeat", json={"profile": "default"}, headers=self.admin).status_code, 403)

    def test_topic_read_and_write_scope(self):
        conn = db.get_connection()
        ids = []
        for members in ('["default"]', '["architect"]'):
            ids.append(conn.execute(
                "INSERT INTO threads(project,title,creator,members,created_at,last_active_at) VALUES(?,?,?,?,?,?)",
                ("alpha", "Private", "user", members, db.now_iso(), db.now_iso())).lastrowid)
        conn.commit()
        conn.close()
        self.assertEqual(self.client.get(f"/thread?id={ids[0]}", headers=self.bot).status_code, 200)
        self.assertEqual(self.client.get(f"/thread?id={ids[1]}", headers=self.bot).status_code, 403)
        result = self.client.get("/threads", headers=self.bot).json()
        self.assertEqual([t["id"] for t in result["threads"]], [ids[0]])
        denied = self.client.post("/msg", json={"thread_id": ids[1], "project": "alpha",
            "text": "intrude", "need_reply": False,
            "execution": {"session": "test", "wake_started_at": 1}}, headers=self.bot)
        self.assertEqual(denied.status_code, 403)

    def test_sse_cookie_refresh_logout_and_member_injection(self):
        result = self.client.post("/auth/events", headers=self.admin)
        self.assertEqual(result.status_code, 200)
        self.assertIn("HttpOnly", result.headers["set-cookie"])
        self.assertIn("Path=/events", result.headers["set-cookie"])
        self.assertEqual(self.client.get("/members").status_code, 401)
        self.app.state.stop_event = threading.Event()
        self.app.state.stop_event.set()
        self.assertEqual(self.client.get("/events").status_code, 200)
        self.assertEqual(self.client.post("/auth/logout", headers=self.admin).status_code, 200)
        self.assertEqual(self.client.get("/events").status_code, 401)
        env = wake._mcp_server_config("default")["env"]
        self.assertEqual(env["GROUP_ENGINE_TOKEN"], auth.bot_token("default"))
        self.assertNotEqual(env["GROUP_ENGINE_TOKEN"], auth.bot_token("architect"))
        context = {"profile": "default", "project": "alpha", "thread_id": None,
                   "session": "test-work", "wake_started_at": 100}
        scoped = wake._mcp_server_config("default", context=context)["env"]
        self.assertEqual(auth.execution_scope("default", scoped["GROUP_ENGINE_TOKEN"]), context)
        self.assertNotEqual(scoped["GROUP_ENGINE_TOKEN"], auth.bot_token("default"))

    def test_member_provisioning_and_health_path_visibility(self):
        response = self.client.post("/auth/member-token", headers=self.admin, json={"profile": "default"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["token"], self.bot["X-Group-Key"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.client.post("/auth/member-token", headers=self.bot, json={"profile": "architect"}).status_code, 403)
        self.assertEqual(self.client.post("/auth/member-token", headers=self.admin, json={"profile": " user "}).status_code, 422)
        with patch.dict(os.environ, {"GROUP_ENGINE_KEY": "isolated-key-one"}):
            token = auth.bot_token("default")
            self.assertTrue(auth.verify_bot("default", token))
            self.assertFalse(auth.verify_bot("architect", token))
        with patch.dict(os.environ, {"GROUP_ENGINE_KEY": "isolated-key-two"}):
            self.assertFalse(auth.verify_bot("default", token))
        from engine.main import health
        self.app.add_api_route("/health", health)
        self.assertNotIn("db_path", self.client.get("/health").json())
        self.assertIn("db_path", self.client.get("/health", headers=self.admin).json())


if __name__ == "__main__":
    unittest.main()
