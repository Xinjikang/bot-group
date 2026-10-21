"""task#79（req#42 一次读完）：引擎 /inbox 批量读取单测。

契约（ENGINEERING 36 节）：
- limit 放开：一次拉回多条未读（work 点对点）
- 逐条带 project / mentioned_you 字段
- FIFO 顺序（id ASC）
- chat（话题）仍按 thread_id 过滤，不跨 thread 误带

（MCP 层 group_read_inbox 只透传 limit 参数，本测试锁引擎侧数据正确性。）
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from pathlib import Path
_ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, _ROOT)
from engine import db, api  # noqa: E402


class ReadInboxBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t79_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        import sqlite3
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        db.init_schema(c)
        self.conn = c
        # 3 条点对点消息，profile=default 都是接收方
        # 1: user @default（mentioned_you=True）
        # 2: architect 发给 default 没 @（mentioned_you=False）
        # 3: user @default（mentioned_you=True）
        c.execute("INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id) "
                  "VALUES(1, 'bot-group', '2026-09-20T10:00:00', 'user', 'user', '@default 看1', '[\"default\"]', NULL)")
        c.execute("INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id) "
                  "VALUES(2, 'cicp', '2026-09-20T10:01:00', 'architect', 'bot', '递话给你', '[]', NULL)")
        c.execute("INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id) "
                  "VALUES(3, 'bot-group', '2026-09-20T10:02:00', 'user', 'user', '@default 看3', '[\"default\"]', NULL)")
        # 1 条话题消息（thread_id 非空）
        c.execute("INSERT INTO threads(id, project, title, creator, members, status, created_at, last_active_at) "
                  "VALUES(100, 'bot-group', '讨论', 'architect', '[\"default\"]', 'open', '2026-09-20T10:00:00', '2026-09-20T10:03:00')")
        c.execute("INSERT INTO messages(id, project, ts, sender, sender_type, content, mentions, thread_id) "
                  "VALUES(4, 'bot-group', '2026-09-20T10:03:00', 'architect', 'bot', '@default 话题里说', '[\"default\"]', 100)")
        # inbox 行：default 待看 4 条
        for mid in (1, 2, 3, 4):
            c.execute("INSERT INTO inbox(message_id, profile, status) VALUES(?, 'default', 'pending')", (mid,))
        c.commit()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def _inbox(self, kind, limit, project="bot-group"):
        return api.get_inbox(profile="default", limit=limit, kind=kind,
                             x_group_profile="default", project=project)

    def test_work_batch_is_scoped_to_project(self):
        """只拉当前项目点对点消息，计数也使用同一范围。"""
        r = self._inbox("work", 100)
        self.assertEqual(r["count"], 2)
        self.assertEqual(r["pending_total"], 2)
        self.assertEqual(r["pending_p2p"], 2)
        self.assertEqual(r["pending_thread"], 0)
        ids = [m["message_id"] for m in r["messages"]]
        self.assertEqual(ids, [1, 3])

    def test_work_per_message_fields(self):
        """逐条带 project / mentioned_you。"""
        r = self._inbox("work", 100)
        by_id = {m["message_id"]: m for m in r["messages"]}
        self.assertEqual(by_id[1]["project"], "bot-group")
        self.assertEqual(by_id[1]["mentioned_you"], True)
        self.assertNotIn(2, by_id)
        other = self._inbox("work", 100, project="cicp")
        self.assertEqual([m["message_id"] for m in other["messages"]], [2])
        self.assertEqual(other["pending_total"], 1)
        self.assertEqual(by_id[3]["mentioned_you"], True)

    def test_work_limit_clamp(self):
        """limit 放开后仍受 clamp 保护（引擎 max 100）。"""
        r = self._inbox("work", 1)  # 显式传 1 仍只回 1
        self.assertEqual(r["count"], 1)

    def test_chat_not_cross_thread(self):
        """chat 只回话题消息，不跨 thread 误带点对点。"""
        self.conn.execute(
            "INSERT INTO wake_jobs(profile, project, kind, trigger, thread_id, status, enqueued_at, chat_context_until) "
            "VALUES('default', 'bot-group', 'chat', 'scan:group', 100, 'done', '2026-09-21T10:00:00', '2099-01-01T00:00:00')"
        )
        self.conn.commit()
        r = api.get_inbox(profile="default", limit=100, kind="chat",
                          thread_id=100, x_group_profile="default")
        ids = [m["message_id"] for m in r["messages"]]
        self.assertEqual(ids, [4])  # 只有 thread_id=100 那条
        self.assertEqual(r["pending_thread"], 1)

    def test_no_kind_p2p_first(self):
        """不传 kind：点对点优先。"""
        r = self._inbox("", 100)
        ids = [m["message_id"] for m in r["messages"]]
        self.assertEqual(ids, [1, 2, 3, 4])  # 点对点优先，话题在后


if __name__ == "__main__":
    unittest.main(verbosity=2)
