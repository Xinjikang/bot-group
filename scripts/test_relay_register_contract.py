"""req#170 返修（manager node2011 后端最小契约）：register 语义回归。

- 首次登记（rotate=false）遇已存在成员：不轮换凭据（credential_hash 不变）、
  不覆盖 mcp_config/expected_mcp_sha、响应不含新凭据。
- 重签（rotate=true）：仅轮换凭据、mcp_config 缺省 None 时保留原配置与
  原期望摘要（长度 64 不变）、credential_revoked=0。
- 凭据仅新创建或重签时生成下发。
"""
import json
import unittest
from contextlib import closing

from engine import db, relay
from scripts.unified_test_support import isolated_database


class RelayRegisterContractTests(unittest.TestCase):
    def setUp(self):
        self.ctx = isolated_database()
        self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        with closing(db.get_connection()) as conn:
            relay.initialize(conn)

    def conn(self):
        return db.get_connection()

    def _register(self, conn, *, rotate=False, credential="", mcp_config=None):
        return relay.register_node(conn, "remote-01", credential, rotate=rotate,
                                   mcp_config=mcp_config)

    def test_first_register_creates_and_issues_credential(self):
        with closing(self.conn()) as conn:
            result = self._register(conn)
        self.assertTrue(result["created"])
        self.assertTrue(result["credential"], "新创建下发凭据")
        node = result["node"]
        self.assertEqual(node["credential_hash"], relay.hash_credential(result["credential"]))
        self.assertFalse(node["credential_revoked"])

    def test_repeat_first_register_keeps_hash_and_config(self):
        # 首次登记重复调用（rotate=false 遇已存在）：完全不动——hash 不变、
        # 不覆盖配置/期望摘要、不返回凭据
        with closing(self.conn()) as conn:
            first = self._register(conn, mcp_config={"command": ["python"],
                                                     "_package_sha": "a" * 64})
            second = self._register(conn, mcp_config={"command": ["other"]})
        self.assertEqual(first["node"]["credential_hash"], second["node"]["credential_hash"],
                         "重复首次登记不轮换凭据")
        self.assertIsNone(second["credential"], "已存在成员首次登记不返回新凭据")
        self.assertFalse(second["created"])
        self.assertEqual(second["node"]["expected_mcp_sha"], "a" * 64,
                         "不覆盖期望摘要")
        self.assertIn("python", second["node"]["mcp_config"], "不覆盖 mcp_config")

    def test_rotate_preserves_config_and_expected_sha(self):
        # 带非空配置的隔离节点重签（mcp_config 缺省 None）：expected_mcp_sha
        # 保持 64 不变、原配置完整保留；仅凭据轮换（bot-36 12423：比对
        # 重签前后完整配置与摘要相等）
        original = {"command": ["python"], "env": {"K": "1"}, "_package_sha": "b" * 64}
        with closing(self.conn()) as conn:
            first = self._register(conn, mcp_config=original)
            rotated = self._register(conn, rotate=True)  # UI 重签只传 member
        self.assertEqual(len(rotated["node"]["expected_mcp_sha"]), 64,
                         "重签后期望摘要长度保持 64")
        self.assertEqual(rotated["node"]["expected_mcp_sha"], "b" * 64,
                         "缺省 mcp_config 保留原期望摘要")
        self.assertEqual(relay.mcp_fingerprint(original),
                         relay.mcp_fingerprint(json.loads(rotated["node"]["mcp_config"])),
                         "重签前后完整配置语义相等（同源指纹）")
        self.assertEqual(json.loads(first["node"]["mcp_config"]),
                         json.loads(rotated["node"]["mcp_config"]),
                         "重签前后完整配置相等")
        self.assertNotEqual(first["node"]["credential_hash"],
                            rotated["node"]["credential_hash"], "重签轮换凭据")
        self.assertFalse(rotated["node"]["credential_revoked"], "重签解除吊销")
        self.assertTrue(rotated["credential"], "重签下发新凭据")

    def test_rotate_invalidates_old_credential(self):
        with closing(self.conn()) as conn:
            first = self._register(conn)
            rotated = self._register(conn, rotate=True)
            self.assertFalse(relay.verify_credential(conn, "remote-01", first["credential"]),
                             "轮换后旧凭据失效")
            self.assertTrue(relay.verify_credential(conn, "remote-01", rotated["credential"]),
                            "轮换后新凭据生效")

    def test_rotate_after_revoke_reissues(self):
        with closing(self.conn()) as conn:
            self._register(conn)
            relay.revoke_credential(conn, "remote-01")
            node = relay.read_node(conn, "remote-01")
            self.assertTrue(node["credential_revoked"])
            rotated = self._register(conn, rotate=True)
            self.assertFalse(rotated["node"]["credential_revoked"],
                             "重签后 credential_revoked=0")
            self.assertTrue(relay.verify_credential(conn, "remote-01", rotated["credential"]))


if __name__ == "__main__":
    unittest.main()
