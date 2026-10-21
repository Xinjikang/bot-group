"""req#72 P1（task#122）单测：process_one 领取与 settle 收口的竞态交叠。

契约（req#72 P1 竞态）：
- process_one 领取 job 时把 queued → running 的 UPDATE 必须带 status='queued' 守卫。
- settle_thread 收口只把 queued 行改 done；若领取 SELECT 之后、UPDATE 之前被收口抢占，
  UPDATE rowcount=0 → 跳过不投递（已收口话题的 done job 不被复活重投）。
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from engine import db, queue, router, wake


class ClaimSettleRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(db, "db_path", return_value=os.path.join(self.tmp.name, "group.db"))
        p.start()
        self.addCleanup(p.stop)
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        db.init_schema(self.conn)
        self.conn.execute(
            "INSERT INTO members(profile,display_name,role) VALUES(?,?,?)",
            ("default", "蛇蛇", "dev"),
        )
        self.conn.execute(
            "INSERT INTO projects(name,root,created_at) VALUES(?,?,?)",
            ("p", ".", db.now_iso()),
        )
        self.conn.execute("INSERT INTO project_members(project,profile,active) VALUES('p','default',1)")
        self.conn.commit()

    def _make_queued_chat_job(self):
        # open 话题，turn 轮到 default，members 含 default（chat discard 检查全过）
        self.conn.execute(
            "INSERT INTO threads(id,project,title,creator,members,status,created_at,last_active_at,turn_profile) "
            "VALUES(42,'p','t','user','[\"user\",\"default\"]','open','now','now','default')"
        )
        self.conn.commit()
        wake.enqueue(self.conn, "default", "chat", "thread-open", thread_id=42, project="p")
        job = self.conn.execute("SELECT id FROM wake_jobs WHERE profile='default'").fetchone()
        return job["id"]

    def test_claim_guard_prevents_unconditional_revive(self):
        """领取 UPDATE 带 status='queued' 守卫：已非 queued 的行 rowcount=0，不复活重投。"""
        self._make_queued_chat_job()
        # 直接把 job 置 done（模拟收口 settle 已处理），再走领取——SELECT 查不到 queued，
        # process_one 无候选，deliver 不调用。
        self.conn.execute("UPDATE wake_jobs SET status='done' WHERE id=1")
        self.conn.commit()
        with patch.object(wake, "session_busy", return_value=False), \
             patch.object(wake, "deliver") as deliver:
            self.assertFalse(queue.process_one(self.conn))
            deliver.assert_not_called()

    def test_claim_instant_settle_race_skips(self):
        """领取瞬间被收口抢占：SELECT 时 queued、UPDATE 时已被 settle 改 done → rowcount=0 跳过。"""
        job_id = self._make_queued_chat_job()

        class RacingConn:
            """代理真实 conn：在领取 UPDATE（SET status='running'）前抢先 settle 改 done。"""
            def __init__(self, real):
                self._real = real

            def execute(self, sql, *args):
                if isinstance(sql, str) and "SET status='running'" in sql:
                    self._real.execute(
                        "UPDATE wake_jobs SET status='done',ended_at=?,detail='topic settled' WHERE id=?",
                        (db.now_iso(), job_id),
                    )
                    self._real.commit()
                return self._real.execute(sql, *args)

            def commit(self):
                return self._real.commit()

            def __getattr__(self, name):
                return getattr(self._real, name)

        with patch.object(wake, "session_busy", return_value=False), \
             patch.object(wake, "deliver") as deliver:
            self.assertFalse(queue.process_one(RacingConn(self.conn)))
            deliver.assert_not_called()
        # job 最终是 settle 改的 done，未被复活成 running
        row = self.conn.execute(
            "SELECT status FROM wake_jobs WHERE id=?", (job_id,)
        ).fetchone()
        self.assertEqual(row["status"], "done")

    def test_normal_claim_still_delivers(self):
        """无竞态时领取照常投递（守卫不误伤正常路径）。"""
        job_id = self._make_queued_chat_job()
        with patch.object(wake, "session_busy", return_value=False), \
             patch.object(wake, "deliver", return_value={"ok": True}) as deliver:
            self.assertTrue(queue.process_one(self.conn))
            deliver.assert_called_once()
        row = self.conn.execute(
            "SELECT status FROM wake_jobs WHERE id=?", (job_id,)
        ).fetchone()
        self.assertEqual(row["status"], "done")  # 投递成功 mark done

    def test_topic_changes_between_selection_and_claim(self):
        job_id = self._make_queued_chat_job()
        changes = (
            ("turn_profile", "someone-else"),
            ("status", "settled"),
            ("members", '["user"]'),
            ("project", "other-project"),
        )
        for field, value in changes:
            with self.subTest(field=field):
                self.conn.execute(
                    "UPDATE threads SET turn_profile='default',status='open',"
                    "members='[\"user\",\"default\"]',project='p' WHERE id=42")
                self.conn.execute("UPDATE wake_jobs SET status='queued',started_at=NULL,ended_at=NULL WHERE id=?", (job_id,))
                self.conn.commit()

                def change_topic(*args):
                    other = db.get_connection()
                    try:
                        other.execute(f"UPDATE threads SET {field}=? WHERE id=42", (value,))
                        other.commit()
                    finally:
                        other.close()
                    return False

                with patch.object(wake, 'session_busy', side_effect=change_topic), \
                        patch.object(wake, 'deliver') as deliver:
                    self.assertFalse(queue.process_one(self.conn))
                    deliver.assert_not_called()
                row = self.conn.execute('SELECT status,started_at,ended_at FROM wake_jobs WHERE id=?', (job_id,)).fetchone()
                self.assertEqual(row['status'], 'done')
                self.assertIsNone(row['started_at'])
                self.assertIsNotNone(row['ended_at'])


if __name__ == "__main__":
    unittest.main()
