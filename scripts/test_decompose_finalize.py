"""req#118 task#201：独立完成提交及幂等语义（漏洞1）。

验收：
① 分批建任务补依赖后 complete=true 且 tasks 空独立提交成功，需求标 done，任务数不变。
② 依赖未补齐独立提交明确失败。
③ done 后重复 complete 幂等安全不报错。
④ 原幂等键附 complete=true 不静默遗漏完成动作。

运行：python -B -m unittest scripts.test_decompose_finalize -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import db, taskpool  # noqa: E402
from scripts.unified_test_support import confirmed_requirement_fields, isolated_database  # noqa: E402
from scripts.test_task_brief import BRIEF  # noqa: E402


class DecomposeFinalizeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = isolated_database()
        self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)

    def req(self, project='alpha'):
        return taskpool.req_create(project, 'default', 'Decompose',
                                   **confirmed_requirement_fields(project))['requirement_id']

    def create(self, rid, role='architect', tasks=None, project='alpha', **kw):
        return taskpool.task_create(project, role, rid,
                                    tasks or [{**BRIEF, 'title': 'Task', 'next_bot': 'dev'}], **kw)

    def _state(self, rid):
        conn = db.get_connection()
        try:
            return conn.execute('SELECT decompose_state FROM requirements WHERE id=?', (rid,)).fetchone()[0]
        finally:
            conn.close()

    def _task_count(self, rid, project='alpha'):
        conn = db.get_connection()
        try:
            return conn.execute('SELECT COUNT(*) FROM tasks WHERE requirement_id=? AND project=?',
                                (rid, project)).fetchone()[0]
        finally:
            conn.close()

    # ---- 验收①：分批建任务补依赖后 complete=true 且 tasks 空独立提交成功 ----
    def test_independent_finalize_with_empty_tasks(self):
        rid = self.req()
        first = self.create(rid, tasks=[{**BRIEF, 'title': 'T1', 'next_bot': 'dev'}])
        t1 = first['task_ids'][0]
        self.create(rid, tasks=[{**BRIEF, 'title': 'T2', 'next_bot': 'dev', 'prerequisites': [t1]}])
        self.assertEqual(self._task_count(rid), 2)
        # 零新增任务，独立提交拆解完成
        final = taskpool.task_create('alpha', 'architect', rid, [], complete=True)
        self.assertNotIn('error', final)
        self.assertEqual(self._state(rid), 'done')
        self.assertEqual(self._task_count(rid), 2)  # 任务数不变

    # ---- 验收②：依赖未补齐独立提交明确失败 ----
    def test_independent_finalize_missing_dependency_fails(self):
        rid = self.req()
        first = self.create(rid, tasks=[{**BRIEF, 'title': 'T1', 'next_bot': 'dev'}])
        t1 = first['task_ids'][0]
        second = self.create(rid, tasks=[{**BRIEF, 'title': 'T2', 'next_bot': 'dev', 'prerequisites': [t1]}])
        t2 = second['task_ids'][0]
        # 直接篡改 T2 依赖指向不存在的任务（绕过建任务时的 validate，模拟依赖未补齐）
        conn = db.get_connection()
        try:
            conn.execute("UPDATE tasks SET prerequisites=? WHERE id=?", ('[99999]', t2))
            conn.commit()
        finally:
            conn.close()
        final = taskpool.task_create('alpha', 'architect', rid, [], complete=True)
        self.assertEqual(final.get('code'), 'decompose_incomplete')
        self.assertEqual(self._state(rid), 'decomposing')  # 未标 done

    # ---- 验收③：done 后重复 complete 幂等安全不报错 ----
    def test_repeat_complete_after_done_is_idempotent(self):
        rid = self.req()
        self.create(rid, tasks=[{**BRIEF, 'title': 'T1', 'next_bot': 'dev'}])
        first = taskpool.task_create('alpha', 'architect', rid, [], complete=True)
        self.assertEqual(self._state(rid), 'done')
        second = taskpool.task_create('alpha', 'architect', rid, [], complete=True)
        self.assertNotIn('error', second)
        self.assertTrue(second.get('already_done'))
        self.assertEqual(self._task_count(rid), 1)

    # ---- 验收④：原幂等键附 complete=true 不静默遗漏完成动作 ----
    def test_idempotency_key_with_independent_complete_not_silently_dropped(self):
        rid = self.req()
        self.create(rid, tasks=[{**BRIEF, 'title': 'T1', 'next_bot': 'dev'}], idempotency_key='k1')
        self.assertEqual(self._state(rid), 'decomposing')
        # 原幂等键附 complete=true + tasks 空 → 独立 finalize（跳过幂等），不静默遗漏完成动作
        final = taskpool.task_create('alpha', 'architect', rid, [], idempotency_key='k1', complete=True)
        self.assertNotIn('error', final)
        self.assertEqual(self._state(rid), 'done')
        self.assertEqual(self._task_count(rid), 1)

    # ---- 验收④补强（node 944 退修）：非空 payload 幂等命中 + complete=true 不静默遗漏完成 ----
    def test_idempotency_nonempty_payload_complete_retry_finalizes(self):
        rid = self.req()
        payload = [{**BRIEF, 'title': 'T1', 'next_bot': 'dev'}]
        first = self.create(rid, tasks=payload, idempotency_key='k1')
        cred = first['credential']
        self.assertEqual(self._state(rid), 'decomposing')
        # 同 payload/k1/凭据仅改 complete=true 重试 → 幂等命中但 finalize，不静默遗漏 complete 动作
        retry = taskpool.task_create('alpha', 'architect', rid, payload,
                                     credential=cred, idempotency_key='k1', complete=True)
        self.assertNotIn('error', retry)
        self.assertTrue(retry.get('replayed'))
        self.assertEqual(self._state(rid), 'done')
        self.assertEqual(self._task_count(rid), 1)  # 任务数不变、不重复建任务


if __name__ == '__main__':
    unittest.main()
