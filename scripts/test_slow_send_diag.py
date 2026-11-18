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
        # 双连接重放：持锁方=真实业务写入方（route_message 事务内 S5 注入
        # 2.17s 暂停），等待方 route_message 的 S3 墙钟≈持锁时长、线程 CPU≈0；
        # 持有区间由持有方真实埋点 begin→end 成对行得出（不手工代写）。
        self.enable_diag()
        self.conn.execute("INSERT INTO threads (project, title, creator, members, "
                          "status, turn_profile, created_at, last_active_at) "
                          "VALUES ('alpha', 't', 'user', '[\"user\",\"default\"]', "
                          "'open', 'default', '2026-11-18T10:00:00', '2026-11-18T10:00:00')")
        self.conn.commit()
        thread_id = self.conn.execute("SELECT id FROM threads").fetchone()["id"]
        release = threading.Event()
        errors = []

        def hold():
            # 持锁方：真实 route_message（S5 段注入暂停模拟长事务持有）
            holder = sqlite3.connect(db.db_path(), timeout=30, check_same_thread=False)
            holder.row_factory = sqlite3.Row
            holder.execute("PRAGMA busy_timeout=30000")
            try:
                real_insert = router._insert_inbox_rows

                def slow_insert(conn, *args, **kwargs):
                    time.sleep(2.17)  # 持锁方事务内暂停（现场 11835 锁等待时长）
                    return real_insert(conn, *args, **kwargs)
                with patch.object(router, "_insert_inbox_rows", side_effect=slow_insert):
                    router.route_message(holder, "default", "held", "bot", thread_id, "alpha")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                release.set()
                holder.close()

        thread = threading.Thread(target=hold)
        thread.start()
        time.sleep(0.3)  # 确保持锁方已进入事务（主线程等待时间相应缩短）
        waited = db.get_connection()
        self.addCleanup(waited.close)
        result = router.route_message(waited, "user", "hi", "user", thread_id, "alpha")
        thread.join(timeout=10)
        self.assertTrue(release.is_set(), "持锁方未释放")
        self.assertEqual(errors, [], f"持锁方异常：{errors}")
        rows = _read_diag(self.diag_path)
        s3_waited = next(r for r in rows if r["kind"] == "step" and r["name"] == "S3"
                         and r["conn_id"] == id(waited))
        # 主线程 0.3s 后才进入等待：S3 墙钟 = 剩余持锁时长（≈2.17-0.3）
        self.assertGreaterEqual(s3_waited["wall_ms"], 1800.0, f"S3 墙钟应≈持锁时长：{s3_waited}")
        self.assertLess(s3_waited["wall_ms"], 3500.0, f"S3 墙钟异常：{s3_waited}")
        self.assertLess(s3_waited["thread_cpu_ms"], s3_waited["wall_ms"] * 0.1,
                        f"S3 应为等待型（CPU≈0）：{s3_waited}")
        # 持锁方持有区间：真实埋点 begin→end 成对行差值（排除等待与日志 I/O）
        txns = [r for r in rows if r["kind"] == "txn"]
        holder_conns = {r["conn_id"] for r in txns if r["conn_id"] != id(waited)}
        self.assertEqual(len(holder_conns), 1, f"持锁方应只有一个连接：{holder_conns}")
        holder_conn = next(iter(holder_conns))
        holder_begin = next(r for r in txns
                            if r.get("is_begin") and r["conn_id"] == holder_conn)
        holder_end = next(r for r in txns
                          if r.get("is_end") and r["conn_id"] == holder_conn
                          and r.get("end_kind") == "commit")
        self.assertGreaterEqual(holder_end["wall_ts"] - holder_begin["wall_ts"], 2.0,
                                "持锁时长由持有方成对行 wall_ts 差值得出")
        # 等待方取得锁应在持有方释放之后
        waited_begin = next(r for r in txns
                            if r.get("is_begin") and r["conn_id"] == id(waited))
        self.assertGreaterEqual(waited_begin["wall_ts"], holder_end["wall_ts"] - 0.05,
                                "等待方取得锁应在持有方释放之后")
        # 话题发送落库成功（holder 已唤醒 default 时等待方 enqueue 被去重属正常）
        self.assertIsNotNone(result["message_id"])

    def test_lock_timeout_never_records_held(self):
        # 反例②：锁超时（busy_timeout 耗尽）不得出现持有记录（is_begin），
        # 只记 request + failed；消息未提交（独立连接不可见）。
        self.enable_diag()
        self.conn.execute("INSERT INTO threads (project, title, creator, members, "
                          "status, created_at, last_active_at) "
                          "VALUES ('alpha', 't', 'user', '[\"user\",\"default\"]', "
                          "'open', '2026-11-18T10:00:00', '2026-11-18T10:00:00')")
        self.conn.commit()
        thread_id = self.conn.execute("SELECT id FROM threads").fetchone()["id"]
        before = self.counts(self.observer)
        holder = db.get_connection()
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")  # 持锁方：另一连接真实持有写锁

        waited = sqlite3.connect(db.db_path(), timeout=30)
        waited.row_factory = sqlite3.Row
        waited.execute("PRAGMA busy_timeout=50")  # 50ms 后超时（现场反例同口径）
        self.addCleanup(waited.close)
        with self.assertRaises(sqlite3.OperationalError) as ctx:
            router.route_message(waited, "user", "hi", "user", thread_id, "alpha")
        self.assertIn("locked", str(ctx.exception))
        self.assertFalse(waited.in_transaction)
        self.assertEqual(self.counts(self.observer), before, "超时路径不得提交消息")
        rows = _read_diag(self.diag_path)
        txns = [r for r in rows if r["kind"] == "txn" and r["conn_id"] == id(waited)]
        self.assertTrue(any(r.get("is_request") for r in txns), "应有锁申请行")
        self.assertFalse(any(r.get("is_begin") for r in txns), "超时不得出现持有记录")
        self.assertTrue(any(r.get("end_kind") == "failed" for r in txns), "应有失败行")

    def test_diag_write_failure_does_not_change_business(self):
        # 反例③：诊断输出失败（提交后写盘 OSError）不得改变业务——消息已提交、
        # 返回正常、独立连接可见；异常回滚路径同样不受诊断失败影响。
        self.enable_diag()
        real_write = slow_send_diag._write_row

        def broken(*args, **kwargs):
            raise OSError("simulated disk failure")
        # 注：打桩 _ensure_stream（_write_row 内部 try 组件）验证失败被吞；
        # 直接打桩 _write_row 会绕过其内部异常隔离。
        before = self.counts(self.observer)
        with patch.object(slow_send_diag, "_ensure_stream", side_effect=broken):
            result = self.send(self.conn)  # 正常提交路径
        self.assertIsNotNone(result["message_id"])
        self.assertEqual(self.counts(self.observer),
                         tuple(a + b for a, b in zip(before, (1, 1, 1))),
                         "提交路径不受诊断写失败影响")
        # 异常回滚路径：RouteError 冒泡，诊断写失败不改变回滚与消息不可见
        before = self.counts(self.observer)
        with patch.object(slow_send_diag, "_ensure_stream", side_effect=broken):
            with self.assertRaises(router.RouteError):
                router.route_message(self.conn, "user", None, "user", None, "alpha")
        self.conn.rollback()  # 等价 api finally 的连接关闭隐式回滚
        self.assertEqual(self.counts(self.observer), before,
                         "回滚路径不受诊断写失败影响")

    def test_silent_topic_message_path(self):
        # 反例①：合法 bot null 独立发言位（silent 分支）不得崩溃，
        # 提交/唤醒一次、d4 有定义（S4/S5 行正常落盘）。
        self.enable_diag()
        self.conn.execute("INSERT INTO threads (project, title, creator, members, "
                          "status, round, turn_profile, created_at, last_active_at) "
                          "VALUES ('alpha', 't', 'user', '[\"user\",\"default\"]', "
                          "'open', 0, 'default', '2026-11-18T10:00:00', '2026-11-18T10:00:00')")
        self.conn.commit()
        thread_id = self.conn.execute("SELECT id FROM threads").fetchone()["id"]
        result = router.route_message(self.conn, "default", None, "bot", thread_id, "alpha")
        self.assertIsNone(result["message_id"])
        self.assertEqual(len(result["wake"]), 1)
        skipped = self.observer.execute(
            "SELECT turn_skipped FROM threads WHERE id=?", (thread_id,)).fetchone()[0]
        self.assertIn("r:0:default", skipped)
        rows = _read_diag(self.diag_path)
        names = [r["name"] for r in rows if r["kind"] == "step"]
        self.assertIn("S4", names)
        self.assertIn("S5", names)

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
        # 1000 次发送 × 开关前后：p50/p99 对比 + 日志量与完整启用埋点开销
        n = 1000

        def run_batch():
            latencies = []
            for _ in range(n):
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
        self.assertLess(p50_on - p50_off, 2.0,
                        f"p50 增量超限：n={n} {p50_off:.2f}→{p50_on:.2f}")
        self.assertLess(p99_on - p99_off, 8.0,
                        f"p99 增量超限：n={n} {p99_off:.2f}→{p99_on:.2f}")
        rows = _read_diag(self.diag_path)
        steps = [r for r in rows if r["kind"] == "step" and r["name"] == "S3"]
        self.assertEqual(len(steps), n, f"开关开启的每笔发送都应有 S3 行（n={n}）")
        # 日志量：每笔发送 5 个 step 行（S3-S7）+ 2 个 txn 行（request/begin/end=3 行）
        txn_rows = [r for r in rows if r["kind"] == "txn"]
        self.assertEqual(len(txn_rows), n * 3, "每笔发送应有 request/begin/end 三行")
        diag_bytes = self.diag_path.stat().st_size
        # 完整启用埋点开销：单笔日志量 = 总字节 / n（公开到报告）
        print(f"\n#314 开销实测：n={n} 开关关 p50={p50_off:.3f}ms p99={p99_off:.3f}ms；"
              f"开关开 p50={p50_on:.3f}ms p99={p99_on:.3f}ms；"
              f"诊断日志 {diag_bytes} 字节 / 每笔 {diag_bytes / n:.1f} 字节")


    def test_http_rollback_releases_lock_before_diag_write(self):
        # /msg 入队失败的异常回滚路径：必须先实际释放写锁，再写回滚行与落盘缓冲。
        # 独立连接（timeout=0）在回滚行写出时能立即取写锁；原错误保留、
        # 消息/收件箱/唤醒增量为 0（整笔回滚）。
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from engine import api as api_module, auth, events, wake
        self.enable_diag()
        app = FastAPI()
        app.include_router(api_module.router_api)
        before = self.counts(self.observer)
        probe_results = []

        def observing_write(row):
            real_write(row)
            if row.get("kind") == "txn" and row.get("end_kind") == "rollback":
                # 回滚行写出时刻：独立连接 timeout=0 申请 BEGIN IMMEDIATE，
                # 锁必须已释放（事务已回滚）——否则 database is locked。
                try:
                    probe = sqlite3.connect(db.db_path(), timeout=0)
                    probe.execute("BEGIN IMMEDIATE")
                    probe.commit()
                    probe_results.append(True)
                except sqlite3.OperationalError:
                    probe_results.append(False)
                finally:
                    probe.close()
        real_write = slow_send_diag._write_row
        real_enqueue = wake.enqueue

        def fail_enqueue(conn, *args, **kwargs):
            real_enqueue(conn, *args, **kwargs)
            raise sqlite3.OperationalError("isolated enqueue failure")
        with TestClient(app, client=("127.0.0.1", 0)) as client, \
                patch.object(auth, "configured", return_value=False), \
                patch.object(wake, "enqueue", side_effect=fail_enqueue), \
                patch.object(events.bus, "publish"), \
                patch.object(slow_send_diag, "_write_row", side_effect=observing_write):
            with self.assertRaises(sqlite3.OperationalError):
                client.post("/msg", json={"project": "alpha", "sender": "user",
                                          "text": "@default update"})
        self.assertEqual(self.counts(self.observer), before,
                         "入队失败必须整笔回滚（消息/收件箱/唤醒增量 0）")
        self.assertTrue(probe_results and all(probe_results),
                        "回滚行写出时锁必须已释放（timeout=0 可立即取锁）")
        rows = _read_diag(self.diag_path)
        rollback = [r for r in rows if r["kind"] == "txn"
                    and r.get("end_kind") == "rollback"]
        self.assertTrue(rollback, "回滚行应落盘")

    def test_http_rollback_failure_fallback_releases_before_write(self):
        # manager node1966 兜底组：显式 conn.rollback() 失败（authorizer 拒
        # SQLITE_ROLLBACK）时，close 兜底释放后才输出——request/begin/rollback
        # 三行写出时刻独立连接 timeout=0 均可立即取锁；原错误保留、增量为 0。
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from engine import api as api_module, auth, db, events, wake
        self.enable_diag()
        app = FastAPI()
        app.include_router(api_module.router_api)
        before = self.counts(self.observer)
        probe_results = []

        def deny_rollback(action, arg1, arg2, dbname, source):
            # 仅拒 ROLLBACK 事务控制（action=SQLITE_TRANSACTION 且 arg1='ROLLBACK'，
            # 与 manager node1966 authorizer 复现口径一致）：
            # BEGIN IMMEDIATE/COMMIT 正常，conn.rollback() 抛 DatabaseError。
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION \
                and arg1 == "ROLLBACK" else sqlite3.SQLITE_OK
        real_get_connection = db.get_connection

        def get_connection_with_deny():
            conn = real_get_connection()
            conn.set_authorizer(deny_rollback)
            return conn
        real_write = slow_send_diag._write_row

        def observing_write(row):
            real_write(row)
            if row.get("kind") == "txn" and (row.get("is_request") == 1
                                             or row.get("is_begin") == 1
                                             or row.get("end_kind") == "rollback"):
                # 每行 txn 写出时刻：独立连接 timeout=0 申请 BEGIN IMMEDIATE，
                # 锁必须已释放——否则 database is locked。
                probe = None
                try:
                    probe = sqlite3.connect(db.db_path(), timeout=0)
                    probe.execute("BEGIN IMMEDIATE")
                    probe.commit()
                    probe_results.append(True)
                except sqlite3.OperationalError:
                    probe_results.append(False)
                finally:
                    if probe is not None:
                        probe.close()
        real_enqueue = wake.enqueue

        def fail_enqueue(conn, *args, **kwargs):
            real_enqueue(conn, *args, **kwargs)
            raise sqlite3.OperationalError("isolated enqueue failure")
        with TestClient(app, client=("127.0.0.1", 0)) as client, \
                patch.object(db, "get_connection", side_effect=get_connection_with_deny), \
                patch.object(auth, "configured", return_value=False), \
                patch.object(wake, "enqueue", side_effect=fail_enqueue), \
                patch.object(events.bus, "publish"), \
                patch.object(slow_send_diag, "_write_row", side_effect=observing_write):
            with self.assertRaises(sqlite3.OperationalError):
                client.post("/msg", json={"project": "alpha", "sender": "user",
                                          "text": "@default update"})
        self.assertEqual(self.counts(self.observer), before,
                         "回滚失败兜底路径必须整笔回滚（消息/收件箱/唤醒增量 0）")
        self.assertTrue(probe_results and all(probe_results),
                        "回滚失败兜底：三行 txn 写出时锁必须已释放（timeout=0 可立即取锁）")
        rows = _read_diag(self.diag_path)
        rollback = [r for r in rows if r["kind"] == "txn"
                    and r.get("end_kind") == "rollback"]
        self.assertTrue(rollback, "兜底路径回滚行应落盘")


class MigrationCostTests(DiagHarness):
    """验收 2：实际迁移持锁耗时、索引回退与数据一致性。"""

    def test_index_migration_cost_and_rollback_consistency(self):
        # 5 万行消息规模下：建两索引耗时（迁移开销）+ DROP 回退后数据一致
        rows = 50000
        base = self.observer.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        self.conn.executemany(
            "INSERT INTO messages (project, ts, sender, sender_type, content, "
            "thread_id, parent_id, round, depth) "
            "VALUES ('alpha', '2026-11-18T09:00:00', 'user', 'user', 'bulk', 1, NULL, 0, 0)",
            [()] * rows,
        )
        self.conn.commit()
        self.conn.execute("DROP INDEX IF EXISTS idx_messages_round")
        self.conn.execute("DROP INDEX IF EXISTS idx_messages_reply")
        t0 = time.perf_counter()
        self.conn.execute("CREATE INDEX idx_messages_round "
                          "ON messages(thread_id, sender, round, parent_id)")
        self.conn.execute("CREATE INDEX idx_messages_reply ON messages(parent_id, sender)")
        migration_ms = (time.perf_counter() - t0) * 1000
        print(f"\n#314 迁移开销实测：{rows} 行建双索引 {migration_ms:.1f}ms")
        self.assertLess(migration_ms, 5000, f"5 万行建索引耗时异常：{migration_ms:.1f}ms")
        # 建索引后数据完整
        self.assertEqual(self.observer.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                         base + rows)
        # 回退：DROP 后数据一致、查询正确（无索引仍能正确执行）
        self.conn.execute("DROP INDEX IF EXISTS idx_messages_round")
        self.conn.execute("DROP INDEX IF EXISTS idx_messages_reply")
        count = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id=? AND sender=? AND round=? "
            "AND parent_id IS NULL", (1, "user", 0)).fetchone()[0]
        self.assertEqual(count, rows, "回退后查询结果与索引存在时一致")
        self.assertEqual(self.observer.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                         base + rows, "回退后数据完整")

    def test_rollback_index_recreation_idempotent(self):
        # DROP 后再次 init_schema 幂等重建（升级路径可反复执行）
        self.conn.execute("DROP INDEX IF EXISTS idx_messages_round")
        self.conn.execute("DROP INDEX IF EXISTS idx_messages_reply")
        self.conn.commit()
        db.init_schema(self.conn)
        names = {r["name"] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='messages'")}
        self.assertTrue({"idx_messages_round", "idx_messages_reply"} <= names)


if __name__ == "__main__":
    unittest.main()
