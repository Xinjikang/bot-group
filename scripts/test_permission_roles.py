"""req#96 task#164：后端权限判定取并集（角色授权 ∪ bot 直接授权）。

验收标准：
- 权限判定取并集：直接授权或角色授权任一命中即可
- 给角色授权权限 = 给该角色下所有 active 成员 bot 授权该权限
- 角色用稳定 ID（改名后授权不断）；删除角色级联清理角色授权行
- save 支持 role:<id> 与 bot profile 混合多选；view 返回 roles + 角色授权

运行：python -B -m unittest scripts.test_permission_roles -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from engine import api, db, project_permissions as pp  # noqa: E402


def _seed(conn, project="proj"):
    conn.execute("INSERT INTO projects(name, root, created_at) VALUES(?,?,?)",
                 (project, f"/tmp/{project}", db.now_iso()))
    for profile, role in [
            ("architect", "architect"), ("reviewer-01", "reviewer"),
            ("be-dev-01", "be-dev"), ("tester-01", "tester"), ("nobody", ""),
    ]:
        conn.execute("INSERT INTO members(profile, display_name, role) VALUES(?,?,?)",
                     (profile, profile, role))
        bot_id = db._ensure_hermes_bot(conn, profile, profile, role)
        conn.execute(
            "INSERT INTO project_members(project, bot_id, profile, active, role) "
            "VALUES(?,?,?,1,?)", (project, bot_id, profile, role))
    conn.commit()
    # 触发角色迁移（种子 + role_id 回填）
    conn.execute("DELETE FROM engine_migrations WHERE name='project_roles_seeded'")
    conn.commit()
    db.init_schema(conn)


def _role_id(conn, name, project="proj"):
    return conn.execute("SELECT id FROM project_roles WHERE project=? AND name=?",
                        (project, name)).fetchone()["id"]


class PermissionRoleCheckTests(unittest.TestCase):
    """check 取并集：直接授权 ∪ 角色授权。"""

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

    def _grant_role(self, role_name, permission, project="proj"):
        rid = _role_id(self.conn, role_name, project)
        self.conn.execute("INSERT INTO project_permissions VALUES(?,?,?)",
                          (project, pp._role_grant(rid), permission))
        self.conn.commit()

    def _grant_bot(self, profile, permission, project="proj"):
        self.conn.execute("INSERT INTO project_permissions VALUES(?,?,?)",
                          (project, profile, permission))
        self.conn.commit()

    def test_role_grant_confers_to_all_active_members(self):
        # architect 角色授 task.update → 该角色下所有 active 成员获得权限
        self._grant_role("architect", "task.update")
        self.assertIsNone(pp.check(self.conn, "proj", "architect", "task.update"))

    def test_role_grant_does_not_leak_to_other_roles(self):
        self._grant_role("architect", "task.update")
        # reviewer 角色成员不得通过 architect 角色的授权获得权限
        self.assertIsNotNone(pp.check(self.conn, "proj", "reviewer-01", "task.update"))

    def test_union_direct_or_role(self):
        # 直接授权命中即放行（无角色授权）
        self._grant_bot("be-dev-01", "task.create")
        self.assertIsNone(pp.check(self.conn, "proj", "be-dev-01", "task.create"))
        # 角色授权命中即放行（无直接授权）
        self._grant_role("tester", "task.complete")
        self.assertIsNone(pp.check(self.conn, "proj", "tester-01", "task.complete"))

    def test_disabled_member_denied_even_with_role_grant(self):
        self._grant_role("architect", "task.update")
        self.conn.execute("UPDATE project_members SET active=0 WHERE project='proj' AND profile='architect'")
        self.conn.commit()
        self.assertIsNotNone(pp.check(self.conn, "proj", "architect", "task.update"))

    def test_unknown_permission_and_missing_member(self):
        self.assertIsNotNone(pp.check(self.conn, "proj", "architect", "unknown"))
        self.assertIsNotNone(pp.check(self.conn, "proj", "ghost", "task.update"))

    def test_granted_profiles_expands_role(self):
        self._grant_role("architect", "task.create")
        self._grant_bot("be-dev-01", "task.create")
        profs = pp.granted_profiles(self.conn, "proj", "task.create")
        self.assertEqual(profs, ["architect", "be-dev-01"])

    def test_granted_profiles_expands_only_active(self):
        self._grant_role("architect", "task.create")
        self.conn.execute("UPDATE project_members SET active=0 WHERE project='proj' AND profile='architect'")
        self.conn.commit()
        self.assertEqual(pp.granted_profiles(self.conn, "proj", "task.create"), [])


class PermissionRoleSaveApiTests(unittest.TestCase):
    """save/view：role:<id> 与 bot 混合多选、revision、删除角色级联清理。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dbfile = str(Path(self.tmp.name) / "group.db")
        self._env = os.environ.get("GROUP_DB")
        os.environ["GROUP_DB"] = self.dbfile
        self.conn = db.get_connection()
        db.init_schema(self.conn)
        _seed(self.conn)
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

    def test_view_returns_roles_and_empty_grants(self):
        data = pp.view("proj")
        self.assertIn("roles", data)
        self.assertEqual({r["name"] for r in data["roles"]},
                         {"architect", "reviewer", "be-dev", "tester"})
        self.assertTrue(all(v == [] for v in data["grants"].values()))

    def test_save_mixed_role_and_bot(self):
        data = pp.view("proj")
        rid = _role_id(self.conn, "architect")
        data["grants"]["task.create"] = [pp._role_grant(rid), "be-dev-01"]
        body = {"revision": data["revision"], "grants": data["grants"]}
        result = pp.save("proj", body, "user")
        self.assertEqual(result["roles"], data["roles"])
        self.assertTrue(all(isinstance(r["member_count"], int) for r in result["roles"]))
        self.assertIn(pp._role_grant(rid), result["grants"]["task.create"])
        self.assertIn("be-dev-01", result["grants"]["task.create"])
        # 角色授权生效 + bot 直接授权生效
        self.assertIsNone(pp.check(self.conn, "proj", "architect", "task.create"))
        self.assertIsNone(pp.check(self.conn, "proj", "be-dev-01", "task.create"))

    def test_permission_role_counts_match_role_list(self):
        rid = _role_id(self.conn, "architect")
        self.conn.execute(
            "UPDATE project_members SET role_id=?,active=0 WHERE project='proj' AND profile='nobody'",
            (rid,))
        self.conn.execute(
            "INSERT INTO project_roles(project,name,created_at) VALUES('proj','empty',?)",
            (db.now_iso(),))
        self.conn.execute(
            "INSERT INTO projects(name,root,created_at) VALUES('other','/tmp/other',?)",
            (db.now_iso(),))
        self.conn.execute(
            "INSERT INTO project_members(project,bot_id,profile,active,role_id) "
            "SELECT 'other',bot_id,profile,1,? FROM project_members "
            "WHERE project='proj' AND profile='architect'", (rid,))
        self.conn.commit()
        response = self.client.get('/projects/proj/permissions')
        self.assertEqual(response.status_code, 200, response.text)
        roles = response.json()['roles']
        counts = {r['name']: r['member_count'] for r in roles}
        self.assertEqual(counts, {'architect': 2, 'reviewer': 1, 'be-dev': 1,
                                  'tester': 1, 'empty': 0})
        self.assertEqual(roles, self.client.get('/projects/proj/roles').json()['roles'])

    def test_save_rejects_unknown_role(self):
        data = pp.view("proj")
        data["grants"]["task.create"] = [pp._role_grant(99999)]
        body = {"revision": data["revision"], "grants": data["grants"]}
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            pp.save("proj", body, "user")

    def test_save_rejects_unknown_bot(self):
        data = pp.view("proj")
        data["grants"]["task.create"] = ["ghost"]
        body = {"revision": data["revision"], "grants": data["grants"]}
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            pp.save("proj", body, "user")

    def test_save_revision_conflict(self):
        data = pp.view("proj")
        data["grants"]["task.create"] = ["be-dev-01"]
        pp.save("proj", {"revision": data["revision"], "grants": data["grants"]}, "user")
        # 用旧 revision 再存 → 409
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            pp.save("proj", {"revision": data["revision"], "grants": data["grants"]}, "user")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_rename_role_keeps_grant(self):
        # 授 architect 角色权限，改名为 senior → 授权不断（id 稳定）
        rid = _role_id(self.conn, "architect")
        self.conn.execute("INSERT INTO project_permissions VALUES(?,?,?)",
                          ("proj", pp._role_grant(rid), "task.update"))
        self.conn.commit()
        self.assertEqual(self.client.patch(f"/projects/proj/roles/{rid}",
                                           json={"name": "senior"}).status_code, 200)
        self.assertIsNone(pp.check(self.conn, "proj", "architect", "task.update"))

    def test_delete_role_cleans_grant(self):
        # 授 architect 角色权限，删除角色 → 授权行级联清理，check 拒绝
        rid = _role_id(self.conn, "architect")
        self.conn.execute("INSERT INTO project_permissions VALUES(?,?,?)",
                          ("proj", pp._role_grant(rid), "task.update"))
        self.conn.commit()
        self.assertEqual(self.client.delete(f"/projects/proj/roles/{rid}").status_code, 200)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM project_permissions WHERE project='proj' AND profile=?",
            (pp._role_grant(rid),)).fetchone())
        # 成员 role_id 置 NULL → check 走不到角色授权 → 拒绝
        self.assertIsNotNone(pp.check(self.conn, "proj", "architect", "task.update"))

    def test_role_change_moves_permissions_and_counts(self):
        old_id = _role_id(self.conn, 'architect')
        new_id = _role_id(self.conn, 'reviewer')
        self.conn.executemany('INSERT INTO project_permissions VALUES(?,?,?)', [
            ('proj', pp._role_grant(old_id), 'task.create'),
            ('proj', pp._role_grant(new_id), 'task.update'),
            ('proj', 'architect', 'req.create')])
        self.conn.commit()
        response = self.client.patch('/projects/proj/members/architect/role',
                                     json={'role': 'reviewer'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNotNone(pp.check(self.conn, 'proj', 'architect', 'task.create'))
        self.assertIsNone(pp.check(self.conn, 'proj', 'architect', 'task.update'))
        self.assertEqual(response.json()['role_id'], new_id)
        self.assertIsNone(pp.check(self.conn, 'proj', 'architect', 'req.create'))
        self.assertEqual(pp.granted_profiles(self.conn, 'proj', 'task.create'), [])
        self.assertEqual(pp.granted_profiles(self.conn, 'proj', 'task.update'),
                         ['architect', 'reviewer-01'])
        roles = self.client.get('/projects/proj/roles').json()['roles']
        counts = {r['name']: r['member_count'] for r in roles}
        self.assertEqual((counts['architect'], counts['reviewer']), (0, 2))
        self.assertEqual(self.client.get('/members', params={'project': 'proj',
                         'role': 'reviewer'}).json()['count'], 2)

    def test_clear_or_unmanaged_role_removes_old_role_permissions(self):
        rid = _role_id(self.conn, 'architect')
        self.conn.execute('INSERT INTO project_permissions VALUES(?,?,?)',
                          ('proj', pp._role_grant(rid), 'task.create'))
        self.conn.execute("INSERT INTO projects(name,root,created_at) VALUES('other','/tmp/other',?)",
                          (db.now_iso(),))
        self.conn.execute("INSERT INTO project_roles(project,name) VALUES('other','foreign')")
        self.conn.commit()
        for role in ('', 'freeform', 'foreign'):
            with self.subTest(role=role):
                self.client.patch('/projects/proj/members/architect/role', json={'role': 'architect'})
                response = self.client.patch('/projects/proj/members/architect/role', json={'role': role})
                self.assertEqual(response.status_code, 200, response.text)
                db.init_schema(self.conn)
                row = self.conn.execute("SELECT role,role_id FROM project_members "
                                        "WHERE project='proj' AND profile='architect'").fetchone()
                self.assertEqual(row['role'], role)
                self.assertIsNone(row['role_id'])
                self.assertIsNotNone(pp.check(self.conn, 'proj', 'architect', 'task.create'))
                self.assertIsNone(response.json()['role_id'])

    def test_recreated_role_requires_explicit_reassignment(self):
        old_id = _role_id(self.conn, 'architect')
        self.assertEqual(self.client.delete(f'/projects/proj/roles/{old_id}').status_code, 200)
        response = self.client.post('/projects/proj/roles', json={'name': 'architect'})
        self.assertEqual(response.status_code, 200, response.text)
        new_id = response.json()['id']
        self.assertNotEqual(new_id, old_id)
        data = self.client.get('/projects/proj/permissions').json()
        data['grants']['task.create'] = [pp._role_grant(new_id)]
        pp.save('proj', {'revision': data['revision'], 'grants': data['grants']}, 'user')
        for _ in range(2):
            db.init_schema(self.conn)
            members = self.client.get('/members', params={'project': 'proj'}).json()['members']
            member = next(m for m in members if m['profile'] == 'architect')
            self.assertEqual(member['role'], 'architect')
            self.assertIsNone(member['role_id'])
            self.assertIsNotNone(pp.check(self.conn, 'proj', 'architect', 'task.create'))
            self.assertEqual(pp.granted_profiles(self.conn, 'proj', 'task.create'), [])
        roles = self.client.get('/projects/proj/roles').json()['roles']
        self.assertEqual(next(r for r in roles if r['id'] == new_id)['member_count'], 0)
        response = self.client.patch('/projects/proj/members/architect/role', json={'role': 'architect'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['role_id'], new_id)
        self.assertIsNone(pp.check(self.conn, 'proj', 'architect', 'task.create'))

    def test_role_assignment_and_audit_roll_back_together(self):
        before = tuple(self.conn.execute("SELECT role,role_id FROM project_members "
                                         "WHERE project='proj' AND profile='architect'").fetchone())
        with patch('engine.project_workspaces.audit', side_effect=RuntimeError('audit failed')):
            with self.assertRaisesRegex(RuntimeError, 'audit failed'):
                self.client.patch('/projects/proj/members/architect/role', json={'role': 'reviewer'})
        after = tuple(self.conn.execute("SELECT role,role_id FROM project_members "
                                        "WHERE project='proj' AND profile='architect'").fetchone())
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
