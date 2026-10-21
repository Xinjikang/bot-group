"""Six-step creation/import with real Git in disposable projects and SQLite."""
import sqlite3
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
import tomlkit

from engine import project_import, project_import_git as git, project_setup_git as setup
from scripts import test_project_import_supplement as supplement_tests
from scripts.test_project_import import fingerprints


class SetupTests(unittest.TestCase):
    setUp = supplement_tests.SupplementTests.setUp
    configured = supplement_tests.SupplementTests.configured
    reviewed = supplement_tests.SupplementTests.reviewed
    submit = supplement_tests.SupplementTests.submit
    def configured_setup(self, new=False):
        body = self.configured()
        body.update(git_setup=True, initialize_git=True, directory_mode='worktree',
                    worktree_root=str(self.base / 'worktrees'))
        if new:
            body.update(root=str(self.base / 'fresh'), creation_mode='new')
        return body

    def test_wizard_codex_members_receive_assigned_directory_grants(self):
        for creation in ('new', 'import'):
            for mode in ('shared', 'worktree'):
                with self.subTest(creation=creation, directory_mode=mode):
                    root = self.base / (creation + '-' + mode)
                    if creation == 'import':
                        root.mkdir()
                    body = self.configured_setup(new=creation == 'new')
                    body.update(root=str(root), directory_mode=mode, creation_mode=creation,
                                initialize_git=mode == 'worktree',
                                worktree_root=str(self.base / ('trees-' + creation)) if mode == 'worktree' else '')
                    body['members'].append({'bot_id': self.ids[1], 'role': body['roles'][1]['name'], 'permissions': []})
                    body, _ = self.reviewed(body)
                    response = self.submit(body)
                    self.assertEqual(response.status_code, 200, response.text)
                    project = response.json()['project']
                    selected = root
                    if mode == 'worktree':
                        row = self.conn.execute('SELECT w.path FROM project_workspaces w JOIN project_members m '
                            'ON m.project=w.project AND m.profile=w.profile WHERE w.project=? AND m.bot_id=?',
                            (project, self.ids[1])).fetchone()
                        self.assertIsNotNone(row)
                        selected = Path(row['path'])
                    config = tomlkit.parse((self.base / 'codex' / 'author.config.toml').read_text(encoding='utf-8'))
                    self.assertEqual(config['permissions'][config['default_permissions']]['filesystem'][selected.as_posix()], 'write')
                    if mode == 'worktree':
                        self.assertEqual(config['permissions'][config['default_permissions']]['filesystem'][(root / 'reports').as_posix()], 'write')

    def test_new_missing_directory_previews_then_creates_real_workspaces(self):
        body, plan = self.reviewed(self.configured_setup(new=True))
        root = self.base / 'fresh'
        self.assertFalse(root.exists())
        self.assertTrue(plan['git_plan']['initialize'])
        self.assertIn('AGENTS.md', plan['git_plan']['selected'])
        self.assertFalse(any(p.startswith('reports/') for p in plan['git_plan']['selected']))
        self.assertIn('docs/requirements/README.md', plan['git_plan']['selected'])
        self.assertIn('docs/design/README.md', plan['git_plan']['selected'])
        self.assertIn('docs/ui-spec/UI-SPEC.md', plan['git_plan']['selected'])
        response = self.submit(body)
        self.assertEqual(response.status_code, 200, response.text)
        head = setup.checked(root, 'rev-parse', 'HEAD')
        self.assertEqual(setup.checked(root, 'branch', '--show-current'), 'main')
        self.assertEqual(setup.checked(root, 'status', '--porcelain'), '')
        self.assertEqual(setup.checked(root, 'show', 'HEAD:AGENTS.md'), (root / 'AGENTS.md').read_text(encoding='utf-8').strip())
        rows = self.conn.execute('SELECT path FROM project_workspaces WHERE project=?', (response.json()['project'],)).fetchall()
        self.assertEqual(len(rows), 1)
        for row in rows:
            self.assertEqual(setup.checked(row['path'], 'rev-parse', 'HEAD'), head)
        self.assertEqual(setup.checked(root, 'remote'), '')
        self.assertTrue((root / 'reports' / 'INDEX.md').is_file())
        self.assertFalse(setup.checked(root, 'ls-files', '--', 'reports'))
        for row in rows:
            self.assertFalse((Path(row['path']) / 'reports').exists())
            self.assertTrue((Path(row['path']) / 'docs/requirements/README.md').is_file())
            self.assertTrue((Path(row['path']) / 'docs/ui-spec/UI-SPEC.md').is_file())
            from engine import wake
            prompt = wake.wake_system_prompt('work', response.json()['project'], row['path'])
            import json
            context = json.JSONDecoder().raw_decode(prompt.split('## 当前项目\n')[1])[0]
            self.assertEqual(context['项目主目录'], str(root))
            self.assertNotIn('报告目录', context)

    def test_explicit_report_selection_rejected_before_publication(self):
        body, plan = self.reviewed(self.configured_setup(new=True))
        body['commit_files'] = plan['git_plan']['selected'] + ['reports/INDEX.md']
        response = self.client.post('/projects/import/preview', json=body)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertFalse((self.base / 'fresh').exists())

    def test_new_rejects_existing_files_at_first_step(self):
        before = fingerprints(self.root)
        response = self.client.post('/projects/import/preview', json={
            'root': str(self.root), 'creation_mode': 'new', 'template': {'id': 'software'}})
        self.assertEqual(response.status_code, 409)
        self.assertIn('不能在已有项目上新建', response.text)
        self.assertEqual(fingerprints(self.root), before)

    def test_new_shared_blank_template(self):
        body = self.configured_setup(new=True)
        body.update(template={'id': 'blank', 'version': '1.0.0'}, directory_mode='shared', initialize_git=False)
        body, _ = self.reviewed(body)
        response = self.submit(body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue((self.base / 'fresh' / 'AGENTS.md').exists())
        self.assertFalse((self.base / 'fresh' / '.git').exists())

    def test_new_templates_omit_import_reminder_in_result_and_receipt(self):
        for template in ('research', 'software', 'writing', 'product-prototype', 'algorithm', 'testing', 'blank'):
            with self.subTest(template=template):
                root = self.base / ('new-' + template)
                body = {**self.configured(template), 'root': str(root),
                        'creation_mode': 'new', 'git_setup': True,
                        'request_id': str(uuid.uuid4())}
                body, plan = self.reviewed(body)
                body['agents_edit'] = plan['agents_draft']
                body, _ = self.reviewed(body)
                response = self.submit(body)
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                self.assertEqual(result['readiness_status'], 'ready')
                self.assertTrue((root / 'AGENTS.md').is_file())
                self.assertNotIn('review_reminder', result)
                self.assertEqual(self.submit(body).json(), result)
                progress = self.client.get('/projects/setup-progress/' + body['request_id'])
                self.assertEqual(progress.json()['result'], result)

    def test_import_initial_commit_excludes_secrets_and_selected_file(self):
        (self.root / '.env').write_text('SECRET=keep-local', encoding='utf-8')
        (self.root / 'source.py').write_text('print(1)', encoding='utf-8')
        body, plan = self.reviewed(self.configured_setup())
        body['agents_edit'] = plan['agents_draft']
        self.assertNotIn('.env', plan['git_plan']['selected'])
        body['commit_files'] = [p for p in plan['git_plan']['selected'] if p != 'code.bin']
        body, _ = self.reviewed(body)
        response = self.submit(body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('已有项目文档与测试脚本已保留', response.json()['review_reminder'])
        tracked = setup.checked(self.root, 'ls-tree', '-r', '--name-only', 'HEAD').splitlines()
        self.assertIn('source.py', tracked)
        self.assertNotIn('.env', tracked)
        self.assertNotIn('code.bin', tracked)
        self.assertTrue((self.root / '.env').exists())

    def test_existing_head_keeps_index_and_local_changes(self):
        setup.checked(self.root, 'init')
        setup.checked(self.root, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '--allow-empty', '-m', 'initial')
        (self.root / 'staged.txt').write_text('staged', encoding='utf-8')
        setup.checked(self.root, 'add', 'staged.txt')
        index = (self.root / '.git' / 'index').read_bytes()
        head = setup.checked(self.root, 'rev-parse', 'HEAD')
        body = {**self.configured_setup(), 'initialize_git': False, 'git_baseline': 'existing'}
        body, _ = self.reviewed(body)
        response = self.submit(body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(setup.checked(self.root, 'rev-parse', 'HEAD'), head)
        self.assertEqual((self.root / '.git' / 'index').read_bytes(), index)

    def test_selected_commit_preserves_unrelated_staged_content(self):
        setup.checked(self.root, 'init')
        setup.checked(self.root, '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '--allow-empty', '-m', 'initial')
        (self.root / 'staged.txt').write_text('staged version', encoding='utf-8')
        setup.checked(self.root, 'add', 'staged.txt')
        (self.root / 'staged.txt').write_text('working version', encoding='utf-8')
        body = {**self.configured_setup(), 'initialize_git': False, 'git_baseline': 'commit'}
        body, plan = self.reviewed(body)
        body['commit_files'] = [p for p in plan['git_plan']['selected'] if p != 'staged.txt']
        body, _ = self.reviewed(body)
        response = self.submit(body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(setup.checked(self.root, 'show', ':staged.txt'), 'staged version')
        self.assertEqual((self.root / 'staged.txt').read_text(), 'working version')
        self.assertNotEqual(git.run(self.root, 'show', 'HEAD:staged.txt').returncode, 0)

    def test_race_in_selected_file_requires_new_preview(self):
        body, _ = self.reviewed(self.configured_setup())
        (self.root / 'doc' / 'design.md').write_text('changed after preview', encoding='utf-8')
        self.assertEqual(self.submit(body).status_code, 409)
        self.assertFalse((self.root / '.git').exists())

    def test_failure_after_git_rolls_back_files_index_and_workspaces(self):
        body, _ = self.reviewed(self.configured_setup())
        before = fingerprints(self.root)
        audit = project_import.ws.audit
        def fail(conn, project, actor, action, detail):
            if action == 'project.import':
                raise sqlite3.IntegrityError('injected final failure')
            return audit(conn, project, actor, action, detail)
        with patch.object(project_import.ws, 'audit', side_effect=fail):
            with self.assertRaises(sqlite3.IntegrityError):
                self.submit(body)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0], 0)
        self.assertFalse((self.base / 'worktrees').exists())
        # Git objects can remain after a rollback; original files must survive.
        after = fingerprints(self.root)
        for name, digest in before.items():
            self.assertEqual(after[name], digest)
        self.assertFalse(git.inspect(self.root)['has_commit'])
        self.assertEqual(git.inspect(self.root)['status'], 'existing')
        # A new reviewed attempt reuses the intact, empty repository.
        body.update(initialize_git=False)
        body, _ = self.reviewed(body)
        self.assertEqual(self.submit(body).status_code, 200)

    def test_custom_ignore_is_respected_before_initializing(self):
        (self.root / '.gitignore').write_text('private/\n*.local\n', encoding='utf-8')
        (self.root / 'private').mkdir()
        (self.root / 'private' / 'credentials.txt').write_text('local', encoding='utf-8')
        (self.root / 'config.local').write_text('local', encoding='utf-8')
        body, plan = self.reviewed(self.configured_setup())
        self.assertNotIn('config.local', plan['git_plan']['selected'])
        self.assertNotIn('private/credentials.txt', plan['git_plan']['selected'])
        self.assertEqual(self.submit(body).status_code, 200)

    def test_durable_receipt_replays_exact_request_and_rejects_changed_input(self):
        body, _ = self.reviewed(self.configured_setup(new=True))
        body['request_id'] = str(uuid.uuid4())
        first = self.submit(body)
        self.assertEqual(first.status_code, 200, first.text)
        replay = self.submit(body)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0], 1)
        progress = self.client.get('/projects/setup-progress/' + body['request_id'])
        self.assertEqual(progress.json()['status'], 'ready')
        self.assertEqual(progress.json()['result']['project'], first.json()['project'])
        self.assertEqual(self.submit({**body, 'description': 'different'}).status_code, 409)


if __name__ == '__main__':
    import unittest
    unittest.main()
