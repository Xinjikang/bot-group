"""HUD lifecycle, isolation, optimistic concurrency and template context."""
import json
import time
import unittest
from pathlib import Path
import os
import subprocess
import sys
from unittest.mock import patch

from engine import auth, project_hud as hud
from scripts import test_project_roles


class HudTests(unittest.TestCase):
    def setUp(self):
        test_project_roles.ProjectRoleTests.setUp(self)
        probes = patch('engine.hud_inspection.run', return_value=(None, 'fixture unavailable'))
        probes.start()
        self.addCleanup(probes.stop)
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

    def test_notification_classification_and_index(self):
        item = self.session('p')
        result = self.infer(item, {'text': '测试脚本由执行成员收尾。',
                                   'notification': {'kind': 'pending'}})
        self.assertEqual(result['state']['notification']['kind'], 'none')
        self.assertEqual(result['status'], 'idle')
        self.assertEqual(result['entries'][-1]['content']['text'], '测试脚本由执行成员收尾。')
        listed = next(s for s in self.client.get('/hud').json()['sessions'] if s['id'] == item['id'])
        self.assertEqual(listed['notification'], result['state']['notification'])
        queued = hud.answer(item['id'], {'revision': result['revision'], 'text': '检查访问权限'})
        self.assertNotIn('notification', queued['state'])
        result = self.infer(queued, {'text': '需要管理员提供必要的访问权限。',
            'notification': {'kind': 'user_action', 'action': '请授权资料访问，当前输入读取受阻。'}})
        self.assertEqual(result['state']['notification']['kind'], 'user_action')
        self.assertEqual(hud.view(item['id'])['state']['notification'], result['state']['notification'])

    def test_legacy_question_is_quiet_and_invalid_action_rejected(self):
        item = self.session()
        result = self.infer(item, {'text': '有一个可选建议', 'question': {
            'title': '要整理资料吗？', 'reason': '可选整理'}})
        self.assertEqual(result['state']['notification']['kind'], 'pending')
        for notification in ({'kind': 'user_action'}, {'kind': 'user_action', 'action': ' '},
                             {'kind': 'urgent'}, {'kind': 'pending', 'action': 42}, True):
            with self.subTest(notification=notification), self.assertRaises(ValueError):
                hud.validate_response({'text': '检查结果', 'notification': notification})

    def test_unlimited_cards_independent_answers_and_confirmations(self):
        item = self.session('p')
        cards = [{'text': f'待办 {i}', 'question': {'title': f'资料 {i} 在哪里？', 'reason': '需要登记输入'}} for i in range(13)]
        cards[11]['notification'] = {'kind': 'user_action', 'action': '请提供外部资料位置，当前读取受阻。'}
        cards += [{'text': '更新简介', 'proposal': {'changes': {'description': '卡片简介'}, 'facts': []}},
                  {'text': '更新名称', 'proposal': {'changes': {'display_name': '卡片名称'}, 'facts': []}}]
        result = self.infer(item, {'text': '已整理全部待办。', 'cards': cards})
        self.assertEqual(len(result['state']['cards']), 15)
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(result['state']['notification']['kind'], 'user_action')
        self.assertEqual(result['state']['notification']['id'], result['state']['cards'][11]['question']['id'])
        with hud.connection() as conn:
            self.assertEqual(len(hud.context(conn, result)['pending_cards']), 15)
        question = result['state']['cards'][9]['question']
        queued = hud.answer(item['id'], {'revision': result['revision'], 'question_id': question['id'], 'text': '资料目录'})
        self.assertEqual(len(queued['state']['cards']), 14)
        result = self.infer(queued, {'text': '已记录该项回答。'})
        proposals = [card['proposal'] for card in result['state']['cards'] if card.get('proposal')]
        queued = hud.accept(item['id'], {'revision': result['revision'], 'proposal_id': proposals[0]['id']})
        result = self.infer(queued, {'text': '简介已保存。'})
        queued = hud.accept(item['id'], {'revision': result['revision'], 'proposal_id': proposals[1]['id']})
        result = self.infer(queued, {'text': '名称已保存。', 'cards': [
            {'text': '同一待办', 'question': {'title': '资料 0 在哪里？', 'reason': '需要登记输入'}}]})
        self.assertEqual(len(result['state']['cards']), 12)
        actual = self.conn.execute("SELECT description,display_name FROM projects WHERE name='p'").fetchone()
        self.assertEqual(tuple(actual), ('卡片简介', '卡片名称'))
        index = next(s for s in self.client.get('/hud').json()['sessions'] if s['id'] == item['id'])
        self.assertEqual(len(index['cards']), 12)

    def test_cards_validate_permissions_and_can_be_dismissed(self):
        for cards in ('invalid', [{'text': '缺少内容'}], [{'text': '嵌套', 'cards': []}]):
            with self.assertRaises(ValueError):
                hud.validate_response({'text': '检查结果', 'cards': cards})
        item = self.session('p')
        result = self.infer(item, {'text': '越权提案', 'cards': [
            {'text': '换目录', 'proposal': {'changes': {'root': '/fixture/alternate'}, 'facts': []}}]})
        self.assertEqual(result['status'], 'error')
        self.assertFalse(result['state'].get('cards'))
        queued = hud.answer(item['id'], {'revision': result['revision'], 'text': '继续检查'})
        result = self.infer(queued, {'text': '两个建议', 'cards': [
            {'text': f'备注 {i}', 'proposal': {'changes': {}, 'facts': [{'label': str(i), 'value': '资料位置'}]}}
            for i in range(2)]})
        first = result['state']['cards'][0]['proposal']['id']
        queued = hud.answer(item['id'], {'revision': result['revision'], 'text': '稍后处理', 'dismiss_proposal': first})
        self.assertEqual(len(queued['state']['cards']), 1)

    def test_draft_change_invalidates_only_proposal_cards(self):
        item = self.session()
        result = self.infer(item, {'text': '创建准备', 'cards': [
            {'text': '资料待办', 'question': {'title': '资料位置？', 'reason': '登记输入'}},
            {'text': '名称建议', 'proposal': {'changes': {'display_name': '建议名称'}, 'facts': []}}]})
        result['activity'] = time.time() - result['idle_seconds'] - 1
        generation = result['generation']
        hud.advance_generation(result)
        self.assertEqual(result['generation'], generation)
        result = hud.update(item['id'], {'revision': result['revision'], 'draft': {'display_name': '手动填写'}})
        self.assertEqual(len(result['state']['cards']), 1)
        self.assertTrue(result['state']['cards'][0]['question'])

    def test_question_without_options_accepts_free_text(self):
        item=self.session()
        result=self.infer(item,{'text':'先确定项目目标','question':{
            'title':'这个项目要做成什么？','reason':'目标决定输入与产出'}})
        question=result['state']['question']
        self.assertEqual(question['options'],[])
        response=self.client.post('/hud/'+item['id']+'/answer',json={
            'revision':result['revision'],'question_id':question['id'],'text':'整理研究资料并形成决策报告'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['state']['decisions'][-1]['answer'],'整理研究资料并形成决策报告')

    def test_template_context_and_proposal_confirmation(self):
        item=self.session()
        item=hud.update(item['id'],{'revision':item['revision'],'draft':{
            'template':'algorithm','root':str(self.root),'display_name':'算法项目'},'stage':'purpose'})
        with hud.connection() as conn:
            payload=hud.context(conn,item)
        self.assertEqual(payload['template']['id'],'algorithm')
        self.assertTrue(Path(payload['template_root']).is_dir())
        self.assertNotIn('contents',payload['template'])
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

    def test_proposal_current_values_are_server_owned_and_duplicates_removed(self):
        item = self.session('p')
        self.conn.execute("UPDATE projects SET description='当前简介' WHERE name='p'")
        self.conn.commit()
        result = self.infer(item, {'text': '建议简化介绍', 'proposal': {
            'reason': '方便新成员了解项目', 'changes': {'description': '建议简介'}, 'facts': []}})
        self.assertEqual(result['state']['proposal']['before']['description'], '当前简介')
        self.assertEqual(self.conn.execute("SELECT description FROM projects WHERE name='p'").fetchone()[0], '当前简介')
        item = hud.answer(item['id'], {'revision': result['revision'], 'question_id': None, 'text': '保持现状',
                                      'dismiss_proposal': result['state']['proposal']['id']})
        result = self.infer(item, {'text': '重复建议', 'proposal': {'changes': {'description': '当前简介'}, 'facts': []}})
        self.assertEqual(result['status'], 'idle')
        self.assertNotIn('proposal', result['state'])
        with self.assertRaises(ValueError):
            hud.validate_response({'text': '伪造当前值', 'proposal': {'changes': {}, 'before': {'description': '假值'}}})

    def test_document_boundary_and_malformed_response(self):
        (self.root/'README.md').write_text('项目说明',encoding='utf-8')
        (self.root/'password.txt').write_text('secret',encoding='utf-8')
        (self.root/'.env').write_text('token',encoding='utf-8')
        content=hud.document_context(str(self.root))
        self.assertEqual(content['files'],['README.md'])
        with self.assertRaises(ValueError):
            hud.validate_response({'text':'执行操作','proposal':{'changes':{'command':'run'},'facts':[]}})

    def test_plain_response_persists_as_conversation_without_configuration_changes(self):
        from engine import hud_runtime
        item = self.session('p')
        before = dict(self.conn.execute('SELECT * FROM projects WHERE name=?', ('p',)).fetchone())
        message = '维护资料用于记录目录用途和资料来源。\n配置调整需要先确认提案。'
        with patch('engine.hud_runtime.respond', side_effect=lambda *_: hud_runtime._parse_response(message)):
            hud.run_one(item['id'])
        saved = hud.view(item['id'])
        self.assertEqual(saved['status'], 'idle')
        self.assertEqual(saved['error'], '')
        self.assertEqual(saved['entries'][-1]['content']['text'], message)
        self.assertFalse(saved['state'].get('proposal'))
        self.assertFalse(saved['state'].get('question'))
        self.assertFalse(saved['state'].get('facts'))
        self.assertEqual(dict(self.conn.execute('SELECT * FROM projects WHERE name=?', ('p',)).fetchone()), before)
        self.assertIsNone(self.conn.execute(
            "SELECT value FROM settings WHERE project='p' AND key='maintenance_facts'").fetchone())

    def test_runtime_response_parser_preserves_structured_validation(self):
        from engine import hud_runtime
        proposal = {'text': '建议补充简介', 'proposal': {'changes': {'description': '测试项目'}, 'facts': []}}
        encoded = json.dumps(proposal, ensure_ascii=False)
        for raw in (encoded, '```json\n' + encoded + '\n```', '```\n' + encoded + '\n```'):
            with self.subTest(raw=raw):
                self.assertEqual(hud_runtime._parse_response(raw), proposal)
        for raw in ('', '   ', '{"text":"建议调整","proposal":', '[{"text":', '```', '```json\n{}'):
            with self.subTest(raw=raw), self.assertRaises(RuntimeError):
                hud_runtime._parse_response(raw)
        for raw in ('[]', 'null', '"回复"', '{"text":"建议调整","proposal":{"changes":{"command":"run"}}}', '正文' * 3001):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                hud.validate_response(hud_runtime._parse_response(raw))

    def test_free_text_after_timeout_is_in_new_context(self):
        item=self.session()
        with hud.connection() as conn:
            conn.execute('UPDATE hud_sessions SET activity=? WHERE id=?',(time.time()-3600,item['id']))
        saved=hud.answer(item['id'],{'revision':item['revision'],'text':'我们调整为原型项目','question_id':None})
        with hud.connection() as conn:
            context=hud.context(conn,saved)
        self.assertEqual(context['history'][-1]['text'],'我们调整为原型项目')

    def test_usage_guide_persists_without_creating_a_decision(self):
        item=self.session()
        guide={'title':'登记需求资料','steps':[{'title':'确认来源','body':'说明资料位置及用途。'}]}
        result=self.infer(item,{'text':'按以下步骤准备。','guide':guide})
        self.assertEqual(result['status'],'idle')
        self.assertEqual(result['entries'][-1]['content']['guide'],guide)
        self.assertNotIn('question',result['state'])
        saved=hud.answer(item['id'],{'revision':result['revision'],'text':'资料分散怎么办？','question_id':None})
        with hud.connection() as conn:
            payload=hud.context(conn,saved)
        self.assertEqual(payload['history'][-2]['guide'],guide)
        self.assertEqual(payload['history'][-1]['text'],'资料分散怎么办？')
        for bad in ({'title':'说明','steps':[]},{'title':'说明','steps':[{'title':'运行','body':'说明','command':'run'}]}):
            with self.assertRaises(ValueError):hud.validate_response({'text':'说明','guide':bad})
        result=hud.validate_response({'text':'需要确认','question':{'title':'来源在哪？','reason':'需要资料'},'guide':guide})
        self.assertIsNone(result['guide'])

    def test_stage_change_defers_question_and_manual_edit_invalidates_proposal(self):
        item=self.session()
        item=self.infer(item,{'text':'确认目标','question':{'title':'预期产出是什么？','reason':'决定准备哪些资料','options':['原型','应用']}})
        saved=hud.update(item['id'],{'revision':item['revision'],'stage':'collaboration','draft':{'description':'新的目标'}})
        self.assertNotIn('question',saved['state'])
        self.assertNotIn('proposal',saved['state'])
        self.assertEqual(len(saved['state']['deferred_questions']),1)

    def test_proposal_takes_precedence_and_timeout_advances_once(self):
        item=self.session()
        with hud.connection() as conn:
            conn.execute('UPDATE hud_sessions SET activity=? WHERE id=?',(time.time()-3600,item['id']))
        result=self.infer(item,{'text':'配置建议','question':{'title':'下一项？','reason':'后续阶段需要','options':['A','B']},
                                'proposal':{'changes':{'description':'旧目标'},'facts':[]}})
        self.assertEqual(result['generation'],item['generation']+1)
        self.assertNotIn('question',result['state'])
        self.assertIn('proposal',result['state'])
        saved=hud.update(item['id'],{'revision':result['revision'],'draft':{'description':'用户新目标'}})
        self.assertNotIn('proposal',saved['state'])

    def test_pending_proposal_survives_timeout_followup_and_history_limit(self):
        item = self.session('p')
        result = self.infer(item, {'text': '逐项核对建议', 'proposal': {'changes': {}, 'facts': [
            {'label': f'第{i}项', 'value': f'待讨论内容{i}'} for i in range(1, 9)]}})
        proposal = result['state']['proposal']
        with hud.connection() as conn:
            for i in range(18):
                hud.add_entry(conn, result, 'user', {'text': f'补充说明{i}'})
            conn.execute('UPDATE hud_sessions SET activity=? WHERE id=?', (time.time()-3600, item['id']))
        response = self.client.post('/hud/'+item['id']+'/answer', json={
            'revision': result['revision'], 'question_id': None, 'text': '这8条我们一条一条谈'})
        self.assertEqual(response.status_code, 200, response.text)
        queued = response.json()
        self.assertEqual(queued['generation'], result['generation'])
        self.assertEqual(queued['state']['proposal'], proposal)
        blocked = self.client.post('/hud/'+item['id']+'/accept', json={
            'revision': queued['revision'], 'proposal_id': proposal['id']})
        self.assertEqual(blocked.status_code, 409)
        with hud.connection() as conn:
            conn.execute('UPDATE hud_sessions SET activity=? WHERE id=?', (time.time()-3600, item['id']))
        def explain(bot, payload):
            self.assertEqual(payload['pending_proposal'], proposal)
            self.assertEqual(len(payload['pending_proposal']['facts']), 8)
            self.assertFalse(any(entry.get('proposal') for entry in payload['history']))
            self.assertEqual(payload['history'][-1]['text'], '这8条我们一条一条谈')
            return {'text': '先看第一项的来源。', 'proposal': None}
        with patch('engine.hud_runtime.respond', side_effect=explain):
            hud.run_one(item['id'])
        saved = hud.view(item['id'])
        self.assertEqual(saved['generation'], result['generation'])
        self.assertEqual(saved['state']['proposal'], proposal)
        self.assertEqual(saved['status'], 'waiting')

    def test_resolving_proposal_restarts_idle_timer_and_allows_later_reset(self):
        for action in ('accept', 'dismiss'):
            with self.subTest(action=action):
                result = self.infer(self.session(), {'text': '建议', 'proposal': {
                    'changes': {'description': '待确认目标'}, 'facts': []}})
                proposal_id = result['state']['proposal']['id']
                with hud.connection() as conn:
                    conn.execute('UPDATE hud_sessions SET activity=? WHERE id=?', (time.time()-3600, result['id']))
                if action == 'accept':
                    response = self.client.post('/hud/'+result['id']+'/accept', json={
                        'revision': result['revision'], 'proposal_id': proposal_id})
                else:
                    stale = self.client.post('/hud/'+result['id']+'/answer', json={
                        'revision': result['revision'], 'question_id': None,
                        'text': '暂不处理', 'dismiss_proposal': 'old-proposal'})
                    self.assertEqual(stale.status_code, 409)
                    response = self.client.post('/hud/'+result['id']+'/answer', json={
                        'revision': result['revision'], 'question_id': None,
                        'text': '暂不处理', 'dismiss_proposal': proposal_id})
                self.assertEqual(response.status_code, 200, response.text)
                resolved = response.json()
                self.assertNotIn('proposal', resolved['state'])
                self.assertEqual(resolved['generation'], result['generation'])
                self.assertLess(time.time()-resolved['activity'], 5)
                finished = self.infer(resolved, {'text': '已处理，继续讨论。'})
                with hud.connection() as conn:
                    conn.execute('UPDATE hud_sessions SET activity=? WHERE id=?', (time.time()-3600, result['id']))
                fresh = hud.answer(result['id'], {'revision': finished['revision'], 'question_id': None, 'text': '新的话题'})
                self.assertEqual(fresh['generation'], result['generation']+1)

    def test_new_proposal_replaces_pending_proposal(self):
        result = self.infer(self.session(), {'text': '建议', 'proposal': {
            'changes': {'description': '原目标'}, 'facts': []}})
        queued = hud.answer(result['id'], {'revision': result['revision'], 'question_id': None, 'text': '改成新目标'})
        replacement = self.infer(queued, {'text': '调整建议', 'proposal': {
            'changes': {'description': '新目标'}, 'facts': []}})
        self.assertNotEqual(replacement['state']['proposal']['id'], result['state']['proposal']['id'])
        self.assertEqual(replacement['state']['proposal']['changes']['description'], '新目标')

    def test_workspace_snapshot_matches_saved_shared_configuration(self):
        from engine import project_workspaces as ws
        item = self.session('p')
        self.join(profile='peer')
        config = {'services': {'frontend': {'port': 21001, 'host': '127.0.0.1'}},
                  'browser': None, 'test_data_directory': str(self.root/'test-data')}
        self.conn.execute('INSERT INTO workspace_resources(project,path,workspace_id,config,revision) VALUES(?,?,?,?,?)',
                          ('p', ws.key(self.root), 'fixture-shared', json.dumps(config), 3))
        self.conn.commit()
        payload = hud.context(self.conn, item)
        resources = payload['project']['workspace_resources']
        self.assertEqual(len(resources), 1)
        self.assertEqual(len(resources[0]['members']), 2)
        profile = resources[0]['members'][0]
        response = self.client.get(f'/projects/p/workspaces/{profile}/resources')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(resources[0]['config'], response.json()['config'])
        self.assertEqual(resources[0]['revision'], response.json()['revision'])
        check = next(c for c in payload['inspection']['checks'] if c['key']=='resources')
        self.assertEqual(check['status'], 'ok')
        self.assertEqual(check['evidence']['configured'], 1)
        self.assertEqual(hud.project_snapshot(self.conn, 'q')['workspace_resources'], [])

    def test_workspace_snapshot_distinguishes_missing_and_unresolved_directories(self):
        item = self.session('p')
        profile = self.conn.execute("SELECT profile FROM project_members WHERE project='p'").fetchone()[0]
        payload = hud.context(self.conn, item)
        resources = payload['project']['workspace_resources']
        self.assertEqual(resources[0]['revision'], 0)
        self.assertIsNone(resources[0]['config'])
        self.assertEqual(next(c for c in payload['inspection']['checks'] if c['key']=='resources')['status'], 'missing')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM workspace_resources').fetchone()[0], 0)
        self.conn.execute("UPDATE projects SET directory_mode='worktree' WHERE name='p'")
        self.conn.execute("INSERT INTO project_workspace_modes(project,profile,mode) VALUES('p',?,'worktree')", (profile,))
        self.conn.commit()
        payload = hud.context(self.conn, item)
        self.assertEqual(payload['project']['workspace_resources'], [])
        self.assertEqual(payload['project']['workspace_errors'][0]['profile'], profile)
        self.assertEqual(next(c for c in payload['inspection']['checks'] if c['key']=='resources')['status'], 'unknown')

    def test_workspace_snapshot_checks_each_selected_directory(self):
        from engine import project_workspaces as ws
        item = self.session('p')
        first = self.conn.execute("SELECT profile FROM project_members WHERE project='p'").fetchone()[0]
        second = self.join(profile='peer')
        separate = self.root/'peer-workspace'
        separate.mkdir()
        self.conn.execute("UPDATE projects SET directory_mode='worktree',worktree_root=? WHERE name='p'", (str(self.root),))
        self.conn.executemany("INSERT INTO project_workspace_modes(project,profile,mode) VALUES('p',?,?)",
                              [(first, 'shared'), (second, 'worktree')])
        self.conn.execute("INSERT INTO project_workspaces(project,profile,path) VALUES('p',?,?)", (second, str(separate)))
        config = {'services': {}, 'browser': None, 'test_data_directory': str(self.root/'isolated-data')}
        self.conn.execute('INSERT INTO workspace_resources(project,path,workspace_id,config) VALUES(?,?,?,?)',
                          ('p', ws.key(self.root), 'fixture-main', json.dumps(config)))
        self.conn.commit()
        payload = hud.context(self.conn, item)
        resources = {r['path']: r for r in payload['project']['workspace_resources']}
        self.assertEqual(resources[ws.key(self.root)]['members'], [first])
        self.assertEqual(resources[ws.key(separate)]['members'], [second])
        check = next(c for c in payload['inspection']['checks'] if c['key']=='resources')
        self.assertEqual(check['status'], 'missing')
        self.assertEqual(check['evidence']['configured'], 1)
        self.assertEqual(check['evidence']['missing_paths'], [ws.key(separate)])
        self.conn.execute('INSERT INTO workspace_resources(project,path,workspace_id,config) VALUES(?,?,?,?)',
                          ('p', ws.key(separate), 'fixture-peer', json.dumps(config)))
        self.conn.commit()
        payload = hud.context(self.conn, item)
        check = next(c for c in payload['inspection']['checks'] if c['key']=='resources')
        self.assertEqual(check['status'], 'ok')
        self.assertEqual(check['evidence']['configured'], 2)

    def test_workspace_snapshot_checks_each_selected_directory(self):
        from engine import project_workspaces as ws
        item = self.session('p')
        first = self.conn.execute("SELECT profile FROM project_members WHERE project='p'").fetchone()[0]
        second = self.join(profile='peer')
        separate = self.root/'peer-workspace'
        separate.mkdir()
        self.conn.execute("UPDATE projects SET directory_mode='worktree',worktree_root=? WHERE name='p'", (str(self.root),))
        self.conn.executemany("INSERT INTO project_workspace_modes(project,profile,mode) VALUES('p',?,?)",
                              [(first, 'shared'), (second, 'worktree')])
        self.conn.execute("INSERT INTO project_workspaces(project,profile,path) VALUES('p',?,?)", (second, str(separate)))
        config = {'services': {}, 'browser': None, 'test_data_directory': str(self.root/'isolated-data')}
        self.conn.execute('INSERT INTO workspace_resources(project,path,workspace_id,config) VALUES(?,?,?,?)',
                          ('p', ws.key(self.root), 'fixture-main', json.dumps(config)))
        self.conn.commit()
        payload = hud.context(self.conn, item)
        resources = {r['path']: r for r in payload['project']['workspace_resources']}
        self.assertEqual(resources[ws.key(self.root)]['members'], [first])
        self.assertEqual(resources[ws.key(separate)]['members'], [second])
        check = next(c for c in payload['inspection']['checks'] if c['key']=='resources')
        self.assertEqual(check['status'], 'missing')
        self.assertEqual(check['evidence']['configured'], 1)
        self.assertEqual(check['evidence']['missing_paths'], [ws.key(separate)])
        self.conn.execute('INSERT INTO workspace_resources(project,path,workspace_id,config) VALUES(?,?,?,?)',
                          ('p', ws.key(separate), 'fixture-peer', json.dumps(config)))
        self.conn.commit()
        payload = hud.context(self.conn, item)
        check = next(c for c in payload['inspection']['checks'] if c['key']=='resources')
        self.assertEqual(check['status'], 'ok')
        self.assertEqual(check['evidence']['configured'], 2)

    def test_default_bot_does_not_enable_other_projects(self):
        item=self.session()
        hud.update(item['id'],{'revision':item['revision'],'bot_id':item['bot_id'],'make_default':True})
        other=hud.create('q')
        self.assertEqual(other['bot_id'],item['bot_id'])
        self.assertEqual(other['status'],'paused')
        self.assertNotIn(other['id'],hud.scan())
        with hud.connection() as conn:
            self.assertFalse(conn.execute("SELECT 1 FROM hud_sessions WHERE project='p'").fetchone())

    def test_background_probe_preserves_input_arriving_during_check(self):
        item = self.infer(self.session('p'), {'text': '检查完成'})
        with hud.connection() as conn:
            conn.execute('UPDATE hud_sessions SET checked=0 WHERE id=?', (item['id'],))
        def concurrent_input(*_):
            hud.answer(item['id'], {'revision': item['revision'], 'question_id': None, 'text': '先回答这个新问题'})
            return {'checks': [{'key': 'fixture', 'status': 'changed'}]}
        with patch('engine.hud_inspection.inspect', side_effect=concurrent_input):
            hud.scan()
        saved = hud.view(item['id'])
        self.assertEqual(saved['checked'], 0)
        self.assertEqual(saved['entries'][-1]['content']['text'], '先回答这个新问题')
        self.assertEqual(saved['status'], 'queued')

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
            self.assertTrue(settings['features']['shell_tool'])
            self.assertFalse(settings['features']['apps'])
            self.assertEqual(settings['default_permissions'], ':workspace')
            self.assertEqual(settings['developer_instructions'], hud_runtime.SYSTEM.strip())
            self.assertNotIn(hud_runtime.SYSTEM, prompt)
            self.assertEqual(env['CODEX_HOME'],str(home))
            self.assertNotIn('GROUP_DB',env)
            Path(command[command.index('--output-last-message')+1]).write_text('{"text":"ready"}',encoding='utf-8')
            return ''
        bot={'runtime':'codex','config_root':str(home),'profile':'default'}
        with patch('engine.hud_runtime.config.data_dir',return_value=self.root),patch('engine.wake._codex_bin',return_value='codex'),patch('engine.hud_runtime._run',side_effect=fake_run):
            self.assertEqual(hud_runtime.respond(bot,{}),{'text':'ready'})
        self.assertFalse(Path(captured[0]).exists())
        self.assertIn('mcp_servers.production',(home/'config.toml').read_text(encoding='utf-8'))

    def test_progress_is_live_revision_fenced_and_cleared_on_completion(self):
        from engine import hud_runtime
        item = self.session()
        def responding(*args):
            hud_runtime.report_progress('stage', '正在等待模型响应')
            running = hud.view(item['id'])
            self.assertEqual(running['revision'], item['revision'])
            self.assertEqual(running['state']['progress']['text'], '正在等待模型响应')
            with patch('engine.project_hud.time.time', return_value=time.time()+1):
                hud_runtime.report_progress('reasoning', '先核对目标\n再检查验收条件')
            running = hud.view(item['id'])
            self.assertEqual(running['state']['progress']['kind'], 'reasoning')
            self.assertEqual(running['state']['progress']['text'], '先核对目标 再检查验收条件')
            return {'text': '检查完成'}
        with patch('engine.hud_runtime.respond', side_effect=responding):
            hud.run_one(item['id'])
        self.assertNotIn('progress', hud.view(item['id'])['state'])
        item = hud.update(item['id'], {'revision': hud.view(item['id'])['revision'], 'draft': {'description': '新版'}})
        def obsolete(*args):
            hud.update(item['id'], {'revision': item['revision'], 'draft': {'description': '更新版'}})
            hud_runtime.report_progress('stage', '过期进度')
            return {'text': '过期回答'}
        with patch('engine.hud_runtime.respond', side_effect=obsolete):
            hud.run_one(item['id'])
        self.assertNotIn('progress', hud.view(item['id'])['state'])

    def test_runtime_streams_codex_progress_before_exit(self):
        from engine import hud_runtime
        inherited = {'__PYVENV_LAUNCHER__': 'other-pythonw', 'PYTHONHOME': 'other-home',
                     'PYTHONPATH': 'other-modules', 'VIRTUAL_ENV': 'other-venv'}
        with patch.dict(os.environ, inherited):
            env = hud_runtime._env()
        self.assertTrue(set(inherited).isdisjoint(env))
        marker = self.root/'child-finished'
        script = (
            'import json,sys,time; from pathlib import Path; sys.stdin.read(); '
            'print(json.dumps({"type":"item.completed","item":{"type":"reasoning","text":"确认资料来源"}}),flush=True); '
            'time.sleep(0.4); Path(sys.argv[1]).touch(); print("{}")'
        )
        updates = []
        def receive(kind, text):
            updates.append((kind, text, marker.exists()))
        with hud_runtime.progress_sink(receive):
            result = hud_runtime._run([sys.executable, '-c', script, str(marker)], 'fixture', env, str(self.root))
        self.assertIn(('reasoning', '确认资料来源', False), updates)
        self.assertIn('item.completed', result)
        script = 'import sys; sys.exit(1)'
        with self.assertRaisesRegex(RuntimeError, '子进程退出码 1'):
            hud_runtime._run([sys.executable, '-c', script], '', env, str(self.root))

    def test_gateway_execution_record_survives_failed_hud_run(self):
        from engine import hud_gateway
        item = self.session()
        def responding(*args):
            hud_gateway._save({'session_id': 'isolated-session', 'admission_id': 'pending'})
            raise RuntimeError('connection lost')
        with patch('engine.hud_runtime.respond', side_effect=responding):
            hud.run_one(item['id'])
        saved = hud.view(item['id'])
        self.assertEqual(saved['status'], 'error')
        self.assertEqual(saved['state']['gateway']['admission_id'], 'pending')
        with hud_gateway.owner(item['id']):
            self.assertEqual(hud_gateway._saved(), saved['state']['gateway'])

    def test_runtime_timeout_terminates_process_and_clears_registration(self):
        from engine import hud_runtime
        original_wait = subprocess.Popen.wait
        def short_wait(proc, timeout=None):
            return original_wait(proc, timeout=0.1 if timeout == 600 else timeout)
        with patch.object(subprocess.Popen, 'wait', short_wait):
            with self.assertRaisesRegex(RuntimeError, '超过 600 秒'):
                hud_runtime._run([sys.executable, '-c', 'import time; time.sleep(30)'], '', hud_runtime._env(), str(self.root))
        self.assertFalse(hud_runtime._PROCESSES)

    def test_legacy_hermes_uses_public_cli_with_isolated_configuration(self):
        from engine import hud_runtime
        import yaml
        home=self.root/'legacy-home';home.mkdir()
        (home/'config.yaml').write_text('model: fixture\nmcp_servers: {production: {command: unused}}', encoding='utf-8')
        bot={'runtime':'hermes','config_root':str(home),'profile':'default'}
        captured=[]
        def cli(command,prompt,env,cwd):
            captured.append(cwd)
            self.assertEqual(command,['hermes-fixture','chat','-Q','--query-file','-'])
            self.assertEqual(env['HERMES_HOME'],cwd)
            self.assertEqual(env['HERMES_DISABLE_LAZY_INSTALLS'],'1')
            self.assertEqual(env['HERMES_EPHEMERAL_SYSTEM_PROMPT'],hud_runtime.SYSTEM)
            self.assertNotIn(hud_runtime.SYSTEM,prompt)
            config=yaml.safe_load((Path(cwd)/'config.yaml').read_text(encoding='utf-8'))
            self.assertEqual(config['platform_toolsets']['cli'],['file'])
            self.assertEqual(config['mcp_servers'],{})
            return '{"text":"ready"}'
        with patch('engine.hermes_gateway.enabled',return_value=False), \
             patch('engine.hermes_runtime.command',return_value=['hermes-fixture']), \
             patch('engine.hud_runtime.config.data_dir',return_value=self.root), \
             patch('engine.hud_runtime._run',side_effect=cli):
            self.assertEqual(hud_runtime.respond(bot,{}),{'text':'ready'})
        self.assertFalse(Path(captured[0]).exists())

    def test_gateway_dispatch_never_falls_back_to_subprocess(self):
        from engine import hud_runtime
        from engine.hermes_gateway_client import GatewayTransportError
        bot={'runtime':'hermes','config_root':str(self.root),'profile':'default'}
        with patch('engine.hermes_gateway.enabled',return_value=True), \
             patch('engine.hud_gateway.respond',return_value='{"text":"gateway"}') as call, \
             patch('engine.hud_runtime._run') as legacy:
            self.assertEqual(hud_runtime.respond(bot,{}),{'text':'gateway'})
            self.assertEqual(call.call_args.kwargs['system_prompt'],hud_runtime.SYSTEM)
            self.assertNotIn(hud_runtime.SYSTEM,call.call_args.args[1])
            call.side_effect=GatewayTransportError('offline')
            with self.assertRaises(GatewayTransportError):
                hud_runtime.respond(bot,{})
            legacy.assert_not_called()


if __name__=='__main__':
    unittest.main()
