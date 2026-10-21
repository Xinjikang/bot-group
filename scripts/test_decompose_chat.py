"""req#117 task#199：CHAT 开放 task_create + 草稿补全/发布权限核对 + 执行凭证隔离。

taskpool 层可验证的验收：
⑤ CHAT 入口拆解带显式 credential（执行凭证）区分同一 bot 的 CHAT/WORK 不同执行——
  首次占用记录执行凭证；同成员不同执行并发收到明确占用结果；同执行续用放行。
⑥ 超时接管后旧凭据 task_update 修改草稿被拒（decompose_credential_stale）。

运行：python -B -m unittest scripts.test_decompose_chat -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import db, taskpool  # noqa: E402
from scripts.unified_test_support import confirmed_requirement_fields, isolated_database  # noqa: E402
from scripts.test_task_brief import BRIEF  # noqa: E402


class DecomposeChatTests(unittest.TestCase):
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

    def _task_count(self, rid, project='alpha'):
        conn = db.get_connection()
        try:
            return conn.execute('SELECT COUNT(*) FROM tasks WHERE requirement_id=? AND project=?',
                                (rid, project)).fetchone()[0]
        finally:
            conn.close()

    # ---- 验收⑤：显式执行凭证首次占用 + 同成员不同执行隔离 ----
    def test_explicit_credential_first_occupy_and_same_bot_isolation(self):
        rid = self.req()
        e_chat = 'exec:architect-bot-group-chat-10085:1'
        e_work = 'exec:architect-bot-group-work:2'
        # CHAT 执行带 E_chat 首次占用（记录执行凭证）
        first = self.create(rid, role='architect', credential=e_chat)
        self.assertEqual(first['credential'], e_chat)
        self.assertEqual(first['decompose_state'], 'decomposing')
        # WORK 执行（同 bot 不同执行）带 E_work 承接 → 同成员不同执行 → 明确占用结果
        second = self.create(rid, role='architect', credential=e_work,
                             tasks=[dict(title='T2', next_bot='dev', **BRIEF)])
        self.assertTrue(second.get('occupied'))
        self.assertEqual(second['code'], 'decompose_occupied')
        self.assertEqual(second['owner'], 'architect')
        # 同执行续用（带 E_chat）→ 放行
        third = self.create(rid, role='architect', credential=e_chat,
                            tasks=[dict(title='T3', next_bot='dev', **BRIEF)])
        self.assertNotIn('occupied', third)
        self.assertEqual(third['credential'], e_chat)

    # ---- 验收⑥：超时接管后旧凭据 task_update 修改草稿被拒 ----
    def test_stale_credential_task_update_rejected(self):
        rid = self.req()
        first = self.create(rid, role='architect', credential='E_chat',
                            tasks=[dict(title='T1', next_bot='dev', draft=True, **BRIEF)])
        c = first['credential']
        tid = first['task_ids'][0]
        # 模拟超时，default 带 E_work 接管
        conn = db.get_connection()
        try:
            conn.execute("UPDATE requirements SET decompose_expires_at='2000-01-01T00:00:00' WHERE id=?", (rid,))
            conn.commit()
        finally:
            conn.close()
        takeover = self.create(rid, role='default', credential='E_work',
                               tasks=[dict(title='T2', next_bot='dev', **BRIEF)])
        self.assertNotEqual(takeover['credential'], c)
        # architect 带旧 E_chat 修改草稿 → 拒绝（decompose_credential_stale）
        stale = taskpool.task_update('alpha', 'architect', tid, credential='E_chat',
                                     description='updated', publish=False)
        self.assertEqual(stale.get('code'), 'decompose_credential_stale')
        # default 带 E_work 修改草稿 → 放行
        ok = taskpool.task_update('alpha', 'default', tid, credential='E_work',
                                  description='updated2', publish=False)
        self.assertNotIn('error', ok)

    # ---- 退修（architect node 925）：_execution_credential 只用 session，不掺 wake_started_at ----
    def test_execution_credential_ignores_wake_started_at(self):
        """同一 session 不同 wake_started_at 跨唤醒续拆 → 同一凭证 exec:{session}，不误判不同执行。"""
        import json
        import importlib.util
        import os as _os
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('project_mcp_server', str(root / 'mcp/server.py'))
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
        prev_profile = _os.environ.get('HERMES_PROFILE')
        prev_ctx = _os.environ.get('XDG_GROUP_ENGINE_EXECUTION')
        try:
            _os.environ['HERMES_PROFILE'] = 'be-dev-01'
            base = {'profile': 'be-dev-01', 'session': 'chat-10085', 'project': 'bot-group',
                    'thread_id': 1, 'wake_started_at': 1}
            _os.environ['XDG_GROUP_ENGINE_EXECUTION'] = json.dumps(base)
            c1 = server._execution_credential()
            _os.environ['XDG_GROUP_ENGINE_EXECUTION'] = json.dumps(dict(base, wake_started_at=2))
            c2 = server._execution_credential()
        finally:
            if prev_profile is None:
                _os.environ.pop('HERMES_PROFILE', None)
            else:
                _os.environ['HERMES_PROFILE'] = prev_profile
            if prev_ctx is None:
                _os.environ.pop('XDG_GROUP_ENGINE_EXECUTION', None)
            else:
                _os.environ['XDG_GROUP_ENGINE_EXECUTION'] = prev_ctx
        self.assertEqual(c1, 'exec:chat-10085')
        self.assertEqual(c1, c2)


if __name__ == '__main__':
    unittest.main()
