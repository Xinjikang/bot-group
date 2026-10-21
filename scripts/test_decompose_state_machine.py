"""req#117 task#198：需求拆解状态机（原子占用/凭据/幂等/完成提交/存量迁移）。

验收六条：
① requirements 表新增 decompose_state/decompose_owner/decompose_credential/decompose_expires_at 列
  （幂等 ALTER TABLE 加列，禁删库重建）
② task_create 入口原子取得需求拆解权，并发同一需求最多一个有效执行、其他调用收到明确占用结果
③ 写任务校验执行凭据，超时接管后旧凭据创建/修改/完成提交均被拒
④ 同一幂等标识重复提交返回同一结果不增重复任务、同标识不同内容明确报冲突
⑤ 分批创建草稿+补依赖，显式提交拆解完成后需求标已拆完停止自动拆解
⑥ 存量迁移：已有任务需求→已拆完、无任务需求→待拆仍进兜底

运行：python -B -m unittest scripts.test_decompose_state_machine -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import db, taskpool  # noqa: E402
from scripts.unified_test_support import confirmed_requirement_fields, isolated_database  # noqa: E402
from scripts.test_task_brief import BRIEF  # noqa: E402


class DecomposeStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.fixture = isolated_database()
        self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)

    def req(self, project='alpha'):
        return taskpool.req_create(project, 'default', 'Decompose',
                                   **confirmed_requirement_fields(project))['requirement_id']

    def create(self, rid, role='architect', tasks=None, project='alpha', **kw):
        return taskpool.task_create(project, role, rid,
                                    tasks or [dict(title='Task', next_bot='dev', **BRIEF)], **kw)

    def _state(self, rid):
        conn = db.get_connection()
        try:
            return conn.execute(
                'SELECT decompose_state, decompose_owner, decompose_credential, decompose_expires_at '
                'FROM requirements WHERE id=?', (rid,)).fetchone()
        finally:
            conn.close()

    def _task_count(self, rid, project='alpha'):
        conn = db.get_connection()
        try:
            return conn.execute('SELECT COUNT(*) FROM tasks WHERE requirement_id=? AND project=?',
                                (rid, project)).fetchone()[0]
        finally:
            conn.close()

    # ---- 验收①：4 列 + 幂等迁移（禁删库重建） ----
    def test_decompose_columns_present_and_init_schema_idempotent(self):
        conn = db.get_connection()
        try:
            cols = {r['name'] for r in conn.execute('PRAGMA table_info(requirements)')}
            self.assertTrue({'decompose_state', 'decompose_owner', 'decompose_credential',
                             'decompose_expires_at'} <= cols)
            before = conn.execute('SELECT COUNT(*) FROM requirements').fetchone()[0]
            db.init_schema(conn)  # 重复初始化幂等，不删库重建
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM requirements').fetchone()[0], before)
            self.assertEqual({r['name'] for r in conn.execute('PRAGMA table_info(requirements)')}, cols)
        finally:
            conn.close()

    # ---- 验收②：原子占用，并发同一需求最多一个有效执行 ----
    def test_concurrent_decompose_only_one_owner(self):
        rid = self.req()
        first = self.create(rid, role='architect')
        self.assertEqual(first['decompose_state'], 'decomposing')
        self.assertTrue(first.get('credential'))
        # default 并发拆解同一需求（有 task.create 权限但非占用者）→ 明确占用结果
        second = self.create(rid, role='default', tasks=[dict(title='Other', next_bot='dev', **BRIEF)])
        self.assertTrue(second.get('occupied'))
        self.assertEqual(second['code'], 'decompose_occupied')
        self.assertEqual(second['owner'], 'architect')
        self.assertEqual(self._task_count(rid), 1)  # 只落一个有效执行的任务

    # ---- 验收③：超时接管后旧凭据被拒 ----
    def test_stale_credential_rejected_after_takeover(self):
        rid = self.req()
        first = self.create(rid, role='architect')
        c1 = first['credential']
        conn = db.get_connection()
        try:
            conn.execute("UPDATE requirements SET decompose_expires_at='2000-01-01T00:00:00' WHERE id=?", (rid,))
            conn.commit()
        finally:
            conn.close()
        # default 超时接管（重新占用，新凭据）
        takeover = self.create(rid, role='default', tasks=[dict(title='T2', next_bot='dev', **BRIEF)])
        c2 = takeover['credential']
        self.assertNotEqual(c1, c2)
        # architect 用旧凭据 c1 写任务 → 拒绝（迟到写入）
        stale = self.create(rid, role='architect', credential=c1,
                            tasks=[dict(title='T3', next_bot='dev', **BRIEF)])
        self.assertEqual(stale.get('code'), 'decompose_credential_stale')
        self.assertEqual(self._task_count(rid), 2)

    # ---- 验收④：幂等标识 ----
    def test_idempotency_replay_and_conflict(self):
        rid = self.req()
        first = self.create(rid, idempotency_key='k1', tasks=[dict(title='A', next_bot='dev', **BRIEF)])
        replay = self.create(rid, idempotency_key='k1', tasks=[dict(title='A', next_bot='dev', **BRIEF)])
        self.assertTrue(replay.get('replayed'))
        self.assertEqual(replay['task_ids'], first['task_ids'])
        conflict = self.create(rid, idempotency_key='k1', tasks=[dict(title='B', next_bot='dev', **BRIEF)])
        self.assertEqual(conflict.get('code'), 'idempotency_conflict')
        self.assertEqual(self._task_count(rid), 1)  # 不增重复任务

    # ---- 验收⑤：分批草稿 + 补依赖 + 显式完成提交 ----
    def test_batch_create_and_complete_finalize(self):
        rid = self.req()
        first = self.create(rid, tasks=[dict(title='T1', next_bot='dev', draft=True, **BRIEF)])
        c = first['credential']
        self.assertEqual(first['decompose_state'], 'decomposing')
        published = taskpool.task_update('alpha', 'architect', first['task_ids'][0],
                                         credential=c, publish=True)
        self.assertNotIn('error', published)
        # 第二批补依赖（指向第一批任务）+ 显式完成提交
        second = self.create(rid, credential=c, complete=True,
                             tasks=[{**BRIEF, 'title': 'T2', 'next_bot': 'dev',
                                     'prerequisites': [first['task_ids'][0]]}])
        self.assertEqual(second['decompose_state'], 'done')
        # 完成后需求标已拆完，再拆解被拒
        third = self.create(rid, tasks=[dict(title='T3', next_bot='dev', **BRIEF)])
        self.assertEqual(third.get('code'), 'decompose_done')

    def test_finalize_rejected_when_no_task(self):
        rid = self.req()
        conn = db.get_connection()
        try:
            # 直接校验：无任务时 _finalize_decompose 拒绝（防御性完整性校验）
            result = taskpool._finalize_decompose(conn, 'alpha', rid)
            self.assertEqual(result.get('code'), 'decompose_incomplete')
        finally:
            conn.close()


class LegacyMigrationTests(unittest.TestCase):
    """验收⑥：存量迁移——已有任务需求→已拆完、无任务需求→待拆。"""

    def test_legacy_migration_marks_existing_tasks_done(self):
        with tempfile.TemporaryDirectory(prefix='legacy_mig_') as d:
            dbfile = os.path.join(d, 'group.db')
            c = sqlite3.connect(dbfile)
            c.row_factory = sqlite3.Row
            # 模拟老库：requirements 无 decompose_state 列（req#117 之前的形态）
            c.execute("""CREATE TABLE requirements (
                id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL DEFAULT 'xjk',
                title TEXT NOT NULL, version TEXT DEFAULT '', description TEXT DEFAULT '',
                background_goal TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT '',
                approach_basis TEXT NOT NULL DEFAULT '', acceptance TEXT NOT NULL DEFAULT '',
                undecided TEXT NOT NULL DEFAULT '', source_ref TEXT NOT NULL DEFAULT '',
                gate_reason TEXT NOT NULL DEFAULT '', priority INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'open', project_root TEXT DEFAULT '',
                archived INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            c.execute("""CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL DEFAULT 'xjk',
                requirement_id INTEGER NOT NULL REFERENCES requirements(id), title TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
                next_bot TEXT DEFAULT '', claimed_by TEXT DEFAULT '', block_kind TEXT DEFAULT '',
                created_at TEXT NOT NULL, claimed_at TEXT DEFAULT '', done_at TEXT DEFAULT '',
                description TEXT NOT NULL DEFAULT '', design_basis TEXT NOT NULL DEFAULT '',
                acceptance_criteria TEXT NOT NULL DEFAULT '', prerequisites TEXT NOT NULL DEFAULT '[]',
                briefing_status TEXT NOT NULL DEFAULT 'needs_details')""")
            ts = db.now_iso()
            c.execute("INSERT INTO requirements(id, project, title, created_at, updated_at) "
                      "VALUES(1,'bot-group','has tasks',?,?)", (ts, ts))
            c.execute("INSERT INTO requirements(id, project, title, created_at, updated_at) "
                      "VALUES(2,'bot-group','no tasks',?,?)", (ts, ts))
            c.execute("INSERT INTO tasks(project, requirement_id, title, created_at) "
                      "VALUES('bot-group',1,'t1',?)", (ts,))
            c.commit()
            with patch.object(db, 'db_path', return_value=dbfile):
                db.init_schema(c)
            self.assertEqual(c.execute('SELECT decompose_state FROM requirements WHERE id=1').fetchone()[0], 'done')
            self.assertEqual(c.execute('SELECT decompose_state FROM requirements WHERE id=2').fetchone()[0], 'pending')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)  # 任务保留
            c.close()


if __name__ == '__main__':
    unittest.main()
