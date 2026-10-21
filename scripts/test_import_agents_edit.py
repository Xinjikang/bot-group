"""Isolated AGENTS editing, backup, stale-file protection and rollback."""
import sqlite3
import unittest
from unittest.mock import patch

from scripts import test_project_import_supplement as baseline
from scripts.test_project_import import fingerprints
from engine import project_import


class AgentsEditTests(unittest.TestCase):
    setUp = baseline.SupplementTests.setUp
    configured = baseline.SupplementTests.configured
    reviewed = baseline.SupplementTests.reviewed
    submit = baseline.SupplementTests.submit
    def edited(self):
        body = self.configured(supplement=False)
        body['roles'] = body['roles'][:1] + [{'name': '全栈开发', 'permissions': []}]
        _, plan = self.reviewed(body)
        body['agents_edit'] = {**plan['agents_draft'], 'content': '# 自定义流程\n项目负责人安排全栈开发交付。\n'}
        return body

    def test_edit_backups_exact_original_and_publishes_reviewed_diff(self):
        before = (self.root / 'AGENTS.md').read_bytes()
        body, plan = self.reviewed(self.edited())
        item = next(f for f in plan['files'] if f['path'] == 'AGENTS.md')
        self.assertEqual(item['action'], 'replace')
        self.assertIn('+项目负责人', item['diff'])
        self.assertEqual((self.root / 'AGENTS.md').read_bytes(), before)
        response = self.submit(body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((self.root / item['backup']).read_bytes(), before)
        self.assertEqual((self.root / 'AGENTS.md').read_bytes(), body['agents_edit']['content'].encode())

    def test_external_edit_after_draft_is_rejected(self):
        body, _ = self.reviewed(self.edited())
        (self.root / 'AGENTS.md').write_text('Concurrent change', encoding='utf-8')
        before = fingerprints(self.root)
        self.assertEqual(self.submit(body).status_code, 409)
        self.assertEqual(self.client.post('/projects/import/preview', json=body).status_code, 409)
        self.assertEqual(fingerprints(self.root), before)

    def test_failed_transaction_restores_original_and_removes_backup(self):
        body, _ = self.reviewed(self.edited())
        before = fingerprints(self.root)
        original = project_import.ws.audit
        def failure(conn, project, actor, action, detail):
            if action == 'project.import':
                raise sqlite3.IntegrityError('rollback after replacement')
            return original(conn, project, actor, action, detail)
        with patch.object(project_import.ws, 'audit', side_effect=failure):
            with self.assertRaises(sqlite3.IntegrityError):
                self.submit(body)
        self.assertEqual(fingerprints(self.root), before)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM projects').fetchone()[0], 0)

    def test_missing_agents_custom_draft_and_empty_rejection(self):
        (self.root / 'AGENTS.md').unlink()
        body = self.edited()
        self.assertIsNone(body['agents_edit']['original_sha256'])
        invalid = {**body, 'agents_edit': {**body['agents_edit'], 'content': ' '}}
        self.assertEqual(self.client.post('/projects/import/preview', json=invalid).status_code, 400)
        body, _ = self.reviewed(body)
        self.assertEqual(self.submit(body).status_code, 200)
        self.assertEqual((self.root / 'AGENTS.md').read_text(encoding='utf-8'), body['agents_edit']['content'])

    def test_rollback_preserves_external_edit_and_original_backup(self):
        original_agents = (self.root / 'AGENTS.md').read_bytes()
        body, plan = self.reviewed(self.edited())
        original_audit = project_import.ws.audit
        def failure(conn, project, actor, action, detail):
            if action == 'project.import':
                (self.root / 'AGENTS.md').write_text('External edit after publication', encoding='utf-8')
                raise sqlite3.IntegrityError('late failure')
            return original_audit(conn, project, actor, action, detail)
        with patch.object(project_import.ws, 'audit', side_effect=failure):
            with self.assertRaises(sqlite3.IntegrityError):
                self.submit(body)
        self.assertEqual((self.root / 'AGENTS.md').read_text(encoding='utf-8'), 'External edit after publication')
        backup = next(f for f in plan['files'] if f['path'] == 'AGENTS.md')['backup']
        self.assertEqual((self.root / backup).read_bytes(), original_agents)
