"""Prototype artifact layout, UI baseline, roles and revision handoffs."""
from pathlib import Path
import subprocess
import unittest

from engine import project_permissions as permissions, project_templates as templates, taskpool
from scripts import test_project_templates as fixtures
from scripts.test_task_brief import BRIEF
from scripts.unified_test_support import confirmed_requirement_fields


class ProductPrototypeTests(unittest.TestCase):
    setUp = fixtures.TemplateTests.setUp
    body = fixtures.TemplateTests.body
    preview = fixtures.TemplateTests.preview
    submit = fixtures.TemplateTests.submit

    def test_modes_rename_preview_and_versioned_design_documents(self):
        manifest = templates.load('product-prototype')
        self.assertEqual(manifest['owner_role_id'], 'pm')
        self.assertEqual([r['id'] for r in manifest['roles']], ['pm', 'ux', 'prototyper', 'reviewer'])
        self.assertTrue(all(r['required'] for r in manifest['roles']))
        self.assertEqual(set(manifest['roles'][0]['permissions']), set(permissions.PERMISSIONS))
        self.assertTrue(all(r['permissions'] == [] for r in manifest['roles'][1:]))
        for mode in ('shared', 'worktree'):
            with self.subTest(mode=mode):
                self.root = self.base / mode
                self.root.mkdir()
                if mode == 'worktree':
                    subprocess.run(['git', 'init', str(self.root)], check=True, capture_output=True)
                    subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Test',
                                    '-c', 'user.email=test@example.test', 'commit', '--allow-empty',
                                    '-m', 'Fixture'], check=True, capture_output=True)
                body = self.body('product-prototype')
                body.update(directory_mode=mode, worktree_root=str(self.base / 'trees') if mode == 'worktree' else '')
                for role in body['roles']:
                    role['name'] = 'Custom-' + role['id']
                response = self.preview(body)
                self.assertEqual(response.status_code, 200, response.text)
                plan = response.json()
                files = {f['path']: f['content'] for f in plan['files']}
                self.assertFalse(any(p.startswith(('internal/', 'scripts/', 'drafts/', 'prototype/')) for p in files))
                self.assertEqual({p.split('/')[0] for p in files},
                                 {'AGENTS.md', 'README.md', 'docs', 'materials', 'ui-spec', 'outputs'})
                self.assertNotIn('docs/ENGINEERING.md', files)
                for text in [*files.values(), plan['template']['workflow']]:
                    self.assertNotIn('{{', text)
                    self.assertNotIn('internal/', text)
                    self.assertNotIn('REVIEW.md', text)
                    self.assertNotIn('ACCEPTANCE.md', text)
                    for role in manifest['roles']:
                        self.assertNotIn(role['name'], text)
                agents = files['AGENTS.md']
                for text in ('Custom-pm是本项目的唯一负责人', 'Custom-ux', 'Custom-prototyper',
                             'Custom-reviewer', '信息架构', '待验证假设', '用户实际走查',
                             '修订完成后回到Custom-reviewer复审', 'logged=true',
                             'ui-spec/UI-SPEC.md', 'UI 规范独立操作原型',
                             '审查、返修、复审和验收结论统一维护在 task node'):
                    self.assertIn(text, agents)
                self.assertEqual('一次只允许一名执行者' in agents, mode == 'shared')
                self.assertEqual('detached HEAD 时，先建立任务分支' in agents, mode == 'worktree')
                self.assertNotIn('架构师', agents)
                result = self.submit(plan).json()
                self.assertEqual(result['status'], 'ready', result)
                for path, content in files.items():
                    self.assertEqual((self.root / path).read_bytes(), content.encode('utf-8'))
                if mode == 'shared':
                    subprocess.run(['git', 'init', str(self.root)], check=True, capture_output=True)
                subprocess.run(['git', '-C', str(self.root), 'add', '-A'], check=True, capture_output=True)
                tracked = subprocess.check_output(['git', '-C', str(self.root), 'ls-files'], text=True).splitlines()
                self.assertIn('outputs/prototype/README.md', tracked)
                self.assertIn('docs/validation/CHECKLIST.md', tracked)
                for path in ('outputs/PRD.md', 'materials/RESEARCH.md', 'docs/design/UX.md',
                             'ui-spec/UI-SPEC.md', 'docs/validation/COVERAGE.md',
                             'materials/references/README.md'):
                    self.assertIn(path, tracked)
                self.assertFalse((self.root / 'internal').exists())
                self.assertFalse((self.root / 'scripts').exists())
                self.assertFalse((self.root / 'prototype').exists())
                spec = files['ui-spec/UI-SPEC.md']
                for text in ('默认基线，可直接采用', 'Custom-ux', 'Custom-prototyper', 'Custom-reviewer',
                             '--color-primary:', '字体', '间距', '响应', '组件规则', '焦点', '版本记录'):
                    self.assertIn(text, spec)
                for path in ('outputs/PRD.md', 'docs/design/UX.md', 'docs/validation/CHECKLIST.md'):
                    self.assertIn('/ui-spec/UI-SPEC.md)', files[path])
                restored = self.client.get('/projects/initializations/' + plan['initialization_id']).json()
                self.assertEqual(restored['template'], result['template'])

    def test_template_resources_match_manifest_and_have_no_stale_directories(self):
        base = Path(__file__).resolve().parents[1] / 'templates/projects/product-prototype'
        manifest = templates.load('product-prototype')
        sources = {f['source'] for f in manifest['files'] if f['source'].startswith('files/')}
        self.assertEqual({p.relative_to(base).as_posix() for p in (base / 'files').rglob('*') if p.is_file()}, sources)
        self.assertEqual({p.name for p in (base / 'files').iterdir()}, {'docs', 'materials', 'ui-spec', 'outputs'})
        for directory in (base / 'files').rglob('*'):
            if directory.is_dir():
                self.assertTrue(any(directory.iterdir()), directory)

    def test_four_role_revision_loop_and_manager_closure(self):
        plan = self.preview(self.body('product-prototype')).json()
        result = self.submit(plan).json()
        self.assertEqual(result['status'], 'ready', result)
        project = result['project']
        handles = result['applied_team']['member_profiles']
        role_ids = result['applied_team']['role_ids']
        members = {m['role']: handles[str(m['bot_id'])] for m in plan['team']['members']}
        pm = members['pm']
        for permission in permissions.PERMISSIONS:
            self.assertIsNone(permissions.check(self.conn, project, pm, permission))
            for role in ('ux', 'prototyper', 'reviewer'):
                self.assertIsNotNone(permissions.check(self.conn, project, members[role], permission))
        req = taskpool.req_create(project, pm, 'Prototype fixture', **confirmed_requirement_fields(project))
        created = taskpool.task_create(project, pm, req['requirement_id'],
                                       [{'title': 'Prototype user flow', 'next_bot': members['ux'], **BRIEF}],
                                       complete=True)
        tid = created['task_ids'][0]
        stages = [
            ('ux', 'design', 'Research assumptions and UX v1', 'pm'),
            ('pm', 'design', 'Confirmed fixture PRD v1 and UX v1', 'prototyper'),
            ('prototyper', 'dev', 'Fixture prototype v1 self-check', 'reviewer'),
            ('reviewer', 'review', 'Revise ISSUE-1: missing cancel interaction in UX', 'ux'),
            ('ux', 'design', 'UX v2 defines cancel interaction', 'prototyper'),
            ('prototyper', 'dev', 'Fixture prototype v2 implements ISSUE-1', 'reviewer'),
            ('reviewer', 'review', 'Pass ISSUE-1 and affected navigation', 'pm'),
        ]
        for role, stage, outcome, target in stages:
            actor = members[role]
            claimed = taskpool.task_claim(project, actor)
            self.assertTrue(claimed['claimed'], claimed)
            if role != 'pm':
                denied = taskpool.task_update(project, actor, tid, title='Unauthorized scope change')
                self.assertEqual(denied.get('code'), 'permission_denied', denied)
                denied = taskpool.task_complete(project, actor, tid, result='Premature acceptance')
                self.assertEqual(denied.get('code'), 'permission_denied', denied)
            logged = taskpool.task_log(project, actor, tid, stage, result=outcome,
                                       meta={'scope': 'deterministic isolated fixture', 'issue': 'ISSUE-1'},
                                       next_bot=members[target], expected_role_id=role_ids[target])
            self.assertTrue(logged['logged'], logged)
        completed = taskpool.task_complete(project, pm, tid,
                                            result='Fixture walkthrough confirmed; PRD/UX finalized')
        self.assertTrue(completed.get('completed'), completed)
        self.assertTrue(taskpool.req_complete(project, pm, req['requirement_id'])['completed'])
        reviews = self.conn.execute("SELECT result FROM nodes WHERE task_id=? AND type='review' ORDER BY id",
                                    (tid,)).fetchall()
        self.assertEqual([r['result'] for r in reviews], [stages[3][2], stages[6][2]])


if __name__ == '__main__':
    unittest.main()
