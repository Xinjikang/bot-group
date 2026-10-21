"""Restart recovery uses isolated SQLite and simulated process/delivery results."""

import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from engine import db, heartbeat, queue, router, scanner, wake


class TopicRestartRecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dbfile = os.path.join(tmp.name, "group.db")
        patcher = patch.object(db, "db_path", return_value=dbfile)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        db.init_schema(self.conn)
        self.conn.executemany(
            "INSERT INTO members(profile,display_name,role) VALUES(?,?,?)",
            [(p, p, p) for p in ("p", "q", "user")],
        )
        self.conn.execute(
            "INSERT INTO projects(name,root,created_at) VALUES('bot-group','.',?)", (db.now_iso(),),
        )
        self.conn.commit()
        self.now = time.time()
        self.old_turn = db.now_iso(seconds_offset=-600)
        for target, attr, value in [(wake, "_running_sessions", set()),
                                    (heartbeat, "_beats", {})]:
            patcher = patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def topic(self, tid=42, project="bot-group", parent=None, job_status="done"):
        from scripts.project_test_support import enable_members
        enable_members(self.conn, (project,))
        self.conn.execute(
            "INSERT INTO threads(id,project,title,creator,members,status,created_at,last_active_at,"
            "turn_profile,turn_wake_at,turn_parent_id,round) VALUES(?,?,'topic','user',?,"
            "'open',?,?,'p',?,?,1)",
            (tid, project, json.dumps(["p", "q", "user"]), db.now_iso(), db.now_iso(), self.old_turn, parent),
        )
        mid = self.conn.execute(
            "INSERT INTO messages(project,ts,sender,sender_type,content,thread_id,round) "
            "VALUES(?,?,'user','user','question',?,1)", (project, self.old_turn, tid),
        ).lastrowid
        self.conn.execute("INSERT INTO inbox(message_id,profile) VALUES(?,'p')", (mid,))
        self.conn.execute(
            "INSERT INTO wake_jobs(profile,project,kind,trigger,trigger_msg_id,thread_id,status,"
            "enqueued_at,started_at,ended_at) VALUES('p',?,'chat','thread-open',?,?,?, ?,?,?)",
            (project, mid, tid, job_status, self.old_turn, self.old_turn,
             self.old_turn if job_status == "done" else None),
        )
        session = wake.session_name("p", "chat", tid, project)
        self.conn.execute(
            "INSERT INTO wake_runs(session,started_at,pid,project) VALUES(?,?,123,?)",
            (session, self.now - 599, project),
        )
        self.conn.commit()
        return mid

    def active_jobs(self):
        return self.conn.execute(
            "SELECT * FROM wake_jobs WHERE status IN ('queued','running') ORDER BY id"
        ).fetchall()

    def turn(self, tid=42):
        return self.conn.execute("SELECT * FROM threads WHERE id=?", (tid,)).fetchone()

    def test_done_delivery_recovers_same_turn_and_preserves_unread_and_history(self):
        self.topic()
        before = dict(self.turn())
        inbox = [tuple(r) for r in self.conn.execute("SELECT * FROM inbox")]
        self.assertEqual(db.recover_on_startup(self.conn), 1)
        job, = self.active_jobs()
        self.assertEqual((job["profile"], job["project"], job["thread_id"]), ("p", "bot-group", 42))
        after = dict(self.turn())
        self.assertNotEqual(after.pop("turn_wake_at"), before.pop("turn_wake_at"))
        self.assertEqual(after, before)
        self.assertEqual([tuple(r) for r in self.conn.execute("SELECT * FROM inbox")], inbox)
        original = self.conn.execute("SELECT status,ended_at FROM wake_jobs WHERE id=1").fetchone()
        self.assertEqual(tuple(original), ("done", self.old_turn))
        self.assertEqual(self.conn.execute("SELECT exit_code FROM wake_runs").fetchone()[0], -1)
        # Startup scanning must leave this same slot eligible; no skipped turn.
        scanner.run_once(self.conn)
        self.assertEqual(self.turn()["turn_profile"], "p")
        self.assertEqual(self.turn()["turn_skipped"], "[]")
        with patch.object(wake, "find_bot_processes", return_value=[]) as processes, \
                patch.object(wake, "deliver", return_value={"ok": True}) as deliver:
            self.assertTrue(queue.process_one(self.conn))
            processes.assert_called_with("p", session="p-bot-group-chat-42")
            self.assertTrue(all(call.args == ('p',) and call.kwargs == {'session':'p-bot-group-chat-42'}
                                for call in processes.call_args_list))
        self.assertEqual(deliver.call_args.args, ("p", "chat"))
        self.assertEqual(deliver.call_args.kwargs["thread_id"], 42)

    def test_restart_twice_deduplicates_recovery(self):
        self.topic()
        db.recover_on_startup(self.conn)
        first = dict(self.active_jobs()[0])
        self.assertEqual(db.recover_on_startup(self.conn), 0)
        self.assertEqual([dict(r) for r in self.active_jobs()], [first])

    def test_running_delivery_reuses_existing_job(self):
        self.topic(job_status="running")
        self.assertEqual(db.recover_on_startup(self.conn), 1)
        job, = self.active_jobs()
        self.assertEqual(job["id"], 1)
        self.assertIsNone(job["started_at"])
        self.assertTrue(job["trigger"].startswith("recover:chat:"))

    def test_existing_queued_job_is_not_duplicated(self):
        self.topic(job_status="queued")
        db.recover_on_startup(self.conn)
        self.assertEqual([j["id"] for j in self.active_jobs()], [1])

    def test_edge_recovery_preserves_parent_and_round(self):
        self.topic(parent=1)
        db.recover_on_startup(self.conn)
        self.assertEqual((self.turn()["turn_parent_id"], self.turn()["round"]), (1, 1))
        with patch.object(wake, "find_bot_processes", return_value=[]), \
                patch.object(wake, "deliver", return_value={"ok": True}):
            self.assertTrue(queue.process_one(self.conn))

    def test_live_or_unknown_process_defers_delivery_and_timeout(self):
        self.topic()
        db.recover_on_startup(self.conn)
        self.conn.execute("UPDATE threads SET turn_wake_at=?", (self.old_turn,))
        self.conn.commit()
        for result in ([999], None):
            with self.subTest(process_result=result), \
                    patch.object(wake, "find_bot_processes", return_value=result), \
                    patch.object(wake, "deliver") as deliver:
                self.assertFalse(queue.process_one(self.conn))
                deliver.assert_not_called()
                self.assertEqual(router.advance_timed_out_turns(self.conn), 0)
        with patch.object(wake, "find_bot_processes", return_value=[]), \
                patch.object(wake, "deliver", return_value={"ok": True}):
            self.assertTrue(queue.process_one(self.conn))
        self.assertNotEqual(self.turn()["turn_wake_at"], self.old_turn)
        self.assertEqual(router.advance_timed_out_turns(self.conn), 0)
        # Once delivered, a genuinely unresponsive bot retains normal timeout behavior.
        self.conn.execute("UPDATE threads SET turn_wake_at=?", (self.old_turn,))
        self.conn.commit()
        with patch.object(wake, "find_bot_processes", return_value=[]):
            self.assertEqual(router.advance_timed_out_turns(self.conn), 1)
        self.assertEqual(self.turn()["turn_profile"], "q")

    def test_ineligible_slots_are_not_recovered(self):
        cases = [
            "UPDATE threads SET status='closed'",
            "UPDATE threads SET status='settled'",
            "UPDATE threads SET turn_profile='q'",
            "UPDATE threads SET members='[\"q\",\"user\"]'",
            "UPDATE threads SET turn_skipped='[\"r:1:p\"]'",
            "UPDATE projects SET archived=1",
            "UPDATE project_members SET active=0 WHERE project='bot-group' AND profile='p'",
        ]
        for change in cases:
            with self.subTest(change=change):
                self.topic()
                self.conn.execute(change)
                self.conn.commit()
                self.assertEqual(db.recover_on_startup(self.conn), 0)
                self.assertEqual(self.active_jobs(), [])
                self.conn.executescript("DELETE FROM inbox; DELETE FROM messages; DELETE FROM threads; "
                                        "DELETE FROM wake_jobs; DELETE FROM wake_runs; DELETE FROM project_members; "
                                        "UPDATE projects SET archived=0;")

    def test_completed_response_is_not_replayed(self):
        for parent in (None, 1):
            with self.subTest(parent=parent):
                tid = 42 if parent is None else 43
                self.topic(tid=tid, parent=parent)
                self.conn.execute(
                    "INSERT INTO messages(project,ts,sender,sender_type,content,thread_id,round,parent_id) "
                    "VALUES('bot-group',?,'p','bot','answer',?,1,?)", (db.now_iso(), tid, parent),
                )
                self.conn.commit()
                self.assertEqual(db.recover_on_startup(self.conn), 0)
                self.assertEqual(self.active_jobs(), [])

    def test_finished_or_old_or_wrong_scope_runs_are_not_recovered(self):
        self.topic()
        session = "p-bot-group-chat-42"
        changes = [
            ("UPDATE wake_runs SET ended_at=?,exit_code=0", (self.now - 10,)),
            ("UPDATE wake_runs SET started_at=?", (self.now - 700,)),
            ("UPDATE wake_runs SET started_at=?", (self.now + 60,)),
            ("UPDATE wake_runs SET project='other'", ()),
            ("UPDATE wake_runs SET session='p-bot-group-work'", ()),
            ("UPDATE wake_runs SET session='p-bot-group-chat-43'", ()),
        ]
        for sql, params in changes:
            with self.subTest(sql=sql):
                self.conn.execute("DELETE FROM wake_runs")
                self.conn.execute("INSERT INTO wake_runs(session,started_at,project) VALUES(?,?,'bot-group')",
                                  (session, self.now - 599))
                self.conn.execute(sql, params)
                self.conn.commit()
                self.assertEqual(db.recover_on_startup(self.conn), 0)
                self.assertEqual(self.active_jobs(), [])

    def test_latest_finished_run_prevents_replaying_older_orphan(self):
        self.topic()
        self.conn.execute(
            "INSERT INTO wake_runs(session,started_at,ended_at,exit_code,project) "
            "VALUES('p-bot-group-chat-42',?,?,0,'bot-group')", (self.now - 20, self.now - 10),
        )
        self.conn.commit()
        self.assertEqual(db.recover_on_startup(self.conn), 0)

    def test_recovery_is_scoped_to_each_topic_and_project(self):
        self.topic()
        self.conn.execute("INSERT INTO projects(name,root,created_at) VALUES('other','.',?)", (db.now_iso(),))
        self.topic(tid=43, project="other")
        self.assertEqual(db.recover_on_startup(self.conn), 2)
        self.assertEqual({(j["project"], j["thread_id"]) for j in self.active_jobs()},
                         {("bot-group", 42), ("other", 43)})

    def test_new_slot_for_same_bot_cancels_stale_recovery(self):
        self.topic()
        db.recover_on_startup(self.conn)
        self.conn.execute("UPDATE threads SET round=2")
        self.conn.commit()
        with patch.object(wake, "deliver") as deliver:
            self.assertFalse(queue.process_one(self.conn))
            deliver.assert_not_called()
        self.assertEqual(self.active_jobs(), [])

    def test_slot_change_during_claim_is_rechecked_atomically(self):
        self.topic()
        db.recover_on_startup(self.conn)

        def change_slot(*args):
            other = db.get_connection()
            try:
                other.execute("UPDATE threads SET turn_parent_id=1")
                other.commit()
            finally:
                other.close()
            return False

        with patch.object(wake, "session_busy", side_effect=change_slot), \
                patch.object(wake, "deliver") as deliver:
            self.assertFalse(queue.process_one(self.conn))
            deliver.assert_not_called()
        self.assertEqual(self.active_jobs(), [])
        self.assertEqual(self.turn()["turn_parent_id"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
