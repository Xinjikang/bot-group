"""req#118 task#202：话题代理凭据传递与空凭据拒（漏洞2）。

taskqueue_proxy 链路可验证的验收：
① A 创建草稿超时 B 接管后 A 经 group_task_update（proxy）修订被拒，草稿内容与发布状态保持原值。
② B 经 proxy 有效写入成功。
③ credential 空（无执行上下文）task_update 被拒防绕过。

运行：python -B -m unittest scripts.test_group_task_update_credential -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import db, taskpool, taskqueue_proxy  # noqa: E402
from scripts.unified_test_support import confirmed_requirement_fields, isolated_database  # noqa: E402
from scripts.test_task_brief import BRIEF  # noqa: E402


class GroupTaskUpdateCredentialTests(unittest.TestCase):
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

    def _thread(self, project, task_id):
        conn = db.get_connection()
        try:
            cur = conn.execute(
                "INSERT INTO threads (project, title, creator, task_id, members, status, created_at, last_active_at) "
                "VALUES (?, ?, 'architect', ?, '[]', 'open', ?, ?)",
                (project, 't', task_id, db.now_iso(), db.now_iso()))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()

    def _proxy(self, thread_id, task_id, profile, fields, credential=None):
        conn = db.get_connection()
        try:
            return taskqueue_proxy.task_update(conn, profile, thread_id, task_id, fields,
                                               credential=credential)
        finally:
            conn.close()

    def _task(self, tid):
        conn = db.get_connection()
        try:
            return conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        finally:
            conn.close()

    def _expire(self, rid):
        conn = db.get_connection()
        try:
            conn.execute("UPDATE requirements SET decompose_expires_at='2000-01-01T00:00:00' WHERE id=?", (rid,))
            conn.commit()
        finally:
            conn.close()

    # ---- 验收③：credential 空（无执行上下文）task_update 被拒防绕过 ----
    def test_empty_credential_rejected_when_decomposing(self):
        rid = self.req()
        first = self.create(rid, role='architect', credential='E_A',
                            tasks=[dict(title='T1', next_bot='dev', draft=True, **BRIEF)])
        tid = first['task_ids'][0]
        # decomposing + credential=None → 拒（空凭据绕过）
        stale = taskpool.task_update('alpha', 'architect', tid, description='updated', publish=False)
        self.assertEqual(stale.get('code'), 'decompose_credential_stale')

    # ---- 验收①：超时接管后 A 经 proxy（旧凭据）修订被拒，草稿保持原值 ----
    def test_stale_credential_via_proxy_rejected(self):
        rid = self.req()
        first = self.create(rid, role='architect', credential='E_A',
                            tasks=[dict(title='T1', next_bot='dev', draft=True, **BRIEF)])
        tid = first['task_ids'][0]
        self._expire(rid)
        takeover = self.create(rid, role='default', credential='E_B',
                               tasks=[dict(title='T2', next_bot='dev', **BRIEF)])
        self.assertNotEqual(takeover['credential'], 'E_A')
        tid_thread = self._thread('alpha', tid)
        # A 经 proxy（旧 E_A）修订 → 拒
        try:
            self._proxy(tid_thread, tid, 'architect', {'description': 'hijacked'}, credential='E_A')
            self.fail('expected ProxyError for stale credential')
        except taskqueue_proxy.ProxyError as e:
            self.assertEqual(e.status, 400)
        # 草稿内容与发布状态保持原值
        row = self._task(tid)
        self.assertEqual(row['briefing_status'], 'draft')
        self.assertNotEqual(row['description'], 'hijacked')

    # ---- 验收②：B 经 proxy（有效凭据）写入成功 ----
    def test_valid_credential_via_proxy_succeeds(self):
        rid = self.req()
        first = self.create(rid, role='architect', credential='E_A',
                            tasks=[dict(title='T1', next_bot='dev', draft=True, **BRIEF)])
        tid = first['task_ids'][0]
        self._expire(rid)
        self.create(rid, role='default', credential='E_B',
                    tasks=[dict(title='T2', next_bot='dev', **BRIEF)])
        tid_thread = self._thread('alpha', tid)
        result = self._proxy(tid_thread, tid, 'default', {'description': 'valid'}, credential='E_B')
        self.assertTrue(result.get('updated'))
        self.assertEqual(self._task(tid)['description'], 'valid')


if __name__ == '__main__':
    unittest.main()
