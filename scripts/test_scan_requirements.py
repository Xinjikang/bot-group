"""scanner 第五来源：未拆解需求按项目 task.create 权限唤醒。

契约（req#62）：
- requirements 表 status=open 且无关联 task 的需求 → 唤醒获授权成员 work 拆解（scan:req）
- 有 task 的需求（已拆）→ 不唤醒
- 非 open 状态（done/blocked/cancelled）→ 不唤醒
- 纯新增只读扫描，零 schema 改动
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
from engine import db, scanner, wake  # noqa: E402


class ScanRequirementsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="t108_")
        self.addCleanup(self.temporary.cleanup)
        self.tmp = self.temporary.name
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        db.init_schema(c)
        self.conn = c
        c.execute("INSERT INTO projects(name, root, created_at) VALUES('bot-group', '.', '2026-09-22T10:00:00')")
        for p, dn, role in [("architect", "苏瑾", "architect"),
                            ("default", "蛇蛇", "orchestrator")]:
            c.execute("INSERT INTO members(profile, display_name, role) VALUES(?,?,?)", (p, dn, role))
        c.execute("INSERT INTO project_members(project,profile,active) SELECT 'bot-group',profile,1 FROM members")
        c.execute("INSERT INTO project_permissions VALUES('bot-group','architect','task.create')")
        c.commit()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def _req(self, rid, status="open", project="bot-group"):
        self.conn.execute(
            "INSERT INTO requirements(id, project, title, status, description, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, 'Legacy body', '2026-09-22T10:00:00', '2026-09-22T10:00:00')",
            (rid, project, f"req{rid}", status),
        )

    def _task(self, tid, req_id, project="bot-group"):
        self.conn.execute(
            "INSERT INTO tasks(id, project, requirement_id, title, status, created_at) "
            "VALUES(?, ?, ?, ?, 'pending', '2026-09-22T10:00:00')",
            (tid, project, req_id, f"task{tid}"),
        )

    def _wake_architect(self):
        return [r["trigger"] for r in self.conn.execute(
            "SELECT trigger FROM wake_jobs WHERE profile='architect' AND kind='work' ORDER BY id"
        ).fetchall()]

    def test_open_no_task_wakes_architect(self):
        """open 且无 task 的需求 → 唤醒 architect（scan:req）。"""
        self._req(1)
        self.conn.commit()
        self.assertEqual(scanner.scan_requirements(self.conn), 1)
        self.assertEqual(self._wake_architect(), ["scan:req"])

    def _signal(self, kind="work", budget=True):
        self.conn.commit()
        with patch.object(wake, "_fits_command_budget", return_value=budget):
            return wake._finalize_injection_signal(
                "architect", kind, None, "bot-group", None, None, ["hermes"])

    def test_empty_inbox_receives_requirement_action(self):
        self._req(86)
        self.conn.commit()
        self.assertEqual(scanner.scan_requirements(self.conn), 1)
        signal = self._signal()
        self.assertIn("#86", signal)
        self.assertIn("req_list(project=\"bot-group\", status='open')", signal)
        self.assertIn("创建任务", signal)
        self.assertIn("即使 inbox 为空", signal)

    def test_coalesced_message_wake_keeps_requirement_action_on_fallback(self):
        self._req(86)
        wake.enqueue(self.conn, "architect", "work", "scan:inbox", project="bot-group")
        mid = self.conn.execute(
            "INSERT INTO messages(project,ts,sender,sender_type,content,mentions) "
            "VALUES('bot-group','now','default','bot','MESSAGE_BODY','[]')"
        ).lastrowid
        self.conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(?,'architect','pending')", (mid,))
        self.conn.commit()
        scanner.scan_requirements(self.conn)
        self.assertEqual(self._wake_architect(), ["scan:inbox"])
        injected = self._signal()
        self.assertIn("MESSAGE_BODY", injected)
        self.assertIn("#86", injected)
        fallback = self._signal(budget=False)
        self.assertNotIn("MESSAGE_BODY", fallback)
        self.assertIn("#86", fallback)

    def test_requirement_context_refreshes_after_enqueue(self):
        self._req(86)
        self.conn.commit()
        scanner.scan_requirements(self.conn)
        self._task(1, 86)
        self.assertNotIn("需求拆解", self._signal())

    def test_requirement_context_respects_scope_and_status(self):
        self._req(86)
        self._req(87, status="done")
        self._req(88)
        self._task(1, 88)
        self._req(89, project="other")
        self._req(90)
        self.conn.execute("UPDATE requirements SET archived=1 WHERE id=90")
        signal = self._signal()
        self.assertIn("#86", signal)
        for rid in (87, 88, 89, 90):
            self.assertNotIn(f"#{rid}", signal)
        self.assertNotIn("需求拆解", self._signal(kind="chat"))
        self.conn.execute("UPDATE projects SET archived=1 WHERE name='bot-group'")
        self.assertNotIn("需求拆解", self._signal())

    def test_requirement_context_rechecks_permissions_and_membership(self):
        self._req(86)
        self.conn.execute("DELETE FROM project_permissions")
        self.assertNotIn("需求拆解", self._signal())
        self.conn.execute("INSERT INTO project_permissions VALUES('bot-group','architect','task.create')")
        self.conn.execute("UPDATE project_members SET active=0 WHERE profile='architect'")
        self.assertNotIn("需求拆解", self._signal())

    def test_requirement_context_bounds_large_backlog(self):
        for rid in range(100, 125):
            self._req(rid)
        signal = self._signal()
        self.assertIn("#100", signal)
        self.assertIn("#119", signal)
        self.assertNotIn("#120", signal)
        self.assertIn("另有待拆需求", signal)

    def test_req_with_task_not_woken(self):
        """有 task 且已拆完（done）的需求 → 不唤醒。"""
        self._req(1)
        self._task(10, 1)
        self.conn.execute("UPDATE requirements SET decompose_state='done' WHERE id=1")
        self.conn.commit()
        self.assertEqual(scanner.scan_requirements(self.conn), 0)

    def test_pending_with_partial_tasks_still_recovered(self):
        """验收④：已有零星任务的需求（pending）仍能被 scanner 恢复，不再漏拆。"""
        self._req(1)
        self._task(10, 1)
        self.conn.commit()
        self.assertEqual(scanner.scan_requirements(self.conn), 1)
        self.assertEqual(self._wake_architect(), ["scan:req"])

    def test_decomposing_expired_recovered(self):
        """验收①：拆解中超时（decompose_expires_at 过期）需求 → 安排恢复。"""
        self._req(1)
        self._task(10, 1)
        self.conn.execute(
            "UPDATE requirements SET decompose_state='decomposing', decompose_owner='architect', "
            "decompose_credential='x', decompose_expires_at='2000-01-01T00:00:00' WHERE id=1")
        self.conn.commit()
        self.assertEqual(scanner.scan_requirements(self.conn), 1)

    def test_decomposing_not_expired_not_recovered(self):
        """占用未超时的需求 → 不扫（拆解进行中）。"""
        self._req(1)
        self._task(10, 1)
        self.conn.execute(
            "UPDATE requirements SET decompose_state='decomposing', decompose_owner='architect', "
            "decompose_credential='x', decompose_expires_at='2999-01-01T00:00:00' WHERE id=1")
        self.conn.commit()
        self.assertEqual(scanner.scan_requirements(self.conn), 0)

    def test_no_candidate_notifies_owner(self):
        """验收②：无 task.create 授权成员 → 写 gate_reason + 唤醒项目负责人。"""
        self._req(1)
        self.conn.execute("DELETE FROM project_permissions")
        self.conn.execute(
            "INSERT INTO settings(key,project,value,updated_at) "
            "VALUES('owner_profile','bot-group','default','2026-01-01T00:00:00')")
        self.conn.commit()
        self.assertEqual(scanner.scan_requirements(self.conn), 1)
        gate = self.conn.execute("SELECT gate_reason FROM requirements WHERE id=1").fetchone()['gate_reason']
        self.assertIn("无 task.create 授权成员", gate)
        self.assertEqual([tuple(r) for r in self.conn.execute(
            "SELECT profile,trigger FROM wake_jobs ORDER BY id")], [('default', 'scan:req')])

    def test_non_open_req_not_woken(self):
        """非 open 状态 → 不唤醒。"""
        self._req(1, status="done")
        self.conn.commit()
        self.assertEqual(scanner.scan_requirements(self.conn), 0)

    def test_mixed_open_and_done(self):
        """open 无任务 + done 无任务 + open 有任务 混合 → 只唤醒 open 无任务那个。"""
        self._req(1, status="open")   # 无 task → 唤醒
        self._req(2, status="done")   # 无 task → 不唤醒（非 open）
        self._req(3, status="open")   # 有 task → 不唤醒（已拆）
        self._task(30, 3)
        self.conn.commit()
        self.assertEqual(scanner.scan_requirements(self.conn), 1)

    def test_dedup_same_project(self):
        """同项目多个 open 无任务需求 → architect 去重（work 按 project 去重），只入队一次。"""
        self._req(1)
        self._req(2)
        self.conn.commit()
        # 第一个入队，第二个同 project 命中 work partial unique index 去重
        n = scanner.scan_requirements(self.conn)
        self.assertGreaterEqual(n, 0)
        # 无论入队次数，队列里 architect 该 project 的 work job 只有一条
        rows = self.conn.execute(
            "SELECT COUNT(*) AS c FROM wake_jobs WHERE profile='architect' AND kind='work' AND project='bot-group'"
        ).fetchone()
        self.assertEqual(rows["c"], 1)

    def test_archived_project_not_woken(self):
        self._req(1)
        self.conn.execute("UPDATE projects SET archived=1 WHERE name='bot-group'")
        self.conn.commit()
        self.assertEqual(scanner.scan_requirements(self.conn), 0)

    def test_disabled_architect_not_woken(self):
        self._req(1)
        self.conn.execute("UPDATE project_members SET active=0 WHERE project='bot-group' AND profile='architect'")
        self.conn.commit()
        self.assertEqual(scanner.scan_requirements(self.conn), 0)

    def test_no_grant_has_no_role_or_profile_fallback(self):
        self._req(1)
        self.conn.execute('DELETE FROM project_permissions')
        self.assertEqual(scanner.scan_requirements(self.conn), 0)
        self.conn.execute("DELETE FROM members WHERE role='architect'")
        self.assertEqual(scanner.scan_requirements(self.conn), 0)

    def test_arbitrary_profiles_and_multiple_grants(self):
        """req#117 #200：多个授权成员 → 只确定性选一个（role_id 升序 + profile 字典序），不再全部唤醒。"""
        self._req(1)
        self.conn.execute('DELETE FROM project_permissions')
        for profile in ['planner-a', 'planner-b']:
            self.conn.execute("INSERT INTO members(profile,display_name,role) VALUES(?,?,'custom')", (profile, profile))
            self.conn.execute("INSERT INTO project_members(project,profile,active) VALUES('bot-group',?,1)", (profile,))
            self.conn.execute("INSERT INTO project_permissions VALUES('bot-group',?,'task.create')", (profile,))
        self.assertEqual(scanner.scan_requirements(self.conn), 1)
        rows = self.conn.execute('SELECT profile,kind,trigger,project FROM wake_jobs ORDER BY profile').fetchall()
        self.assertEqual([tuple(r) for r in rows], [
            ('planner-a', 'work', 'scan:req', 'bot-group'),
        ])

    def test_grants_are_project_scoped_and_read_each_scan(self):
        self.conn.execute("INSERT INTO projects(name,root,created_at) VALUES('other','.','2026-09-24')")
        self.conn.execute("INSERT INTO project_members(project,profile,active) VALUES('other','default',1)")
        self._req(1)
        self._req(2, project='other')
        self.conn.execute('DELETE FROM project_permissions')
        self.conn.execute("INSERT INTO project_permissions VALUES('other','default','task.create')")
        self.assertEqual(scanner.scan_requirements(self.conn), 1)
        self.assertEqual([tuple(r) for r in self.conn.execute('SELECT profile,project FROM wake_jobs')], [('default', 'other')])
        self.conn.execute('DELETE FROM wake_jobs')
        self.conn.execute('DELETE FROM project_permissions')
        self.assertEqual(scanner.scan_requirements(self.conn), 0)
        self.conn.execute("INSERT INTO project_permissions VALUES('bot-group','default','task.create')")
        self.assertEqual(scanner.scan_requirements(self.conn), 1)
        self.assertEqual([tuple(r) for r in self.conn.execute('SELECT profile,project FROM wake_jobs')], [('default', 'bot-group')])

    def test_deleted_member_user_and_other_permissions_do_not_wake(self):
        self._req(1)
        self.conn.execute("DELETE FROM members WHERE profile='architect'")
        self.conn.execute("INSERT INTO members(profile,display_name,role) VALUES('user','用户','user')")
        self.conn.execute("INSERT INTO project_permissions VALUES('bot-group','user','task.create')")
        self.conn.execute("INSERT INTO project_permissions VALUES('bot-group','default','task.update')")
        self.assertEqual(scanner.scan_requirements(self.conn), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
