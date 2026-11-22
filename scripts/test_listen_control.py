"""req#172：指定网卡监听动态配置与真实生效（限来源）隔离回归。

不动主引擎 8180：远程监听用独立高位端口，真实 socket 连远程地址验证
实际生效/停用/名单外拒绝；回环与主 Server 不参与。
"""
import socket
import unittest
from contextlib import closing

from engine import auth, db, listen_control, relay, signing_keys
from scripts.unified_test_support import isolated_database

TEST_PORT = 18820


def _lan_ipv4() -> str | None:
    for entry in listen_control.enumerate_interfaces():
        if entry['family'] == 'IPv4':
            return entry['address']
    return None


def _http_get(host: str, port: int, timeout: float = 3.0) -> tuple[int, bytes]:
    """真实 TCP 连接发 GET /health，返回 (status, body)；连接失败返回 (None, b'')。"""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            data = b''
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        first = data.split(b'\r\n', 1)[0].decode(errors='replace')
        if first.startswith('HTTP/1.1 ') or first.startswith('HTTP/1.0 '):
            return int(first.split()[1]), data
        return None, data
    except OSError:
        return None, b''


class ListenControlTests(unittest.TestCase):
    def setUp(self):
        self.fixture = isolated_database()
        self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        with closing(db.get_connection()) as conn:
            signing_keys.initialize(conn)
        self.addCleanup(listen_control.ensure, None)
        self.lan = _lan_ipv4()

    # ---- 枚举与校验 ----

    def test_enumerate_excludes_wildcard_loopback(self):
        addresses = {e['address'] for e in listen_control.enumerate_interfaces()}
        for banned in ('0.0.0.0', '::', '127.0.0.1', '::1'):
            self.assertNotIn(banned, addresses, f"枚举不得含 {banned}")
        for entry in listen_control.enumerate_interfaces():
            self.assertFalse(entry['address'].lower().startswith('fe80:'),
                             "枚举不得含链路本地")
        if self.lan:
            self.assertIn(self.lan, addresses, "本机 LAN 地址应在枚举中")

    def test_validate_rejects_wildcard_loopback_foreign(self):
        for bad in ('0.0.0.0', '::', '127.0.0.1', '127.8.8.8', '::1', 'fe80::1',
                    '198.51.100.7', 'not-an-ip', 'ff02::1'):
            with self.subTest(address=bad):
                with self.assertRaises(ValueError, msg=f"{bad} 必须被拒绝"):
                    listen_control.validate_address(bad)
        if self.lan:
            self.assertEqual(listen_control.validate_address(self.lan), self.lan)

    # ---- 联动：来源绑定先就绪 + 真实生效 + 名单外拒 ----

    def test_enable_from_zero_bindings(self):
        # req#175 node2118 ①：零绑定启用远程监听（首次接入从零走通——
        # 来源绑定随一次性邀请兑换建立，不再要求预先登记）；启用后来源
        # 控制仍生效：非回环未绑定来源 HTTP 403（第一层拦截不变）。
        if not self.lan:
            self.skipTest("本机无 LAN 单播地址")
        state = listen_control.ensure(self.lan, port=TEST_PORT)
        self.assertTrue(state['remote_enabled'], state)
        self.assertEqual(state['remote_address'], self.lan)
        # 真实 TCP 全链：本机源即 LAN 地址，未绑定 → 403
        status, _ = _http_get(self.lan, TEST_PORT)
        self.assertEqual(status, 403, "零绑定启用后未绑定来源必须仍被拒")

    def test_real_listen_bindings_gate_and_disable(self):
        if not self.lan:
            self.skipTest("本机无 LAN 单播地址")
        # 来源绑定先就绪
        with closing(db.get_connection()) as conn:
            from engine import ip_allowlist
            ip_allowlist.set_bindings(conn, "architect", [self.lan])
        state = listen_control.ensure(self.lan, port=TEST_PORT)
        self.assertTrue(state['remote_enabled'], state)
        self.assertEqual(state['remote_address'], self.lan)
        self.assertEqual(state['remote_port'], TEST_PORT)
        # 真实 TCP 全链：本机源即 LAN 地址，在绑定并集内 → 200
        status, _ = _http_get(self.lan, TEST_PORT)
        self.assertEqual(status, 200, "绑定来源实际连通远程监听")
        # 移除绑定 → 再次连接被拒（真实 TCP 限源联动）
        with closing(db.get_connection()) as conn:
            from engine import ip_allowlist
            ip_allowlist.remove_binding(conn, "architect", self.lan)
        status, _ = _http_get(self.lan, TEST_PORT)
        self.assertEqual(status, 403, "移除后再次连接必须被拒")
        # 停用远程监听：回退回环，远程地址不再可连
        state = listen_control.ensure(None)
        self.assertFalse(state['remote_enabled'])
        status, _ = _http_get(self.lan, TEST_PORT)
        self.assertIsNone(status, "停用后远程地址不得再连通")

    # ---- 绑定失败保持本机可用 ----

    def test_port_conflict_reports_error_and_rolls_back(self):
        if not self.lan:
            self.skipTest("本机无 LAN 单播地址")
        with closing(db.get_connection()) as conn:
            from engine import ip_allowlist
            ip_allowlist.set_bindings(conn, "architect", [self.lan])
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind((self.lan, TEST_PORT))
        blocker.listen(1)
        self.addCleanup(blocker.close)
        state = listen_control.ensure(self.lan, port=TEST_PORT)
        self.assertFalse(state['remote_enabled'], "端口被占必须如实报错且不启用")
        self.assertTrue(state['error'], "失败必须携带错误信息")
        with closing(db.get_connection()) as conn:
            self.assertIsNone(listen_control.read_config(conn),
                              "启动失败必须回滚配置，重启不得重复失败")

    # ---- 重启恢复 ----

    def test_restart_restores_last_config(self):
        if not self.lan:
            self.skipTest("本机无 LAN 单播地址")
        with closing(db.get_connection()) as conn:
            from engine import ip_allowlist
            ip_allowlist.set_bindings(conn, "architect", [self.lan])
        listen_control.ensure(self.lan, port=TEST_PORT)
        self.assertTrue(listen_control.status()['remote_enabled'])
        # 模拟进程退出：只停运行时服务、保留配置（进程退出不清 settings）
        with listen_control._lock:
            listen_control._stop_locked()
        self.assertFalse(listen_control.status()['remote_enabled'])
        with closing(db.get_connection()) as conn:
            self.assertEqual(listen_control.read_config(conn), self.lan,
                             "进程退出后配置必须保留")
        state = listen_control.restore_on_startup(port=TEST_PORT)
        self.assertTrue(state['remote_enabled'], "重启后必须恢复上次监听配置")
        self.assertEqual(state['remote_address'], self.lan)
        status, _ = _http_get(self.lan, TEST_PORT)
        self.assertEqual(status, 200)


class ListenApiTests(unittest.TestCase):
    """管理 API 层：权限与校验（不触发真实监听，仅回环 TestClient）。"""

    def setUp(self):
        self.fixture = isolated_database()
        self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from engine import api
        app = FastAPI()
        app.include_router(api.router_api)
        self.client = TestClient(app, client=("127.0.0.1", 4321))
        self.addCleanup(self.client.close)

    def test_admin_only(self):
        from engine import auth
        bot = {"X-Group-Profile": "default", "X-Group-Key": auth.bot_token("default")}
        for method, path in (("GET", "/admin/listen/interfaces"),
                             ("GET", "/admin/listen/status"),
                             ("POST", "/admin/listen")):
            with self.subTest(method=method):
                response = self.client.request(method, path,
                                               json={"address": ""},
                                               headers=bot)
                self.assertEqual(response.status_code, 403, "监听配置仅管理员")

    def test_apply_rejects_wildcard(self):
        for address in ("0.0.0.0", "::", "127.0.0.1", "198.51.100.7"):
            with self.subTest(address=address):
                response = self.client.post("/admin/listen", json={"address": address})
                self.assertEqual(response.status_code, 400, f"{address} 必须被拒")

    def test_interfaces_and_status_shape(self):
        interfaces = self.client.get("/admin/listen/interfaces").json()
        for entry in interfaces["interfaces"]:
            self.assertIn("address", entry)
            self.assertNotIn(entry["address"], ("0.0.0.0", "::"))
        status = self.client.get("/admin/listen/status").json()
        self.assertIn("remote_enabled", status)
        self.assertIn("loopback", status)


if __name__ == "__main__":
    unittest.main()
