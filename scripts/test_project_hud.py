"""HUD lifecycle, isolation, optimistic concurrency and template context."""
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import auth, project_hud as hud
from scripts import test_project_roles


class HudTests(unittest.TestCase):
    setUp = test_project_roles.ProjectRoleTests.setUp
    join = test_project_roles.ProjectRoleTests.join

    def session(self, project=None):
        self.join()
        bot_id = self.conn.execute("SELECT id FROM bots WHERE runtime='codex'").fetchone()[0]
        item = self.client.post('/hud', json={'project': project}).json()
        response = self.client.patch('/hud/' + item['id'], json={'revision': item['revision'], 'bot_id': bot_id})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def infer(self, item, response):
        with patch('engine.hud_runtime.respond', return_value=response):
            hud.run_one(item['id'])
        return hud.view(item['id'])

    def test_options_custom_answer_timeout_and_group_isolation(self):
        item = self.session('p')
        result = self.infer(item, {'text': '请确认资料来源', 'question': {
            'title': '需求放在哪里？', 'reason': '用于判断项目输入是否齐备', 'options': ['已有文档', '从现有资料整理']}})
        self.assertEqual(result['status'], 'waiting')
        question = result['state']['question']
        self.assertEqual(self.client.post('/hud/'+item['id']+'/answer', json={
            'revision':result['revision'], 'text':'自定义目录', 'question_id':'old'}).status_code, 409)
        with hud.connection() as conn:
            conn.execute('UPDATE hud_sessions SET activity=? WHERE id=?', (time.time()-3600,item['id']))
        reply=self.client.post('/hud/'+item['id']+'/answer',json={
            'revision':result['revision'],'text':'已有文档\n补充：资料放在需求集目录', 'question_id':question['id']})
        self.assertEqual(reply.status_code,200,reply.text)
        fresh=reply.json()
        self.assertGreater(fresh['generation'],result['generation'])
        self.assertIn('需求集',fresh['state']['decisions'][0]['answer'])
        for table in ('messages','threads','session_bindings','wake_jobs'):
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0,table)

    def test_template_context_and_proposal_confirmation(self):
        item=self.session()
        item=hud.update(item['id'],{'revision':item['revision'],'draft':{
            'template':'algorithm','root':str(self.root),'display_name':'算法项目'},'stage':'purpose'})
        with hud.connection() as conn:
            payload=hud.context(conn,item)
        self.assertEqual(payload['template']['id'],'algorithm')
        self.assertIn('contents',payload['template'])
        result=self.infer(item,{'text':'建议登记资料位置','proposal':{'changes':{'description':'模型评测'},'facts':[
            {'label':'原始资料','value':'specifications，用户提供的需求文档目录'}]}})
        self.assertNotIn('description',result['state']['draft'])
        saved=self.client.post('/hud/'+item['id']+'/accept',json={'revision':result['revision'],
            'proposal_id':result['state']['proposal']['id']})
        self.assertEqual(saved.status_code,200,saved.text)
        self.assertEqual(saved.json()['state']['draft']['description'],'模型评测')
        self.assertEqual(saved.json()['state']['facts'][0]['label'],'原始资料')

    def test_stale_inference_cannot_overwrite_new_draft(self):
        item=self.session()
        def delayed(*args):
            hud.update(item['id'],{'revision':item['revision'],'draft':{'description':'用户的新输入'}})
            return {'text':'旧结果'}
        with patch('engine.hud_runtime.respond',side_effect=delayed):
            hud.run_one(item['id'])
        saved=hud.view(item['id'])
        self.assertEqual(saved['state']['draft']['description'],'用户的新输入')
        self.assertEqual(saved['status'],'queued')
        self.assertFalse(saved['entries'])

    def test_bind_receipt_idempotent_and_shared_facts(self):
        item=self.session()
        with hud.connection() as conn:
            self.assertFalse(item['project'])
            state={'facts':[{'label':'需求目录','value':'requirements-v2'}]}
            conn.execute('UPDATE hud_sessions SET state=? WHERE id=?',(json.dumps(state),item['id']))
            conn.execute("INSERT INTO project_setup_requests(id,digest,status,payload,result,updated_at) VALUES('receipt','','ready','{}',?,?)",
                         (json.dumps({'project':'p'}),time.time()))
        result=hud.bind(item['id'],'receipt')
        self.assertEqual(result['project'],'p')
        self.assertEqual(hud.bind(item['id'],'receipt')['id'],item['id'])
        facts=self.conn.execute("SELECT value FROM settings WHERE project='p' AND key='maintenance_facts'").fetchone()[0]
        self.assertIn('requirements-v2',facts)

    def test_error_retry_authorization_and_project_scope(self):
        item=self.session('p')
        with patch('engine.hud_runtime.respond',side_effect=RuntimeError('模型不可用')):
            hud.run_one(item['id'])
        failed=hud.view(item['id'])
        self.assertEqual(failed['status'],'error')
        self.assertEqual(hud.create('p')['id'],item['id'])
        self.assertNotEqual(hud.create('q')['id'],item['id'])
        member=self.conn.execute('SELECT profile FROM members WHERE bot_id=?',(item['bot_id'],)).fetchone()[0]
        response=self.client.get('/hud/'+item['id'],headers={'X-Group-Profile':member,'X-Group-Key':auth.bot_token(member)})
        self.assertEqual(response.status_code,403)
        retried=self.client.post('/hud/'+item['id']+'/retry',json={'revision':failed['revision']})
        self.assertEqual(retried.json()['status'],'queued')

    def test_project_changes_require_confirmation_and_conflict_check(self):
        item=self.session('p')
        result=self.infer(item,{'text':'修改名称','proposal':{'changes':{'display_name':'新名称'},'facts':[]}})
        self.conn.execute("UPDATE projects SET display_name='另一个修改' WHERE name='p'");self.conn.commit()
        response=self.client.post('/hud/'+item['id']+'/accept',json={'revision':result['revision'],
            'proposal_id':result['state']['proposal']['id']})
        self.assertEqual(response.status_code,409)

    def test_document_boundary_and_malformed_response(self):
        (self.root/'README.md').write_text('项目说明',encoding='utf-8')
        (self.root/'password.txt').write_text('secret',encoding='utf-8')
        (self.root/'.env').write_text('token',encoding='utf-8')
        content=hud.document_context(str(self.root))
        self.assertEqual(content['files'],['README.md'])
        with self.assertRaises(ValueError):
            hud.validate_response({'text':'执行操作','proposal':{'changes':{'command':'run'},'facts':[]}})

    def test_free_text_after_timeout_is_in_new_context(self):
        item=self.session()
        with hud.connection() as conn:
            conn.execute('UPDATE hud_sessions SET activity=? WHERE id=?',(time.time()-3600,item['id']))
        saved=hud.answer(item['id'],{'revision':item['revision'],'text':'我们调整为原型项目','question_id':None})
        with hud.connection() as conn:
            context=hud.context(conn,saved)
        self.assertEqual(context['history'][-1]['text'],'我们调整为原型项目')

    def test_stage_change_defers_question_and_manual_edit_invalidates_proposal(self):
        item=self.session()
        item=self.infer(item,{'text':'确认目标','question':{'title':'预期产出是什么？','reason':'决定准备哪些资料','options':['原型','应用']},
                              'proposal':{'changes':{'description':'旧目标'},'facts':[]}})
        saved=hud.update(item['id'],{'revision':item['revision'],'stage':'collaboration','draft':{'description':'新的目标'}})
        self.assertNotIn('question',saved['state'])
        self.assertNotIn('proposal',saved['state'])
        self.assertEqual(len(saved['state']['deferred_questions']),1)

    def test_default_bot_does_not_enable_other_projects(self):
        item=self.session()
        hud.update(item['id'],{'revision':item['revision'],'bot_id':item['bot_id'],'make_default':True})
        other=hud.create('q')
        self.assertEqual(other['bot_id'],item['bot_id'])
        self.assertEqual(other['status'],'paused')
        self.assertNotIn(other['id'],hud.scan())
        with hud.connection() as conn:
            self.assertFalse(conn.execute("SELECT 1 FROM hud_sessions WHERE project='p'").fetchone())

    def test_paused_draft_stays_paused_after_creation(self):
        item=hud.create()
        with hud.connection() as conn:
            conn.execute("INSERT INTO project_setup_requests(id,digest,status,payload,result,updated_at) VALUES('paused-receipt','','ready','{}',?,?)",
                         (json.dumps({'project':'p'}),time.time()))
        self.assertEqual(hud.bind(item['id'],'paused-receipt')['status'],'paused')

    def test_codex_adapter_uses_disposable_tool_restricted_configuration(self):
        from engine import hud_runtime
        import tomlkit
        home=self.root/'runtime-source';home.mkdir()
        (home/'config.toml').write_text('model="test-model"\n[mcp_servers.production]\ncommand="unused"\n',encoding='utf-8')
        (home/'auth.json').write_text('{"fixture":true}',encoding='utf-8')
        captured=[]
        def fake_run(command,prompt,env,cwd):
            captured.append(cwd)
            settings=tomlkit.parse((Path(cwd)/'config.toml').read_text(encoding='utf-8'))
            self.assertNotIn('mcp_servers',settings)
            self.assertFalse(settings['features']['shell_tool'])
            self.assertEqual(env['CODEX_HOME'],cwd)
            self.assertNotIn('GROUP_DB',env)
            Path(command[command.index('--output-last-message')+1]).write_text('{"text":"ready"}',encoding='utf-8')
            return ''
        bot={'runtime':'codex','config_root':str(home),'profile':'default'}
        with patch('engine.hud_runtime.config.data_dir',return_value=self.root),patch('engine.wake._codex_bin',return_value='codex'),patch('engine.hud_runtime._run',side_effect=fake_run):
            self.assertEqual(hud_runtime.respond(bot,{}),{'text':'ready'})
        self.assertFalse(Path(captured[0]).exists())
        self.assertIn('mcp_servers.production',(home/'config.toml').read_text(encoding='utf-8'))


if __name__=='__main__':
    unittest.main()
