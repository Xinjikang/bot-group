"""req#171：来源 IP 白名单——HTTP 中间件与 relay WS 拦截的隔离回归。

- 回环 127.0.0.1/::1 恒放行；白名单内精确放行；白名单外全拒（403）。
- 不信任 X-Forwarded-For/X-Real-IP 转发头——伪造头不能获得白名单资格或本机权限。
- 移除即时生效（无缓存）；精确 IP 匹配，不自动放行网段。
- relay WS 与 HTTP 同规则（socket peer IP，同一张表）。
- 管理 API 仅管理员（bot 身份 403）；成员凭据校验保持独立。
"""
import unittest
from contextlib import closing
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from engine import db, main, relay_server, signing_keys
from scripts.unified_test_support import isolated_database

REMOTE_A = "192.168.1.50"   # 白名单内来源（测试中登记）
REMOTE_B = "10.0.0.9"       # 白名单外来源
IPV6_A = "fd00::1"          # IPv6 精确登记


class IpAllowlistTests(unittest.TestCase):
    def setUp(self):
        self.fixture = isolated_database()
        self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        # main.app 的 lifespan 不经 TestClient 触发；表已由 isolated_database 建好。
        with closing(db.get_connection()) as conn:
            signing_keys.initialize(conn)
        self.loop = TestClient(main.app, client=("127.0.0.1", 4321))
        self.addCleanup(self.loop.close)

    def remote(self, ip):
        client = TestClient(main.app, client=(ip, 4321))
        self.addCleanup(client.close)
        return client

    def add_ip(self, ip):
        return self.loop.post("/admin/source-ips", json={"ip": ip})

    # ---- 回环恒放行 ----

    def test_loopback_always_allowed(self):
        for ip in ("127.0.0.1", "::1"):
            with self.subTest(peer=ip):
                self.assertEqual(self.remote(ip).get("/health").status_code, 200)

    def test_local_existing_paths_unaffected(self):
        # 本机既有路径回归：回环下中间件一律放行，行为不变
        for path in ("/auth/status", "/commands", "/projects", "/members"):
            response = self.loop.get(path)
            self.assertNotEqual(response.status_code, 403, f"{path} 被白名单中间件误拒")

    # ---- 白名单内放行 / 白名单外拒绝 ----

    def test_unlisted_remote_rejected(self):
        response = self.remote(REMOTE_B).get("/health")
        self.assertEqual(response.status_code, 403)
        self.assertIn("白名单", response.json()["detail"])

    def test_allowlisted_remote_allowed(self):
        self.assertEqual(self.add_ip(REMOTE_A).status_code, 200)
        self.assertEqual(self.remote(REMOTE_A).get("/health").status_code, 200)

    def test_ipv6_exact_match_no_subnet(self):
        self.assertEqual(self.add_ip(IPV6_A).status_code, 200)
        self.assertEqual(self.remote(IPV6_A).get("/health").status_code, 200)
        self.assertEqual(self.remote("fd00::2").get("/health").status_code, 403,
                         "精确 IP 匹配，不自动放行网段")

    # ---- 伪造转发头无效 ----

    def test_forged_forwarded_headers_rejected(self):
        self.assertEqual(self.add_ip(REMOTE_A).status_code, 200)
        for headers in ({"X-Forwarded-For": REMOTE_A}, {"X-Real-IP": REMOTE_A},
                        {"X-Forwarded-For": "127.0.0.1"}, {"X-Forwarded-For": REMOTE_A, "X-Real-IP": REMOTE_A}):
            with self.subTest(headers=headers):
                response = self.remote(REMOTE_B).get("/health", headers=headers)
                self.assertEqual(response.status_code, 403,
                                 f"伪造转发头 {headers} 不得获得白名单资格或本机权限")

    # ---- 移除即时生效（无缓存） ----

    def test_remove_takes_effect_immediately(self):
        self.assertEqual(self.add_ip(REMOTE_A).status_code, 200)
        self.assertEqual(self.remote(REMOTE_A).get("/health").status_code, 200)
        self.assertEqual(self.loop.delete(f"/admin/source-ips/{REMOTE_A}").status_code, 200)
        # 无缓存：移除后的下一次判定即拒绝
        self.assertEqual(self.remote(REMOTE_A).get("/health").status_code, 403)

    # ---- 管理 API：登记约束与权限 ----

    def test_registration_validation(self):
        self.assertEqual(self.add_ip("not-an-ip").status_code, 400)
        for loop in ("127.0.0.1", "::1"):
            self.assertEqual(self.add_ip(loop).status_code, 400, "回环恒放行，无需登记")
        # 重复登记幂等
        self.assertEqual(self.add_ip(REMOTE_A).status_code, 200)
        self.assertEqual(self.add_ip(REMOTE_A).status_code, 200)
        listing = self.loop.get("/admin/source-ips").json()
        self.assertEqual([e["ip"] for e in listing["entries"]], [REMOTE_A])
        self.assertEqual(listing["loopback_always_allowed"], ["127.0.0.1", "::1"])
        self.assertTrue(all(e["active"] for e in listing["entries"]))

    def test_admin_api_rejects_bot_identity(self):
        with closing(db.get_connection()) as conn:
            from engine import auth
            bot_headers = {"X-Group-Profile": "default",
                           "X-Group-Key": auth.bot_token("default")}
        for method, path, body in (("GET", "/admin/source-ips", None),
                                   ("POST", "/admin/source-ips", {"ip": REMOTE_A}),
                                   ("DELETE", f"/admin/source-ips/{REMOTE_A}", None)):
            with self.subTest(method=method):
                response = self.loop.request(method, path, json=body, headers=bot_headers)
                self.assertEqual(response.status_code, 403, "白名单管理不对 bot 身份开放")
        # 白名单管来源、凭据管身份：白名单外来源带合法 bot 凭据同样被拒（先拒来源）
        with closing(db.get_connection()) as conn:
            from engine import auth
            bot_headers = {"X-Group-Profile": "default",
                           "X-Group-Key": auth.bot_token("default")}
        self.assertEqual(self.remote(REMOTE_B).get("/commands", headers=bot_headers).status_code, 403)

    # ---- relay WS 同规则 ----

    def test_relay_ws_same_rules(self):
        async def fake_relay(websocket):
            # 完成握手 accept 后即返回（只验证白名单拦截层，不测 relay 协议）
            await websocket.accept()

        with patch.object(relay_server, "relay_ws", side_effect=fake_relay) as stub:
            # 白名单外来源：握手即拒（close 1008），不进 relay 协议层
            with self.assertRaises(WebSocketDisconnect) as caught:
                with self.remote(REMOTE_B).websocket_connect("/relay/ws"):
                    pass
            self.assertEqual(caught.exception.code, 1008)
            self.assertEqual(stub.call_count, 0, "白名单外来源不得进入 relay 协议层")
            # 回环恒放行：进入 relay 协议层（stub 被调用）
            with self.loop.websocket_connect("/relay/ws"):
                pass
            self.assertEqual(stub.call_count, 1)
            # 白名单内来源：同样放行
            self.assertEqual(self.add_ip(REMOTE_A).status_code, 200)
            with self.remote(REMOTE_A).websocket_connect("/relay/ws"):
                pass
            self.assertEqual(stub.call_count, 2)


if __name__ == "__main__":
    unittest.main()
