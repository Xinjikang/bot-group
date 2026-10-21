"""Terminal requirement gates across creation, claim, scan and late execution."""

import unittest
from unittest.mock import patch

from engine import db, scanner, taskpool, task_retry
from scripts.test_task_brief import BRIEF
from scripts.unified_test_support import isolated_database, confirmed_requirement_fields, role_id_of


class RequirementLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = isolated_database()
        self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)

    def create(self):
        rid = taskpool.req_create("alpha", "default", "Lifecycle",
                                  **confirmed_requirement_fields())["requirement_id"]
        created = taskpool.task_create("alpha", "architect", rid,
                                       [{**BRIEF, "title": "Work", "next_bot": "dev"}])
        return rid, created["task_ids"][0], created["credential"]

    def test_inactive_requirements_cannot_create_claim_scan_or_retry(self):
        for status, archived in (("cancelled", 0), ("done", 0), ("open", 1)):
            with self.subTest(status=status, archived=archived):
                rid, tid, cred = self.create()
                conn = db.get_connection()
                try:
                    conn.execute("UPDATE requirements SET status=?,archived=? WHERE id=?", (status, archived, rid))
                    conn.commit()
                    blocked = taskpool.task_create("alpha", "architect", rid,
                        [{**BRIEF, "title": "Late"}], credential=cred, complete=True)
                    self.assertEqual(blocked["code"], "requirement_inactive")
                    self.assertFalse(taskpool.task_claim("alpha", "dev")["claimed"])
                    self.assertIsNone(task_retry.select_task(conn, "dev", "alpha"))
                    with patch("engine.wake.enqueue") as enqueue:
                        self.assertEqual(scanner.scan_tasks(conn)["claimable"], 0)
                        self.assertEqual(task_retry.finish("dev", "alpha", tid, "late", "failure"),
                                         {"blocked": [], "retry": []})
                        enqueue.assert_not_called()
                    conn.execute("UPDATE tasks SET status='in_progress',claimed_by='dev',claimed_at='2000-01-01T00:00:00+00:00' WHERE id=?", (tid,))
                    conn.commit()
                    with patch("engine.wake.enqueue") as enqueue:
                        self.assertEqual(scanner.scan_tasks(conn)["stale"], 0)
                        enqueue.assert_not_called()
                    result = taskpool.task_log("alpha", "dev", tid, "handoff", result="Done",
                                               next_bot="reviewer", expected_role_id=role_id_of("alpha", "reviewer"))
                    self.assertEqual(result["code"], "requirement_inactive")
                    self.assertEqual(taskpool.task_block("alpha", "dev", tid, "late")["code"], "requirement_inactive")
                    self.assertEqual(conn.execute("SELECT status FROM requirements WHERE id=?", (rid,)).fetchone()[0], status)
                    conn.execute("UPDATE tasks SET status='cancelled',claimed_by='' WHERE id=?", (tid,))
                    conn.commit()
                finally:
                    conn.close()

    def test_cancel_then_replay_or_finalize_rejects_old_credential(self):
        rid, tid, cred = self.create()
        self.assertTrue(taskpool.req_cancel("alpha", "default", rid)["cancelled"])
        result = taskpool.task_create("alpha", "architect", rid, [], credential=cred, complete=True)
        self.assertEqual(result["code"], "requirement_inactive")
        self.assertFalse(taskpool.task_claim("alpha", "dev")["claimed"])

    def test_draft_finalize_rolls_back_and_can_be_published_then_finalized(self):
        rid = taskpool.req_create("alpha", "default", "Draft",
                                  **confirmed_requirement_fields())["requirement_id"]
        result = taskpool.task_create("alpha", "architect", rid,
                                     [{"title": "Empty", "draft": True}], complete=True)
        self.assertEqual(result["code"], "decompose_incomplete")
        conn = db.get_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks WHERE requirement_id=?", (rid,)).fetchone()[0], 0)
        conn.close()
        draft = taskpool.task_create("alpha", "architect", rid, [{"title": "Draft", "draft": True}])
        blocked = taskpool.task_create("alpha", "architect", rid, [], credential=draft["credential"], complete=True)
        self.assertEqual(blocked["code"], "decompose_incomplete")
        updated = taskpool.task_update("alpha", "architect", draft["task_ids"][0],
                                       **BRIEF, publish=True, credential=draft["credential"])
        self.assertNotIn("error", updated)
        done = taskpool.task_create("alpha", "architect", rid, [], credential=draft["credential"], complete=True)
        self.assertEqual(done["decompose_state"], "done")


if __name__ == "__main__":
    unittest.main()
