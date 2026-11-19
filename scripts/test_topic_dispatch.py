"""Durability and concurrency checks for topic sends on isolated SQLite."""
from contextlib import closing
import asyncio
import json
import threading
import time
import unittest
from unittest.mock import patch

from engine import api, db, router, scanner, topic_dispatch, wake
from scripts.unified_test_support import isolated_database


class TopicDispatchTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_database())
        self.conn = self.enterContext(closing(db.get_connection()))
        self.topic = self.conn.execute(
            "INSERT INTO threads(project,title,creator,members,round,created_at,last_active_at) "
            """VALUES('alpha','test','user','["default","architect","user"]',1,'now','now')"""
        ).lastrowid
        self.conn.commit()

    def send(self, text):
        return router.route_message(self.conn, "user", text, "user", self.topic)["message_id"]

    def turn(self):
        return tuple(self.conn.execute("SELECT turn_profile,turn_parent_id FROM threads WHERE id=?",
                                       (self.topic,)).fetchone())

    def test_cutoff_and_restart_after_failed_wake(self):
        first = self.send("first")
        self.send("@architect later")
        scanner.scan_inbox(self.conn)
        self.assertEqual(self.turn(), ("", None))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM topic_dispatch").fetchone()[0], 2)
        real_enqueue = wake.enqueue

        def interrupted(*args, **kwargs):
            real_enqueue(*args, **kwargs)
            raise RuntimeError("before commit")

        # req#169 缺陷③：处理异常不再向上抛——job 保留、该话题退避，
        # dispatcher 循环继续处理其他话题（同进程内不阻塞）。
        with closing(db.get_connection()) as worker, patch.object(wake, "enqueue", side_effect=interrupted):
            self.assertTrue(topic_dispatch.process_one(worker))
        self.assertEqual(self.turn(), ("", None))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wake_jobs").fetchone()[0], 0)
        # 退避期内跳过该话题（每话题只处理其最早待办，不越序）
        self.assertFalse(topic_dispatch.process_one(self.conn))
        # 重启（退避内存态清零）后 drain 保存的待办——job 未丢、按序、不重复
        topic_dispatch._clear_backoff(self.topic)
        with closing(db.get_connection()) as restarted:
            db.init_schema(restarted)
            self.assertTrue(topic_dispatch.process_one(restarted))
            self.assertEqual(self.turn(), ("default", None))
            self.assertTrue(topic_dispatch.process_one(restarted))
            self.assertFalse(topic_dispatch.process_one(restarted))
        self.assertEqual([tuple(r) for r in self.conn.execute("SELECT profile,trigger_msg_id FROM wake_jobs")],
                         [("default", first)])

    def test_engine_start_drains_saved_work_and_joins_dispatcher(self):
        from fastapi import FastAPI
        from engine import main
        self.send("ready before startup")

        async def start():
            async with main._engine_lifespan(FastAPI()):
                for _ in range(100):
                    if self.turn() == ("default", None):
                        return
                    await asyncio.sleep(0.02)
                self.fail("engine did not dispatch persisted work")

        self.addCleanup(wake._shutting_down.clear)
        with patch("engine.hermes_gateway.recover"), patch("engine.hermes_gateway.close_connections"), \
             patch("engine.queue.run_forever"), patch("engine.scanner.run_forever"), \
             patch("engine.project_hud.run_forever"), patch("engine.browser_resources.release", return_value={"ok": True}):
            asyncio.run(start())
        self.assertFalse(any(t.name == "topic-dispatch" for t in threading.enumerate()))

    def test_close_during_selection_cancels_stale_result(self):
        self.send("@architect work")
        select = router._next_speaker

        def close_after_read(*args):
            result = select(*args)
            api.post_thread_close(self.topic, {"conclusion": "closed"})
            return result

        with closing(db.get_connection()) as worker, patch.object(router, "_next_speaker", close_after_read):
            self.assertTrue(topic_dispatch.process_one(worker))
        self.assertEqual(self.turn(), ("", None))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wake_jobs").fetchone()[0], 0)
        self.assertFalse(topic_dispatch.process_one(self.conn))

    def test_pending_project_resumes_without_losing_wake(self):
        self.send("hello")
        self.conn.execute("UPDATE projects SET readiness_status='pending' WHERE name='alpha'")
        self.conn.commit()
        self.assertFalse(topic_dispatch.process_one(self.conn))
        self.conn.execute("UPDATE projects SET readiness_status='ready' WHERE name='alpha'")
        self.conn.commit()
        self.assertTrue(topic_dispatch.process_one(self.conn))
        self.assertEqual(self.turn(), ("default", None))

    def test_member_change_rechecks_before_committing(self):
        self.send("hello")
        select = router._next_speaker
        calls = []

        def disable_after_read(*args):
            result = select(*args)
            if not calls:  # 只在锁外选人这一次停用；锁内重算不重复写（会撞写锁）
                calls.append(1)
                self.conn.execute("UPDATE project_members SET active=0 WHERE project='alpha' AND profile='default'")
                self.conn.commit()
            return result

        # req#169 缺陷②：选人快照后成员停用 → 提交前复核候选可唤醒，当轮
        # 重判推进到活跃成员（不再等下一轮）。
        with closing(db.get_connection()) as worker, patch.object(router, "_next_speaker", disable_after_read):
            self.assertTrue(topic_dispatch.process_one(worker))
        self.assertEqual(self.turn(), ("architect", None))

    def test_last_pass_with_already_saved_interjection(self):
        self.conn.execute("""UPDATE threads SET members='["default","user"]',turn_profile='default' WHERE id=?""",
                          (self.topic,))
        router.route_message(self.conn, "default", None, "bot", self.topic)
        later = self.send("@default next question")
        self.assertTrue(topic_dispatch.process_one(self.conn))
        row = self.conn.execute("SELECT status,round FROM threads WHERE id=?", (self.topic,)).fetchone()
        self.assertEqual(tuple(row), ("open", 2))
        self.assertTrue(topic_dispatch.process_one(self.conn))
        self.assertEqual(self.turn(), ("default", later))
        self.assertEqual(self.conn.execute("SELECT status FROM inbox WHERE message_id=?", (later,)).fetchone()[0],
                         "pending")


class Req169RegressionTests(unittest.TestCase):
    """req#169 三条隔离反例转回归（话题审查记录 12102）。

    反例一：扫描器选人/等待不持写锁——持锁期间发送回执不被卡、收口不覆盖新消息。
    反例二：停用成员不占发言位——发送前/选人后停用，活跃成员接棒。
    反例三：跨话题重试阻塞——A 话题持续失败退避，B 话题正常唤醒，A 恢复后按序不重复。
    """

    def setUp(self):
        self.enterContext(isolated_database())
        self.conn = self.enterContext(closing(db.get_connection()))

    def topic(self, members):
        tid = self.conn.execute(
            "INSERT INTO threads(project,title,creator,members,round,created_at,last_active_at) "
            "VALUES('alpha','test','user',?,1,'now','now')",
            (json.dumps(members),),
        ).lastrowid
        self.conn.commit()
        return tid

    def send(self, tid, text):
        return router.route_message(self.conn, "user", text, "user", tid)["message_id"]

    # ---- 反例一：扫描器收口选人锁外，写锁内只复核+写 ----

    def _all_pass_open_topic(self):
        """构造全员 ack（pass）且 status=open、无待办的话题：scanner 收口候选。"""
        tid = self.topic(["default", "architect", "user"])
        self.send(tid, "hello")                                # 用户开场
        self.assertTrue(topic_dispatch.process_one(self.conn))  # turn=default
        router.route_message(self.conn, "default", "ok", "bot", tid)
        self.assertTrue(topic_dispatch.process_one(self.conn))  # turn=architect
        router.route_message(self.conn, "architect", "ok", "bot", tid)
        self.assertTrue(topic_dispatch.process_one(self.conn))  # 全员 pass → 收口
        self.assertEqual(self.conn.execute("SELECT status FROM threads WHERE id=?",
                                           (tid,)).fetchone()[0], "settled")
        # 用户恢复讨论：重新 open（新 round），无待办
        self.conn.execute("UPDATE threads SET status='open' WHERE id=?", (tid,))
        self.conn.execute("DELETE FROM topic_dispatch WHERE thread_id=?", (tid,))
        self.conn.commit()
        self.assertIsNone(router._next_speaker(self.conn, tid), "全员 pass 仍成立")
        return tid

    def test_scanner_settle_selection_does_not_hold_write_lock(self):
        tid = self._all_pass_open_topic()
        # topic_busy 慢 5 秒（模拟进程查询 I/O）——选人在锁外，期间发送不被卡
        real_busy = wake.topic_busy
        slow = threading.Event()

        def slow_busy(conn, thread_id, project):
            slow.set()
            time.sleep(5)
            return real_busy(conn, thread_id, project)

        def run_scanner():
            # SQLite 连接必须在使用它的线程内创建
            with closing(db.get_connection()) as scanner_conn:
                scanner.scan_thread_all_responded(scanner_conn)

        with patch.object(wake, "topic_busy", side_effect=slow_busy):
            worker = threading.Thread(target=run_scanner, daemon=True)
            worker.start()
            self.assertTrue(slow.wait(timeout=5))
            # 慢选人期间：主连接发送两条消息，回执（route_message 返回）必须
            # 在恢复前到达——写锁不因选人等待被持有
            started = time.monotonic()
            first_id = self.send(tid, "new one")
            second_id = self.send(tid, "new two")
            self.assertLess(time.monotonic() - started, 4.0,
                            "扫描器选人等待期间发送被写锁卡住")
            worker.join(timeout=10)
        # 锁内复核发现新消息（水位变化）→ 收口不覆盖新消息，话题保持 open，
        # 新消息已登记待办待重判
        self.assertEqual(self.conn.execute("SELECT status FROM threads WHERE id=?",
                                           (tid,)).fetchone()[0], "open")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM topic_dispatch WHERE thread_id=?",
                              (tid,)).fetchone()[0],
            2, "新消息登记待办，不被收口吞掉")
        ids = {r["id"] for r in self.conn.execute(
            "SELECT id FROM messages WHERE thread_id=?", (tid,)).fetchall()}
        self.assertIn(first_id, ids)
        self.assertIn(second_id, ids)

    def test_scanner_settle_write_phase_is_short(self):
        # 写锁内只做复核点查 + settle 写：慢 topic_busy（2s）时锁内写阶段必须
        # 远小于等待时长——选人/等待不在写锁内（写锁持有时间修复后对比）。
        tid = self._all_pass_open_topic()
        real_busy = wake.topic_busy

        def slow_busy(conn, thread_id, project):
            time.sleep(2)
            return real_busy(conn, thread_id, project)

        with closing(db.get_connection()) as scanner_conn, \
                patch.object(wake, "topic_busy", side_effect=slow_busy):
            started = time.monotonic()
            n = scanner.scan_thread_all_responded(scanner_conn)
            elapsed = time.monotonic() - started
        self.assertEqual(n, 1)
        self.assertGreaterEqual(elapsed, 2.0, "锁外选人等待计入总时长")
        self.assertEqual(self.conn.execute("SELECT status FROM threads WHERE id=?",
                                          (tid,)).fetchone()[0], "settled")

    # ---- 反例二：停用成员不占发言位 ----

    def test_disabled_member_mention_skipped_in_selection(self):
        tid = self.topic(["default", "architect", "user"])
        # 发送时 default 仍活跃，发送后才停用：@default 边不产生发言位，
        # 选人直接落到 architect（停用成员不占发言位）
        self.send(tid, "@default please")
        self.conn.execute("UPDATE project_members SET active=0 WHERE project='alpha' AND profile='default'")
        self.conn.commit()
        scanner.scan_inbox(self.conn)
        self.assertTrue(topic_dispatch.process_one(self.conn))  # dispatcher 消费登记
        self.assertEqual(self.turn_of(tid), ("architect", None))

    def turn_of(self, tid):
        return tuple(self.conn.execute(
            "SELECT turn_profile,turn_parent_id FROM threads WHERE id=?", (tid,)).fetchone())

    def test_disable_during_selection_rechecks_to_active_member(self):
        tid = self.topic(["default", "architect", "user"])
        self.send(tid, "hello")
        select = router._next_speaker
        calls = []

        def disable_after_read(*args):
            result = select(*args)  # 锁外选人返回 default
            if not calls:  # 只在锁外选人这一次停用；锁内重算不重复写（会撞写锁）
                calls.append(1)
                self.conn.execute(
                    "UPDATE project_members SET active=0 WHERE project='alpha' AND profile='default'")
                self.conn.commit()
            return result

        with closing(db.get_connection()) as worker, patch.object(
                router, "_next_speaker", side_effect=disable_after_read):
            self.assertTrue(topic_dispatch.process_one(worker))
        # 提交前复核候选可唤醒：default 已停用 → 当轮重判到 architect（活跃成员接棒）
        self.assertEqual(self.turn_of(tid), ("architect", None))

    # ---- 反例三：跨话题退避重试 ----

    def test_failing_topic_backs_off_and_other_topics_continue(self):
        tid_a = self.topic(["default", "architect", "user"])
        tid_b = self.topic(["default", "architect", "user"])
        self.send(tid_a, "a first")
        self.send(tid_a, "@architect a later")
        self.send(tid_b, "b first")
        scanner.scan_inbox(self.conn)
        # A 话题处理持续失败（模拟 _next_speaker 异常）
        real_next = router._next_speaker

        def fail_for_a(conn, thread_id, through_id=router.MAX_MESSAGE_ID):
            if thread_id == tid_a:
                raise RuntimeError("topic A persistent failure")
            return real_next(conn, thread_id, through_id)

        with closing(db.get_connection()) as worker, patch.object(
                router, "_next_speaker", side_effect=fail_for_a):
            # A 首次失败：退避标记，job 保留
            self.assertTrue(topic_dispatch.process_one(worker))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM topic_dispatch WHERE thread_id=?", (tid_a,)).fetchone()[0], 2,
            "A 的待办保留不丢")
        # 退避期内 A 被跳过，B 正常唤醒（不越 A 的序——B 独立推进）
        self.assertTrue(topic_dispatch.process_one(self.conn))
        self.assertEqual(self.turn_of(tid_b), ("default", None))
        self.assertEqual(self.turn_of(tid_a), ("", None), "A 未处理")
        # 退避到期后 A 恢复：按序处理最早待办，不重复；@architect 边在
        # default 发言后才产生发言位（树模型顺序语义），两条待办都消费完
        topic_dispatch._clear_backoff(tid_a)
        self.assertTrue(topic_dispatch.process_one(self.conn))
        self.assertEqual(self.turn_of(tid_a), ("default", None), "A 最早待办先处理")
        self.assertTrue(topic_dispatch.process_one(self.conn))
        self.assertFalse(topic_dispatch.process_one(self.conn))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM topic_dispatch").fetchone()[0], 0,
            "A 恢复后按序清空待办，不重复")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM topic_dispatch WHERE thread_id=?", (tid_b,)).fetchone()[0], 0)
