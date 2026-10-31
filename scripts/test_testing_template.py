"""Independent testing template publication, authorization and report handoffs."""
import subprocess
import unittest

from engine import project_permissions as permissions, project_templates as templates, taskpool
from scripts import test_project_templates as fixtures
from scripts.test_task_brief import BRIEF
from scripts.unified_test_support import confirmed_requirement_fields


class TestingTemplateTests(unittest.TestCase):
    setUp = fixtures.TemplateTests.setUp
    body = fixtures.TemplateTests.body
    preview = fixtures.TemplateTests.preview
    submit = fixtures.TemplateTests.submit

    def test_renamed_roles_publish_product_case_script_and_report_contract(self):
        manifest = templates.load('testing')
        self.assertEqual([r['id'] for r in manifest['roles']], ['lead', 'tester', 'reviewer'])
        self.assertEqual(manifest['owner_role_id'], 'lead')
        body = self.body('testing')
        for role in body['roles']:
            role['name'] = 'Custom-' + role['id']
        response = self.preview(body)
        self.assertEqual(response.status_code, 200, response.text)
        plan = response.json()
        files = {f['path']: f['content'] for f in plan['files']}
        self.assertEqual(len(files), 15)
        self.assertEqual({p.split('/')[0] for p in files}, {'AGENTS.md', 'README.md', 'docs', 'scripts', 'reports'})
        self.assertEqual({p.split('/')[1] for p in files if p.startswith('docs/')},
                         {'INDEX.md', 'product', 'plan', 'cases', 'environment'})
        for content in [*files.values(), plan['template']['workflow']]:
            self.assertNotIn('{{', content)
            for role in manifest['roles']:
                self.assertNotIn(role['name'], content)
        for text in ('PRD', '产品原型', '一个脚本可覆盖多个用例', '一个用例可由多个脚本',
                     '产品质量结论单独标注', '同一交付提交', 'logged=true', 'scripts/TEST_INDEX.md'):
            self.assertIn(text, files['AGENTS.md'])
        self.assertIn('被测产品提交', files['reports/REPORT.md'])
        self.assertIn('首次结果', files['reports/RUN.md'])
        self.assertIn('人工步骤', files['scripts/TEST_INDEX.md'])
        result = self.submit(plan).json()
        self.assertEqual(result['status'], 'ready', result)
        for path, content in files.items():
            self.assertEqual((self.root / path).read_bytes(), content.encode('utf-8'))
        subprocess.run(['git', 'init', str(self.root)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.root), 'check-ignore', 'reports/REPORT.md',
                        'reports/round-01/evidence/output.log'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.root), 'add', '-A'], check=True, capture_output=True)
        tracked = subprocess.check_output(['git', '-C', str(self.root), 'ls-files'], text=True).splitlines()
        for path in ('docs/product/PRD.md', 'docs/product/PROTOTYPE.md', 'docs/cases/CASE.md', 'scripts/TEST_INDEX.md'):
            self.assertIn(path, tracked)
        self.assertFalse(any(p.startswith('reports/') for p in tracked))
        base = templates.TEMPLATE_ROOT / 'testing'
        self.assertEqual({p.relative_to(base).as_posix() for p in base.rglob('*') if p.is_file()},
                         {'manifest.json', 'assistant.json', *[f['source'] for f in manifest['files']],
                          *[r['soul_reference'] for r in manifest['roles']]})

    def test_report_acceptance_preserves_product_failure_and_role_permissions(self):
        plan = self.preview(self.body('testing')).json()
        result = self.submit(plan).json()
        self.assertEqual(result['status'], 'ready', result)
        project = result['project']
        handles = result['applied_team']['member_profiles']
        role_ids = result['applied_team']['role_ids']
        members = {m['role']: handles[str(m['bot_id'])] for m in plan['team']['members']}
        lead = members['lead']
        for permission in permissions.PERMISSIONS:
            self.assertIsNone(permissions.check(self.conn, project, lead, permission))
            for role in ('tester', 'reviewer'):
                self.assertIsNotNone(permissions.check(self.conn, project, members[role], permission))
        req = taskpool.req_create(project, lead, 'Independent testing fixture', **confirmed_requirement_fields(project))
        created = taskpool.task_create(project, lead, req['requirement_id'],
                                       [{'title': 'Deliver test report', 'next_bot': members['tester'], **BRIEF}],
                                       complete=True)
        tid = created['task_ids'][0]
        stages = [
            ('tester', 'design', 'Cases and scripts v1 linked to PRD', 'reviewer'),
            ('reviewer', 'review', 'Revise missing boundary CASE-2', 'tester'),
            ('tester', 'design', 'Cases and scripts v2 add CASE-2', 'reviewer'),
            ('reviewer', 'review', 'Design coverage accepted', 'tester'),
            ('tester', 'test', 'RUN-1 product failure DEFECT-1; CASE-2 blocked', 'reviewer'),
            ('reviewer', 'review', 'Evidence verified; DEFECT-1 remains open', 'lead'),
        ]
        for role, stage, outcome, target in stages:
            actor = members[role]
            self.assertTrue(taskpool.task_claim(project, actor)['claimed'])
            denied = taskpool.task_complete(project, actor, tid, result='Early closure')
            self.assertEqual(denied.get('code'), 'permission_denied')
            logged = taskpool.task_log(project, actor, tid, stage, result=outcome,
                                       meta={'scope': 'isolated fixture', 'product_quality': 'failed'},
                                       next_bot=members[target], expected_role_id=role_ids[target])
            self.assertTrue(logged['logged'], logged)
        self.assertTrue(taskpool.task_complete(project, lead, tid,
                                              result='Report accepted; product failure and blocked coverage retained')['completed'])
        self.assertTrue(taskpool.req_complete(project, lead, req['requirement_id'])['completed'])
        nodes = [r[0] for r in self.conn.execute('SELECT result FROM nodes WHERE task_id=? ORDER BY id', (tid,))]
        self.assertIn(stages[4][2], nodes)
        self.assertIn(stages[5][2], nodes)


if __name__ == '__main__':
    unittest.main()
