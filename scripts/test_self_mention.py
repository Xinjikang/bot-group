"""req#57 根因C（self-mention 自污染）：点对点 @ 排除 sender 自己。

契约：自我提及应避免触发重复投递和唤醒。
- bot 点对点正文 @ 了自己 → 不给自己插 inbox 行、不唤醒自己（和话题路径 m != sender 对齐）
- 正常 @ 别人 → 照旧插行 + 唤醒
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from engine import db, router  # noqa: E402


class SelfMentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t57_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        import sqlite3
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        db.init_schema(c)
        self.conn = c
        for p, dn, role in [("architect", "架构师", "architect"),
                            ("default", "蛇蛇", "default")]:
            c.execute("INSERT INTO members(profile, display_name, role) VALUES(?,?,?)", (p, dn, role))
        from scripts.project_test_support import enable_members
        enable_members(c, ('bot-group',))
        c.commit()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def _inbox_profiles(self):
        return sorted(r["profile"] for r in self.conn.execute(
            "SELECT profile FROM inbox WHERE status='pending'").fetchall())

    def _wake_work(self):
        return sorted(r["profile"] for r in self.conn.execute(
            "SELECT profile FROM wake_jobs WHERE kind='work'").fetchall())

    def test_self_mention_not_woken(self):
        """bot @ 了自己 + 别人 → 只给别人插行/唤醒，不给自己。"""
        router.route_message(
            self.conn, sender="architect", text="@architect @default 收口确认",
            sender_type="bot", thread_id=None, project="bot-group",
        )
        self.assertEqual(self._inbox_profiles(), ["default"])
        self.assertEqual(self._wake_work(), ["default"])

    def test_normal_mention_still_woken(self):
        """正常 @ 别人（不含自己）→ 照旧插行 + 唤醒。"""
        router.route_message(
            self.conn, sender="architect", text="@default 派活",
            sender_type="bot", thread_id=None, project="bot-group",
        )
        self.assertEqual(self._inbox_profiles(), ["default"])
        self.assertEqual(self._wake_work(), ["default"])

    def test_pure_self_mention_no_inbox(self):
        """只 @ 了自己 → 不插任何 inbox 行、不唤醒任何人。"""
        router.route_message(
            self.conn, sender="architect", text="@architect 备忘",
            sender_type="bot", thread_id=None, project="bot-group",
        )
        self.assertEqual(self._inbox_profiles(), [])
        self.assertEqual(self._wake_work(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
