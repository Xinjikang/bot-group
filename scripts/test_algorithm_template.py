"""Algorithm template layout, reproducible evidence and four-role handoffs."""
import subprocess
import unittest

from engine import project_permissions as permissions, project_templates as templates, taskpool
from scripts import test_project_templates as fixtures
from scripts.test_task_brief import BRIEF
from scripts.unified_test_support import confirmed_requirement_fields


class AlgorithmTemplateTests(unittest.TestCase):
    setUp = fixtures.TemplateTests.setUp
    body = fixtures.TemplateTests.body
    preview = fixtures.TemplateTests.preview
    submit = fixtures.TemplateTests.submit

    def test_layout_renaming_and_report_exclusion(self):
        manifest = templates.load('algorithm')
        self.assertEqual([r['id'] for r in manifest['roles']], ['lead', 'researcher', 'engineer', 'reviewer'])
        self.assertEqual(manifest['owner_role_id'], 'lead')
        body = self.body('algorithm')
        for role in body['roles']:
            role['name'] = 'Custom-' + role['id']
        response = self.preview(body)
        self.assertEqual(response.status_code, 200, response.text)
        plan = response.json()
        files = {f['path']: f['content'] for f in plan['files']}
        self.assertEqual({p.split('/')[0] for p in files}, {'AGENTS.md', 'README.md', 'docs', 'reports'})
        self.assertEqual({p.split('/')[1] for p in files if p.startswith('docs/')},
                         {'INDEX.md', 'requirements', 'literature', 'design', 'engineering'})
        for content in [*files.values(), plan['template']['workflow']]:
            self.assertNotIn('{{', content)
            for role in manifest['roles']:
                self.assertNotIn(role['name'], content)
        for field in ('数据泄漏', '随机种子', '复杂度', '停止条件', '负面结论', 'logged=true',
                      '源码、Demo、配置、数据和模型目录由用户管理'):
            self.assertIn(field, files['AGENTS.md'])
        for field in ('实验计划', '基线与对照', '预算与停止条件'):
            self.assertIn(field, files['docs/design/ALGORITHM.md'])
        self.assertIn('待填写骨架', files['reports/REPORT.md'])
        self.assertIn('校验值', files['reports/experiments/EXPERIMENT.md'])
        result = self.submit(plan).json()
        self.assertEqual(result['status'], 'ready', result)
        for path, content in files.items():
            self.assertEqual((self.root / path).read_bytes(), content.encode('utf-8'))
        subprocess.run(['git', 'init', str(self.root)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.root), 'check-ignore', 'reports/REPORT.md',
                        'reports/experiments/run-001/record.md'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.root), 'add', '-A'], check=True, capture_output=True)
        tracked = subprocess.check_output(['git', '-C', str(self.root), 'ls-files'], text=True).splitlines()
        self.assertIn('docs/design/ALGORITHM.md', tracked)
        self.assertFalse(any(p.startswith('reports/') for p in tracked))
        base = templates.TEMPLATE_ROOT / 'algorithm'
        self.assertEqual({p.relative_to(base).as_posix() for p in base.rglob('*') if p.is_file()},
                         {'manifest.json', 'assistant.json', *[f['source'] for f in manifest['files']],
                          *[r['soul_reference'] for r in manifest['roles']]})

    def test_four_role_revision_and_acceptance(self):
        plan = self.preview(self.body('algorithm')).json()
        result = self.submit(plan).json()
        self.assertEqual(result['status'], 'ready', result)
        project = result['project']
        handles = result['applied_team']['member_profiles']
        role_ids = result['applied_team']['role_ids']
        members = {m['role']: handles[str(m['bot_id'])] for m in plan['team']['members']}
        lead = members['lead']
        for permission in permissions.PERMISSIONS:
            self.assertIsNone(permissions.check(self.conn, project, lead, permission))
            for role in ('researcher', 'engineer', 'reviewer'):
                self.assertIsNotNone(permissions.check(self.conn, project, members[role], permission))
        req = taskpool.req_create(project, lead, 'Algorithm fixture', **confirmed_requirement_fields(project))
        created = taskpool.task_create(project, lead, req['requirement_id'],
                                       [{'title': 'Compare algorithm against baseline',
                                         'next_bot': members['researcher'], **BRIEF}], complete=True)
        tid = created['task_ids'][0]
        stages = [
            ('researcher', 'design', 'Baseline and experiment plan v1', 'engineer'),
            ('engineer', 'dev', 'Implementation and experiment RUN-001', 'reviewer'),
            ('reviewer', 'review', 'Revise ISSUE-1: unfair baseline budget', 'researcher'),
            ('researcher', 'design', 'Plan v2 aligns resource budgets', 'engineer'),
            ('engineer', 'dev', 'RUN-002 with comparable budgets', 'reviewer'),
            ('reviewer', 'review', 'Pass independent reproduction of RUN-002', 'researcher'),
            ('researcher', 'design', 'Final report with limitations and evidence', 'lead'),
        ]
        for role, stage, outcome, target in stages:
            actor = members[role]
            self.assertTrue(taskpool.task_claim(project, actor)['claimed'])
            self.assertEqual(taskpool.task_complete(project, actor, tid, result='Early closure').get('code'),
                             'permission_denied')
            logged = taskpool.task_log(project, actor, tid, stage, result=outcome,
                                       meta={'scope': 'isolated fixture', 'experiment': 'RUN-002'},
                                       next_bot=members[target], expected_role_id=role_ids[target])
            self.assertTrue(logged['logged'], logged)
        self.assertTrue(taskpool.task_complete(project, lead, tid, result='Fixture user acceptance')['completed'])
        self.assertTrue(taskpool.req_complete(project, lead, req['requirement_id'])['completed'])


if __name__ == '__main__':
    unittest.main()
