"""req#171：成员级来源绑定——HTTP/relay WS 两层访问控制回归（主人 12417）。

- 成员级绑定：两节点各配一 IP，重开设置各自对应（A 从 A 的 IP 放行、B 从 B 的放行）。
- 交换来源拒：A 成员凭据从 B 的已绑定 IP 发起 HTTP（403）与 relay WS（close 4004）均被拒。
- 回环 127.0.0.1/::1 恒放行；未绑定来源全拒（HTTP 403 / WS close 1008）。
- 不信任 X-Forwarded-For/X-Real-IP 转发头；归一化匹配（::ffff:x.x.x.x 折 IPv4）。
- 移除/变更即时生效（无缓存）；精确 IP 匹配不自动放行网段。
- 管理 API 按成员登记/回读/更新/移除；bot 身份 403；成员凭据校验不变。
"""
import unittest
from contextlib import closing
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from engine import auth, db, ip_allowlist, main, relay, relay_server, signing_keys
from scripts.unified_test_support import isolated_database

IP_A = "192.168.1.50"   # A 成员（architect）绑定来源
IP_B = "10.0.0.9"       # B 成员（default）绑定来源
IP_C = "172.16.8.8"     # 未绑定来源
IPV6_A = "fd00::1"      # IPv6 精确登记

RELEASE_VERSION = "v-test"
CRED_A = "cred-architect-source-binding"
CRED_B = "cred-default-source-binding"


class MemberSourceBindingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = isolated_database()
        self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        # main.app 的 lifespan 不经 TestClient 触发；表已由 isolated_database 建好。
        with closing(db.get_connection()) as conn:
            signing_keys.initialize(conn)
            relay.initialize(conn)
            relay.register_node(conn, "architect", CRED_A)
            relay.register_node(conn, "default", CRED_B)
        self._version_patch = patch.object(relay_server, "_engine_release_version",
                                           return_value=RELEASE_VERSION)
        self._version_patch.start()
        self.addCleanup(self._version_patch.stop)
        self.loop = TestClient(main.app, client=("127.0.0.1", 4321))
        self.addCleanup(self.loop.close)

    def remote(self, ip):
        client = TestClient(main.app, client=(ip, 4321))
        self.addCleanup(client.close)
        return client

    def bind(self, member, ips):
        response = self.loop.post("/admin/source-ips", json={"member": member, "ips": ips})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def bot_headers(self, profile):
        return {"X-Group-Profile": profile, "X-Group-Key": auth.bot_token(profile)}

    def relay_hello(self, member, credential):
        return {"member": member, "credential": credential,
                "code_version": RELEASE_VERSION, "plugin_version": RELEASE_VERSION,
                "mcp_fingerprint": "", "protocol_version": 1}

    # ---- 回环恒放行 + 本机既有路径 ----

    def test_loopback_always_allowed(self):
        for ip in ("127.0.0.1", "::1"):
            with self.subTest(peer=ip):
                self.assertEqual(self.remote(ip).get("/health").status_code, 200)

    def test_local_existing_paths_unaffected(self):
        for path in ("/auth/status", "/commands", "/projects", "/members"):
            response = self.loop.get(path)
            self.assertNotEqual(response.status_code, 403, f"{path} 被来源中间件误拒")

    # ---- 第一层：未绑定来源拒 / 已绑定并集放行 ----

    def test_unbound_source_rejected(self):
        response = self.remote(IP_C).get("/health")
        self.assertEqual(response.status_code, 403)
        self.assertIn("绑定", response.json()["detail"])

    def test_bound_source_reaches_second_layer(self):
        # 第一层只看并集：来源属于任一成员绑定即放行进入身份解析
        self.bind("architect", [IP_A])
        self.assertEqual(self.remote(IP_A).get("/health").status_code, 200)

    # ---- 第二层：成员级对应与交换来源 ----

    def test_two_nodes_each_bound_ip_work_independently(self):
        self.bind("architect", [IP_A])
        self.bind("default", [IP_B])
        # 各自对应：A 从 IP_A、B 从 IP_B 发起 bot 请求均放行
        self.assertEqual(self.remote(IP_A).get("/commands",
                                               headers=self.bot_headers("architect")).status_code, 200)
        self.assertEqual(self.remote(IP_B).get("/commands",
                                               headers=self.bot_headers("default")).status_code, 200)

    def test_swapped_source_http_rejected(self):
        # 交换来源冒用：A 成员凭据从 B 的已绑定 IP 发起 HTTP → 第二层拒绝
        self.bind("architect", [IP_A])
        self.bind("default", [IP_B])
        response = self.remote(IP_B).get("/commands", headers=self.bot_headers("architect"))
        self.assertEqual(response.status_code, 403, "A 成员不得从 B 的已绑定 IP 发起请求")
        response = self.remote(IP_A).get("/commands", headers=self.bot_headers("default"))
        self.assertEqual(response.status_code, 403, "B 成员不得从 A 的已绑定 IP 发起请求")

    # ---- relay WS：同规则 + 交换来源（握手成员级核对） ----

    def test_relay_ws_swap_source_rejected(self):
        self.bind("architect", [IP_A])
        self.bind("default", [IP_B])
        # A 凭据从 B 的 IP 连接：第一层放行（IP 在并集），握手第二层拒 4004
        with self.remote(IP_B).websocket_connect("wss://testserver/relay/ws") as ws:
            ws.send_json(self.relay_hello("architect", CRED_A))
            with self.assertRaises(WebSocketDisconnect) as caught:
                ws.receive_text()
            self.assertEqual(caught.exception.code, relay_server.CLOSE_BAD_SOURCE,
                             "交换来源（A 凭据 + B 的 IP）握手必须被拒")

    def test_relay_ws_bound_source_accepted(self):
        self.bind("architect", [IP_A])
        with self.remote(IP_A).websocket_connect("wss://testserver/relay/ws") as ws:
            ws.send_json(self.relay_hello("architect", CRED_A))
            reply = ws.receive_json()
            self.assertEqual(reply["relay"]["kind"], "hello_ok", "绑定内来源正常握手")

    def test_relay_ws_unbound_source_requires_invite_bootstrap(self):
        with self.assertRaises(WebSocketDisconnect) as blocked:
            with self.remote(IP_C).websocket_connect("wss://testserver/relay/ws"):
                self.fail("无可用邀请时，未绑定来源不应进入握手")
        self.assertEqual(blocked.exception.code, 1008)
        conn = db.get_connection()
        try:
            relay.issue_invitation(conn)
        finally:
            conn.close()
        with self.remote(IP_C).websocket_connect("wss://testserver/relay/ws") as ws:
            ws.send_json(self.relay_hello("architect", CRED_A))
            with self.assertRaises(WebSocketDisconnect) as caught:
                ws.receive_text()
            self.assertEqual(caught.exception.code,
                             relay_server.CLOSE_BAD_SOURCE,
                             "未绑定来源且无邀请的成员握手必须被端点拒绝")

    def test_outbound_pairing_decisions_are_admin_only(self):
        head = self.bot_headers('architect')
        path = '/relay/pairings/isolated-id'
        body = {'pairing_url': 'wss://127.0.0.1:1'}
        self.assertEqual(self.loop.get(path, headers=head).status_code, 403)
        for endpoint in ('/relay/pairings/preview', '/relay/pairings/import', path + '/confirm', path + '/cancel'):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(self.loop.post(endpoint, json=body, headers=head).status_code, 403)

    def test_relay_ws_unbound_source_invite_bootstrap_binds(self):
        # req#175 node2118 ①：从未绑定来源首接的唯一通道=管理员签发的一次
        # 性邀请——兑换成功把 peer 登记进该新成员绑定集合（来源 IP 从零
        # 走通），之后同成员同来源可用持久凭据重连；HTTP 仍被第一层拦
        # （邀请只引导 /relay/ws 握手，不扩大 HTTP 面）。
        conn = db.get_connection()
        try:
            issued = relay.issue_invitation(conn)
        finally:
            conn.close()
        with self.remote(IP_C).websocket_connect("wss://testserver/relay/ws") as ws:
            hello = self.relay_hello("remoteX", "")
            hello.update(invite=issued['invite_token'], join_proof='bc' * 32,
                         credential_commitment=relay.hash_credential('isolated-source-test'))
            ws.send_json(hello)
            reply = ws.receive_json()
            self.assertEqual(reply['relay']['kind'], 'hello_pending')
            with closing(db.get_connection()) as observer:
                self.assertFalse(ip_allowlist.source_allowed(observer, IP_C))
        self.assertEqual(self.remote(IP_C).get('/health').status_code, 403)
        path = f"/relay/invitations/{issued['pairing_id']}"
        denied = self.loop.post(path + '/confirm', headers=self.bot_headers('architect'))
        self.assertEqual(denied.status_code, 403)
        pending = self.loop.get(path).json()
        self.assertEqual((pending['state'], pending['member'], pending['peer_ip']),
                         ('pending_confirmation', 'remoteX', IP_C))
        confirmed = self.loop.post(path + '/confirm').json()
        self.assertEqual(confirmed['state'], 'confirmed')
        member = confirmed['node_member']
        persistent = 'isolated-source-test'
        conn = db.get_connection()
        try:
            self.assertFalse(ip_allowlist.member_allowed(conn, "remoteX", IP_C))
            self.assertTrue(ip_allowlist.member_allowed(conn, member, IP_C),
                            "兑换后 peer 已登记为该成员来源绑定")
            row = relay.read_node(conn, member)
            self.assertEqual(row["direction"], "listen")
            self.assertNotEqual(row["state"], "pending", "验证成功后正式登记")
        finally:
            conn.close()
        # 重连：持久凭据 + 已绑定来源（不再用邀请）
        with self.remote(IP_C).websocket_connect("wss://testserver/relay/ws") as ws:
            ws.send_json(self.relay_hello("remoteX", persistent))
            reply = ws.receive_json()
            self.assertEqual(reply["relay"]["kind"], "hello_ok")
        # HTTP 面：该来源已进入绑定并集，第一层放行到身份解析
        # （绑定建立前的 HTTP 仍 403——见 test_unbound_source_rejected）
        self.assertEqual(self.remote(IP_C).get("/health").status_code, 200)

    def test_relay_ws_loopback_accepted(self):
        # 回环恒放行：本机 hello 直接通过来源核对
        with self.loop.websocket_connect("wss://testserver/relay/ws") as ws:
            ws.send_json(self.relay_hello("architect", CRED_A))
            reply = ws.receive_json()
            self.assertEqual(reply["relay"]["kind"], "hello_ok")

    # ---- 伪造转发头无效 ----

    def test_forged_forwarded_headers_rejected(self):
        self.bind("architect", [IP_A])
        for headers in ({"X-Forwarded-For": IP_A}, {"X-Real-IP": IP_A},
                        {"X-Forwarded-For": "127.0.0.1"}, {"X-Forwarded-For": IP_A, "X-Real-IP": IP_A}):
            with self.subTest(headers=headers):
                response = self.remote(IP_C).get("/health", headers=headers)
                self.assertEqual(response.status_code, 403,
                                 f"伪造转发头 {headers} 不得获得绑定资格或本机权限")

    # ---- 归一化匹配 ----

    def test_normalization_ipv4_mapped_and_ipv6(self):
        self.bind("architect", [IP_A, IPV6_A])
        # ::ffff:x.x.x.x 折 IPv4 匹配；IPv6 压缩匹配；同网段其他地址不放行
        self.assertEqual(self.remote("::ffff:192.168.1.50").get("/health").status_code, 200)
        self.assertEqual(self.remote("192.168.1.50").get("/health").status_code, 200)
        self.assertEqual(self.remote("fd00:0:0:0:0:0:0:1").get("/health").status_code, 200)
        self.assertEqual(self.remote("fd00::2").get("/health").status_code, 403,
                         "精确 IP 匹配，不自动放行网段")
        self.assertEqual(self.remote("192.168.1.51").get("/health").status_code, 403)

    # ---- 移除/更新即时生效（无缓存） ----

    def test_remove_takes_effect_immediately(self):
        self.bind("architect", [IP_A])
        self.assertEqual(self.remote(IP_A).get("/health").status_code, 200)
        response = self.loop.delete(f"/admin/source-ips/architect/{IP_A}")
        self.assertEqual(response.status_code, 200)
        # 无缓存：移除后的下一次判定即拒绝
        self.assertEqual(self.remote(IP_A).get("/health").status_code, 403)

    def test_update_bindings_immediately(self):
        self.bind("architect", [IP_A])
        self.assertEqual(self.remote(IP_A).get("/health").status_code, 200)
        self.assertEqual(self.remote(IP_B).get("/health").status_code, 403)
        # 更新该成员绑定集合：IP_A 移出、IP_B 移入——立即生效
        self.bind("architect", [IP_B])
        self.assertEqual(self.remote(IP_A).get("/health").status_code, 403)
        self.assertEqual(self.remote(IP_B).get("/health").status_code, 200)

    # ---- 管理 API：按成员登记/回读 + 权限 + 约束 ----

    def test_bindings_listed_by_member(self):
        self.bind("architect", [IP_A, IPV6_A])
        self.bind("default", [IP_B])
        listing = self.loop.get("/admin/source-ips").json()
        by_member = {b["member"]: b["ips"] for b in listing["bindings"]}
        self.assertEqual(by_member["architect"], [IP_A, IPV6_A], "一成员多 IP 按成员回读")
        self.assertEqual(by_member["default"], [IP_B])
        self.assertEqual(listing["loopback_always_allowed"], ["127.0.0.1", "::1"])

    def test_source_network_labels_use_member_and_observed_ip(self):
        self.bind('architect', ['100.64.0.5', IP_A, '1.1.1.1', '2606:4700::1111'])
        self.bind('default', ['100.64.0.5'])
        with patch.object(relay_server, 'last_inbound_endpoints', return_value={'architect': ('100.64.0.5', '100.64.0.8')}), \
             patch('engine.listen_control.enumerate_interfaces', return_value=[
                 {'address': '100.64.0.8', 'name': 'Tailscale', 'network': 'Tailscale'}]):
            bindings = self.loop.get('/admin/source-ips').json()['bindings']
        labels = {item['member']: item['networks'] for item in bindings}
        self.assertEqual(labels['architect'], {'100.64.0.5': 'Tailscale', IP_A: '内网',
                                               '1.1.1.1': '公网', '2606:4700::1111': '公网'})
        self.assertEqual(labels['default'], {'100.64.0.5': '共享地址'})

    def test_same_ip_multiple_members(self):
        # 同 IP 多成员共享（bot-43 12418）
        self.bind("architect", [IP_A])
        self.bind("default", [IP_A])
        listing = self.loop.get("/admin/source-ips").json()
        members = {b["member"] for b in listing["bindings"] if IP_A in b["ips"]}
        self.assertEqual(members, {"architect", "default"})
        self.assertEqual(self.remote(IP_A).get("/commands",
                                               headers=self.bot_headers("architect")).status_code, 200)
        self.assertEqual(self.remote(IP_A).get("/commands",
                                               headers=self.bot_headers("default")).status_code, 200)

    def test_registration_validation(self):
        response = self.loop.post("/admin/source-ips", json={"member": "architect",
                                                              "ips": ["not-an-ip"]})
        self.assertEqual(response.status_code, 400)
        for loop in ("127.0.0.1", "::1"):
            response = self.loop.post("/admin/source-ips",
                                      json={"member": "architect", "ips": [loop]})
            self.assertEqual(response.status_code, 400, "回环恒放行，无需登记")
        response = self.loop.post("/admin/source-ips", json={"member": "", "ips": [IP_A]})
        self.assertEqual(response.status_code, 400, "member 必填")

    def test_admin_api_rejects_bot_identity(self):
        self.bind("architect", [IP_A])
        for method, path, body in (("GET", "/admin/source-ips", None),
                                   ("POST", "/admin/source-ips", {"member": "default", "ips": [IP_B]}),
                                   ("DELETE", "/admin/source-ips/default/10.0.0.9", None)):
            with self.subTest(method=method):
                response = self.loop.request(method, path, json=body,
                                             headers=self.bot_headers("default"))
                self.assertEqual(response.status_code, 403, "绑定管理不对 bot 身份开放")

    def test_unbound_source_with_valid_credentials_rejected(self):
        # 凭据管身份、来源绑定管来源：未绑定来源带合法 bot 凭据先被第一层拒
        self.bind("architect", [IP_A])
        self.assertEqual(self.remote(IP_C).get("/commands",
                                               headers=self.bot_headers("architect")).status_code, 403)

    # ---- 幂等添加端点（#320 manager node2030 并发覆盖阻断） ----

    def test_add_binding_idempotent_endpoint(self):
        # 两个独立页面快照分别添加两个地址：均保留（不清已有、互不覆盖）
        self.loop.post("/admin/source-ips/add", json={"member": "architect", "ip": IP_A})
        self.loop.post("/admin/source-ips/add", json={"member": "architect", "ip": IP_B})
        listing = self.loop.get("/admin/source-ips").json()
        by_member = {b["member"]: b["ips"] for b in listing["bindings"]}
        self.assertEqual(sorted(by_member["architect"]), sorted([IP_A, IP_B]),
                         "并发页面各加各的地址必须都保留")
        # 重复添加同地址：只一条
        self.loop.post("/admin/source-ips/add", json={"member": "architect", "ip": IP_A})
        listing = self.loop.get("/admin/source-ips").json()
        by_member = {b["member"]: b["ips"] for b in listing["bindings"]}
        self.assertEqual(sorted(by_member["architect"]), sorted([IP_A, IP_B]),
                         "重复添加同地址只能有一条")

    def test_add_binding_does_not_restore_removed(self):
        # 一方移除后另一方加不同地址：已移除项不得被带回
        self.bind("architect", [IP_A])
        self.loop.delete(f"/admin/source-ips/architect/{IP_A}")
        self.loop.post("/admin/source-ips/add", json={"member": "architect", "ip": IP_B})
        listing = self.loop.get("/admin/source-ips").json()
        by_member = {b["member"]: b["ips"] for b in listing["bindings"]}
        self.assertEqual(by_member.get("architect", []), [IP_B],
                         "旧快照添加其他地址不得带回已移除项")

    def test_add_binding_preserves_existing(self):
        # 登记绑定改用幂等语义：保留已有 IP，只追加新 IP
        self.bind("architect", [IP_A])
        self.loop.post("/admin/source-ips/add", json={"member": "architect", "ip": IP_B})
        listing = self.loop.get("/admin/source-ips").json()
        by_member = {b["member"]: b["ips"] for b in listing["bindings"]}
        self.assertEqual(sorted(by_member["architect"]), sorted([IP_A, IP_B]),
                         "幂等添加必须保留该成员已有绑定")

    def test_add_binding_validation_and_permissions(self):
        response = self.loop.post("/admin/source-ips/add", json={"member": "architect", "ip": "bad"})
        self.assertEqual(response.status_code, 400)
        response = self.loop.post("/admin/source-ips/add", json={"member": "", "ip": IP_A})
        self.assertEqual(response.status_code, 400)
        response = self.loop.post("/admin/source-ips/add", json={"member": "architect", "ip": "127.0.0.1"})
        self.assertEqual(response.status_code, 400, "回环恒放行，无需登记")
        # bot 身份 403
        self.bind("architect", [IP_A])
        response = self.loop.post("/admin/source-ips/add",
                                  json={"member": "architect", "ip": IP_B},
                                  headers=self.bot_headers("default"))
        self.assertEqual(response.status_code, 403, "幂等添加端点同样仅管理员")


if __name__ == "__main__":
    unittest.main()
