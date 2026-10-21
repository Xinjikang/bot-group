"""成员停用语义测试（req#23 task#53 + req#27 task#57 按项目维度，规格 ENGINEERING 27.3/28.2）。

语义：跳过不唤醒、不踢出；启停按项目维度（project_members，无记录=未加入）。
覆盖：停用成员 ①不在轮次 ②不插 inbox ③_next_speaker 跳过 ④历史不动
⑤正占 turn → 立即推进 ⑥旧 pending 作废 ⑦重新启用不补投
⑧queue 存量 job 堵死 ⑨同 bot A 项目停用 / B 项目启用（req#27 核心语义）。

跑法：python -B -m unittest scripts.test_member_disabled -v
"""
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import api, queue, router  # noqa: E402

SCHEMA = (ROOT / "engine" / "schema.sql").read_text(encoding="utf-8")


class _KeepOpen(sqlite3.Connection):
    def close(self):
        pass


class _Base(unittest.TestCase):
    """公共夹具：内存库 + 三个 bot + 一个话题（project=bot-group）。"""

    PROJECT = "bot-group"

    def setUp(self):
        self.conn = sqlite3.connect(":memory:", factory=_KeepOpen)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for project in (self.PROJECT, 'other-project'):
            self.conn.execute("INSERT INTO projects(name,root,created_at) VALUES(?,'.','now')", (project,))
            for profile in ('a', 'b', 'c'):
                self.conn.execute('INSERT INTO project_members(project,profile,active) VALUES(?,?,1)', (project, profile))
        for p in ("a", "b", "c", "user"):
            self.conn.execute(
                "INSERT INTO members(profile,display_name,role) VALUES(?,?,?)", (p, p, "dev")
            )
        self.conn.execute(
            "INSERT INTO threads(id,project,title,creator,members,created_at,last_active_at,round,turn_profile) "
            "VALUES(1,?, 't','a',?, 'now','now',1,'')",
            (self.PROJECT, json.dumps(["a", "b", "c", "user"])),
        )
        self.conn.execute(
            "INSERT INTO messages(id,ts,sender,sender_type,content,thread_id,round,project) "
            "VALUES(1,'now','a','bot','opening',1,0,?)", (self.PROJECT,)
        )
        self.conn.commit()
        p = patch.object(api, "_conn", return_value=self.conn)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.conn.close)

    def _disable(self, profile, project=None):
        """走真实接口停用（覆盖 api 的作废 + 即时推进接线）。"""
        return api.set_member_active(
            profile, {"active": False, "project": project or self.PROJECT}, ('user', 'user')
        )

    def _enable(self, profile, project=None):
        return api.set_member_active(
            profile, {"active": True, "project": project or self.PROJECT}, ('user', 'user')
        )

    def _pm(self, profile, project=None):
        return self.conn.execute(
            "SELECT active FROM project_members WHERE project=? AND profile=?",
            (project or self.PROJECT, profile),
        ).fetchone()

    def _thread_members(self):
        return json.loads(
            self.conn.execute("SELECT members FROM threads WHERE id=1").fetchone()["members"]
        )

    def _messages_digest(self):
        import hashlib
        rows = self.conn.execute(
            "SELECT id,sender,content,round,depth FROM messages WHERE thread_id=1 ORDER BY id"
        ).fetchall()
        return hashlib.sha256(
            json.dumps([dict(r) for r in rows], ensure_ascii=False).encode()
        ).hexdigest()

    def _inbox_profiles(self):
        return [r["profile"] for r in self.conn.execute("SELECT profile FROM inbox ORDER BY id")]

    def _queued_profiles(self):
        return [
            r["profile"]
            for r in self.conn.execute(
                "SELECT profile FROM wake_jobs WHERE status IN ('queued','running') ORDER BY id"
            )
        ]


class MemberDisabledTests(_Base):
    # ---------- ① 不在轮次顺序 ----------

    def test_disabled_member_leaves_round_order(self):
        self.assertIn("b", router._round_members(self.conn, 1))
        self._disable("b")
        self.assertNotIn("b", router._round_members(self.conn, 1))
        # 其余成员顺序不变（user 本就在轮次列表里、creator 仍压轴，既有行为）
        self.assertEqual(router._round_members(self.conn, 1), ["c", "user", "a"])

    def test_disabled_creator_drops_out_of_round(self):
        """creator 自己被停用 → 自然退出轮次，不再压轴。"""
        self._disable("a")
        self.assertEqual(router._round_members(self.conn, 1), ["b", "c", "user"])

    # ---------- ② 不插 inbox 行 ----------

    def test_disabled_member_gets_no_inbox_row(self):
        self._disable("b")
        self.conn.execute("DELETE FROM inbox")
        router._insert_inbox_rows(self.conn, 1, ["a", "b", "c", "user"], self.PROJECT)
        self.assertEqual(sorted(self._inbox_profiles()), ["a", "c", "user"])

    def test_user_always_receives_inbox_rows(self):
        """user 不在启停语义里，任何情况都照发。"""
        self.conn.execute("UPDATE project_members SET active=0 WHERE profile='user'")
        self.conn.commit()
        router._insert_inbox_rows(self.conn, 1, ["user"], self.PROJECT)
        self.assertEqual(self._inbox_profiles(), ["user"])

    def test_unknown_profile_is_filtered(self):
        """未加入项目的 profile 不接收 inbox。"""
        router._insert_inbox_rows(self.conn, 1, ["ghost"], self.PROJECT)
        self.assertEqual(self._inbox_profiles(), [])

    # ---------- ③ _next_speaker 跳过它且轮次继续 ----------

    def test_next_speaker_skips_disabled_member_and_continues(self):
        self.assertEqual(router._next_speaker(self.conn, 1)["profile"], "b")
        self._disable("b")
        nxt = router._next_speaker(self.conn, 1)
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt["profile"], "c")

    def test_round_continues_without_disabled_member(self):
        """停用 b 后，轮次继续推进（b 不占位、不阻塞）。"""
        self._disable("b")
        nxt = router._next_speaker(self.conn, 1)
        router._set_turn(self.conn, 1, nxt['profile'], nxt['parent_id'], nxt['round'])
        router.route_message(self.conn, "c", "实质发言一", "bot", 1)
        nxt = router._next_speaker(self.conn, 1)
        self.assertIsNotNone(nxt)
        self.assertNotEqual(nxt["profile"], "b")
        self.assertEqual(nxt["profile"], "a")

    # ---------- ④ 不踢出：members 与历史消息不变 ----------

    def test_disable_does_not_touch_members_or_history(self):
        thread_members_before = self._thread_members()
        digest_before = self._messages_digest()
        self._disable("b")
        self.assertEqual(self._thread_members(), thread_members_before)
        self.assertEqual(self._messages_digest(), digest_before)
        # members 行还在（纯花名册不动），只是 project_members 里记了停用
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM members WHERE profile='b'").fetchone())
        self.assertEqual(self._pm("b")["active"], 0)

    # ---------- ⑤ 停用时正占 turn → 立即推进 ----------

    def test_disable_current_turn_advances_immediately(self):
        self.assertEqual(router._next_speaker(self.conn, 1)["profile"], "b")
        router._set_turn(self.conn, 1, "b", None, 1)
        self.conn.commit()
        result = self._disable("b")
        self.assertEqual(result["advanced_topics"], 1)
        row = self.conn.execute(
            "SELECT turn_profile, turn_skipped FROM threads WHERE id=1"
        ).fetchone()
        self.assertEqual(row["turn_profile"], "c")
        self.assertIn("r:1:b", json.loads(row["turn_skipped"]))
        self.assertIn("c", self._queued_profiles())

    def test_disable_turn_leaves_when_nobody_left(self):
        """除它之外没有别的可发言人 → 不留卡死 turn。"""
        self.conn.execute("UPDATE project_members SET active=0 WHERE profile IN ('c','user')")
        self.conn.execute("UPDATE threads SET members=? WHERE id=1", (json.dumps(["a", "b"]),))
        self.conn.commit()
        router._set_turn(self.conn, 1, "b", None, 1)
        self.conn.commit()
        result = self._disable("b")
        self.assertEqual(result["advanced_topics"], 1)
        row = self.conn.execute("SELECT turn_profile FROM threads WHERE id=1").fetchone()
        self.assertNotEqual(row["turn_profile"], "b")

    def test_disable_does_not_wait_for_turn_timeout(self):
        """硬判据：当场推进，不等 TURN_TIMEOUT_SECONDS。"""
        router._set_turn(self.conn, 1, "b", None, 1)
        self.conn.commit()
        with patch.object(router, "TURN_TIMEOUT_SECONDS", 10 ** 9):
            result = self._disable("b")
        self.assertEqual(result["advanced_topics"], 1)

    def test_advance_only_touches_threads_this_profile_holds(self):
        """只推进「turn 指向它」的话题，不误伤别人的 turn。"""
        self.conn.execute(
            "INSERT INTO threads(id,project,title,creator,members,created_at,last_active_at,round,turn_profile) "
            "VALUES(2,?, 't2','c',?, 'now','now',1,'a')",
            (self.PROJECT, json.dumps(["a", "b", "c"])),
        )
        self.conn.execute(
            "INSERT INTO messages(id,ts,sender,sender_type,content,thread_id,round) "
            "VALUES(2,'now','c','bot','opening2',2,0)"
        )
        self.conn.commit()
        router._set_turn(self.conn, 1, "b", None, 1)
        self.conn.commit()
        result = self._disable("b")
        self.assertEqual(result["advanced_topics"], 1)
        other = self.conn.execute("SELECT turn_profile FROM threads WHERE id=2").fetchone()
        self.assertEqual(other["turn_profile"], "a")

    # ---------- ⑥ 旧 pending 作废（项目作用域）----------

    def test_disable_expires_stale_pending_rows_project_scoped(self):
        """停用时只作废该项目消息产生的 pending（跨项目未读不动）；不删行、closed_by 留 NULL。"""
        # 项目内未读
        self.conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(1,'b','pending')")
        # 跨项目未读（另一项目的消息）——req#27：停用只作用于该项目
        self.conn.execute(
            "INSERT INTO messages(id,ts,sender,sender_type,content,thread_id,project,mentions) "
            "VALUES(99,'now','user','user','别的项目的私信',NULL,'other-project','[\"b\"]')"
        )
        self.conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(99,'b','pending')")
        self.conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(1,'c','pending')")
        self.conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(1,'b','seen')")
        self.conn.commit()
        rows_before = self.conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]

        result = self._disable("b")

        self.assertEqual(result["expired_inbox"], 1)   # 只作废项目内那条
        statuses = {
            (r["message_id"], r["profile"], r["status"], r["closed_by"])
            for r in self.conn.execute("SELECT * FROM inbox")
        }
        self.assertIn((1, "b", "expired", None), statuses)
        self.assertIn((99, "b", "pending", None), statuses)   # 跨项目未读不动
        self.assertIn((1, "b", "seen", None), statuses)       # 已读行不动
        self.assertIn((1, "c", "pending", None), statuses)    # 别人的 pending 不动
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], rows_before
        )  # 不删行

    # ---------- ⑦ 重新启用（项目作用域）----------

    def test_reenable_does_not_replay_history(self):
        from engine import scanner
        from engine import wake as _wake
        self.conn.execute(
            "INSERT INTO messages(id,ts,sender,sender_type,content,thread_id,project,mentions) "
            "VALUES(9,'now','user','user','@b 停用前的私信',NULL,?, '[\"b\"]')", (self.PROJECT,)
        )
        self.conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(9,'b','pending')")
        self.conn.commit()
        self._disable("b")
        self._enable("b")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM inbox WHERE profile='b' AND status='pending' AND "
                "message_id IN (SELECT id FROM messages WHERE project=?)", (self.PROJECT,)
            ).fetchone()[0],
            0,
        )
        enqueued = []
        with patch.object(_wake, "enqueue",
                          side_effect=lambda conn, profile, *a, **k: enqueued.append(profile) or True):
            scanner.scan_inbox(self.conn)
        self.assertNotIn("b", enqueued)

    def test_reenable_restores_round_participation(self):
        self._disable("b")
        self.assertNotIn("b", router._round_members(self.conn, 1))
        self._enable("b")
        self.assertIn("b", router._round_members(self.conn, 1))

    # ---------- ⑨ req#27 核心语义：同 bot A 项目停用 / B 项目启用 ----------

    def test_same_bot_disabled_in_one_project_enabled_in_another(self):
        """req#27 核心：停用只作用于该项目，另一项目照常唤醒/参与。"""
        self._disable("b", project="other-project")
        # bot-group 里 b 仍启用
        self.assertIn("b", router._round_members(self.conn, 1))
        self.assertEqual(router._next_speaker(self.conn, 1)["profile"], "b")
        # other-project 里 b 被滤掉
        self.assertFalse(router._profile_active(self.conn, "other-project", "b"))
        self.assertTrue(router._profile_active(self.conn, self.PROJECT, "b"))

    def test_disable_in_this_project_does_not_expire_other_project_inbox(self):
        """停用 A 项目时，B 项目的未读不受影响（项目作用域）。"""
        self.conn.execute(
            "INSERT INTO messages(id,ts,sender,sender_type,content,thread_id,project,mentions) "
            "VALUES(99,'now','user','user','@b B项目的私信',NULL,'other-project','[\"b\"]')"
        )
        self.conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(99,'b','pending')")
        self.conn.commit()
        self._disable("b")   # 停 bot-group 里的 b
        st = self.conn.execute(
            "SELECT status FROM inbox WHERE profile='b' AND message_id=99"
        ).fetchone()["status"]
        self.assertEqual(st, "pending")   # other-project 的未读不动

    # ---------- mentions（消费点 1）----------

    def test_mentions_filtered_by_project(self):
        from engine import mentions
        self._disable("b", project="other-project")
        # bot-group 里 b 仍启用 → @b 能解析
        self.assertEqual(mentions.parse_mentions("hello @b", self.conn, self.PROJECT), ["b"])
        # other-project 里 b 停用 → @b 解析不出
        self.assertEqual(mentions.parse_mentions("hello @b", self.conn, "other-project"), [])
        self.assertEqual(mentions.parse_mentions("hello @c", self.conn, "other-project"), ["c"])

    # ---------- 接口缺 project → 400 ----------

    def test_active_endpoint_requires_project(self):
        import unittest.mock
        from fastapi import HTTPException
        with self.assertRaises(api.HTTPException) as error:
            api.set_member_active("b", {"active": False}, ('user', 'user'))   # 不带 project
        self.assertEqual(error.exception.status_code, 400)


class QueuedJobDisabledTests(unittest.TestCase):
    """规格 27.3 第 6 点 + req#27：停用者的存量 queued job 在 queue 层堵死（按项目）。"""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:", factory=_KeepOpen)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for project in ('xjk', 'other-project'):
            self.conn.execute("INSERT INTO projects(name,root,created_at) VALUES(?,'.','now')", (project,))
            for profile in ('a', 'b'):
                self.conn.execute('INSERT INTO project_members(project,profile,active) VALUES(?,?,1)', (project, profile))
        for p in ("a", "b", "user"):
            self.conn.execute(
                "INSERT INTO members(profile,display_name,role) VALUES(?,?,?)", (p, p, "dev")
            )
        self.conn.commit()
        patcher = patch.object(queue.wake, "deliver", side_effect=self._deliver)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.delivered = []
        busy = patch.object(queue.wake, 'session_busy', return_value=False)
        busy.start()
        self.addCleanup(busy.stop)

    def _deliver(self, profile, kind, **kwargs):
        self.delivered.append(profile)
        return {"ok": True, "detail": "spawned"}

    def tearDown(self):
        self.conn.close()

    def _queue(self, profile, project="xjk", kind="work", trigger="@msg", thread_id=None):
        self.conn.execute(
            "INSERT INTO wake_jobs(profile,project,kind,trigger,status,enqueued_at,thread_id) "
            "VALUES(?,?,?,?,'queued','now',?)",
            (profile, project, kind, trigger, thread_id),
        )
        self.conn.commit()

    def _disable(self, profile, project="xjk"):
        # req#86 兼容期：pm 主键 (project,bot_id) 对 NULL bot_id 不去重，ON CONFLICT
        # 按主键判冲突会漏——直接按 profile 定位行 UPDATE，无行才插入。
        self.conn.execute(
            "UPDATE project_members SET active=0 WHERE project=? AND profile=?",
            (project, profile),
        )
        self.conn.execute(
            "INSERT INTO project_members(project,profile,active) "
            "SELECT ?,?,0 WHERE NOT EXISTS ("
            "  SELECT 1 FROM project_members WHERE project=? AND profile=?)",
            (project, profile, project, profile),
        )
        self.conn.commit()

    def test_queued_work_job_not_delivered_after_project_disable(self):
        self._queue("b", project="xjk")
        self._disable("b", project="xjk")
        self.assertFalse(queue.process_one(self.conn))
        self.assertEqual(self.delivered, [])
        row = self.conn.execute(
            "SELECT status, detail FROM wake_jobs WHERE profile='b'"
        ).fetchone()
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["detail"], "profile disabled")

    def test_same_bot_enabled_in_other_project_still_delivered(self):
        """req#27 核心：A 项目停用不影响 B 项目的存量 job。"""
        self._queue("b", project="other-project")
        self._disable("b", project="xjk")    # 只停 xjk
        self.assertTrue(queue.process_one(self.conn))
        self.assertEqual(self.delivered, ["b"])   # other-project 的 job 照常投递

    def test_enabled_member_still_delivered(self):
        self._queue("a", project="xjk")
        self.assertTrue(queue.process_one(self.conn))
        self.assertEqual(self.delivered, ["a"])

    def test_unknown_profile_not_delivered(self):
        """未加入项目的 profile 不投递。"""
        self._queue("ghost", project="xjk")
        queue.process_one(self.conn)
        self.assertEqual(self.delivered, [])

    def test_user_never_filtered(self):
        self._queue("user", project="xjk")
        queue.process_one(self.conn)
        self.assertEqual(self.delivered, ["user"])

    def test_disable_does_not_delete_jobs(self):
        self._queue("b", project="xjk")
        self._disable("b", project="xjk")
        before = self.conn.execute("SELECT COUNT(*) FROM wake_jobs").fetchone()[0]
        queue.process_one(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wake_jobs").fetchone()[0], before)

    def test_one_disabled_job_does_not_block_others(self):
        self._queue("b", project="xjk")
        self._queue("a", project="xjk")
        self._disable("b", project="xjk")
        self.assertTrue(queue.process_one(self.conn))
        self.assertEqual(self.delivered, ["a"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
