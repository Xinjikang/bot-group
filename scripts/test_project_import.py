"""req#102 isolated real HTTP/SQLite import, rollback and Windows path evidence."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from engine import api, db, project_import, project_permissions, taskpool
from scripts.test_project_readiness import snapshot

EVIDENCE = {}


def fingerprints(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else 'directory'
            for p in sorted(root.rglob('*'))}


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='project_import_')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'ExistingProject'
        self.root.mkdir()
        (self.root / 'AGENTS.md').write_bytes(b'# Original rules\r\nKeep this workflow\r\n')
        (self.root / 'doc').mkdir()
        (self.root / 'doc' / 'design.md').write_bytes(b'# Original design')
        (self.root / 'code.bin').write_bytes(bytes(range(256)))
        (self.root / 'empty').mkdir()
        env = patch.dict(os.environ, {'GROUP_DB': str(self.base / 'group.db'), 'HERMES_HOME': str(self.base / 'host'),
                                     'CODEX_HOME': str(self.base / 'codex')})
        env.start()
        self.addCleanup(env.stop)
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        db.init_schema(self.conn)
        self.ids = []
        (self.base / 'codex').mkdir()
        (self.base / 'codex' / 'author.config.toml').write_text('default_permissions=":workspace"\n', encoding='utf-8')
        for runtime, profile in [('hermes', 'author'), ('codex', 'author')]:
            bot_id = self.conn.execute('INSERT INTO bots(host_type,config_root,profile,display_name,runtime) VALUES(?,?,?,?,?)',
                 (runtime, str(self.base / runtime), profile, runtime + ' author', runtime)).lastrowid
            self.ids.append(bot_id)
        self.conn.commit()
        self.body = dict(root=str(self.root), display_name='Imported project', description='Existing workflow',
            roles=[{'name': 'editor', 'permissions': ['task.create', 'task.update']}],
            members=[{'bot_id': self.ids[0], 'role': 'editor', 'permissions': ['req.create']},
                     {'bot_id': self.ids[1], 'role': 'editor', 'permissions': []}], owner_bot_id=self.ids[0])
        app = FastAPI()
        app.include_router(api.router_api)
        app.dependency_overrides[api._resolve_identity] = lambda: ('user', 'user')
        self.app = app
        self.client = TestClient(app, client=('127.0.0.1', 4321))
        self.addCleanup(self.client.close)

    def preview(self, root=None):
        return self.client.post('/projects/import/preview', json={'root': root or str(self.root)})

    def submit(self, body=None):
        return self.client.post('/projects/import', json=body if body is not None else self.body)

    def assertRejectedCleanly(self, body):
        before = snapshot(self.conn)
        files = fingerprints(self.root)
        response = self.submit(body)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(snapshot(self.conn), before)
        self.assertEqual(fingerprints(self.root), files)

    def test_read_only_preview_and_atomic_configuration_preserve_all_files(self):
        before = fingerprints(self.root)
        rows = snapshot(self.conn)
        preview = self.preview()
        self.assertEqual(preview.status_code, 200, preview.text)
        data = preview.json()
        self.assertEqual({d['path'] for d in data['documents']}, {'AGENTS.md', 'doc/design.md'})
        self.assertIn('Keep this workflow', data['documents'][0]['content'])
        self.assertEqual(data['file_action'], 'preserve')
        self.assertEqual(snapshot(self.conn), rows)
        self.assertEqual(fingerprints(self.root), before)
        response = self.submit()
        self.assertEqual(response.status_code, 200, response.text)
        project = response.json()['project']
        self.assertTrue(project.startswith('p_'))
        self.assertEqual(response.json()['readiness_status'], 'ready')
        member_rows = list(self.conn.execute('SELECT * FROM project_members WHERE project=? ORDER BY bot_id', (project,)))
        self.assertEqual(len(member_rows), 2)
        role_id = self.conn.execute('SELECT id FROM project_roles WHERE project=?', (project,)).fetchone()[0]
        self.assertEqual({r['role_id'] for r in member_rows}, {role_id})
        for row in member_rows:
            self.assertIsNone(project_permissions.check(self.conn, project, row['profile'], 'task.create'))
        self.assertIsNone(project_permissions.check(self.conn, project, member_rows[0]['profile'], 'req.create'))
        self.assertIsNotNone(project_permissions.check(self.conn, project, member_rows[1]['profile'], 'req.create'))
        owner = self.conn.execute("SELECT value FROM settings WHERE project=? AND key='owner_profile'", (project,)).fetchone()[0]
        self.assertEqual(owner, member_rows[0]['profile'])
        self.assertEqual(response.json()['owner_profile'], owner)
        self.assertIn(project, {p['name'] for p in self.client.get('/projects').json()['projects']})
        actions = {r[0] for r in self.conn.execute('SELECT action FROM project_config_events WHERE project=?', (project,))}
        self.assertTrue({'project.create', 'role.create', 'member.enroll', 'member.role', 'permissions.update', 'project.import'} <= actions)
        self.assertEqual(fingerprints(self.root), before)
        EVIDENCE['preserved_files'] = {'before': before, 'after': fingerprints(self.root), 'equal': True,
                                      'preview_read_only': True, 'ready': True, 'selected_members': len(member_rows)}

    def test_existing_and_legacy_duplicate_roots_are_openable(self):
        for name in ('old-a', 'old-b'):
            self.assertNotIn('error', taskpool.project_add(name, str(self.root)))
        before = snapshot(self.conn)
        self.assertEqual([p['name'] for p in self.preview().json()['existing_projects']], ['old-a', 'old-b'])
        response = self.submit()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail']['code'], 'directory_registered')
        self.assertEqual(len(response.json()['detail']['existing_projects']), 2)
        self.assertEqual(snapshot(self.conn), before)
        self.assertEqual(len(self.client.get('/projects').json()['projects']), 2)

    def test_submit_rechecks_after_preview_and_repeat(self):
        self.assertEqual(self.preview().json()['existing_projects'], [])
        first = self.submit()
        self.assertEqual(first.status_code, 200, first.text)
        second = self.submit()
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()['detail']['existing_projects'][0]['name'], first.json()['project'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0], 1)

    def test_concurrent_submits_create_one_complete_project(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.submit(), range(2)))
        self.assertEqual(sorted(r.status_code for r in results), [200, 409])
        winner = next(r for r in results if r.status_code == 200).json()['project']
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM project_members WHERE project=?', (winner,)).fetchone()[0], 2)

    def test_auth_failures_preserve_database_and_files(self):
        before = snapshot(self.conn)
        files = fingerprints(self.root)
        self.app.dependency_overrides[api._resolve_identity] = lambda: ('dev', 'bot')
        self.assertEqual(self.preview().status_code, 403)
        self.assertEqual(self.submit().status_code, 403)
        self.assertEqual(snapshot(self.conn), before)
        self.assertEqual(fingerprints(self.root), files)

    def test_invalid_team_and_fields_are_atomic(self):
        variants = []
        for changes in ({'members': 'bad'}, {'roles': [None]}, {'owner_bot_id': True}, {'display_name': 42},
                        {'root': 'relative'}, {'root': str(self.base / 'missing')}, {'initialize': True},
                        {'directory_mode': 'invalid'}, {'display_name': ''}):
            variants.append({**self.body, **changes})
        for change in ({'bot_id': 999999}, {'bot_id': True}, {'role': ['editor']}, {'role': 'missing'},
                       {'permissions': ['unknown']}, {'permissions': 'req.create'}):
            value = copy.deepcopy(self.body)
            value['members'][0].update(change)
            variants.append(value)
        value = copy.deepcopy(self.body)
        value['members'].append(copy.deepcopy(value['members'][0]))
        variants.append(value)
        value = copy.deepcopy(self.body)
        value['roles'].append(copy.deepcopy(value['roles'][0]))
        variants.append(value)
        for value in variants:
            with self.subTest(body=value):
                self.assertRejectedCleanly(value)

    def test_database_failure_after_writes_rolls_back_every_table(self):
        before, files = snapshot(self.conn), fingerprints(self.root)
        original = project_import.ws.audit
        def failure(conn, project, actor, action, detail):
            original(conn, project, actor, action, detail)
            if action == 'project.import':
                raise sqlite3.IntegrityError('Injected final audit failure')
        with patch.object(project_import.ws, 'audit', side_effect=failure):
            with self.assertRaises(sqlite3.IntegrityError):
                self.submit()
        self.assertEqual(snapshot(self.conn), before)
        self.assertEqual(fingerprints(self.root), files)

    def test_shared_permission_service_failure_rolls_back_team(self):
        before = snapshot(self.conn)
        with patch.object(project_permissions, 'save_in_transaction', side_effect=api.HTTPException(400, 'Invalid grant')):
            self.assertEqual(self.submit().status_code, 400)
        self.assertEqual(snapshot(self.conn), before)

    def test_no_members_no_roles_no_owner_is_ready(self):
        response = self.submit({'root': str(self.root), 'display_name': 'Empty team'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['members'], [])
        self.assertEqual(response.json()['owner_profile'], '')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM project_roles').fetchone()[0], 0)

    def test_worktree_import_preserves_git_index_and_all_files(self):
        def git(*args):
            subprocess.run(['git', '-C', str(self.root), *args], check=True, capture_output=True)
        git('init')
        git('add', 'AGENTS.md')
        git('-c', 'user.name=Import test', '-c', 'user.email=import@example.invalid', 'commit', '-m', 'Existing project')
        # Trigger Git's optional index stat refresh while keeping the content intact.
        agents = self.root / 'AGENTS.md'
        stat = agents.stat()
        os.utime(agents, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        before = fingerprints(self.root)
        body = {**self.body, 'directory_mode': 'worktree', 'worktree_root': str(self.base / 'worktrees')}
        response = self.submit(body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(fingerprints(self.root), before)
        self.assertFalse((self.base / 'worktrees').exists())
        EVIDENCE['worktree'] = {'all_files_and_git_index_unchanged': True, 'allocated_directories': 0}

    def test_role_seed_gate_remains_after_import_and_delete(self):
        response = self.submit()
        project = response.json()['project']
        role_id = self.conn.execute('SELECT id FROM project_roles WHERE project=?', (project,)).fetchone()[0]
        self.assertEqual(self.client.delete(f'/projects/{project}/roles/{role_id}').status_code, 200)
        db.init_schema(self.conn)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM project_roles WHERE project=?', (project,)).fetchone()[0], 0)
        self.assertTrue(all(r[0] is None for r in self.conn.execute('SELECT role_id FROM project_members WHERE project=?', (project,))))

    def test_document_preview_is_bounded_and_links_stay_in_root(self):
        (self.root / 'AGENTS.md').write_bytes(b'A' * (project_import.AGENTS_BYTES + 2))
        (self.root / 'doc' / 'big.md').write_bytes(b'B' * (project_import.TEXT_BYTES + 2))
        preview = self.preview().json()
        docs = {d['path']: d for d in preview['documents']}
        self.assertTrue(docs['AGENTS.md']['truncated'])
        self.assertEqual(len(docs['AGENTS.md']['content']), project_import.AGENTS_BYTES)
        self.assertTrue(docs['doc/big.md']['truncated'])
        for i in range(100):
            (self.root / 'doc' / f'{i}.md').write_text('Document', encoding='utf-8')
        preview = self.preview().json()
        self.assertLessEqual(len(preview['documents']), project_import.MAX_DOCUMENTS)
        self.assertTrue(any('上限' in warning for warning in preview['warnings']))

    def test_symbolic_link_duplicate_and_outside_document_preview(self):
        link = self.base / 'symbolic'
        outside = self.base / 'outside.md'
        outside.write_text('Outside private content', encoding='utf-8')
        try:
            os.symlink(self.root, link, target_is_directory=True)
            os.symlink(outside, self.root / 'doc' / 'outside.md')
        except OSError as exc:
            EVIDENCE['symbolic_link'] = {'creation': 'unavailable', 'error': str(exc), 'note': 'Existing accessible links use Path.resolve; junction tested separately.'}
            self.skipTest('Host symbolic-link creation unavailable: ' + str(exc))
        response = self.submit()
        self.assertEqual(response.status_code, 200, response.text)
        preview = self.preview(str(link))
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()['existing_projects'][0]['name'], response.json()['project'])
        self.assertFalse(any(d['path'] == 'doc/outside.md' for d in preview.json()['documents']))
        self.assertTrue(any('目录外' in warning for warning in preview.json()['warnings']))
        EVIDENCE['symbolic_link'] = {'matched_project': True, 'outside_document_skipped': True}

    @unittest.skipUnless(os.name == 'nt', 'Windows live paths')
    def test_windows_case_separator_junction_subst_and_unc_boundary(self):
        first = self.submit()
        self.assertEqual(first.status_code, 200, first.text)
        expected = first.json()['project']
        variants = {'case': str(self.root).swapcase(), 'separator': str(self.root).replace('\\', '/')}
        junction = self.base / 'junction'
        command = "New-Item -ItemType Junction -Path '" + str(junction).replace("'", "''") + "' -Target '" + str(self.root).replace("'", "''") + "' | Out-Null"
        result = subprocess.run(['powershell', '-NoProfile', '-Command', command], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(os.rmdir, junction)  # Removes the junction itself, preserves its target.
        variants['junction'] = str(junction)
        for name, root in variants.items():
            response = self.preview(root)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['existing_projects'][0]['name'], expected)
            EVIDENCE[name] = {'resolved': response.json()['root'], 'matched_project': True}
        drives = [f'{letter}:' for letter in 'ZYXWVUTSRQPONMLKJIHGF' if not Path(f'{letter}:/').exists()]
        self.assertTrue(drives, 'A spare drive letter is required for the subst test')
        drive = drives[0]
        result = subprocess.run(['subst', drive, str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            response = self.preview(drive + '\\')
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['existing_projects'][0]['name'], expected)
            EVIDENCE['subst'] = {'resolved': response.json()['root'], 'matched_project': True}
        finally:
            subprocess.run(['subst', drive, '/D'], check=True, capture_output=True)
        unc = '\\\\localhost\\' + self.root.drive.rstrip(':') + '$' + str(self.root)[2:]
        response = self.preview(unc)
        self.assertEqual(response.status_code, 400)
        self.assertIn('UNC', response.json()['detail'])
        EVIDENCE['unc'] = {'path': unc, 'http_status': response.status_code, 'support': 'explicitly rejected'}
        extended = '\\\\?\\' + str(self.root)
        response = self.preview(extended)
        self.assertEqual(response.status_code, 400)
        EVIDENCE['extended_prefix'] = {'http_status': response.status_code, 'support': 'explicitly rejected; use ordinary local path'}


if __name__ == '__main__':
    args = sys.argv[1:]
    evidence = Path(args[args.index('--evidence') + 1]) if '--evidence' in args else None
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ImportTests))
    if evidence:
        evidence.write_text(json.dumps({'isolated_database': True, 'tests_run': result.testsRun,
            'successful': result.wasSuccessful(), **EVIDENCE}, ensure_ascii=False, indent=2), encoding='utf-8')
    sys.exit(0 if result.wasSuccessful() else 1)
