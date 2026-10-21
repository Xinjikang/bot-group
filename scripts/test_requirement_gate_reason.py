"""req#111 task#192：scanner 落库门禁原因 gate_reason。

验收四条：
① requirements 表新增 gate_reason 字段（幂等 ALTER TABLE 加列，禁删库重建）
② scanner 扫到未过门禁需求时把 requirement_gate.check 返回原因写回 gate_reason、过门禁清空
③ GET /requirements 列表与 /requirements/{id} 详情均能拿到 gate_reason
④ 已过门禁需求 gate_reason 为空不误标
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine import api, db, scanner  # noqa: E402


def _insert_req(conn, rid, *, title=None, description='', background='', scope='',
                approach='', acceptance='', undecided='', source_ref='',
                status='open'):
    conn.execute(
        "INSERT INTO requirements(id, project, title, description, background_goal, scope, "
        "approach_basis, acceptance, undecided, source_ref, status, created_at, updated_at) "
        "VALUES(?, 'bot-group', ?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-09-22T10:00:00', '2026-09-22T10:00:00')",
        (rid, title or f'req{rid}', description, background, scope, approach,
         acceptance, undecided, source_ref, status),
    )


class RequirementGateReasonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t192_")
        self.dbfile = os.path.join(self.tmp, "group.db")
        self._patcher = patch.object(db, "db_path", return_value=self.dbfile)
        self._patcher.start()
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        db.init_schema(c)
        self.conn = c
        c.execute("INSERT INTO projects(name, root, created_at) VALUES('bot-group', '.', '2026-09-22T10:00:00')")
        c.execute("INSERT INTO members(profile, display_name, role) VALUES('architect', '苏瑾', 'architect')")
        c.execute("INSERT INTO project_members(project,profile,active) SELECT 'bot-group',profile,1 FROM members")
        c.execute("INSERT INTO project_permissions VALUES('bot-group','architect','task.create')")
        # 用户消息 id=1，供 source_ref 引用（sender_type=user、未撤回且属于本项目）。
        c.execute("INSERT INTO messages(project,ts,sender,sender_type,content,mentions) "
                  "VALUES('bot-group',?,'user','user','fixture source','[]')", (db.now_iso(),))
        c.commit()

    def tearDown(self):
        self._patcher.stop()
        self.conn.close()

    def _gate_reason(self, rid):
        return self.conn.execute("SELECT gate_reason FROM requirements WHERE id=?", (rid,)).fetchone()[0]

    # ---- 验收①：gate_reason 字段（幂等加列，禁删库重建） ----
    def test_gate_reason_column_present(self):
        cols = {r['name'] for r in self.conn.execute('PRAGMA table_info(requirements)')}
        self.assertIn('gate_reason', cols)

    def test_gate_reason_migration_idempotent_keeps_data(self):
        _insert_req(self.conn, 1, undecided='待澄清', background='bg', scope='sc',
                    approach='ap', acceptance='ac')
        self.conn.execute("UPDATE requirements SET gate_reason='stale reason' WHERE id=1")
        self.conn.commit()
        db.init_schema(self.conn)  # 重复 init_schema（幂等，不删库重建）
        self.assertEqual(self._gate_reason(1), 'stale reason')  # 数据保留
        self.assertEqual(self.conn.execute("SELECT count(*) FROM requirements").fetchone()[0], 1)

    # ---- 验收②④：scanner 写回/清空 gate_reason ----
    def test_scanner_writes_gate_reason_when_blocked(self):
        # 六字段不全空（不走 legacy），undecided='待澄清' 非「无」→ 未过门禁
        _insert_req(self.conn, 1, undecided='待澄清', background='bg', scope='sc',
                    approach='ap', acceptance='ac', source_ref='thread_id=0; message_id=1')
        self.conn.commit()
        self.assertEqual(scanner.scan_requirements(self.conn), 0)  # 未过门禁不唤醒
        self.assertIn('未决', self._gate_reason(1))  # 写回 check 返回原因

    def test_scanner_clears_gate_reason_when_passed(self):
        # legacy：description 有值 + 六字段空 → 过门禁
        _insert_req(self.conn, 1, description='Legacy body')
        self.conn.execute("UPDATE requirements SET gate_reason='stale' WHERE id=1")
        self.conn.commit()
        self.assertGreaterEqual(scanner.scan_requirements(self.conn), 1)  # 过门禁唤醒
        self.assertEqual(self._gate_reason(1), '')  # 清空（验收④不误标）

    def test_gate_reason_cleared_after_requirement_fixed(self):
        # 先未过门禁（写回），再补全字段过门禁（清空）
        _insert_req(self.conn, 1, undecided='待澄清', background='bg', scope='sc',
                    approach='ap', acceptance='ac', source_ref='thread_id=0; message_id=1')
        self.conn.commit()
        scanner.scan_requirements(self.conn)
        self.assertNotEqual(self._gate_reason(1), '')
        self.conn.execute("UPDATE requirements SET undecided='无' WHERE id=1")
        self.conn.commit()
        scanner.scan_requirements(self.conn)
        self.assertEqual(self._gate_reason(1), '')

    # ---- 验收③：API 列表/详情均能拿到 gate_reason ----
    def test_api_list_and_detail_return_gate_reason(self):
        _insert_req(self.conn, 1, undecided='待澄清', background='bg', scope='sc',
                    approach='ap', acceptance='ac', source_ref='thread_id=0; message_id=1')
        self.conn.commit()
        scanner.scan_requirements(self.conn)
        reason = self._gate_reason(1)
        self.assertTrue(reason)

        app = FastAPI()
        app.include_router(api.router_api)
        client = TestClient(app, client=('127.0.0.1', 4321))
        try:
            # 详情 /requirements/{id}
            detail = client.get('/requirements/1', params={'project': 'bot-group'}).json()
            self.assertEqual(detail['requirement']['gate_reason'], reason)
            # 列表 /tasks 里的 requirements
            listing = client.get('/tasks').json()
            reqs = {r['id']: r for r in listing['requirements']}
            self.assertEqual(reqs[1]['gate_reason'], reason)
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
