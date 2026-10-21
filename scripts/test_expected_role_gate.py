"""req#112 task#193：task_log 交接增加 expected_role_id 角色一致性校验。

验收标准：
① 声明 expected_role_id=架构师、next_bot 当前 role_id 非架构师时，引擎拒绝流转，
   任务状态与历史保持不变（本次 bug 场景被拦截）
② expected_role_id 与 next_bot 当前 role_id 匹配时正常流转
③ 未声明 expected_role_id 时引擎拒绝
④ 引擎基于数据库最新 role_id 实时核对，不复用旧快照
⑤ 记录边界：Bot 角色认知错（声明了错误的 expected_role_id）不在本校验覆盖范围

运行：python -B -m unittest scripts.test_expected_role_gate -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import db, taskpool  # noqa: E402


def _seed(conn, project="proj"):
    conn.execute("INSERT INTO projects(name, root, created_at) VALUES(?,?,?)",
                 (project, f"/tmp/{project}", db.now_iso()))
    role_ids = {}
    for name in ("architect", "reviewer", "be-dev"):
        role_ids[name] = conn.execute(
            "INSERT INTO project_roles(project, name, created_at) VALUES(?,?,?)",
            (project, name, db.now_iso())).lastrowid
    for profile, role in [("architect", "architect"), ("reviewer-01", "reviewer"),
                          ("be-dev-01", "be-dev")]:
        conn.execute("INSERT INTO members(profile, display_name, role) VALUES(?,?,?)",
                     (profile, profile, role))
    for profile, role in [("architect", "architect"), ("reviewer-01", "reviewer"),
                          ("be-dev-01", "be-dev")]:
        conn.execute("INSERT INTO project_members(project, profile, active, role, role_id) "
                     "VALUES(?,?,1,?,?)", (project, profile, role, role_ids[role]))
    conn.commit()


def _role_id(conn, name, project="proj"):
    return conn.execute("SELECT id FROM project_roles WHERE project=? AND name=?",
                        (project, name)).fetchone()["id"]


def _setup_in_progress_task(conn, task_id=1, project="proj", claimed_by="be-dev-01"):
    conn.execute("INSERT INTO requirements(id, project, title, description, created_at, updated_at) "
                 "VALUES(1, ?, 'req', 'Legacy body', ?, ?)",
                 (project, db.now_iso(), db.now_iso()))
    conn.execute("INSERT INTO tasks(id, project, requirement_id, title, status, claimed_by, created_at) "
                 "VALUES(?, ?, 1, 'task', 'in_progress', ?, ?)",
                 (task_id, project, claimed_by, db.now_iso()))
    conn.commit()


class ExpectedRoleGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbfile = str(Path(self.tmp.name) / "group.db")
        self._env = os.environ.get("GROUP_DB")
        os.environ["GROUP_DB"] = self.dbfile
        self.conn = db.get_connection()
        db.init_schema(self.conn)
        _seed(self.conn)

    def tearDown(self):
        self.conn.close()
        if self._env is None:
            os.environ.pop("GROUP_DB", None)
        else:
            os.environ["GROUP_DB"] = self._env
        self.tmp.cleanup()

    def test_mismatched_role_rejected_and_state_preserved(self):
        # 验收①：声明 architect，next_bot 实际是 reviewer → 拒绝，任务状态与历史不变
        _setup_in_progress_task(self.conn)
        arch_id = _role_id(self.conn, "architect")
        before = tuple(self.conn.execute(
            "SELECT status, next_bot, claimed_by FROM tasks WHERE id=1").fetchone())
        before_nodes = self.conn.execute(
            "SELECT count(*) FROM nodes WHERE task_id=1").fetchone()[0]
        result = taskpool.task_log("proj", "be-dev-01", 1, "dev",
                                   result="Implemented and verified fixture delivery",
                                   next_bot="reviewer-01", expected_role_id=arch_id)
        self.assertIn("error", result)
        after = tuple(self.conn.execute(
            "SELECT status, next_bot, claimed_by FROM tasks WHERE id=1").fetchone())
        after_nodes = self.conn.execute(
            "SELECT count(*) FROM nodes WHERE task_id=1").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(before_nodes, after_nodes)

    def test_matched_role_flows(self):
        # 验收②：声明 reviewer，next_bot 实际是 reviewer → 正常流转
        _setup_in_progress_task(self.conn)
        reviewer_id = _role_id(self.conn, "reviewer")
        result = taskpool.task_log("proj", "be-dev-01", 1, "dev",
                                   result="Implemented and verified fixture delivery",
                                   next_bot="reviewer-01", expected_role_id=reviewer_id)
        self.assertTrue(result["logged"], result)
        self.assertEqual(result["next_bot"], "reviewer-01")

    def test_missing_expected_role_id_rejected(self):
        # 验收③：未声明 expected_role_id → 拒绝
        _setup_in_progress_task(self.conn)
        result = taskpool.task_log("proj", "be-dev-01", 1, "dev", result="Verified fixture delivery", next_bot="reviewer-01")
        self.assertIn("expected_role_id", result["error"])

    def test_uses_latest_role_id_not_stale_snapshot(self):
        # 验收④：实时核对最新 role_id，不复用旧快照
        _setup_in_progress_task(self.conn)
        arch_id = _role_id(self.conn, "architect")
        # reviewer-01 当前 role_id=reviewer，声明 architect → 拒（当前不是 architect）
        denied = taskpool.task_log("proj", "be-dev-01", 1, "dev",
                                   result="Implemented and verified fixture delivery",
                                   next_bot="reviewer-01", expected_role_id=arch_id)
        self.assertIn("error", denied)
        # 实时把 reviewer-01 改为 architect 后，声明 architect → 通过（读最新 role_id）
        self.conn.execute("UPDATE project_members SET role_id=?, role='architect' "
                          "WHERE project='proj' AND profile='reviewer-01'", (arch_id,))
        self.conn.commit()
        allowed = taskpool.task_log("proj", "be-dev-01", 1, "dev",
                                    result="Implemented and verified fixture delivery",
                                    next_bot="reviewer-01", expected_role_id=arch_id)
        self.assertTrue(allowed["logged"], allowed)

    def test_non_integer_expected_role_id_rejected(self):
        _setup_in_progress_task(self.conn)
        result = taskpool.task_log("proj", "be-dev-01", 1, "dev",
                                   result="Implemented and verified fixture delivery",
                                   next_bot="reviewer-01", expected_role_id="abc")
        self.assertIn("expected_role_id", result["error"])


if __name__ == "__main__":
    unittest.main()
