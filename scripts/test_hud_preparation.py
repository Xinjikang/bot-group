"""Project-type preparation, usage evidence and confirmed document/config writes."""
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from engine import hud_documents, hud_inspection, hud_knowledge, project_hud as hud, project_templates
from scripts import test_project_hud


class PreparationTests(unittest.TestCase):
    setUp = test_project_hud.HudTests.setUp
    join = test_project_hud.HudTests.join
    session = test_project_hud.HudTests.session
    def infer(self, item, response):
        # Historical document proposals originate from a complete-content snapshot.
        if response.get('proposal', {}).get('documents'):
            original = hud.document_context
            def complete(root, **kwargs):
                kwargs['include_contents'] = True
                return original(root, **kwargs)
            with patch.object(hud, 'document_context', side_effect=complete):
                return test_project_hud.HudTests.infer(self, item, response)
        return test_project_hud.HudTests.infer(self, item, response)

    def select_template(self, template_id):
        self.conn.execute("INSERT INTO project_config_events(project,actor,action,detail,created_at) VALUES('p','fixture','project.import',?,?)",
                          (json.dumps({'template': {'id': template_id}}), hud.db.now_iso()))
        self.conn.commit()

    def proposal(self, name='docs/product/PRD.md', text='# 产品需求\n\n来源：用户本轮提供的产品说明。\n'):
        return {'text': '建议整理需求。', 'proposal': {'changes': {}, 'facts': [], 'documents': [
            {'path': name, 'content': text, 'sources': ['用户本轮提供的产品说明']} ]}}

    def test_all_templates_declare_real_files_and_type_requirements(self):
        for summary in project_templates.catalogue()['templates']:
            template = project_templates.load(summary['id'])
            guide = hud_knowledge.guidance(template)
            self.assertTrue(guide['inputs'])
            self.assertTrue(guide['configuration'])
            for document in guide['documents']:
                self.assertIn(document['path'], template['contents'])
        self.assertEqual(hud_knowledge.guidance({'id': 'blank'})['documents'], [])

    def test_testing_skeleton_and_missing_document_are_detected(self):
        self.select_template('testing')
        template = project_templates.load('testing')
        target = self.root / 'docs/product/PRD.md'
        target.parent.mkdir(parents=True)
        target.write_text(template['contents']['docs/product/PRD.md'], encoding='utf-8')
        item = self.session('p')
        with hud.connection() as conn:
            context = hud.context(conn, item)
        found = {d['path']: d for d in context['preparation']['documents']}
        self.assertEqual(found['docs/product/PRD.md']['status'], 'unknown')
        self.assertEqual(context['materials']['documents'], {})
        self.assertEqual(found['docs/product/PROTOTYPE.md']['status'], 'missing')
        self.assertIn('normalization', context['preparation']['guidance'])
        self.assertTrue(context['usage_knowledge']['documents'])

    def test_legacy_doc_directory_and_truncated_content(self):
        template = project_templates.load('testing')
        path = self.root / 'doc/product/PRD.md'
        path.parent.mkdir(parents=True)
        path.write_text('已确认产品规则' * 4000, encoding='utf-8')
        materials = hud_inspection.documents(str(self.root))
        result = hud_knowledge.preparation({'root': str(self.root)}, template, materials)
        doc = result['documents'][0]
        self.assertEqual(doc['path'], 'doc/product/PRD.md')
        self.assertEqual(doc['status'], 'unknown')

    def test_usage_knowledge_is_from_installation_and_matches_question(self):
        result = hud_knowledge.usage([{'text': '如何给项目配置工作区资源和测试数据目录？'}])
        self.assertEqual(result['source'], 'current_installation')
        self.assertTrue(any('资源' in d['text'] for d in result['documents']))
        self.assertTrue(all(d['source'] == 'installed_bot_group_documentation' for d in result['documents']))
        self.assertLessEqual(sum(len(d['text']) for d in result['documents']), 28000)

    def test_confirm_writes_two_standard_documents_and_preserves_sources(self):
        item = self.session('p')
        response = self.proposal()
        response['proposal']['documents'].append({'path': 'docs/product/PROTOTYPE.md',
            'content': '# 原型\n\n来源：用户提供的页面说明；版本待确认。', 'sources': ['用户提供的页面说明']})
        result = self.infer(item, response)
        self.assertEqual(result['status'], 'waiting', result['error'])
        self.assertFalse((self.root / 'docs').exists())
        proposal = result['state']['proposal']
        self.assertIsNone(proposal['documents'][0]['before_sha256'])
        accepted = hud.accept(item['id'], {'revision': result['revision'], 'proposal_id': proposal['id']})
        self.assertEqual((self.root / 'docs/product/PRD.md').read_text(encoding='utf-8'), response['proposal']['documents'][0]['content'])
        self.assertTrue((self.root / 'docs/product/PROTOTYPE.md').is_file())
        self.assertIn('已保存项目文档', accepted['entries'][-1]['content']['text'])

    def test_concurrent_file_creation_or_edit_keeps_proposal_and_original(self):
        item = self.session('p')
        result = self.infer(item, self.proposal())
        target = self.root / 'docs/product/PRD.md'
        target.parent.mkdir(parents=True)
        target.write_text('其他成员的新内容', encoding='utf-8')
        with self.assertRaises(HTTPException) as error:
            hud.accept(item['id'], {'revision': result['revision'], 'proposal_id': result['state']['proposal']['id']})
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(target.read_text(encoding='utf-8'), '其他成员的新内容')
        self.assertIn('proposal', hud.view(item['id'])['state'])

    def test_existing_file_preview_and_root_change_conflict(self):
        target = self.root / 'docs/product/PRD.md'
        target.parent.mkdir(parents=True)
        target.write_text('# 已有需求\n已确认的业务规则。', encoding='utf-8')
        item = self.session('p')
        result = self.infer(item, self.proposal())
        proposal = result['state']['proposal']
        self.assertEqual(proposal['documents'][0]['before'], target.read_bytes().decode('utf-8'))
        self.conn.execute("UPDATE projects SET root=? WHERE name='p'", (str(self.root / 'other'),))
        self.conn.commit()
        with self.assertRaises(HTTPException):
            hud.accept(item['id'], {'revision': result['revision'], 'proposal_id': proposal['id']})
        self.assertIn('已有需求', target.read_text(encoding='utf-8'))

    def test_truncated_existing_file_cannot_be_overwritten(self):
        target = self.root / 'docs/product/PRD.md'
        target.parent.mkdir(parents=True)
        target.write_text('原始业务规则' * 2000, encoding='utf-8')
        result = self.infer(self.session('p'), self.proposal())
        self.assertEqual(result['status'], 'error')
        self.assertNotIn('proposal', result['state'])

    def test_file_batch_failure_rolls_back_files_and_database(self):
        result = self.infer(self.session('p'), self.proposal())
        with patch.object(hud, 'store', side_effect=RuntimeError('fixture failed')):
            with self.assertRaises(RuntimeError):
                hud.accept(result['id'], {'revision': result['revision'], 'proposal_id': result['state']['proposal']['id']})
        self.assertFalse((self.root / 'docs').exists())
        self.assertIn('proposal', hud.view(result['id'])['state'])

    def test_document_validation_rejects_escape_rules_duplicates_and_missing_sources(self):
        for name in ('../outside.md', 'docs/../../outside.md', '.git/config.md',
                     'docs/AGENTS.md', 'reports/result.md', 'docs/secret.txt'):
            with self.subTest(name=name), self.assertRaises((ValueError, HTTPException)):
                hud.validate_response(self.proposal(name=name))
        value = self.proposal()
        value['proposal']['documents'][0]['sources'] = []
        with self.assertRaises(ValueError):
            hud.validate_response(value)
        value = self.proposal()
        value['proposal']['documents'] *= 2
        with self.assertRaises(ValueError):
            hud.validate_response(value)

    def test_document_proposal_is_unavailable_in_creation_draft(self):
        result = self.infer(self.session(), self.proposal())
        self.assertEqual(result['status'], 'error')
        self.assertFalse((self.root / 'docs').exists())

    def test_creation_and_detail_share_configuration_guidance(self):
        item = self.session()
        item = hud.update(item['id'], {'revision': item['revision'], 'draft': {
            'template': 'algorithm', 'display_name': '算法评测', 'wizard': {'roles': [], 'members': []}}})
        with hud.connection() as conn:
            context = hud.context(conn, item)
        self.assertIn('算法', context['preparation']['guidance']['inputs'][0])
        self.assertEqual(set(context['preparation']['configuration_fields']), hud.DRAFT_FIELDS)
        self.assertFalse(context['project_created'])
        self.assertEqual(context['project']['wizard']['roles'], [])

    def test_identical_document_proposal_is_removed(self):
        target = self.root / 'docs/product/PRD.md'
        target.parent.mkdir(parents=True)
        target.write_text('# 内容已确认\n', encoding='utf-8')
        result = self.infer(self.session('p'), self.proposal(text='# 内容已确认\n'))
        self.assertEqual(result['status'], 'idle', result['error'])
        self.assertFalse(result['state'].get('proposal'))

    def test_second_file_write_failure_restores_existing_file(self):
        target = self.root / 'docs/product/PRD.md'
        target.parent.mkdir(parents=True)
        target.write_text('# 原始已确认内容\n', encoding='utf-8')
        original = target.read_bytes()
        response = self.proposal()
        response['proposal']['documents'].append({'path': 'docs/product/PROTOTYPE.md',
            'content': '# 原型', 'sources': ['用户输入']})
        result = self.infer(self.session('p'), response)
        real_replace = hud_documents.replace
        calls = []
        def fail_second(path, raw):
            calls.append(path)
            if len(calls) == 2:
                raise OSError('fixture write failure')
            return real_replace(path, raw)
        with patch.object(hud_documents, 'replace', side_effect=fail_second):
            with self.assertRaises(OSError):
                hud.accept(result['id'], {'revision': result['revision'], 'proposal_id': result['state']['proposal']['id']})
        self.assertEqual(target.read_bytes(), original)
        self.assertFalse((target.parent / 'PROTOTYPE.md').exists())
        self.assertIn('proposal', hud.view(result['id'])['state'])

    def test_linked_document_targets_are_rejected(self):
        with patch.object(hud_inspection, 'safe_path', return_value=False):
            with self.assertRaises(HTTPException):
                hud_documents.snapshot(str(self.root), {'path': 'docs/linked.md'})

    def test_created_project_with_selected_default_bot_starts_check_and_bind_retry_preserves_todo(self):
        selected = self.session()
        hud.update(selected['id'], {'revision': selected['revision'], 'bot_id': selected['bot_id'], 'make_default': True})
        item = hud.create()
        self.assertEqual(item['status'], 'paused')
        with hud.connection() as conn:
            conn.execute("INSERT INTO project_setup_requests(id,digest,status,payload,result,updated_at) VALUES('auto-receipt','','ready','{}',?,?)",
                         (json.dumps({'project': 'p'}), time.time()))
        result = hud.bind(item['id'], 'auto-receipt')
        self.assertEqual(result['status'], 'queued')
        result = self.infer(result, self.proposal())
        retry = hud.bind(item['id'], 'auto-receipt')
        self.assertEqual(retry['state']['proposal']['id'], result['state']['proposal']['id'])

    def test_detail_directory_config_uses_validation_and_access_sync(self):
        destination = self.root / 'new-root'
        destination.mkdir()
        result = self.infer(self.session('p'), {'text': '更新主目录', 'proposal': {
            'changes': {'root': str(destination)}, 'facts': []}})
        self.assertEqual(result['status'], 'waiting', result['error'])
        from engine import codex_project_access
        with patch.object(codex_project_access, 'commit_directory_access', wraps=codex_project_access.commit_directory_access) as sync:
            hud.accept(result['id'], {'revision': result['revision'], 'proposal_id': result['state']['proposal']['id']})
        self.assertEqual(sync.call_count, 1)
        self.assertEqual(self.conn.execute("SELECT root FROM projects WHERE name='p'").fetchone()[0], str(destination))

    def test_pending_session_rescans_when_source_documents_change(self):
        result = self.infer(self.session('p'), {'text': '等待来源', 'question': {
            'title': '来源在哪里？', 'reason': '整理需求需要来源'}})
        (self.root / 'README.md').write_text('需求来源已补充。', encoding='utf-8')
        with hud.connection() as conn:
            conn.execute('UPDATE hud_sessions SET checked=? WHERE id=?', (time.time()-3700, result['id']))
        hud.scan()
        self.assertEqual(hud.view(result['id'])['status'], 'queued')


if __name__ == '__main__':
    import unittest
    unittest.main()
