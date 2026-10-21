"""req#58 task#104 单测：heartbeat.active_entries 会话级判活 + db.recover_on_startup 孤儿行收尸。

契约（req#58 验收③ / 迁移要求）：
- active_entries 按「心跳槽」粒度输出 {profile, thread_id}：chat 槽带自己的
  thread_id、work 槽 thread_id=None，同 profile 双话题并行各自独立，不串台。
- key=-1（旧 MCP 退化单槽）不参与 TTL 判定、不产出（与 pids_alive 对齐）。
- recover_on_startup：wake_runs 里 ended_at IS NULL 且 started_at < 启动时刻的
  孤儿行补 ended_at=启动时刻、exit_code=-1；启动时刻之后的新行不动。
"""
import sys
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import db, heartbeat  # noqa: E402


class ActiveEntriesSessionTests(unittest.TestCase):
    def tearDown(self):
        with heartbeat._lock:
            heartbeat._beats.clear()

    def test_chat_slot_carries_its_thread_id(self):
        heartbeat.beat("default", thread_id=100, pid=1)
        entries = heartbeat.active_entries()
        self.assertEqual(entries, [{"profile": "default", "thread_id": 100}])

    def test_work_slot_thread_id_none(self):
        heartbeat.beat("default", thread_id=None, pid=1)
        entries = heartbeat.active_entries()
        self.assertEqual(entries, [{"profile": "default", "thread_id": None}])

    def test_parallel_chat_threads_distinct(self):
        """同 profile 双话题并行：两个槽各自带 thread_id，互不串台（验收③核心）。"""
        heartbeat.beat("default", thread_id=100, pid=1)
        heartbeat.beat("default", thread_id=200, pid=2)
        entries = heartbeat.active_entries()
        got = {(e["profile"], e["thread_id"]) for e in entries}
        self.assertEqual(got, {("default", 100), ("default", 200)})

    def test_work_and_chat_parallel(self):
        heartbeat.beat("default", thread_id=None, pid=1)   # work
        heartbeat.beat("default", thread_id=100, pid=2)    # chat
        entries = heartbeat.active_entries()
        got = {(e["profile"], e["thread_id"]) for e in entries}
        self.assertEqual(got, {("default", None), ("default", 100)})

    def test_legacy_minus1_slot_not_emitted(self):
        """旧 MCP 退化单槽（key=-1）不产出条目（与 pids_alive 对齐）。"""
        heartbeat.beat("default", pid=None)
        self.assertEqual(heartbeat.active_entries(), [])

    def test_stale_slot_cleaned(self):
        heartbeat.beat("default", thread_id=100, pid=1)
        with heartbeat._lock:
            heartbeat._beats["default"][1]["ts"] = time.time() - 100
        self.assertEqual(heartbeat.active_entries(), [])
        with heartbeat._lock:
            self.assertNotIn("default", heartbeat._beats)


class RecoverOnStartupOrphanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t104_")
        self.dbfile = self.tmp + "\\group.db"
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        import sqlite3
        self.conn = sqlite3.connect(self.dbfile)
        self.conn.row_factory = sqlite3.Row
        db.init_schema(self.conn)

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def _seed_orphan(self, started_at):
        self.conn.execute(
            "INSERT INTO wake_runs(session, started_at, pid) VALUES(?,?,?)",
            ("p-bot-group-work", started_at, 4242),
        )
        self.conn.commit()

    def test_orphan_row_collected_with_sentinel(self):
        old = time.time() - 3600
        self._seed_orphan(old)
        db.recover_on_startup(self.conn)
        row = self.conn.execute(
            "SELECT ended_at, exit_code FROM wake_runs WHERE session='p-bot-group-work'"
        ).fetchone()
        self.assertIsNotNone(row["ended_at"])
        self.assertEqual(row["exit_code"], -1)

    def test_future_row_untouched(self):
        """启动后新 spawn 的行（started_at > 启动时刻）不受收尸影响。"""
        future = time.time() + 3600
        self._seed_orphan(future)
        db.recover_on_startup(self.conn)
        row = self.conn.execute(
            "SELECT ended_at, exit_code FROM wake_runs WHERE session='p-bot-group-work'"
        ).fetchone()
        self.assertIsNone(row["ended_at"])
        self.assertIsNone(row["exit_code"])

    def test_already_collected_row_not_double_updated(self):
        """已有 ended_at 的行（正常退出收过尸）不再被改。"""
        self._seed_orphan(time.time() - 3600)
        self.conn.execute(
            "UPDATE wake_runs SET ended_at=?, exit_code=0 WHERE session=?",
            (12345.0, "p-bot-group-work"),
        )
        self.conn.commit()
        db.recover_on_startup(self.conn)
        row = self.conn.execute(
            "SELECT ended_at, exit_code FROM wake_runs WHERE session='p-bot-group-work'"
        ).fetchone()
        self.assertEqual(row["ended_at"], 12345.0)
        self.assertEqual(row["exit_code"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
