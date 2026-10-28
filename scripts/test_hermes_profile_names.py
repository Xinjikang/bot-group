"""Reserved profile names use isolated homes, SQLite and synthetic runtime output."""

import json
import unittest

from fastapi import HTTPException

from engine import bot_identity, discovery, execution_errors, hermes_profile_names
from scripts import test_profile_create


class HermesProfileNameTests(unittest.TestCase):
    setUp = test_profile_create.ProfileCreateTests.setUp
    fixture = test_profile_create.ProfileCreateTests.fixture
    create = test_profile_create.ProfileCreateTests.create

    def legacy_bot(self, root, name='test'):
        home = root / 'profiles' / name
        home.mkdir(parents=True)
        (home / 'config.yaml').write_text('model:\n  default: fixture-model\n', encoding='utf-8')
        bot_id = self.conn.execute(
            "INSERT INTO bots(host_type,profile,config_root,runtime,display_name) "
            "VALUES('hermes',?,?,'hermes','Legacy')", (name, str(root))).lastrowid
        self.conn.commit()
        return bot_id, home

    def test_reserved_creation_rejected_before_files_or_registration(self):
        root, body = self.fixture('hermes')
        before = {table: self.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                  for table in ('bots', 'members', 'project_members')}
        for name in ('hermes', 'test', 'tmp', 'root', 'sudo'):
            with self.subTest(name=name):
                response = self.create({**body, 'profile': name}, 400)
                self.assertIn('被 Hermes 保留', response.json()['detail'])
                self.assertFalse((root / 'profiles' / name).exists())
        self.assertFalse((root / 'profiles').exists())
        for table, count in before.items():
            self.assertEqual(self.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], count)

    def test_reserved_registration_rejects_new_and_existing(self):
        root, _ = self.fixture('hermes')
        self.legacy_bot(root)
        count = self.conn.execute('SELECT COUNT(*) FROM bots').fetchone()[0]
        for name in ('hermes', 'test', 'tmp', 'root', 'sudo'):
            with self.subTest(name=name):
                response = self.client.post('/bots/register', json={
                    'host_type': 'hermes', 'profile': name, 'config_root': str(root), 'project': 'p'})
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn('被 Hermes 保留', response.json()['detail'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM bots').fetchone()[0], count)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM project_members').fetchone()[0], 0)

    def test_scans_hide_reserved_homes_and_registered_external_bots(self):
        root, _ = self.fixture('hermes')
        existing = [self.legacy_bot(root, name) for name in ('hermes', 'test', 'tmp', 'root', 'sudo')]
        existing.append(self.legacy_bot(self.root / 'external-hermes'))
        for endpoint in ('/discovery/scan', '/profiles/candidates'):
            response = self.client.get(endpoint)
            self.assertEqual(response.status_code, 200, response.text)
            hermes = [c for c in response.json()['candidates'] if c['host_type'] == 'hermes']
            self.assertTrue(any(c['profile'] == 'default' for c in hermes))
            self.assertFalse(any(hermes_profile_names.reserved(c['profile']) for c in hermes))
        for bot_id, home in existing:
            self.assertTrue((home / 'config.yaml').exists())
            bot = self.conn.execute('SELECT * FROM bots WHERE id=?', (bot_id,)).fetchone()
            self.assertIsNotNone(bot)
            self.assertFalse(discovery.candidate_available(bot))

    def test_existing_bot_id_cannot_bypass_enrollment_validation(self):
        root, _ = self.fixture('hermes')
        bot_id, _ = self.legacy_bot(root)
        with self.assertRaises(HTTPException) as caught:
            bot_identity.enroll(self.conn, bot_id, 'p')
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn('被 Hermes 保留', caught.exception.detail)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM project_members').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM members WHERE bot_id=?', (bot_id,)).fetchone()[0], 0)

    def test_default_alias_and_codex_names_remain_usable(self):
        root, _ = self.fixture('hermes')
        response = self.client.post('/bots/register', json={
            'host_type': 'hermes', 'profile': 'default', 'config_root': str(root), 'project': 'p'})
        self.assertEqual(response.status_code, 200, response.text)
        _, body = self.fixture('codex')
        result = self.create({**body, 'profile': 'test'}).json()
        self.assertEqual(result['profile'], 'test')
        candidates = self.client.get('/profiles/candidates').json()['candidates']
        self.assertTrue(any(c['host_type'] == 'codex' and c['profile'] == 'test' for c in candidates))

    def test_reserved_runtime_failure_is_actionable_configuration(self):
        from scripts.project_test_support import enable_members
        enable_members(self.conn, ('p',), ('default',))
        self.conn.commit()
        for name in ('test', 'future-reserved-name'):
            detail = f"process exit=1\nError: Profile name '{name}' is reserved — it collides with the Hermes installation."
            self.assertEqual(execution_errors.classify(detail), 'configuration')
            with self.assertLogs('engine.execution_errors'):
                mid = execution_errors.report('default', 'p', None, name, detail)
            row = self.conn.execute('SELECT category,diagnostic FROM execution_errors WHERE message_id=?', (mid,)).fetchone()
            self.assertEqual(row['category'], 'configuration')
            self.assertIn('is reserved', row['diagnostic'])
            text = self.conn.execute('SELECT content FROM messages WHERE id=?', (mid,)).fetchone()[0]
            self.assertIn(hermes_profile_names.message(name), text)

    def test_tool_output_does_not_override_explicit_failure(self):
        detail = '\n'.join([
            json.dumps({'type': 'item.completed', 'item': {'aggregated_output': "Profile name 'test' is reserved"}}),
            json.dumps({'type': 'turn.failed', 'error': {'message': 'HTTP 401 Unauthorized'}}),
        ])
        self.assertEqual(execution_errors.classify(detail), 'authentication')
        self.assertEqual(execution_errors.classify('resource name is reserved'), 'unknown')


if __name__ == '__main__':
    unittest.main()
