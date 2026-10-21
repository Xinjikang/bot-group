"""req#118 task#203：完成后旧凭据迟到写入拒（漏洞3）。

taskpool 层可验证的验收：
① B（接管者）完成拆解后，A 旧执行凭据的迟到创建、修改、完成操作持续被拒。
② 合法完成请求幂等重放安全（#201 已覆盖 already_done）。
③ 完成后新授权修订正常（credential 不在 stale 列表放行）。

运行：python -B -m unittest scripts.test_decompose_stale_after_done -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import db, taskpool  # noqa: E402
from scripts.unified_test_support import confirmed_requirement_fields, isolated_database  # noqa: E402
from scripts.test_task_brief import BRIEF  # noqa: E402


class DecomposeStaleAfterDoneTests(unittest.TestCase):
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
            return conn.execute('SELECT decompose_state FROM requirements WHERE id=?', (rid,)).fetchone()[0]
        finally:
            conn.close()

    def _expire(self, rid):
        conn = db.get_connection()
        try:
            conn.execute("UPDATE requirements SET decompose_expires_at='2000-01-01T00:00:00' WHERE id=?", (rid,))
            conn.commit()
        finally:
            conn.close()

    def finalize(self, rid):
        conn = db.get_connection()
        try:
            drafts = [row[0] for row in conn.execute(
                "SELECT id FROM tasks WHERE requirement_id=? AND briefing_status='draft'", (rid,))]
        finally:
            conn.close()
        for tid in drafts:
            published = taskpool.task_update('alpha', 'default', tid, credential='E_B', publish=True)
            self.assertNotIn('error', published)
        result = taskpool.task_create('alpha', 'default', rid, [], credential='E_B', complete=True)
        self.assertEqual(result.get('decompose_state'), 'done', result)
        return result

    # ---- 验收①：B 完成后 A 旧凭据迟到修改/创建持续被拒 ----
    def test_stale_credential_update_rejected_after_done(self):
        rid = self.req()
        first = self.create(rid, role='architect', credential='E_A',
                            tasks=[dict(title='T1', next_bot='dev', draft=True, **BRIEF)])
        tid = first['task_ids'][0]
        self._expire(rid)
        self.create(rid, role='default', credential='E_B',
                    tasks=[dict(title='T2', next_bot='dev', **BRIEF)])
        # B 独立提交拆解完成
        final = self.finalize(rid)
        self.assertEqual(self._state(rid), 'done')
        # A 旧凭据迟到修改草稿 → 拒（decompose_credential_stale，落在 stale 列表）
        stale = taskpool.task_update('alpha', 'architect', tid, credential='E_A', description='hijack')
        self.assertEqual(stale.get('code'), 'decompose_credential_stale')

    def test_stale_credential_create_rejected_after_done(self):
        rid = self.req()
        self.create(rid, role='architect', credential='E_A',
                    tasks=[dict(title='T1', next_bot='dev', draft=True, **BRIEF)])
        self._expire(rid)
        self.create(rid, role='default', credential='E_B',
                    tasks=[dict(title='T2', next_bot='dev', **BRIEF)])
        self.finalize(rid)
        # A 旧凭据迟到创建 → 拒（decompose_done，需求已拆完）
        stale = taskpool.task_create('alpha', 'architect', rid,
                                     [dict(title='T3', next_bot='dev', **BRIEF)], credential='E_A')
        self.assertEqual(stale.get('code'), 'decompose_done')

    # ---- 验收① 补：旧凭据迟到完成提交（空 tasks 独立 complete）被拒 ----
    def test_stale_credential_complete_rejected_after_done(self):
        rid = self.req()
        self.create(rid, role='architect', credential='E_A',
                    tasks=[dict(title='T1', next_bot='dev', draft=True, **BRIEF)])
        self._expire(rid)
        self.create(rid, role='default', credential='E_B',
                    tasks=[dict(title='T2', next_bot='dev', **BRIEF)])
        self.finalize(rid)
        # A 旧凭据迟到完成提交（空 tasks 独立 complete）→ 拒，不得伪装完成
        stale = taskpool.task_create('alpha', 'architect', rid, [], credential='E_A', complete=True)
        self.assertEqual(stale.get('code'), 'decompose_credential_stale')

    # ---- 验收① 补：旧凭据迟到完成提交（非空 payload 幂等重放 complete）被拒 ----
    def test_stale_credential_idempotent_complete_rejected_after_done(self):
        rid = self.req()
        self.create(rid, role='architect', credential='E_A',
                    tasks=[dict(title='T1', next_bot='dev', draft=True, **BRIEF)], idempotency_key='k1')
        self._expire(rid)
        self.create(rid, role='default', credential='E_B',
                    tasks=[dict(title='T2', next_bot='dev', **BRIEF)])
        self.finalize(rid)
        # A 旧凭据幂等重放 complete（同 k1 + 同 payload）→ 拒，不得伪装完成
        stale = taskpool.task_create('alpha', 'architect', rid,
                                     [dict(title='T1', next_bot='dev', draft=True, **BRIEF)],
                                     credential='E_A', idempotency_key='k1', complete=True)
        self.assertEqual(stale.get('code'), 'decompose_credential_stale')

    # ---- 验收②：合法完成者（B）重复 complete 幂等安全 already_done ----
    def test_valid_completer_already_done_retry(self):
        rid = self.req()
        self.create(rid, role='architect', credential='E_A',
                    tasks=[dict(title='T1', next_bot='dev', draft=True, **BRIEF)])
        self._expire(rid)
        self.create(rid, role='default', credential='E_B',
                    tasks=[dict(title='T2', next_bot='dev', **BRIEF)])
        self.finalize(rid)
        # 合法完成者（B，凭据不在 stale）重复 complete → already_done 幂等安全
        again = taskpool.task_create('alpha', 'default', rid, [], credential='E_B', complete=True)
        self.assertTrue(again.get('already_done'))

    # ---- 验收③：完成后新授权修订正常（credential 不在 stale 列表放行） ----
    def test_valid_new_credential_revision_after_done(self):
        rid = self.req()
        first = self.create(rid, role='architect', credential='E_A',
                            tasks=[dict(title='T1', next_bot='dev', draft=True, **BRIEF)])
        tid = first['task_ids'][0]
        self._expire(rid)
        self.create(rid, role='default', credential='E_B',
                    tasks=[dict(title='T2', next_bot='dev', **BRIEF)])
        self.finalize(rid)
        # 完成后新凭据（不在 stale）修订 → 正常
        ok = taskpool.task_update('alpha', 'architect', tid, credential='E_C', description='revised')
        self.assertNotIn('error', ok)

    # ---- 验收5 补：done 状态空凭据（无执行上下文）task_update 拒 ----
    def test_empty_credential_update_rejected_after_done(self):
        rid = self.req()
        first = self.create(rid, role='architect', credential='E_A',
                            tasks=[dict(title='T1', next_bot='dev', draft=True, **BRIEF)])
        tid = first['task_ids'][0]
        self._expire(rid)
        self.create(rid, role='default', credential='E_B',
                    tasks=[dict(title='T2', next_bot='dev', **BRIEF)])
        taskpool.task_create('alpha', 'default', rid, [], credential='E_B', complete=True)
        # done 后空凭据（不传 credential）修订 → 拒（与 decomposing 空凭据拒一致，满足验收5）
        stale = taskpool.task_update('alpha', 'architect', tid, description='hijack')
        self.assertEqual(stale.get('code'), 'decompose_credential_stale')


if __name__ == '__main__':
    unittest.main()
