"""Native assistant configuration capability, execution fencing and scan cadence."""
import asyncio
import importlib.util
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from engine import hud_tools, project_hud as hud
from scripts import test_project_hud


class NativeToolTests(unittest.TestCase):
    setUp = test_project_hud.HudTests.setUp
    join = test_project_hud.HudTests.join
    session = test_project_hud.HudTests.session
    infer = test_project_hud.HudTests.infer

    def running(self, project='p'):
        item = self.session(project)
        item = hud.answer(item['id'], {'revision': item['revision'], 'text': '按要求更新项目配置', 'question_id': None})
        with hud.connection() as conn:
            conn.execute("UPDATE hud_sessions SET status='running' WHERE id=?", (item['id'],))
        return hud.view(item['id'])

    def request(self, item, changes, *, token=None, version=None, sid=None):
        with hud.connection() as conn:
            expected = hud_tools.version(hud_tools.configuration(conn, item))
        return self.client.post('/hud/' + (sid or item['id']) + '/tool-config',
            headers={'Authorization': 'Bearer ' + (token or hud_tools.issue(item))},
            json={'changes': changes, 'expected_version': version or expected})

    def test_project_update_is_saved_audited_and_does_not_invalidate_running_turn(self):
        item = self.running()
        response = self.request(item, {'description': '已按用户要求更新'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['saved'])
        current = hud.view(item['id'])
        self.assertEqual(current['revision'], item['revision'])
        self.assertEqual(current['status'], 'running')
        self.assertEqual(current['state']['config_update']['changes']['description'], '已按用户要求更新')
        self.assertEqual(self.conn.execute("SELECT description FROM projects WHERE name='p'").fetchone()[0], '已按用户要求更新')
        self.assertEqual(self.conn.execute("SELECT count(*) FROM project_config_events WHERE action='project.update'").fetchone()[0], 1)

    def test_draft_updates_preserve_wizard_state(self):
        item = self.session()
        item = hud.update(item['id'], {'revision': item['revision'], 'draft': {'template': 'testing', 'wizard': {'roles': ['fixture']}}})
        item = hud.answer(item['id'], {'revision': item['revision'], 'text': '更新当前创建草稿', 'question_id': None})
        with hud.connection() as conn:
            conn.execute("UPDATE hud_sessions SET status='running' WHERE id=?", (item['id'],))
        item = hud.view(item['id'])
        response = self.request(item, {'display_name': '新的项目名称', 'description': '新的项目范围'})
        self.assertEqual(response.status_code, 200, response.text)
        current = hud.view(item['id'])
        self.assertEqual(current['state']['draft']['wizard']['roles'], ['fixture'])
        self.assertEqual(current['state']['draft']['display_name'], '新的项目名称')
        self.assertTrue(current['state']['config_update']['draft'])

    def test_version_conflict_returns_current_values(self):
        item = self.running()
        response = self.request(item, {'description': '修改'}, version='0' * 64)
        self.assertEqual(response.status_code, 409)
        self.assertIn('config_version', response.json()['detail'])

    def test_background_round_cannot_reuse_previous_user_authorization(self):
        item = self.running()
        self.assertIn('user_request', item['state'])
        with hud.connection() as conn:
            conn.execute("UPDATE hud_sessions SET status='queued' WHERE id=?", (item['id'],))
        completed = self.infer(item, {'text': '已完成当前要求'})
        self.assertNotIn('user_request', completed['state'])
        with hud.connection() as conn:
            conn.execute("UPDATE hud_sessions SET status='running' WHERE id=?", (item['id'],))
        background = hud.view(item['id'])
        self.assertEqual(self.request(background, {'description': '后台自动修改'}).status_code, 409)

    def test_failed_user_execution_retains_request_for_explicit_retry(self):
        item = self.running()
        with hud.connection() as conn:
            conn.execute("UPDATE hud_sessions SET status='queued' WHERE id=?", (item['id'],))
        with patch('engine.hud_runtime.respond', side_effect=RuntimeError('fixture failure')):
            hud.run_one(item['id'])
        failed = hud.view(item['id'])
        self.assertEqual(failed['status'], 'error')
        self.assertEqual(failed['state']['user_request'], item['state']['user_request'])
        self.assertEqual(self.request(failed, {'description': '错误状态写入'}).status_code, 409)

    def test_forged_cross_session_and_expired_execution_credentials_are_rejected(self):
        item = self.running()
        self.assertEqual(self.request(item, {'description': '修改'}, token='hud.invalid.signature').status_code, 403)
        other = hud.create('q')
        self.assertEqual(self.request(item, {'description': '修改'}, sid=other['id']).status_code, 403)
        with hud.connection() as conn:
            conn.execute("UPDATE hud_sessions SET status='idle' WHERE id=?", (item['id'],))
        self.assertEqual(self.request(item, {'description': '修改'}).status_code, 409)

    def test_changed_revision_archived_project_and_unsupported_fields_are_rejected(self):
        item = self.running()
        self.assertEqual(self.request(item, {'template': 'software'}).status_code, 400)
        self.assertEqual(self.request(item, {'unknown': 'value'}).status_code, 400)
        self.assertEqual(self.request(item, {'display_name': ''}).status_code, 400)
        with hud.connection() as conn:
            conn.execute('UPDATE hud_sessions SET revision=revision+1 WHERE id=?', (item['id'],))
        self.assertEqual(self.request(item, {'description': '修改'}).status_code, 409)
        item = hud.view(item['id'])
        self.conn.execute("UPDATE projects SET archived=1 WHERE name='p'")
        self.conn.commit()
        self.assertEqual(self.request(item, {'description': '修改'}).status_code, 409)

    def test_hourly_background_check_skips_filesystem_within_interval(self):
        item = self.infer(self.session('p'), {'text': '检查完成'})
        with hud.connection() as conn:
            conn.execute('UPDATE hud_sessions SET checked=? WHERE id=?', (time.time()-1800, item['id']))
        with patch.object(hud, 'document_context', side_effect=AssertionError('scan must be skipped')):
            hud.scan()
        self.assertEqual(hud.view(item['id'])['status'], 'idle')
        with hud.connection() as conn:
            conn.execute('UPDATE hud_sessions SET checked=? WHERE id=?', (time.time()-3700, item['id']))
        with patch.object(hud, 'document_context', wraps=hud.document_context) as documents:
            hud.scan()
        self.assertEqual(documents.call_count, 1)
        self.assertFalse(documents.call_args.kwargs['include_contents'])

    def test_context_uses_native_file_locations_and_optimistic_config_version(self):
        item = self.running()
        with hud.connection() as conn:
            context = hud.context(conn, item)
        self.assertEqual(len(context['config_version']), 64)
        self.assertEqual(context['materials']['documents'], {})
        self.assertTrue(Path(context['installation_root']).is_dir())

    def test_mcp_advertises_exactly_one_tool_and_no_resource_or_prompt_bridges(self):
        spec = importlib.util.spec_from_file_location('hud_config_mcp', Path(__file__).resolve().parents[1] / 'mcp/hud_server.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tools = asyncio.run(module.list_tools())
        self.assertEqual([tool.name for tool in tools], ['update_project_config'])
        capabilities = module.mcp.create_initialization_options().capabilities
        self.assertIsNone(capabilities.resources)
        self.assertIsNone(capabilities.prompts)


if __name__ == '__main__':
    unittest.main()
