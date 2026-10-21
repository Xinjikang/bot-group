"""task#87 + task#93 单测：heartbeat.pids_alive 的 TTL 感知 + 会话语义。

心跳存活判定与话题隔离契约：
- pids_alive 只返回 now-ts <= 90s 的 pid，过期心跳不判活跃
- 崩溃 bot 残留的过期心跳 → pids_alive 返回空，超时弃权能正常推进
- 顺带清理过期槽
- 会话语义：thread_id 非 None 时只返回「该话题」的活跃 pid，work 心跳（thread_id=None）
  不再替 chat 话题背书；thread_id=None（缺省）不过滤，返回所有活跃 pid。
"""
import sys
from pathlib import Path
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import heartbeat  # noqa: E402


class PidsAliveTests(unittest.TestCase):
    def tearDown(self):
        # 清掉全局心跳表，避免测试间串扰
        with heartbeat._lock:
            heartbeat._beats.clear()

    def test_fresh_heartbeat_alive(self):
        heartbeat.beat("default", pid=100)
        self.assertIn(100, heartbeat.pids_alive("default"))

    def test_stale_heartbeat_not_alive(self):
        heartbeat.beat("default", pid=100)
        # 人为把心跳时间戳拨到 100s 前（模拟崩溃 bot 残留过期心跳）
        with heartbeat._lock:
            heartbeat._beats["default"][100]["ts"] = time.time() - 100
        self.assertEqual(heartbeat.pids_alive("default"), [])

    def test_stale_slot_cleaned(self):
        heartbeat.beat("default", pid=100)
        with heartbeat._lock:
            heartbeat._beats["default"][100]["ts"] = time.time() - 100
        heartbeat.pids_alive("default")  # 触发清理
        with heartbeat._lock:
            self.assertNotIn(100, heartbeat._beats.get("default", {}))
            # 槽清空 → profile 整个移除
            self.assertNotIn("default", heartbeat._beats)

    def test_legacy_minus1_slot_ignored(self):
        """旧 MCP 单槽（key=-1）不参与 TTL 判定。"""
        heartbeat.beat("default", pid=None)  # 退化单槽 key=-1
        self.assertEqual(heartbeat.pids_alive("default"), [])

    # ---- req#51 task#93 会话语义 ----

    def test_chat_heartbeat_only_alive_for_its_thread(self):
        """chat 进程心跳带 thread_id=100 → 按话题判活跃：thread=100 活跃，thread=200 无。"""
        heartbeat.beat("default", thread_id=100, pid=100)
        self.assertIn(100, heartbeat.pids_alive("default", thread_id=100))
        self.assertEqual(heartbeat.pids_alive("default", thread_id=200), [])

    def test_work_heartbeat_does_not_back_chat_thread(self):
        """work 心跳（thread_id=None）不替 chat 话题背书——判 chat thread 活跃应为空。"""
        heartbeat.beat("default", thread_id=None, pid=100)  # work 进程心跳
        self.assertEqual(heartbeat.pids_alive("default", thread_id=100), [])

    def test_work_and_chat_parallel_distinct(self):
        """同一 profile 的 work 心跳 + chat(thread=100) 心跳并行：判 thread=100 活跃只看 chat。"""
        heartbeat.beat("default", thread_id=None, pid=1)      # work
        heartbeat.beat("default", thread_id=100, pid=2)       # chat thread 100
        # 判 thread=100 活跃 → 只返回 chat 那个 pid
        self.assertEqual(heartbeat.pids_alive("default", thread_id=100), [2])
        # 判 thread=200 活跃 → 空（work 不背书）
        self.assertEqual(heartbeat.pids_alive("default", thread_id=200), [])

    def test_pids_alive_no_filter_returns_all(self):
        """thread_id=None（缺省）不过滤，返回所有活跃 pid。"""
        heartbeat.beat("default", thread_id=None, pid=1)
        heartbeat.beat("default", thread_id=100, pid=2)
        self.assertEqual(set(heartbeat.pids_alive("default")), {1, 2})


if __name__ == "__main__":
    unittest.main(verbosity=2)
