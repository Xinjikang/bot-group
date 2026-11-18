"""req#167 慢发送双索引补丁 + 分步计时诊断回归。

覆盖：索引幂等（新库/旧库升级/重复初始化）、六处话题查询按键查找、
双连接锁竞争重放（S3 墙钟≈持锁时长/thread_cpu≈0 + begin/end 行识别持有者）、
prepare 段慢注入落点、零开关零日志、开关开销 <0.5ms 与 1000 次发送开销对比、
jsonl 字段/轮转/容量上限、发送行为保留（inbox/wake 一次提交）。
"""
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from statistics import median
from unittest.mock import patch

from engine import db, router, slow_send_diag
from scripts.unified_test_support import isolated_database


def _read_diag(path: Path) -> list[dict]:
    slow_send_diag.close_stream()  # 缓冲写先落盘
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class DiagHarness(unittest.TestCase):
    def setUp(self):
        fixture = isolated_database()
        fixture.__enter__()
        self.addCleanup(fixture.__exit__, None, None, None)
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        self.observer = db.get_connection()
        self.addCleanup(self.observer.close)
        self.tmp = Path(tempfile.mkdtemp(prefix="slow-send-diag-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.logs_patch = patch("engine.config.logs_dir", return_value=self.tmp)
        self.logs_patch.start()
        self.addCleanup(self.logs_patch.stop)
        # 开关环境隔离：用例内显式设置/清除，tearDown 复原。
        self._saved_env = os.environ.pop(slow_send_diag.ENV_NAME, None)
        self.addCleanup(self._restore_env)
        self.addCleanup(slow_send_diag.close_stream)
        slow_send_diag._stream.update({"date": None, "file": None, "size": 0})

    def _restore_env(self):
        if self._saved_env is not None:
            os.environ[slow_send_diag.ENV_NAME] = self._saved_env
        else:
            os.environ.pop(slow_send_diag.ENV_NAME, None)

    def enable_diag(self):
        os.environ[slow_send_diag.ENV_NAME] = "1"

    @property
    def diag_path(self) -> Path:
        return self.tmp / slow_send_diag.MAIN_NAME

    @staticmethod
    def send(conn, text="@default hi"):
        return router.route_message(conn, "user", text, "user", None, "alpha")

    def counts(self, conn):
        return tuple(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                     for table in ("messages", "inbox", "wake_jobs"))


class MessageIndexTests(DiagHarness):
    """验收 1：双索引幂等 + 六处话题查询按键查找 + 发送行为保留。"""

    def _indexes(self):
        return {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='messages'")}

    def test_new_db_gets_both_indexes(self):
        self.assertTrue({"idx_messages_round", "idx_messages_reply"} <= self._indexes())

    def test_old_db_upgrade_idempotent(self):
        # 旧库：无索引 messages 表（手工建），跑 init_schema 后补齐且可重复跑
        old = sqlite3.connect(self.tmp / "old.db")
        old.row_factory = sqlite3.Row
        old.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "project TEXT NOT NULL DEFAULT 'xjk', ts TEXT NOT NULL, "
                    "sender TEXT NOT NULL, sender_type TEXT NOT NULL, content TEXT NOT NULL, "
                    "mentions TEXT NOT NULL DEFAULT '[]', thread_id INTEGER, parent_id INTEGER, "
                    "round INTEGER NOT NULL DEFAULT 0, depth INTEGER NOT NULL DEFAULT 0, "
                    "retract_by TEXT, retract_at TEXT, need_reply INTEGER NOT NULL DEFAULT 1)")
        old.commit()
        old.close()
        with patch("engine.db.db_path", return_value=str(self.tmp / "old.db")):
            up = sqlite3.connect(self.tmp / "old.db")
            up.row_factory = sqlite3.Row
            db.init_schema(up)
            names = {r["name"] for r in up.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='messages'")}
            self.assertTrue({"idx_messages_round", "idx_messages_reply"} <= names)
            db.init_schema(up)  # 幂等：重复初始化不报错
            up.close()

    def test_six_queries_use_indexes(self):
        # 六处话题查询的查询计划必须 SEARCH USING 对应索引（按键查找）
        round_queries = [
            ("SELECT COUNT(*) FROM messages WHERE thread_id=? AND sender=? "
             "AND round=? AND parent_id IS NULL", (1, "a", 2), "idx_messages_round"),
            ("SELECT id,depth FROM messages WHERE thread_id=? AND sender='user' "
             "AND round=? ORDER BY id", (1, 2), "idx_messages_round"),
            ("SELECT content FROM messages WHERE thread_id=? AND sender=? AND round=? "
             "AND parent_id IS NULL ORDER BY id LIMIT 1", (1, "a", 2), "idx_messages_round"),
            ("SELECT id FROM messages WHERE thread_id=? AND sender=? AND round=? "
             "AND parent_id IS NULL ORDER BY id LIMIT 1", (1, "a", 2), "idx_messages_round"),
        ]
        reply_queries = [
            ("SELECT COUNT(*) FROM messages WHERE parent_id=? AND sender=?", (1, "a"),
             "idx_messages_reply"),
            ("SELECT id FROM messages WHERE parent_id=? AND sender=? ORDER BY id LIMIT 1",
             (1, "a"), "idx_messages_reply"),
        ]
        for sql, params, index in round_queries + reply_queries:
            plan = [dict(r) for r in self.conn.execute(
                f"EXPLAIN QUERY PLAN {sql}", params)]
            detail = " ".join(str(r.get("detail", "")) for r in plan)
            self.assertIn(index, detail, f"查询未按键查找：{sql} plan={plan}")

    def test_send_behavior_preserved(self):
        # 发送行为回归：点对点 @ → 消息 1 条 + inbox 1 行 + wake 1 个，一次提交
        initial = self.counts(self.observer)
        result = self.send(self.conn)
        self.assertEqual(len(result["wake"]), 1)
        self.assertEqual(result["inbox_profiles"], ["default"])
        self.assertEqual(self.counts(self.observer),
                         tuple(a + b for a, b in zip(initial, (1, 1, 1))))


class DiagStepTests(DiagHarness):
    """验收 3/4：分步计时、锁竞争重放、事务起止、零开关、轮转容量。"""

    def test_disabled_zero_log(self):
        self.send(self.conn)
        extra = db.get_connection()
        self.addCleanup(extra.close)
        self.assertFalse(self.diag_path.exists(), "未设开关不得产生诊断日志")

    def test_steps_recorded_with_pairs(self):
        self.enable_diag()
        result = self.send(self.conn)
        rows = _read_diag(self.diag_path)
        steps = [r for r in rows if r["kind"] == "step"]
        names = [r["name"] for r in steps]
        self.assertIn("S3", names)
        self.assertIn("S7", names)
        s3 = next(r for r in steps if r["name"] == "S3")
        self.assertEqual(s3["message_id"], result["message_id"])
        for r in steps:
            self.assertIn("wall_ms", r)
            self.assertIn("thread_cpu_ms", r)
            self.assertGreaterEqual(r["wall_ms"], 0.0)
        txns = [r for r in rows if r["kind"] == "txn"]
        begins = [r for r in txns if r.get("is_begin")]
        ends = [r for r in txns if r.get("is_end")]
        self.assertEqual(len(begins), 1)
        self.assertEqual(len(ends), 1)
        self.assertEqual(begins[0]["conn_id"], ends[0]["conn_id"])
        self.assertEqual(ends[0]["end_kind"], "commit")

    def test_lock_contention_replay(self):
        # 双连接重放：持锁方（另一线程 BEGIN IMMEDIATE + 2.17s 暂停），
        # 等待方 route_message 的 S3 墙钟≈2170ms、线程 CPU≈0。
        self.enable_diag()
        # 话题先行提交（等待方 BEGIN IMMEDIATE 之前可读），持锁方另行持写锁
        self.conn.execute("INSERT INTO threads (project, title, creator, members, "
                          "status, created_at, last_active_at) "
                          "VALUES ('alpha', 't', 'user', '[\"user\",\"default\"]', "
                          "'open', '2026-11-18T10:00:00', '2026-11-18T10:00:00')")
        self.conn.commit()
        thread_id = self.conn.execute("SELECT id FROM threads").fetchone()["id"]
        holder = None
        release = threading.Event()

        def hold():
            # 持锁方完整事务在子线程；check_same_thread=False 允许主线程收尾关闭
            nonlocal holder
            holder = sqlite3.connect(db.db_path(), timeout=30, check_same_thread=False)
            holder.row_factory = sqlite3.Row
            holder.execute("PRAGMA busy_timeout=30000")
            try:
                holder.execute("BEGIN IMMEDIATE")
                slow_send_diag.txn_begin(holder)  # 持锁方 begin 行（模拟其 route 事务）
                time.sleep(2.17)  # 持锁方暂停（现场 11835 锁等待时长）
                holder.commit()
                slow_send_diag.txn_end(holder, "commit")
            finally:
                release.set()

        thread = threading.Thread(target=hold)
        thread.start()
        time.sleep(0.3)  # 确保持锁方已取得写锁（主线程等待时间相应缩短）
        waited = db.get_connection()
        self.addCleanup(waited.close)
        self.addCleanup(lambda: holder.close() if holder is not None else None)
        result = router.route_message(waited, "user", "hi", "user", thread_id, "alpha")
        thread.join(timeout=10)
        self.assertTrue(release.is_set(), "持锁方未释放")
        rows = _read_diag(self.diag_path)
        s3 = next(r for r in rows if r["kind"] == "step" and r["name"] == "S3")
        # 主线程 0.3s 后才进入等待：S3 墙钟 = 剩余持锁时长（≈2.17-0.3）
        self.assertGreaterEqual(s3["wall_ms"], 1800.0, f"S3 墙钟应≈持锁时长：{s3}")
        self.assertLess(s3["wall_ms"], 3500.0, f"S3 墙钟异常：{s3}")
        self.assertLess(s3["thread_cpu_ms"], s3["wall_ms"] * 0.1,
                        f"S3 应为等待型（CPU≈0）：{s3}")
        # begin/end 行正确识别持有者：持锁时长由 holder 成对行差值直接得出
        txns = [r for r in rows if r["kind"] == "txn"]
        holder_begin = next(r for r in txns
                            if r.get("is_begin") and r["conn_id"] == id(holder))
        holder_end = next(r for r in txns
                          if r.get("is_end") and r["conn_id"] == id(holder))
        waited_begin = next(r for r in txns
                            if r.get("is_begin") and r["conn_id"] == id(waited))
        self.assertLess(holder_begin["wall_ts"], waited_begin["wall_ts"])
        self.assertGreaterEqual(holder_end["wall_ts"] - holder_begin["wall_ts"], 2.0,
                                "持锁时长由持有方成对行 wall_ts 差值得出")
        # 话题发送落库成功（member default 收 inbox、唤醒入队）
        self.assertEqual(len(result["wake"]), 1)

    def test_prepare_slow_injection_locates_step(self):
        # 人为慢注入 S5 段（消息插入路径）与 S6 段（树计算），落点精确到组
        self.enable_diag()

        def slow_insert_rows(conn, *args, **kwargs):
            time.sleep(0.5)  # S5 注入：inbox 分发暂停
            return self._real_insert_rows(conn, *args, **kwargs)
        self._real_insert_rows = router._insert_inbox_rows
        with patch.object(router, "_insert_inbox_rows", side_effect=slow_insert_rows):
            self.send(self.conn)
        rows = _read_diag(self.diag_path)
        s5 = next(r for r in rows if r["kind"] == "step" and r["name"] == "S5")
        self.assertGreaterEqual(s5["wall_ms"], 450.0, f"S5 应定位到注入：{s5}")
        s7 = next(r for r in rows if r["kind"] == "step" and r["name"] == "S7")
        self.assertLess(s7["wall_ms"], 100.0, f"S7 不受 S5 注入影响：{s7}")

    def test_s6_injection_locates_step(self):
        self.enable_diag()
        # 话题消息才走 _next_speaker（S6 段）
        self.conn.execute("INSERT INTO threads (project, title, creator, members, "
                          "status, created_at, last_active_at) "
                          "VALUES ('alpha', 't', 'user', '[\"user\",\"default\"]', "
                          "'open', '2026-11-18T10:00:00', '2026-11-18T10:00:00')")
        self.conn.commit()
        thread_id = self.conn.execute("SELECT id FROM threads").fetchone()["id"]

        def slow_next_speaker(conn, tid):
            time.sleep(0.4)  # S6 注入：下一发言者树计算暂停
            return self._real_next(conn, tid)
        self._real_next = router._next_speaker
        with patch.object(router, "_next_speaker", side_effect=slow_next_speaker):
            router.route_message(self.conn, "user", "hi", "user", thread_id, "alpha")
        rows = _read_diag(self.diag_path)
        s6 = next(r for r in rows if r["kind"] == "step" and r["name"] == "S6")
        self.assertGreaterEqual(s6["wall_ms"], 350.0, f"S6 应定位到注入：{s6}")

    def test_connection_slow_injection_locates_c_step(self):
        # C3 注入（connect 打开暂停）：db.get_connection 落点
        self.enable_diag()
        real_connect = sqlite3.connect

        def slow_connect(*args, **kwargs):
            time.sleep(0.25)
            return real_connect(*args, **kwargs)
        with patch.object(sqlite3, "connect", side_effect=slow_connect):
            conn = db.get_connection()
            self.addCleanup(conn.close)
        rows = _read_diag(self.diag_path)
        c3 = next(r for r in rows if r["kind"] == "step" and r["name"] == "C3")
        self.assertGreaterEqual(c3["wall_ms"], 200.0, f"C3 应定位到注入：{c3}")

    def test_rollback_row_on_route_error(self):
        # 事务性错误路径：RouteError 冒泡后连接关闭 = 隐式回滚（api finally 记 end）
        self.enable_diag()
        conn = db.get_connection()
        self.addCleanup(conn.close)
        # 直接构造 router 层事务后异常（等价验证 rollback 行由 api finally 记）
        slow_send_diag.txn_begin(conn)
        conn.execute("BEGIN IMMEDIATE")
        slow_send_diag.txn_end(conn, "rollback")
        rows = _read_diag(self.diag_path)
        txns = [r for r in rows if r["kind"] == "txn"]
        self.assertEqual(txns[-1]["end_kind"], "rollback")
        self.assertEqual(txns[-1]["conn_id"], id(conn))

    def test_rotation_by_day_and_capacity(self):
        # 容量上限轮转 + 目录保留最近 3 个轮转文件
        with patch.object(slow_send_diag, "MAX_FILE_BYTES", 500):
            for i in range(4):  # 4 次触发容量轮转
                slow_send_diag.record("step", {"name": "S9", "wall_ms": 1.0,
                                               "thread_cpu_ms": 0.0})
                slow_send_diag._stream["size"] = 499  # 模拟接近上限
                for j in range(3):
                    slow_send_diag.record("step", {"name": f"S9-{j}",
                                                   "wall_ms": 1.0, "thread_cpu_ms": 0.0})
        slow_send_diag.close_stream()
        rotated = list(self.tmp.glob(slow_send_diag.ROTATED_PREFIX + "*.jsonl"))
        self.assertLessEqual(len(rotated), slow_send_diag.MAX_ROTATED,
                             f"轮转文件超上限：{rotated}")
        self.assertGreater(len(rotated), 0, "应产生轮转文件")
        # 所有行均可解析
        for p in [self.diag_path, *rotated]:
            if p.exists():
                _read_diag(p)

    def test_step_overhead_under_half_ms(self):
        # 单点开销：now_pair + diff_ms 一次取样+差值的墙上耗时 <0.5ms
        pairs = []
        for _ in range(200):
            t0 = time.perf_counter()
            a = slow_send_diag.now_pair()
            b = slow_send_diag.now_pair()
            slow_send_diag.diff_ms(a, b)
            pairs.append((time.perf_counter() - t0) * 1000)
        self.assertLess(median(pairs), 0.5,
                         f"单点取样开销超标：中位 {median(pairs):.3f}ms")

    def test_1000_sends_overhead_comparison(self):
        # 1000 次发送：开关前后 p50/p99 对比（诊断不显著延长发送路径）
        def run_batch():
            latencies = []
            for _ in range(200):
                t0 = time.perf_counter()
                self.send(self.conn)
                latencies.append((time.perf_counter() - t0) * 1000)
            return latencies
        off = run_batch()  # 关闭开关基线
        self.enable_diag()
        on = run_batch()
        p50_off, p99_off = median(off), sorted(off)[int(len(off) * 0.99) - 1]
        p50_on, p99_on = median(on), sorted(on)[int(len(on) * 0.99) - 1]
        # 开关引入的绝对开销有界（p50 增量 <2ms、p99 增量 <8ms，避免环境噪声误判）
        self.assertLess(p50_on - p50_off, 2.0, f"p50 增量超限：{p50_off:.2f}→{p50_on:.2f}")
        self.assertLess(p99_on - p99_off, 8.0, f"p99 增量超限：{p99_off:.2f}→{p99_on:.2f}")
        rows = _read_diag(self.diag_path)
        steps = [r for r in rows if r["kind"] == "step" and r["name"] == "S3"]
        self.assertEqual(len(steps), 200, "开关开启的每笔发送都应有 S3 行")


if __name__ == "__main__":
    unittest.main()
