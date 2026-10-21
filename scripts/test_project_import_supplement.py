"""Import supplements against isolated HTTP/SQLite and temporary projects."""
import copy
import json
import os
import shutil
import sqlite3
import unittest
from unittest.mock import patch

from engine import project_import, project_import_files as supplements
from scripts import test_project_import as import_tests
from scripts.test_project_import import fingerprints
from scripts.test_project_readiness import snapshot


class SupplementTests(unittest.TestCase):
    setUp = import_tests.ImportTests.setUp
    submit = import_tests.ImportTests.submit

    def configured(self, template='software', supplement=True):
        response = self.client.post('/projects/import/preview', json={'root': str(self.root), 'template': {'id': template}})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        return {**self.body, 'template': data['template'], 'supplement': supplement,
                'roles': data['default_roles'], 'members': [
                    {'bot_id': self.ids[0], 'role': data['owner_role'], 'permissions': []}]}

    def reviewed(self, body):
        response = self.client.post('/projects/import/preview', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        plan = response.json()['file_plan']
        return {**body, 'plan_hash': plan['plan_hash']}, plan

    def test_project_reference_uses_latest_type_for_new_and_imported_projects(self):
        templates = supplements.templates
        copied = self.base / 'templates'
        shutil.copytree(templates.TEMPLATE_ROOT, copied)
        with patch.object(templates, 'TEMPLATE_ROOT', copied):
            projects = []
            for mode in ('new', 'import'):
                for kind in ('software', 'research', 'writing', 'product-prototype', 'algorithm', 'blank'):
                    root = self.base / (mode + '-' + kind)
                    root.mkdir()
                    body = {**self.configured(kind, supplement=mode == 'new'), 'root': str(root), 'creation_mode': mode}
                    body, _ = self.reviewed(body)
                    response = self.submit(body)
                    self.assertEqual(response.status_code, 200, response.text)
                    projects.append((response.json()['project'], kind))
            untyped = self.submit().json()['project']
            projects.append((untyped, 'blank'))
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM project_initializations').fetchone()[0], 0)
            # Publish newer resources after creation, with different role text.
            for kind in ('software', 'research', 'writing', 'product-prototype', 'algorithm'):
                path = copied / kind / 'manifest.json'
                manifest = json.loads(path.read_text(encoding='utf-8'))
                manifest['version'] = '99.0.0'
                manifest['roles'][0]['name'] += '最新'
                path.write_text(json.dumps(manifest), encoding='utf-8')
                soul = copied / kind / manifest['roles'][0]['soul_reference']
                soul.write_text('# Latest ' + kind + ' reference', encoding='utf-8')
            before = snapshot(self.conn)
            for project, kind in projects:
                response = self.client.get('/projects/' + project + '/template-reference')
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['project'], project)
                if kind == 'blank':
                    self.assertIsNone(response.json()['template'])
                else:
                    result = response.json()['template']
                    self.assertEqual(result['id'], kind)
                    self.assertEqual(result['version'], '99.0.0')
                    self.assertEqual(result['references'], templates.load(kind)['references'])
            self.assertEqual(snapshot(self.conn), before)

    def test_preserve_existing_and_reuse_doc_with_real_owner_grants(self):
        before = fingerprints(self.root)
        configured = self.configured()
        configured['owner_bot_id'] = None  # The role alone selects the notification owner.
        body, plan = self.reviewed(configured)
        self.assertEqual(fingerprints(self.root), before)
        self.assertEqual(next(f for f in plan['files'] if f['path'] == 'AGENTS.md')['action'], 'preserve')
        self.assertIn('doc/INDEX.md', [f['path'] for f in plan['files']])
        response = self.submit(body)
        self.assertEqual(response.status_code, 200, response.text)
        after = fingerprints(self.root)
        from engine.task_retry import project_owner
        self.assertEqual(project_owner(self.conn, response.json()['project']), response.json()['owner_profile'])
        self.assertTrue(response.json()['owner_profile'])
        self.assertTrue(all(after[k] == v for k, v in before.items()))
        self.assertFalse((self.root / 'docs').exists())
        for item in plan['files']:
            if item['action'] == 'create':
                self.assertEqual((self.root / item['path']).read_text(encoding='utf-8'), item['content'])
        from engine import project_permissions
        for permission in supplements.OWNER_PERMISSIONS:
            self.assertIsNone(project_permissions.check(self.conn, response.json()['project'], response.json()['owner_profile'], permission))
        self.assertIsNotNone(project_permissions.check(self.conn, response.json()['project'], response.json()['owner_profile'], 'task.create'))

    def test_software_ui_spec_import_creates_missing_and_preserves_existing(self):
        for document_dir in ('docs', 'doc'):
            for existing in (False, True):
                with self.subTest(document_dir=document_dir, existing=existing):
                    root = self.base / f'ui-{document_dir}-{existing}'
                    spec = root / document_dir / 'ui-spec' / 'UI-SPEC.md'
                    spec.parent.mkdir(parents=True)
                    original = '# Existing brand\n\nKeep custom components.\n'
                    if existing:
                        spec.write_text(original, encoding='utf-8')
                    body = {**self.configured(), 'root': str(root)}
                    body, plan = self.reviewed(body)
                    entry = next(f for f in plan['files']
                                 if f['path'] == f'{document_dir}/ui-spec/UI-SPEC.md')
                    self.assertEqual(entry['action'], 'preserve' if existing else 'create')
                    self.assertEqual(spec.exists(), existing)
                    response = self.submit(body)
                    self.assertEqual(response.status_code, 200, response.text)
                    content = spec.read_text(encoding='utf-8')
                    self.assertEqual(content, original if existing else entry['content'])
                    if not existing:
                        self.assertIn('组件与交互状态', content)
                        self.assertNotIn('{{ROLE_', content)
                    other_dir = 'docs' if document_dir == 'doc' else 'doc'
                    self.assertFalse((root / other_dir).exists())

    def test_algorithm_import_preserves_layout_reports_and_existing_documents(self):
        for document_dir in ('docs', 'doc'):
            with self.subTest(document_dir=document_dir):
                root = self.base / ('algorithm-' + document_dir)
                originals = {
                    document_dir + '/design/ALGORITHM.md': b'# Existing algorithm plan\r\n',
                    'examples/demo.py': b'print("custom demo")\r\n',
                    'settings/trial.toml': b'seed = 23\r\n',
                    'reports/REPORT.md': b'# Existing research report\r\n',
                }
                for path, content in originals.items():
                    target = root / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                body = {**self.configured('algorithm'), 'root': str(root)}
                body, plan = self.reviewed(body)
                preserved = {f['path'] for f in plan['files'] if f['action'] == 'preserve'}
                self.assertIn(document_dir + '/design/ALGORITHM.md', preserved)
                self.assertIn('reports/REPORT.md', preserved)
                response = self.submit(body)
                self.assertEqual(response.status_code, 200, response.text)
                for path, content in originals.items():
                    self.assertEqual((root / path).read_bytes(), content)
                self.assertTrue((root / document_dir / 'literature/REVIEW.md').is_file())
                self.assertTrue((root / 'reports/experiments/EXPERIMENT.md').is_file())
                agents = (root / 'AGENTS.md').read_text(encoding='utf-8')
                self.assertIn(f'{document_dir}/design/', agents)
                self.assertFalse((root / ('docs' if document_dir == 'doc' else 'doc')).exists())
                for directory in ('src', 'config', 'configs', document_dir + '/results', document_dir + '/experiments'):
                    self.assertFalse((root / directory).exists(), directory)

    def test_all_templates_generate_agents_and_use_configured_permissions(self):
        for template in ('software', 'research', 'writing', 'product-prototype', 'algorithm'):
            with self.subTest(template=template):
                root = self.base / template
                root.mkdir()
                body = {**self.configured(template), 'root': str(root)}
                body['roles'][0]['permissions'] = ['req.create']
                body, plan = self.reviewed(body)
                self.assertFalse(list(root.iterdir()))
                response = self.submit(body)
                self.assertEqual(response.status_code, 200, response.text)
                content = (root / 'AGENTS.md').read_text(encoding='utf-8')
                self.assertRegex(content, body['roles'][0]['name'] + r'：[^\n]+默认权限：req.create')
                self.assertNotIn('{{', content)
                self.assertTrue((root / 'docs' / 'INDEX.md').is_file())

    def test_opt_out_keeps_all_files(self):
        before = fingerprints(self.root)
        body, plan = self.reviewed(self.configured(supplement=False))
        self.assertEqual(plan['files'], [])
        self.assertEqual(self.submit(body).status_code, 200)
        self.assertEqual(before, fingerprints(self.root))

    def test_prototype_doc_reuse_preserves_existing_design_and_legacy_materials(self):
        import re
        legacy = self.root / 'internal' / 'requirements' / 'PRD.md'
        legacy.parent.mkdir(parents=True)
        legacy.write_text('Existing project PRD', encoding='utf-8')
        old_spec = self.root / 'doc' / 'ui-spec.md'
        old_spec.write_text('# Previous UI rules', encoding='utf-8')
        old_prd = self.root / 'doc' / 'PRD.md'
        old_prd.write_text('# Previous PRD', encoding='utf-8')
        ui_spec = self.root / 'ui-spec' / 'UI-SPEC.md'
        ui_spec.parent.mkdir()
        ui_spec.write_text('# Existing brand rules', encoding='utf-8')
        before = fingerprints(self.root)
        body, plan = self.reviewed(self.configured('product-prototype'))
        self.assertFalse(any(f['path'].startswith(('internal/', 'scripts/')) for f in plan['files']))
        self.assertEqual(next(f['action'] for f in plan['files'] if f['path'] == 'ui-spec/UI-SPEC.md'), 'preserve')
        response = self.submit(body)
        self.assertEqual(response.status_code, 200, response.text)
        after = fingerprints(self.root)
        self.assertTrue(all(after[k] == v for k, v in before.items()))
        self.assertFalse((self.root / 'docs').exists())
        self.assertTrue((self.root / 'outputs' / 'PRD.md').is_file())
        self.assertTrue((self.root / 'doc' / 'design' / 'UX.md').is_file())
        self.assertTrue((self.root / 'materials' / 'references' / 'README.md').is_file())
        for item in plan['files']:
            if item['action'] == 'create':
                path = self.root / item['path']
                text = path.read_text(encoding='utf-8')
                self.assertNotIn('internal/', text)
                for link in re.findall(r'\]\(([^)]+)\)', text):
                    self.assertTrue((path.parent / link).is_file(), (item['path'], link))

    def test_research_owner_retains_task_grants_and_notification_identity(self):
        from engine import project_permissions
        from engine.task_retry import project_owner
        root = self.base / 'Research'; root.mkdir()
        body = {**self.configured('research'), 'root': str(root)}
        self.assertEqual([r['name'] for r in body['roles']], ['调研负责人', '资料研究员', '分析员', '复核员'])
        self.assertEqual(set(body['roles'][0]['permissions']), set(project_permissions.PERMISSIONS))
        self.assertTrue(all(r['permissions'] == [] for r in body['roles'][1:]))
        body['members'].append({'bot_id': self.ids[1], 'role': '资料研究员', 'permissions': []})
        body['owner_bot_id'] = self.ids[1]  # The owner role determines the actual notification target.
        duplicate = copy.deepcopy(body)
        duplicate['members'][1]['role'] = '调研负责人'
        before = snapshot(self.conn)
        self.assertEqual(self.submit(duplicate).status_code, 400)
        self.assertEqual(snapshot(self.conn), before)
        body, plan = self.reviewed(body)
        response = self.submit(body)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        owner = result['owner_profile']
        self.assertEqual(project_owner(self.conn, result['project']), owner)
        row = self.conn.execute('SELECT m.bot_id,pm.role FROM project_members pm JOIN members m ON m.profile=pm.profile '
                                'WHERE pm.project=? AND pm.profile=?', (result['project'], owner)).fetchone()
        self.assertEqual((row['bot_id'], row['role']), (self.ids[0], '调研负责人'))
        for permission in project_permissions.PERMISSIONS:
            self.assertIsNone(project_permissions.check(self.conn, result['project'], owner, permission))
        content = (root / 'AGENTS.md').read_text(encoding='utf-8')
        self.assertEqual(content.count('- 调研负责人：'), 1)
        self.assertNotIn('- 项目负责人：', content)
        for text in ('明确调研问题和时间范围', '调研负责人是本项目的负责人',
                     '接收并跟进任务阻塞通知', '调研负责人组织澄清，按用户授权建立需求、拆解任务',
                     '调研负责人验收报告并关闭任务和需求'):
            self.assertIn(text, content)
        self.assertEqual(content, plan['agents_draft']['content'])
        for name in ('资料研究员', '分析员', '复核员'):
            self.assertRegex(content, name + r'：[^\n]+默认权限：无。')
        self.assertIn('资料研究员、分析员和复核员的默认管理权限为空', content)

    def test_template_owner_names_grants_and_stable_role_identity(self):
        from engine import project_permissions, project_team
        from engine.task_retry import project_owner
        for template, name, count in (('blank', '项目负责人', 5), ('software', '项目负责人', 5),
                                      ('research', '调研负责人', 10), ('writing', '主编', 10),
                                      ('product-prototype', '产品经理', 10),
                                      ('algorithm', '研发负责人', 10)):
            with self.subTest(template=template):
                root = self.base / ('owner-' + template); root.mkdir()
                body = {**self.configured(template), 'root': str(root)}
                self.assertEqual(body['roles'][0]['name'], name)
                self.assertEqual(len(body['roles'][0]['permissions']), count)
                if template in ('research', 'writing', 'product-prototype', 'algorithm'):
                    self.assertEqual(len(body['roles']), 4)
                    self.assertTrue(all(r['permissions'] == [] for r in body['roles'][1:]))
                missing = copy.deepcopy(body)
                missing['roles'] = missing['roles'][1:]; missing['members'] = []; missing['owner_bot_id'] = None
                self.assertEqual(self.submit(missing).status_code, 400)
                body, plan = self.reviewed(body)
                result = self.submit(body)
                self.assertEqual(result.status_code, 200, result.text)
                project = result.json()['project']
                owner = project_owner(self.conn, project)
                self.assertTrue(owner)
                actual = [p for p in project_permissions.PERMISSIONS if project_permissions.check(self.conn, project, owner, p) is None]
                self.assertEqual(set(actual), set(body['roles'][0]['permissions']))
                roles = self.client.get(f'/projects/{project}/roles').json()['roles']
                owners = [r for r in roles if r['is_owner']]
                self.assertEqual([r['name'] for r in owners], [name])
                owner_id = owners[0]['id']
                self.assertEqual(project_team.owner_role_id(self.conn, project), owner_id)
                self.assertEqual(self.client.patch(f'/projects/{project}/roles/{owner_id}', json={'name': '自定义负责人'}).status_code, 200)
                renamed = self.client.get(f'/projects/{project}/roles').json()['roles']
                self.assertEqual([r['name'] for r in renamed if r['is_owner']], ['自定义负责人'])
                self.assertEqual(project_owner(self.conn, project), owner)

    def test_preview_required_and_configuration_or_disk_changes_rejected(self):
        body = self.configured()
        self.assertEqual(self.submit(body).status_code, 409)
        body, _ = self.reviewed(body)
        changed = {**body, 'description': 'Edited after preview'}
        self.assertEqual(self.submit(changed).status_code, 409)
        (self.root / 'README.md').write_text('Concurrent user content', encoding='utf-8')
        before = fingerprints(self.root)
        self.assertEqual(self.submit(body).status_code, 409)
        self.assertEqual(before, fingerprints(self.root))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0], 0)

    def test_database_failure_removes_only_new_files(self):
        body, _ = self.reviewed(self.configured())
        before, rows = fingerprints(self.root), snapshot(self.conn)
        original = project_import.ws.audit
        def failure(conn, project, actor, action, detail):
            if action == 'project.import':
                raise sqlite3.IntegrityError('Injected final audit failure')
            return original(conn, project, actor, action, detail)
        with patch.object(project_import.ws, 'audit', side_effect=failure):
            with self.assertRaises(sqlite3.IntegrityError):
                self.submit(body)
        self.assertEqual(fingerprints(self.root), before)
        self.assertEqual(snapshot(self.conn), rows)

    def test_mid_publication_failure_rolls_back(self):
        body, _ = self.reviewed(self.configured())
        before = fingerprints(self.root)
        original = os.link
        count = 0
        def failure(source, target):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('Injected storage failure')
            return original(source, target)
        with patch.object(supplements.os, 'link', side_effect=failure):
            self.assertEqual(self.submit(body).status_code, 409)
        self.assertEqual(fingerprints(self.root), before)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0], 0)

    def test_role_contract_and_path_conflicts(self):
        body = self.configured()
        wrong = copy.deepcopy(body)
        wrong['members'][0]['role'] = '协调者'
        self.assertEqual(self.client.post('/projects/import/preview', json=wrong).status_code, 400)
        wrong = copy.deepcopy(body)
        wrong['roles'] = wrong['roles'][:1]
        self.assertEqual(self.client.post('/projects/import/preview', json=wrong).status_code, 200)
        (self.root / 'scripts').write_text('An existing file', encoding='utf-8')
        response = self.client.post('/projects/import/preview', json=body)
        self.assertEqual(response.status_code, 409, response.text)

    def test_publish_race_preserves_concurrent_file_and_rollback_preserves_edits(self):
        body, _ = self.reviewed(self.configured())
        original = supplements.Publication.publish
        def race(publication, plan):
            (self.root / 'README.md').write_text('Concurrent writer', encoding='utf-8')
            return original(publication, plan)
        with patch.object(supplements.Publication, 'publish', race):
            self.assertEqual(self.submit(body).status_code, 409)
        self.assertEqual((self.root / 'README.md').read_text(), 'Concurrent writer')
        self.assertFalse((self.root / 'scripts').exists())
        body, _ = self.reviewed(self.configured())
        audit = project_import.ws.audit
        def failure(conn, project, actor, action, detail):
            if action == 'project.import':
                (self.root / 'doc' / 'INDEX.md').write_text('User edit during commit', encoding='utf-8')
                raise sqlite3.IntegrityError('Injected failure')
            return audit(conn, project, actor, action, detail)
        with patch.object(project_import.ws, 'audit', side_effect=failure):
            with self.assertRaises(sqlite3.IntegrityError):
                self.submit(body)
        self.assertEqual((self.root / 'doc' / 'INDEX.md').read_text(), 'User edit during commit')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0], 0)

    def test_symlink_target_rejected_without_writing_outside(self):
        outside = self.base / 'outside.md'
        outside.write_text('Outside content', encoding='utf-8')
        try:
            (self.root / 'README.md').symlink_to(outside)
        except OSError:
            self.skipTest('Windows symlink permission unavailable')
        body = self.configured()
        response = self.client.post('/projects/import/preview', json=body)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(outside.read_text(), 'Outside content')

    def test_worktree_rules_generated_and_template_version_checked(self):
        import subprocess
        subprocess.run(['git', 'init', str(self.root)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                        'commit', '--allow-empty', '-m', 'Initial'], check=True, capture_output=True)
        body = {**self.configured(), 'directory_mode': 'worktree', 'worktree_root': str(self.base / 'worktrees')}
        # Existing AGENTS stays untouched; engineering documentation carries the mode.
        (self.root / 'AGENTS.md').unlink()
        _, plan = self.reviewed(body)
        agents = next(f['content'] for f in plan['files'] if f['path'] == 'AGENTS.md')
        self.assertIn('独立 Git worktree', agents)
        body['template']['version'] = '0.0.0'
        self.assertEqual(self.client.post('/projects/import/preview', json=body).status_code, 400)

    def test_git_init_preview_readonly_then_local_repo_without_commit_or_remote(self):
        body = {**self.configured(), 'initialize_git': True}
        before = fingerprints(self.root)
        body, plan = self.reviewed(body)
        self.assertEqual(plan['git']['status'], 'missing')
        self.assertTrue(plan['initialize_git'])
        self.assertEqual(before, fingerprints(self.root))
        response = self.submit(body)
        self.assertEqual(response.status_code, 200, response.text)
        state = supplements.git.inspect(self.root)
        self.assertEqual(state['status'], 'existing')
        self.assertFalse(state['has_commit'])
        self.assertEqual(supplements.git.run(self.root, 'remote').stdout.strip(), '')
        self.assertEqual(supplements.git.run(self.root, 'ls-files').stdout.strip(), '')
        self.assertIn('.env', (self.root / '.gitignore').read_text())

    def test_existing_git_and_gitignore_preserved(self):
        supplements.git.run(self.root, 'init')
        (self.root / '.gitignore').write_bytes(b'# custom\r\nprivate/\r\n')
        before = fingerprints(self.root)
        body, plan = self.reviewed(self.configured())
        self.assertEqual(plan['git']['status'], 'existing')
        self.assertFalse(plan['initialize_git'])
        self.assertEqual(self.submit(body).status_code, 200)
        after = fingerprints(self.root)
        self.assertTrue(all(after[k] == v for k, v in before.items()))
        report = self.root / 'reports' / 'research' / 'report.md'
        report.write_text('Private evidence', encoding='utf-8')
        ignored = supplements.git.run(self.root, 'check-ignore', 'reports/research/report.md')
        self.assertEqual(ignored.returncode, 0, ignored.stderr)
        self.assertEqual(supplements.git.run(self.root, 'ls-files', '--', 'reports').stdout, '')

    def test_git_init_and_all_supplements_rollback_on_database_failure(self):
        body, _ = self.reviewed({**self.configured(), 'initialize_git': True})
        before = fingerprints(self.root)
        original = project_import.ws.audit
        def failure(conn, project, actor, action, detail):
            if action == 'project.import':
                self.assertEqual(supplements.git.inspect(self.root)['status'], 'existing')
                raise sqlite3.IntegrityError('Injected failure after Git initialization')
            return original(conn, project, actor, action, detail)
        with patch.object(project_import.ws, 'audit', side_effect=failure):
            with self.assertRaises(sqlite3.IntegrityError):
                self.submit(body)
        self.assertEqual(before, fingerprints(self.root))

    def test_git_init_failure_rolls_back_and_worktree_explains_initial_commit(self):
        import subprocess
        body, _ = self.reviewed({**self.configured(), 'initialize_git': True})
        before = fingerprints(self.root)
        original = supplements.git.run
        def failure(root, *arguments):
            if arguments[0] == 'init':
                return subprocess.CompletedProcess([], 1, '', 'Injected Git init failure')
            return original(root, *arguments)
        with patch.object(supplements.git, 'run', side_effect=failure):
            self.assertEqual(self.submit(body).status_code, 409)
        self.assertEqual(before, fingerprints(self.root))
        body.update(directory_mode='worktree', worktree_root=str(self.base / 'worktrees'))
        response = self.client.post('/projects/import/preview', json=body)
        self.assertEqual(response.status_code, 400)
        self.assertIn('初始提交', response.text)
        supplements.git.run(self.root, 'init')
        body['initialize_git'] = False
        response = self.client.post('/projects/import/preview', json=body)
        self.assertEqual(response.status_code, 400)
        self.assertIn('初始提交', response.text)

    def test_parent_repository_and_preview_race_are_preserved(self):
        body, _ = self.reviewed({**self.configured(), 'initialize_git': True})
        supplements.git.run(self.base, 'init')
        self.assertEqual(supplements.git.inspect(self.root)['status'], 'parent')
        self.assertEqual(self.submit(body).status_code, 409)
        self.assertFalse((self.root / '.git').exists())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
