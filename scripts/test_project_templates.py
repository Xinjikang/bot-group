"""req#104: static safety + isolated HTTP/SQLite + three actual taskpool flows."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
import tomlkit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import api, db, bot_identity, project_templates as templates, project_initialization as init
from engine import project_permissions as permissions, project_team, taskpool
from engine import project_workspaces as ws
from scripts import test_project_initialization as baseline
from scripts.test_project_import import fingerprints
from scripts.test_project_readiness import snapshot
from scripts.unified_test_support import confirmed_requirement_fields
from scripts.test_task_brief import BRIEF

EVIDENCE = {'static_validation': {}, 'automated_flows': {}}


class TemplateTests(unittest.TestCase):
    def setUp(self):
        baseline.InitializationTests.setUp(self)
        self.ids = []
        self.hosts = self.base / 'hosts'; self.hosts.mkdir()
        for i in range(8):
            home = self.hosts / str(i); home.mkdir()
            for name in ('SOUL.md', 'AGENTS.md', 'config.toml', 'config.yaml', 'auth.json'):
                content = ('sandbox_mode = "workspace-write"\napproval_policy = "on-request"\n'
                           '[sandbox_workspace_write]\nnetwork_access = false\nwritable_roots = []\n'
                           if name == 'config.toml' else 'preserved host config ' + str(i))
                (home / name).write_text(content, encoding='utf-8')
            runtime = 'hermes' if i % 2 else 'codex'
            self.ids.append(self.conn.execute('INSERT INTO bots(host_type,config_root,profile,display_name,runtime) VALUES(?,?,?,?,?)',
                (runtime, str(home), 'default', 'Member ' + str(i), runtime)).lastrowid)
        self.conn.commit()

    def body(self, name='research'):
        manifest = templates.load(name)
        roles = [{k: r[k] for k in ('id', 'name', 'permissions')} for r in manifest['roles']]
        return {'root': str(self.root), 'display_name': 'Isolated ' + name, 'description': 'Minimal verified scenario',
                'template': {'id': name, 'version': manifest['version']}, 'roles': roles,
                'members': [{'bot_id': bot, 'role': r['id'], 'permissions': []} for bot, r in zip(self.ids, roles)],
                'owner_bot_id': self.ids[0]}

    def preview(self, body=None):
        return self.client.post('/projects/initializations/preview', json=body or self.body())

    def submit(self, plan, action='submit'):
        return baseline.InitializationTests.submit(self, plan, action)

    def assert_host_changes(self, before):
        """Enrollment may add project roots and backups; other host data stays intact."""
        after = fingerprints(self.hosts)
        for i, bot_id in enumerate(self.ids):
            if i % 2:
                continue
            memberships = self.conn.execute(
                'SELECT project,profile FROM project_members WHERE bot_id=? ORDER BY rowid', (bot_id,)).fetchall()
            if not memberships:
                continue
            expected_roots = []
            for row in memberships:
                project = ws.project(self.conn, row['project'])
                directory = Path(ws.selected_path(self.conn, project, row['profile'])).resolve()
                reports = Path(project['root']).resolve() / 'reports'
                expected_roots.append(directory.as_posix())
                if not reports.is_relative_to(directory):
                    expected_roots.append(reports.as_posix())
            expected_roots = list(dict.fromkeys(expected_roots))
            config = tomlkit.parse((self.hosts / str(i) / 'config.toml').read_text(encoding='utf-8')).unwrap()
            self.assertEqual(config, {
                'sandbox_mode': 'workspace-write', 'approval_policy': 'on-request',
                'sandbox_workspace_write': {'network_access': False, 'writable_roots': expected_roots}})
            config_key = f'{i}/config.toml'
            after[config_key] = before[config_key]
            backup_key = f'{i}/.group-engine-backups'
            after = {key: value for key, value in after.items()
                     if key != backup_key and not key.startswith(backup_key + '/')}
        self.assertEqual(before, after)

    def test_catalogue_details_validate_every_resource(self):
        result = self.client.get('/projects/templates')
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual([t['id'] for t in result.json()['templates']], ['software', 'research', 'writing', 'product-prototype', 'algorithm', 'testing'])
        for name in ('software', 'research', 'writing', 'product-prototype', 'algorithm', 'testing'):
            detail = self.client.get('/projects/templates/' + name).json()
            self.assertEqual(len(detail['bots']), 8)
            self.assertEqual(len(detail['permissions']), 10)
            self.assertEqual(len(detail['references']), len(detail['roles']))
            if name == 'software':
                roles = {r['id']: r for r in detail['roles']}
                self.assertNotIn('coordinator', roles)
                self.assertEqual(set(roles['architect']['permissions']),
                                 {'task.create', 'task.update', 'task.resume', 'task.complete', 'task.cancel'})
                for role in ('backend', 'frontend', 'reviewer', 'tester'):
                    self.assertEqual(roles[role]['permissions'], [])
                from engine import project_import_files
                _, defaults = project_import_files.defaults({'id': name, 'version': detail['version']})
                self.assertEqual(sum(r['name'] == '项目负责人' for r in defaults), 1)
                self.assertIn('task.cancel', next(r for r in defaults if r['name'] == '架构师')['permissions'])
            EVIDENCE['static_validation'][name] = {'version': detail['version'], 'roles': len(detail['roles']), 'files': len(detail['files']), 'result': 'pass'}

    def test_all_templates_generate_exact_preview_and_valid_links(self):
        before = fingerprints(self.hosts)
        for name in ('software', 'research', 'writing', 'product-prototype', 'algorithm', 'testing'):
            with self.subTest(name=name):
                self.root = self.base / name; self.root.mkdir()
                body = self.body(name); body['roles'][0]['name'] = 'Custom lead'
                plan = self.preview(body).json()
                self.assertEqual(list(self.root.iterdir()), [])
                result = self.submit(plan).json()
                self.assertEqual(result['status'], 'ready', result)
                self.assertTrue(result['team_configured'])
                self.assertEqual(result, self.submit(plan).json())
                for file in plan['files']:
                    path = self.root / file['path']
                    self.assertEqual(path.read_bytes(), file['content'].encode('utf-8'))
                    for link in re.findall(r'\]\(([^)]+)\)', path.read_text(encoding='utf-8')):
                        self.assertTrue((path.parent / link).is_file(), (name, file['path'], link))
                agents = (self.root / 'AGENTS.md').read_text(encoding='utf-8')
                self.assertIn('前置依赖', agents)
                self.assertIn('具体接手人', agents)
                self.assertNotIn('next_bot', agents)
                self.assertNotIn('expected_role_id', agents)
                self.assertEqual(len(result['applied_team']['role_ids']), len(body['roles']))
        self.assert_host_changes(before)
        EVIDENCE['bot_configuration'] = {'files': len(before), 'only_project_grants_and_backups_changed': True}

    def test_nonsoftware_worktree_rules_follow_roles_through_publication(self):
        for name, review_text in (
                ('research', '复核结果写入任务节点'),
                ('writing', '审校结果写入任务节点'),
                ('product-prototype', '评审结果写入任务节点'),
                ('algorithm', '评审结果写入任务节点'),
                ('testing', '评审结果写入任务节点')):
            with self.subTest(template=name):
                self.root = self.base / (name + '-worktree')
                self.root.mkdir()
                subprocess.run(['git', 'init', str(self.root)], check=True, capture_output=True)
                subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Test',
                                '-c', 'user.email=test@example.test', 'commit', '--allow-empty',
                                '-m', 'Fixture'], check=True, capture_output=True)
                body = self.body(name)
                body.update(directory_mode='worktree', worktree_root=str(self.base / (name + '-trees')))
                for role in body['roles']:
                    role['name'] = 'Custom-' + role['id']
                response = self.preview(body)
                self.assertEqual(response.status_code, 200, response.text)
                plan = response.json()
                agents = next(f['content'] for f in plan['files'] if f['path'] == 'AGENTS.md')
                rules = agents.split('使用引擎分配的独立 Git worktree 工作区。', 1)[1].split('\n## ', 1)[0]
                for role in body['roles']:
                    self.assertIn(role['name'], rules)
                for text in ('task/<任务ID>-<阶段>', '上游交付提交', 'detached HEAD 时，先建立任务分支',
                             '已被其他工作区占用', '准确提交', '冲突解决', '远端同步', review_text):
                    self.assertIn(text, rules)
                for text in ('release/', 'feature/', '架构师', '测试员', '协调者', '发布 master', '{{'):
                    self.assertNotIn(text, rules)
                result = self.submit(plan).json()
                self.assertEqual(result['status'], 'ready', result)
                self.assertEqual((self.root / 'AGENTS.md').read_text(encoding='utf-8'), agents)
                restored = self.client.get('/projects/initializations/' + plan['initialization_id']).json()
                self.assertEqual(restored['template'], result['template'])

    def test_software_modes_render_and_publish_distinct_rules(self):
        before = fingerprints(self.hosts)
        for mode in ('shared', 'worktree'):
            with self.subTest(mode=mode):
                self.root = self.base / mode; self.root.mkdir()
                if mode == 'worktree':
                    subprocess.run(['git', 'init', str(self.root)], check=True, capture_output=True)
                    subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Test',
                                    '-c', 'user.email=test@example.test', 'commit', '--allow-empty',
                                    '-m', 'Fixture'], check=True, capture_output=True)
                body = self.body('software')
                body.update(directory_mode=mode, worktree_root=str(self.base / 'trees') if mode == 'worktree' else '')
                for role in body['roles']:
                    role['name'] = 'Custom-' + role['id']
                response = self.preview(body)
                self.assertEqual(response.status_code, 200, response.text)
                plan = response.json()
                self.assertEqual(plan['directory_mode'], mode)
                files = {f['path']: f['content'] for f in plan['files']}
                agents = files['AGENTS.md']
                for text in ('scripts/TEST_INDEX.md', '提交技术方案', '提交本人负责的变更',
                             'codegraph', '测试身份权限', '安全存储引用'):
                    self.assertIn(text, agents)
                self.assertIn('Custom-architect', agents)
                self.assertIn('reports/tests/<release版本>/', agents)
                self.assertIn('报告绝对路径', agents)
                self.assertNotIn('docs/reports/tests/', agents)
                self.assertNotIn('reports/req-', agents)
                self.assertNotRegex(files['docs/INDEX.md'], r'\]\([^)]*reports/')
                self.assertNotIn('架构师', agents)
                self.assertEqual('一次只允许一名执行者' in agents, mode == 'shared')
                self.assertEqual('feature/<任务ID>-<模块>-<功能简述>' in agents, mode == 'worktree')
                self.assertEqual('清理工作区' in agents, mode == 'worktree')
                dev_flow = agents.split('## 开发流程', 1)[1].split('## 独立版本测试流程', 1)[0]
                self.assertNotIn('Custom-tester', dev_flow)
                for text in ('用户发出测试指令', '测试流程以报告交付为止',
                             '开发 → 测试 → 开发 → 测试', '新一轮开发需求',
                             '用户单独发出下一轮测试指令'):
                    self.assertIn(text, agents)
                if mode == 'worktree':
                    for text in ('Custom-reviewer', 'Custom-tester', 'Custom-owner', '冲突', '合并后的版本'):
                        self.assertIn(text, agents)
                engineering = files['docs/engineering/ENGINEERING.md']
                ui_spec = files['docs/ui-spec/UI-SPEC.md']
                self.assertIn('Custom-frontend', ui_spec)
                self.assertIn('Custom-reviewer', ui_spec)
                self.assertIn('Custom-owner', ui_spec)
                self.assertNotIn('{{ROLE_', ui_spec)
                self.assertIn('docs/ui-spec/UI-SPEC.md', agents)
                self.assertIn('待配置', engineering)
                self.assertIn('由引擎维护', engineering)
                self.assertNotIn('CDP 端口/监听地址 |', engineering)
                self.assertIn('账号用途/权限/凭据引用', engineering)
                result = self.submit(plan).json()
                self.assertEqual(result['status'], 'ready', result)
                self.assertEqual(result['directory_mode'], mode)
                for path, content in files.items():
                    self.assertEqual((self.root / path).read_bytes(), content.encode('utf-8'))
                if mode == 'shared':
                    subprocess.run(['git', 'init', str(self.root)], check=True, capture_output=True)
                report = self.root / 'reports' / 'tests' / 'test.md'
                report.write_text('Internal test evidence', encoding='utf-8')
                subprocess.run(['git', '-C', str(self.root), 'check-ignore', 'reports/INDEX.md',
                                'reports/.gitignore', 'reports/tests/test.md', 'reports/research/README.md'], check=True, capture_output=True)
                subprocess.run(['git', '-C', str(self.root), 'add', '-A'], check=True, capture_output=True)
                tracked = subprocess.check_output(['git', '-C', str(self.root), 'ls-files'], text=True).splitlines()
                self.assertIn('docs/engineering/ENGINEERING.md', tracked)
                self.assertIn('docs/requirements/README.md', tracked)
                self.assertIn('docs/design/README.md', tracked)
                self.assertIn('docs/ui-spec/UI-SPEC.md', tracked)
                self.assertIn('scripts/TEST_INDEX.md', tracked)
                self.assertFalse(any(p.startswith('reports/') for p in tracked))
                restored = self.client.get('/projects/initializations/' + plan['initialization_id']).json()
                self.assertEqual(restored['directory_mode'], mode)
        self.assert_host_changes(before)

    def test_configured_role_multiple_members_later_join_and_personal_grants(self):
        body = self.body()
        next(r for r in body['roles'] if r['id'] == 'researcher')['permissions'] = ['task.update']
        body['members'].append({'bot_id': self.ids[4], 'role': 'researcher', 'permissions': ['task.resume']})
        plan = self.preview(body).json(); result = self.submit(plan).json()
        self.assertEqual(result['status'], 'ready', result)
        project = result['project']; applied = result['applied_team']; handles = applied['member_profiles']
        for bot in (self.ids[1], self.ids[4]):
            self.assertIsNone(permissions.check(self.conn, project, handles[str(bot)], 'task.update'))
            row = self.conn.execute('SELECT role_id FROM project_members WHERE project=? AND profile=?', (project, handles[str(bot)])).fetchone()
            self.assertEqual(row['role_id'], applied['role_ids']['researcher'])
        self.assertIsNone(permissions.check(self.conn, project, handles[str(self.ids[4])], 'task.resume'))
        self.assertIsNotNone(permissions.check(self.conn, project, handles[str(self.ids[1])], 'task.resume'))
        later = bot_identity.enroll(self.conn, self.ids[5], project)
        project_team.assign_role(self.conn, project, later, '资料研究员', 'user'); self.conn.commit()
        self.assertIsNone(permissions.check(self.conn, project, later, 'task.update'))
        self.assertIsNotNone(permissions.check(self.conn, project, later, 'task.resume'))
        grant = self.conn.execute('SELECT profile FROM project_permissions WHERE project=? AND permission=? AND profile LIKE ?',
                                  (project, 'task.update', 'role:%')).fetchall()
        self.assertIn('role:' + str(applied['role_ids']['researcher']), [r[0] for r in grant])
        EVIDENCE['role_grants'] = {'same_role_members': 2, 'later_join_inherits': True, 'personal_grants_separate': True}

    def test_invalid_team_inputs_leave_no_intent_or_project(self):
        variants = []
        body = self.body(); body['roles'][1]['permissions'] = ['filesystem.write']; variants.append(body)
        body = self.body(); body['members'][1]['permissions'] = ['unknown']; variants.append(body)
        body = self.body(); body['members'].pop(); variants.append(body)
        body = self.body(); body['members'][1]['bot_id'] = 99999; variants.append(body)
        body = self.body(); body['members'][1]['bot_id'] = self.ids[0]; variants.append(body)
        body = self.body(); body['members'][1]['role'] = ['researcher', 'analyst']; variants.append(body)
        body = self.body(); body['roles'][1]['id'] = 'lead'; variants.append(body)
        body = self.body(); body['roles'][1]['name'] = body['roles'][0]['name']; variants.append(body)
        body = self.body(); body['owner_bot_id'] = self.ids[7]; variants.append(body)
        body = self.body(); body['owner_bot_id'] = self.ids[1]; variants.append(body)
        body = self.body(); body['members'].append({'bot_id': self.ids[4], 'role': 'lead', 'permissions': []}); variants.append(body)
        body = self.body(); body['template']['version'] = '9.0.0'; variants.append(body)
        body = self.body(); body['members'][0]['config_root'] = 'other'; variants.append(body)
        for body in variants:
            with self.subTest(body=body):
                before = snapshot(self.conn)
                response = self.preview(body)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(snapshot(self.conn), before)
                self.assertEqual(list(self.root.iterdir()), [])

    def copied_templates(self):
        copied = self.base / 'template-copy'; shutil.copytree(templates.TEMPLATE_ROOT, copied)
        return copied

    def test_illegal_resource_target_and_variables_block_before_staging(self):
        copied = self.copied_templates()
        body = self.body()
        path = copied / 'research' / 'manifest.json'
        original = json.loads(path.read_text(encoding='utf-8'))
        with patch.object(templates, 'TEMPLATE_ROOT', copied):
            for value in ('../outside.md', 'C:/escape.md', '/escape.md', '.git/config', 'docs/../../escape', 'docs\\escape.md', 'docs/CON.md', 'docs/INDEX.md/child', 'internal/.git/config', 'internal/.gitignore/child'):
                manifest = copy.deepcopy(original); manifest['files'][-1]['target'] = value
                path.write_text(json.dumps(manifest), encoding='utf-8')
                self.assertEqual(self.preview(body).status_code, 400, value)
            path.write_text(json.dumps(original), encoding='utf-8')
            for owner_id in ('missing', None, ['lead']):
                manifest = copy.deepcopy(original); manifest['owner_role_id'] = owner_id
                path.write_text(json.dumps(manifest), encoding='utf-8')
                self.assertEqual(self.preview(body).status_code, 400)
            path.write_text(json.dumps(original), encoding='utf-8')
            source = copied / 'research' / 'AGENTS.md.tmpl'
            for value in ('{{SECRET}}', '{{Project_Name}}', '{{ PROJECT_NAME }}', '{{OPEN'):
                source.write_text(value, encoding='utf-8')
                self.assertEqual(self.preview(body).status_code, 400, value)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM project_initializations').fetchone()[0], 0)

    def test_legacy_project_reference_uses_latest_type_and_preserves_snapshot(self):
        copied = self.copied_templates()
        with patch.object(templates, 'TEMPLATE_ROOT', copied):
            plan = self.preview().json()
            reference = copied / 'research' / 'references' / 'souls' / 'lead.md'
            reference.write_text('Updated future template reference', encoding='utf-8')
            manifest_path = copied / 'research' / 'manifest.json'
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            manifest['version'] = '99.0.0'
            manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
            result = self.submit(plan).json(); self.assertEqual(result['status'], 'ready', result)
            response = self.client.get('/projects/' + result['project'] + '/template-reference')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['template']['version'], '99.0.0')
            self.assertEqual(response.json()['template']['references'], templates.load('research')['references'])
            saved = json.loads(self.conn.execute('SELECT plan FROM project_initializations WHERE project=?', (result['project'],)).fetchone()[0])
            self.assertEqual(saved['template'], plan['template'])
            shutil.rmtree(copied)
            self.assertEqual(self.client.get('/projects/' + result['project'] + '/template-reference').status_code, 400)
        EVIDENCE['snapshot'] = {'latest_reference_by_type': True, 'application_snapshot_unchanged': True}

    def test_member_removed_after_preview_rolls_back_team_and_files(self):
        plan = self.preview().json()
        self.conn.execute('DELETE FROM bots WHERE id=?', (self.ids[1],)); self.conn.commit()
        before = snapshot(self.conn)
        result = self.submit(plan).json()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['phase'], 'preview')
        self.assertEqual(result['error']['code'], 'invalid_template')
        # Only the intent's failure state changes.
        after = snapshot(self.conn)
        before.pop('project_initializations'); after.pop('project_initializations')
        self.assertEqual(before, after); self.assertEqual(list(self.root.iterdir()), [])

    def test_unavailable_host_and_changed_binding_are_rejected(self):
        body = self.body()
        home = self.hosts / '1'; moved = self.hosts / 'moved-1'; home.rename(moved)
        self.assertEqual(self.preview(body).status_code, 400)
        moved.rename(home)
        plan = self.preview(body).json()
        self.conn.execute('UPDATE bots SET profile=? WHERE id=?', ('changed-profile', self.ids[1])); self.conn.commit()
        result = self.submit(plan).json()
        self.assertEqual(result['status'], 'failed'); self.assertEqual(result['phase'], 'preview')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0], 0)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_team_grant_failure_atomic_rollback_and_retry(self):
        plan = self.preview().json(); before = snapshot(self.conn)
        with patch.object(permissions, 'save_in_transaction', side_effect=RuntimeError('audit fault')):
            result = self.submit(plan).json()
        self.assertEqual(result['status'], 'failed'); self.assertEqual(result['phase'], 'preview')
        after = snapshot(self.conn); before.pop('project_initializations'); after.pop('project_initializations')
        self.assertEqual(before, after)
        result = self.submit(plan, 'retry').json(); self.assertEqual(result['status'], 'ready', result)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM project_roles').fetchone()[0], 4)

    def test_nested_file_crash_recovery_and_manual_conflict(self):
        plan = self.preview().json()
        def crash(_id, relative):
            if relative == 'drafts/ANALYSIS.md': raise baseline.ProcessCrash()
        with patch.object(init, '_after_publish', side_effect=crash):
            with self.assertRaises(baseline.ProcessCrash): self.submit(plan)
        old = (self.root / 'AGENTS.md').stat().st_mtime_ns
        result = self.submit(plan, 'retry').json(); self.assertEqual(result['status'], 'ready', result)
        self.assertEqual((self.root / 'AGENTS.md').stat().st_mtime_ns, old)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM project_roles').fetchone()[0], 4)
        EVIDENCE['recovery'] = {'nested_file_published_before_journal': True, 'same_roles': True, 'prior_file_mtime_preserved': True}

    def test_partial_template_manual_edit_stays_gated_and_preserved(self):
        plan = self.preview().json()
        with patch.object(init, '_after_publish', side_effect=PermissionError('isolated publishing fault')) as publish:
            failed = self.submit(plan).json()
            self.assertEqual(failed['status'], 'failed', failed)
            publish.assert_called_once()
        project = self.conn.execute('SELECT readiness_status FROM projects').fetchone()
        self.assertIsNotNone(project, failed)
        self.assertEqual(project['readiness_status'], 'failed')
        (self.root / 'AGENTS.md').write_bytes(b'owner manual edit')
        result = self.submit(plan, 'retry').json()
        self.assertEqual(result['status'], 'failed'); self.assertEqual(result['error']['code'], 'file_conflict')
        self.assertEqual((self.root / 'AGENTS.md').read_bytes(), b'owner manual edit')
        self.assertEqual(self.conn.execute('SELECT readiness_status FROM projects').fetchone()[0], 'failed')

    def test_nested_junction_escape_rejected_after_configuration(self):
        if os.name != 'nt': self.skipTest('Windows junction probe')
        import subprocess
        plan = self.preview().json()
        def crash(_id, relative):
            if relative == 'README.md': raise baseline.ProcessCrash()
        with patch.object(init, '_after_publish', side_effect=crash):
            with self.assertRaises(baseline.ProcessCrash): self.submit(plan)
        outside = self.base / 'outside'; outside.mkdir()
        junction = self.root / 'materials'
        response = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(outside)], capture_output=True)
        self.assertEqual(response.returncode, 0)
        try:
            result = self.submit(plan, 'retry').json()
            self.assertEqual(result['status'], 'failed'); self.assertEqual(result['error']['code'], 'unsafe_path')
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            junction.rmdir()

    def test_template_operations_user_only_and_blank_reference(self):
        plan = self.client.post('/projects/initializations/preview', json={'root': str(self.root), 'display_name': 'Blank'}).json()
        result = self.submit(plan).json()
        self.assertIsNone(self.client.get('/projects/' + result['project'] + '/template-reference').json()['template'])
        self.app.dependency_overrides[api._resolve_identity] = lambda: ('bot-1', 'bot')
        for path in ('/projects/templates', '/projects/templates/research', '/projects/' + result['project'] + '/template-reference'):
            self.assertEqual(self.client.get(path).status_code, 403)

    def test_three_minimal_scenarios_use_real_taskpool_and_default_grants(self):
        stages = {'software': [('backend', 'dev'), ('frontend', 'dev'), ('reviewer', 'review'), ('architect', 'merge')],
                  'research': [('researcher', 'sources'), ('analyst', 'analysis'), ('verifier', 'review')],
                  'writing': [('researcher', 'sources'), ('writer', 'draft'), ('proofreader', 'review'), ('writer', 'revision')]}
        for name, workflow in stages.items():
            with self.subTest(name=name):
                self.root = self.base / ('flow-' + name); self.root.mkdir()
                plan = self.preview(self.body(name)).json(); result = self.submit(plan).json()
                self.assertEqual(result['status'], 'ready', result)
                project = result['project']; handles = result['applied_team']['member_profiles']
                role_ids_map = result['applied_team']['role_ids']
                roles = {m['role']: handles[str(m['bot_id'])] for m in plan['team']['members']}
                lead = roles['owner' if name == 'software' else 'lead' if name == 'research' else 'editor']
                planner = roles['architect'] if name == 'software' else lead
                req = taskpool.req_create(project, lead, 'Minimal ' + name, **confirmed_requirement_fields(project))
                self.assertIn('requirement_id', req, req)
                created = taskpool.task_create(project, planner, req['requirement_id'], [{'title': 'scenario proof', 'next_bot': roles[workflow[0][0]], **BRIEF}])
                self.assertIn('task_ids', created, created); tid = created['task_ids'][0]
                for index, (role, stage) in enumerate(workflow):
                    actor = roles[role]; claimed = taskpool.task_claim(project, actor)
                    if name == 'software' and role in ('backend', 'frontend', 'reviewer', 'tester'):
                        self.assertIsNotNone(permissions.check(self.conn, project, actor, 'task.update'))
                    self.assertTrue(claimed['claimed'], claimed)
                    if name in ('research', 'writing'):
                        for permission in permissions.PERMISSIONS:
                            self.assertIsNotNone(permissions.check(self.conn, project, actor, permission))
                        denied = taskpool.task_update(project, actor, tid, title='Unapproved definition change')
                        self.assertEqual(denied.get('code'), 'permission_denied', denied)
                        self.assertEqual(self.conn.execute('SELECT title FROM tasks WHERE id=?', (tid,)).fetchone()[0], 'scenario proof')
                        self.assertTrue(taskpool.task_block(project, actor, tid, 'Need source clarification')['blocked'])
                        self.assertTrue(taskpool.task_resume(project, planner, tid, next_bot=actor)['resumed'])
                        self.assertTrue(taskpool.task_claim(project, actor)['claimed'])
                    path = self.root / ('scripts/TEST_INDEX.md' if name == 'software' else 'materials/SOURCES.md' if stage == 'sources' else 'drafts/ANALYSIS.md' if name == 'research' else 'drafts/OUTLINE.md' if stage == 'draft' else 'drafts/ARTICLE.md')
                    with path.open('a', encoding='utf-8') as stream:
                        stream.write(f'\nIsolated fixture: stage={stage}, task={tid}, actor={actor}. Evidence: deterministic 2+2=4; reviewed against source fixture.\n')
                    if index + 1 < len(workflow):
                        downstream = roles[workflow[index + 1][0]]
                        expected_role = role_ids_map[workflow[index + 1][0]]
                    else:
                        downstream = planner
                        expected_role = role_ids_map['architect' if name == 'software' else 'lead' if name == 'research' else 'editor']
                    logged = taskpool.task_log(project, actor, tid, stage, result='pass: isolated deterministic fixture 2+2=4',
                        meta={'artifact': path.relative_to(self.root).as_posix()}, next_bot=downstream,
                        expected_role_id=expected_role)
                    self.assertTrue(logged['logged'], logged)
                self.assertTrue(taskpool.task_complete(project, planner, tid, result='All fixture stages verified')['completed'])
                self.assertTrue(taskpool.req_complete(project, lead, req['requirement_id'])['completed'])
                nodes = [dict(r) for r in self.conn.execute('SELECT type,actor,result,meta FROM nodes WHERE task_id=? ORDER BY id', (tid,))]
                EVIDENCE['automated_flows'][name] = {'task_status': 'done', 'requirement_status': 'done', 'nodes': nodes,
                    'generated_files': [{'path': f['path'], 'sha256': f['sha256']} for f in plan['files']], 'scope': 'isolated deterministic fixture, no model inference'}
                if name == 'software':
                    self.assertNotIn('test', [n['type'] for n in nodes])
                    # 用户另行授权的测试需求；报告包含缺陷仍可完成报告交付。
                    test_req = taskpool.req_create(project, lead, 'Release test report',
                                                   **confirmed_requirement_fields(project))
                    self.assertNotEqual(test_req['requirement_id'], req['requirement_id'])
                    test_task = taskpool.task_create(project, planner, test_req['requirement_id'],
                        [{'title': 'Version test report', 'next_bot': roles['tester'], **BRIEF}], complete=True)
                    test_tid = test_task['task_ids'][0]
                    self.assertTrue(taskpool.task_claim(project, roles['tester'])['claimed'])
                    report = taskpool.task_log(project, roles['tester'], test_tid, 'test',
                        result='Report delivered: one defect, awaiting user decision',
                        meta={'report': 'fixture-report', 'defects': 1}, next_bot=planner,
                        expected_role_id=role_ids_map['architect'])
                    self.assertTrue(report['logged'], report)
                    self.assertTrue(taskpool.task_complete(project, planner, test_tid,
                        result='Report delivery accepted; quality issues recorded')['completed'])
                    self.assertTrue(taskpool.req_complete(project, lead, test_req['requirement_id'])['completed'])
                    self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM tasks WHERE project=?',
                                                     (project,)).fetchone()[0], 2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--evidence'); args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TemplateTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if args.evidence:
        EVIDENCE['automated_tests'] = {'run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors), 'skipped': len(result.skipped)}
        Path(args.evidence).write_text(json.dumps(EVIDENCE, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    sys.exit(0 if result.wasSuccessful() else 1)
