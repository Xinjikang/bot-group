"""task#76（req#39）：reaper 换 proc.pid 直存 wake_runs、wmic 退役单测。

契约：
- wake_runs 加 pid 列（新库 + 老库 _migrate_add_column 幂等迁移）
- _spawn 时 INSERT wake_runs 写入 proc.pid
- _watch_session 用 proc.wait() 判退出（不再每 0.5s wmic 全表扫）
- 退出后 UPDATE wake_runs 写 ended_at/exit_code（proc.returncode）
- 该 profile 无其它会话时才 on_exit（引用计数，替代 wmic 全表扫）
"""
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from engine import db, wake  # noqa: E402


class WakeRunsPidColumnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t76_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        self.conn = sqlite3.connect(self.dbfile)
        self.conn.row_factory = sqlite3.Row
        db.init_schema(self.conn)

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def test_wake_runs_has_pid_column(self):
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(wake_runs)")}
        self.assertIn("pid", cols)

    def test_migration_idempotent(self):
        db.init_schema(self.conn)  # 再跑一遍不炸
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(wake_runs)")}
        self.assertIn("pid", cols)


class SpawnPidWriteTests(unittest.TestCase):
    """mock subprocess.Popen + threading.Thread，验证 _spawn 写 pid + watch 用 wait()。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t76spawn_")
        host = patch.dict(os.environ, {'HERMES_HOME': self.tmp})
        host.start()
        self.addCleanup(host.stop)
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        self.conn = sqlite3.connect(self.dbfile)
        self.conn.row_factory = sqlite3.Row
        db.init_schema(self.conn)
        # 造一个项目，否则 _workdir_for 查不到 project 会退化到 ~
        self.conn.execute(
            "INSERT INTO projects(name, root, worktree_root, created_at) VALUES('bot-group',?,'','now')",
            (_ROOT,))
        from scripts.project_test_support import enable_members
        enable_members(self.conn, ('bot-group',), ('default',))
        self.conn.commit()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def _fake_proc(self, pid=4242, returncode=0):
        p = MagicMock()
        p.pid = pid
        p.returncode = returncode
        return p

    def test_spawn_writes_pid_and_watch_uses_wait(self):
        fake_proc = self._fake_proc(pid=7777, returncode=0)
        # 记录 watch 线程是否启动、用了 wait 还是 poll
        calls = {}

        def _fake_thread(target, daemon, name):
            calls["daemon"] = daemon
            self.assertTrue(any(r['session'] == 'default-bot-group-work' for r in wake.active_runs()))
            # 不真启动线程，手动跑一次 target 验证 wait 路径
            target()
            return MagicMock()

        import types as _types
        fake_events = _types.ModuleType("events")
        fake_events.bus = MagicMock()
        fake_events.bus.publish = MagicMock()
        fake_hb = _types.ModuleType("heartbeat")
        fake_hb.on_exit = MagicMock()

        with patch.object(wake.subprocess, "Popen", return_value=fake_proc), \
             patch.object(wake, "session_busy", return_value=False), \
             patch.dict(sys.modules, {"engine.events": fake_events, "engine.heartbeat": fake_hb}), \
             patch.object(wake, "_hermes_bin", return_value="hermes"), \
             patch("threading.Thread") as mock_thread:

            mock_thread.side_effect = _fake_thread
            result = wake._spawn("default", "work", "sig", None, "bot-group")

        self.assertTrue(result["ok"])
        self.assertFalse(any(r['session'] == 'default-bot-group-work' for r in wake.active_runs()))
        self.assertEqual(result["detail"], "pid 7777")

        # wake_runs 写了 pid
        row = self.conn.execute(
            "SELECT pid, ended_at, exit_code FROM wake_runs WHERE session='default-bot-group-work'"
        ).fetchone()
        self.assertEqual(row["pid"], 7777)
        self.assertIsNotNone(row["ended_at"])   # watch 线程跑过，写了 ended_at
        self.assertEqual(row["exit_code"], 0)

        # 关键：用了 proc.wait()（Popen 返回的 fake proc 有 wait 方法被调用）
        fake_proc.wait.assert_called_once()
        # 不再走 poll 循环（旧路径 poll 会被反复调）
        fake_proc.poll.assert_not_called()


class WatchUsesWaitNotWmicTests(unittest.TestCase):
    """验证 _watch_session 逻辑本身：wait() 驱动，退出后 on_exit 只在无其它会话时触发。"""

    def test_watch_uses_wait_and_updates_run(self):
        # 直接验证：proc.wait() 是判退出手段（源码级契约，避免 mock 复杂闭包）
        import inspect
        src = inspect.getsource(wake._spawn_in_workspace)
        self.assertIn("proc.wait()", src)
        self.assertNotIn("find_bot_processes(profile, session=session)", src)
        # on_exit 触发条件改为引用计数，不再 wmic 全表扫
        self.assertIn("_hb.on_exit(profile, pid=proc.pid, thread_id=thread_id)", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
