"""Disabled-member session management, using isolated databases and mock Hermes."""
import sqlite3
from pathlib import Path
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from engine import api, auth, session_adapter, wake, project_workspaces
from scripts import test_abnormal_exit as fixture


class MemberSessionResetTests(unittest.TestCase):
    def setUp(self):
        fixture.AbnormalExitTests.setUp(self)
        path = patch.object(session_adapter, 'profile_state_db', side_effect=lambda p: Path(self.tmp.name) / (p + '.db'))
        path.start()
        self.addCleanup(path.stop)
        password = patch.object(auth, '_password', return_value='')
        password.start()
        self.addCleanup(password.stop)
        self.conn.execute("UPDATE project_members SET active=0 WHERE project='p' AND profile='dev-1'")
        self.conn.commit()
        app = FastAPI()
        app.include_router(api.router_api)
        self.client = TestClient(app, client=('127.0.0.1', 4321))
        self.addCleanup(self.client.close)

    def bind(self, project, kind='work', tid=0):
        # req#86 task#148：绑定主体切 bot_id。夹具按运行时同口径写入——先反推
        # hermes Bot（与 session_adapter 写点一致），老形态裸插 profile 行会因
        # bot_id 键读不到而误报 work_session_exists=False。
        from engine import db as engine_db
        bot_id = engine_db._ensure_hermes_bot(self.conn, 'dev-1')
        self.conn.execute('INSERT INTO session_bindings(project,bot_id,profile,kind,thread_id,session_id,title,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                          (project, bot_id, 'dev-1', kind, tid, project + kind, wake.session_name('dev-1', kind, tid, project), 'now'))
        self.conn.commit()

    def reset(self, project='p', headers=None):
        return self.client.post(f'/projects/{project}/members/dev-1/reset-session', headers=headers or {})

    def test_disabled_member_can_reset_existing_work_session_only(self):
        self.bind('p')
        self.bind('q')
        self.bind('p', 'chat', 10)
        view = project_workspaces.view('p', profile='dev-1')['members'][0]
        self.assertFalse(view['active'])
        self.assertTrue(view['work_session_exists'])
        with patch.object(wake, 'session_busy', return_value=False), \
             patch.object(session_adapter, 'create_session', return_value='new-p-work') as create:
            result = self.reset()
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()['ok'])
        self.assertEqual(create.call_args.args[0], 'dev-1')
        self.assertTrue(create.call_args.args[1].startswith('dev-1-p-work#'))
        rows = {(r['project'],r['kind']):r['session_id'] for r in self.conn.execute('SELECT * FROM session_bindings')}
        self.assertEqual(rows, {('p','work'):'new-p-work',('q','work'):'qwork',('p','chat'):'pchat'})
        self.assertEqual(self.conn.execute("SELECT active FROM project_members WHERE project='p' AND profile='dev-1'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT status FROM commands ORDER BY id DESC LIMIT 1').fetchone()[0], 'ok')

    def test_no_work_session_does_not_create_or_enable(self):
        self.bind('q')
        self.bind('p', 'chat', 10)
        self.assertFalse(project_workspaces.view('p', profile='dev-1')['members'][0]['work_session_exists'])
        with patch.object(session_adapter, 'create_session') as create:
            result = self.reset()
        self.assertFalse(result.json()['ok'])
        self.assertIn('暂无', result.json()['detail'])
        create.assert_not_called()
        self.assertFalse((Path(self.tmp.name) / 'dev-1.db').exists())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM session_bindings WHERE project='p' AND kind='work'").fetchone()[0], 0)

    def test_legacy_title_is_read_without_importing_hermes(self):
        with sqlite3.connect(Path(self.tmp.name) / 'dev-1.db') as legacy:
            legacy.execute('CREATE TABLE sessions(id TEXT,title TEXT,started_at REAL)')
            legacy.execute("INSERT INTO sessions VALUES('legacy-p','dev-1-p-work',1)")
        legacy.close()
        self.assertTrue(project_workspaces.view('p', profile='dev-1')['members'][0]['work_session_exists'])
        with patch.object(wake, 'session_busy', return_value=False), \
             patch.object(session_adapter, 'create_session', return_value='new-p'):
            self.assertTrue(self.reset().json()['ok'])
        self.assertEqual(self.conn.execute('SELECT old_session_id FROM session_switch_ops').fetchone()[0], 'legacy-p')

    def test_busy_and_bot_identity_are_rejected(self):
        self.bind('p')
        with patch.object(wake, 'session_busy', return_value=True), \
             patch.object(session_adapter, 'create_session') as create:
            self.assertFalse(self.reset().json()['ok'])
            self.assertEqual(self.reset(headers={'X-Group-Profile':'dev-1', 'X-Group-Key': auth.bot_token('dev-1')}).status_code, 403)
        create.assert_not_called()

    def test_missing_project_and_unknown_member(self):
        self.assertEqual(self.reset('missing').status_code, 404)
        self.assertEqual(self.client.post('/projects/p/members/missing/reset-session').status_code, 400)


if __name__ == '__main__':
    unittest.main()
