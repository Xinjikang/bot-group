"""Durability and concurrency checks for topic sends on isolated SQLite."""
from contextlib import closing
import asyncio
import threading
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

        with closing(db.get_connection()) as worker, patch.object(wake, "enqueue", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "before commit"):
                topic_dispatch.process_one(worker)
        self.assertEqual(self.turn(), ("", None))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wake_jobs").fetchone()[0], 0)
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

        def disable_after_read(*args):
            result = select(*args)
            self.conn.execute("UPDATE project_members SET active=0 WHERE project='alpha' AND profile='default'")
            self.conn.commit()
            return result

        with closing(db.get_connection()) as worker, patch.object(router, "_next_speaker", disable_after_read):
            self.assertTrue(topic_dispatch.process_one(worker))
        self.assertEqual(self.turn(), ("", None))
        self.assertTrue(topic_dispatch.process_one(self.conn))
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
