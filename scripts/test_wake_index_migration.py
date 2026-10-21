"""task#75（req#38 串话题修复）：wake 去重索引 project→thread_id 迁移 + 去重语义单测。

契约（ENGINEERING 32 节，架构师定稿）：
- 旧 idx_wake_active(profile, kind, project) 幂等重建为两个 partial unique index：
  idx_wake_chat_active(profile, thread_id) WHERE kind='chat'
  idx_wake_work_active(profile, project) WHERE kind='work'
- chat 去重粒度 = (profile, thread_id)：同项目不同话题可并行唤醒同一 bot（不再互相顶）
- work 去重粒度 = (profile, project)：保持，不加 thread_id
- round 不进粒度
- bot 行为零改动（enqueue 仍靠 IntegrityError 去重，签名不变）
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from engine import db, wake  # noqa: E402


class WakeIndexMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t75_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        self.conn = sqlite3.connect(self.dbfile)
        self.conn.row_factory = sqlite3.Row
        db.init_schema(self.conn)
        from scripts.project_test_support import enable_members
        enable_members(self.conn, ('bot-group', 'xjk', 'cicp'), ('architect', 'be-dev-01'))
        self.conn.commit()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def _indexes(self):
        return {
            r["name"]: r["sql"]
            for r in self.conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' AND name LIKE 'idx_wake%'"
            )
        }

    def test_old_index_dropped_new_created(self):
        idx = self._indexes()
        self.assertNotIn("idx_wake_active", idx)
        self.assertIn("idx_wake_chat_active", idx)
        self.assertIn("idx_wake_work_active", idx)

    def test_migration_idempotent(self):
        """再跑一遍 init_schema 不炸、索引形态不变。"""
        db.init_schema(self.conn)
        idx = self._indexes()
        self.assertNotIn("idx_wake_active", idx)
        self.assertIn("idx_wake_chat_active", idx)
        self.assertIn("idx_wake_work_active", idx)

    def test_chat_different_threads_not_deduped(self):
        """🔴核心：同 profile 同 project 不同 thread_id 的 chat 唤醒并行并存。"""
        ok1 = wake.enqueue(self.conn, "architect", "chat", "thread-open",
                           thread_id=101, project="bot-group")
        ok2 = wake.enqueue(self.conn, "architect", "chat", "thread-open",
                           thread_id=202, project="bot-group")
        self.assertTrue(ok1)
        self.assertTrue(ok2)
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM wake_jobs WHERE kind='chat' AND profile='architect' "
            "AND status IN ('queued','running')").fetchone()["n"]
        self.assertEqual(n, 2)

    def test_chat_same_thread_deduped(self):
        """同 profile 同 thread_id 的 chat 仍去重（一次只醒一个）。"""
        ok1 = wake.enqueue(self.conn, "architect", "chat", "thread-open",
                           thread_id=101, project="bot-group")
        ok2 = wake.enqueue(self.conn, "architect", "chat", "thread-open",
                           thread_id=101, project="bot-group")
        self.assertTrue(ok1)
        self.assertFalse(ok2)  # 去重命中
        n = self.conn.execute(
            "SELECT COUNT(*) AS n FROM wake_jobs WHERE kind='chat' AND profile='architect' "
            "AND thread_id=101 AND status IN ('queued','running')").fetchone()["n"]
        self.assertEqual(n, 1)

    def test_work_deduped_by_project(self):
        """work 保持 (profile, project)：同 project 去重，不同 project 并存。"""
        ok1 = wake.enqueue(self.conn, "be-dev-01", "work", "scan:task", project="xjk")
        ok2 = wake.enqueue(self.conn, "be-dev-01", "work", "scan:task", project="xjk")
        ok3 = wake.enqueue(self.conn, "be-dev-01", "work", "scan:task", project="cicp")
        self.assertTrue(ok1)
        self.assertFalse(ok2)  # 同 project 去重
        self.assertTrue(ok3)   # 不同 project 并存

    def test_chat_and_work_independent(self):
        """chat 与 work 分索引，互不去重。"""
        ok_chat = wake.enqueue(self.conn, "architect", "chat", "thread-open",
                               thread_id=101, project="bot-group")
        ok_work = wake.enqueue(self.conn, "architect", "work", "scan:group", project="bot-group")
        self.assertTrue(ok_chat)
        self.assertTrue(ok_work)


class LegacyIndexUpgradeTests(unittest.TestCase):
    """模拟老库：已有 idx_wake_active(profile,kind,project)，验证 init_schema 升级。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t75legacy_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        # 手工造老库：先 init_schema 建全表，再手动恢复旧索引定义
        self.conn = sqlite3.connect(self.dbfile)
        self.conn.row_factory = sqlite3.Row
        db.init_schema(self.conn)
        # 删掉新索引，重建旧单索引（模拟老库形态）
        self.conn.execute("DROP INDEX idx_wake_chat_active")
        self.conn.execute("DROP INDEX idx_wake_work_active")
        self.conn.execute(
            "CREATE UNIQUE INDEX idx_wake_active ON wake_jobs(profile, kind, project) "
            "WHERE status IN ('queued','running')"
        )
        self.conn.commit()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def test_legacy_upgraded_to_two_indexes(self):
        db.init_schema(self.conn)  # 触发 _migrate_wake_active_index
        idx = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_wake%'")}
        self.assertNotIn("idx_wake_active", idx)
        self.assertIn("idx_wake_chat_active", idx)
        self.assertIn("idx_wake_work_active", idx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
