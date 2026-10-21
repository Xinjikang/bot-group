"""req#96 task#163：项目角色实体化迁移测试（project_roles 表 + role_id 回填 + CRUD）。

覆盖（验收标准五条）：
- project_roles 表存在，UNIQUE(project,name) 幂等
- 6 个历史角色值（architect/reviewer/be-dev/fe-dev/tester/orchestrator）初始化
  成角色记录（按项目一套）
- 成员 role_id 按角色名回填正确
- 角色 CRUD API 可用（创建/改名/删除/列表）
- 改名后成员 role_id 关联不断（id 稳定）；删除后成员 role_id 置 NULL、文本保留

运行：python -B -m unittest scripts.test_role_entity -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from engine import api, db  # noqa: E402


def _seed_legacy(conn, project="proj"):
    """模拟存量库：项目 + 成员 + project_members 带历史 role 文本。"""
    conn.execute("INSERT INTO projects(name, root, created_at) VALUES(?,?,?)",
                 (project, f"/tmp/{project}", db.now_iso()))
    for profile, role in [
            ("architect", "architect"), ("reviewer-01", "reviewer"),
            ("be-dev-01", "be-dev"), ("fe-dev-01", "fe-dev"),
            ("tester-01", "tester"), ("orch", "orchestrator"), ("nobody", ""),
    ]:
        conn.execute("INSERT INTO members(profile, display_name, role) VALUES(?,?,?)",
                     (profile, profile, role))
        bot_id = db._ensure_hermes_bot(conn, profile, profile, role)
        conn.execute(
            "INSERT INTO project_members(project, bot_id, profile, active, role) "
            "VALUES(?,?,?,1,?)", (project, bot_id, profile, role))
    conn.commit()


class RoleEntityMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbfile = str(Path(self.tmp.name) / "group.db")
        self._env = os.environ.get("GROUP_DB")
        os.environ["GROUP_DB"] = self.dbfile
        self.conn = db.get_connection()
        db.init_schema(self.conn)
        _seed_legacy(self.conn)
        # 模拟存量库首次升级：pm 已有历史 role 数据，重置种子标记让迁移段真正执行
        self.conn.execute("DELETE FROM engine_migrations WHERE name='project_roles_seeded'")
        self.conn.commit()
        db.init_schema(self.conn)  # 触发角色迁移（种子 + 回填）

    def tearDown(self):
        self.conn.close()
        if self._env is None:
            os.environ.pop("GROUP_DB", None)
        else:
            os.environ["GROUP_DB"] = self._env
        self.tmp.cleanup()

    def _roles(self, project="proj"):
        return {r["name"]: dict(r) for r in self.conn.execute(
            "SELECT * FROM project_roles WHERE project=?", (project,))}

    def test_roles_table_and_six_historical_roles(self):
        roles = self._roles()
        self.assertEqual(set(roles), {"architect", "reviewer", "be-dev",
                                      "fe-dev", "tester", "orchestrator"})

    def test_members_role_id_backfilled(self):
        rows = {r["profile"]: r["role_id"] for r in self.conn.execute(
            "SELECT profile, role_id FROM project_members WHERE project='proj'")}
        roles = self._roles()
        self.assertEqual(rows["architect"], roles["architect"]["id"])
        self.assertEqual(rows["be-dev-01"], roles["be-dev"]["id"])
        self.assertIsNone(rows["nobody"], "空 role 成员 role_id 应为 NULL")
        for profile, rid in rows.items():
            if profile != "nobody":
                self.assertIsNotNone(rid, f"{profile} 的 role_id 应回填")

    def test_idempotent(self):
        before = {r["id"] for r in self.conn.execute("SELECT id FROM project_roles")}
        db.init_schema(self.conn)
        after = {r["id"] for r in self.conn.execute("SELECT id FROM project_roles")}
        self.assertEqual(before, after)

    def test_roles_project_scoped(self):
        conn = self.conn
        conn.execute("INSERT INTO projects(name, root, created_at) VALUES('other','/tmp/other',?)",
                     (db.now_iso(),))
        conn.commit()
        db.init_schema(self.conn)
        # other 项目没有 proj 的历史角色（除非其 pm 有 role 文本）
        others = {r["name"] for r in self.conn.execute(
            "SELECT name FROM project_roles WHERE project='other'")}
        self.assertEqual(others, set())


class RoleEntityApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbfile = str(Path(self.tmp.name) / "group.db")
        self._env = os.environ.get("GROUP_DB")
        os.environ["GROUP_DB"] = self.dbfile
        self.conn = db.get_connection()
        db.init_schema(self.conn)
        _seed_legacy(self.conn, "proj")
        self.conn.execute("DELETE FROM engine_migrations WHERE name='project_roles_seeded'")
        self.conn.commit()
        db.init_schema(self.conn)
        self.client = TestClient(FastAPI(), client=('127.0.0.1', 4321))
        self.client.app.include_router(api.router_api)
        self.addCleanup(self.client.close)

    def tearDown(self):
        self.conn.close()
        if self._env is None:
            os.environ.pop("GROUP_DB", None)
        else:
            os.environ["GROUP_DB"] = self._env
        self.tmp.cleanup()

    def _get_roles(self):
        r = self.client.get("/projects/proj/roles")
        assert r.status_code == 200, r.text
        return r.json()["roles"]

    def test_list_roles(self):
        roles = self._get_roles()
        self.assertEqual(len(roles), 6)
        self.assertTrue(all("member_count" in r for r in roles))

    def test_create_role(self):
        r = self.client.post("/projects/proj/roles", json={"name": "qa"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["name"], "qa")
        self.assertEqual(self.client.post("/projects/proj/roles", json={"name": "qa"}).status_code, 409)
        self.assertEqual(self.client.post("/projects/proj/roles", json={"name": "  "}).status_code, 400)

    def test_rename_role_keeps_member_link(self):
        roles = {r["name"]: r["id"] for r in self._get_roles()}
        rid = roles["reviewer"]
        before = self.conn.execute(
            "SELECT role, role_id FROM project_members WHERE project='proj' AND role_id=?",
            (rid,)).fetchone()
        self.assertIsNotNone(before)
        r = self.client.patch(f"/projects/proj/roles/{rid}", json={"name": "senior-reviewer"})
        self.assertEqual(r.status_code, 200)
        after = self.conn.execute(
            "SELECT role, role_id FROM project_members WHERE project='proj' AND role_id=?",
            (rid,)).fetchone()
        self.assertEqual(after["role_id"], rid, "改名后 role_id 关联应保持")
        self.assertEqual(after["role"], "senior-reviewer", "role 文本应跟随改名")
        rid2 = roles["architect"]
        self.assertEqual(self.client.patch(f"/projects/proj/roles/{rid2}",
                                           json={"name": "senior-reviewer"}).status_code, 409)

    def test_delete_role_clears_link(self):
        roles = {r["name"]: r["id"] for r in self._get_roles()}
        rid = roles["tester"]
        r = self.client.delete(f"/projects/proj/roles/{rid}")
        self.assertEqual(r.status_code, 200)
        rows = self.conn.execute(
            "SELECT role, role_id FROM project_members WHERE project='proj' AND role='tester'").fetchall()
        self.assertTrue(all(row["role_id"] is None for row in rows))
        self.assertTrue(all(row["role"] == "tester" for row in rows))
        self.assertIsNone(self.conn.execute("SELECT 1 FROM project_roles WHERE id=?", (rid,)).fetchone())

    def test_delete_then_reinit_does_not_resurrect(self):
        # 删除角色后重新初始化，应保持删除状态，并保留
        # 已删角色成员的 role_id（保持 NULL）；role 文本按设计保留。
        roles = {r["name"]: r["id"] for r in self._get_roles()}
        rid = roles["architect"]
        self.assertEqual(self.client.delete(f"/projects/proj/roles/{rid}").status_code, 200)
        # 删除后成员 role 文本保留、role_id 置 NULL
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM project_roles WHERE id=?", (rid,)).fetchone())
        member = self.conn.execute(
            "SELECT role, role_id FROM project_members WHERE project='proj' AND role='architect'").fetchone()
        self.assertIsNotNone(member)
        self.assertEqual(member["role"], "architect")
        self.assertIsNone(member["role_id"])
        # 触发多次 init_schema（API 每次 _conn 都会 init）
        db.init_schema(self.conn)
        db.init_schema(self.conn)
        # 角色不得复活（同 project+name 不得再出现）
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM project_roles WHERE project='proj' AND name='architect'").fetchone())
        # 成员 role_id 保持 NULL，role 文本仍保留
        member2 = self.conn.execute(
            "SELECT role, role_id FROM project_members WHERE project='proj' AND role='architect'").fetchone()
        self.assertIsNone(member2["role_id"])
        self.assertEqual(member2["role"], "architect")
        # 复活后 POST 同名应可重建（不再被残留角色挡 409）
        r = self.client.post("/projects/proj/roles", json={"name": "architect"})
        self.assertEqual(r.status_code, 200)

    def test_member_view_returns_role_id(self):
        r = self.client.get("/members", params={"project": "proj"})
        self.assertEqual(r.status_code, 200)
        m = {x["profile"]: x for x in r.json()["members"]}
        self.assertIsNotNone(m["architect"]["role_id"])


if __name__ == "__main__":
    unittest.main()
