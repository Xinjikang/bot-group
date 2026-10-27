"""Failure notifications use isolated SQLite, real child exits and synthetic credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from engine import db, execution_errors as errors, wake
from scripts import test_project_roles


class ExecutionErrorTests(unittest.TestCase):
    def setUp(self):
        test_project_roles.ProjectRoleTests.setUp(self)
        environment = patch.dict(os.environ, {'GROUP_DATA_DIR': str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        from scripts.project_test_support import enable_members
        enable_members(self.conn, ('p',), ('default',))
        self.conn.commit()

    def test_categories(self):
        cases = {'configuration': "No usable credentials found for provider 'ollama-cloud'. Set OLLAMA_API_KEY.",
                 'authentication': 'HTTP 401 Unauthorized', 'permission': 'status_code=403 Forbidden',
                 'rate_limit': 'Error code: 429 Too Many Requests', 'timeout': 'httpx.ReadTimeout: timed out',
                 'network': 'ConnectionError: connection refused', 'upstream': 'HTTP 503 Service Unavailable',
                 'unknown': 'unexpected process termination'}
        for expected, text in cases.items():
            with self.subTest(expected=expected):
                self.assertEqual(errors.classify(text), expected)
        self.assertEqual(errors.classify('process pid 403 stopped at line 429'), 'unknown')
        self.assertEqual(errors.classify('主账户凭据已过期'), 'authentication')
        self.assertEqual(errors.classify('access token expired'), 'authentication')

    def test_hermes_compatibility_notifications_are_actionable(self):
        for exception, expected in [('HermesCompatibilityError', '更新或修复 Hermes'),
                                    ('HermesConfigurationError', 'HERMES_BIN、HERMES_HOME')]:
            with self.subTest(exception=exception), self.assertLogs('engine.execution_errors'):
                message_id = errors.report('default', 'p', None, exception,
                                           exception + ': private/source/path missing-interface')
                row = self.conn.execute('SELECT category FROM execution_errors WHERE attempt=?', (exception,)).fetchone()
                self.assertEqual(row['category'], 'configuration')
                text = self.conn.execute('SELECT content FROM messages WHERE id=?', (message_id,)).fetchone()[0]
                self.assertIn(expected, text)
                self.assertNotIn('private/source/path', text)

    def test_profile_custom_secret_is_redacted(self):
        home = self.root / 'hermes'
        home.mkdir(exist_ok=True)
        (home / '.env').write_text('COMPANY_VALUE=custom-profile-secret\nBROWSER_TIMEOUT=30\n')
        (home / 'config.yaml').write_text('model:\n  key_env: COMPANY_VALUE\n')
        with self.assertLogs('engine.execution_errors') as logs:
            errors.report('default', 'p', None, 'custom-secret', 'HTTP 401 custom-profile-secret timeout=30')
        diagnostic = self.conn.execute('SELECT diagnostic FROM execution_errors').fetchone()[0]
        self.assertNotIn('custom-profile-secret', diagnostic)
        self.assertNotIn('custom-profile-secret', '\n'.join(logs.output))
        self.assertIn('timeout=30', diagnostic)

    def test_persistent_deduplication_redaction_and_no_wake(self):
        text = "HTTP 429 token=synthetic-token Bearer opaque-token https://example.test/?key=secret custom-secret"
        with self.assertLogs('engine.execution_errors', level='ERROR') as logs:
            first = errors.report('default', 'p', None, 'run-1', text, env={'API_KEY': 'custom-secret'})
        self.assertEqual(errors.report('default', 'p', None, 'run-1', text), first)
        row = self.conn.execute('SELECT * FROM execution_errors').fetchone()
        self.assertEqual(row['category'], 'rate_limit')
        for secret in ('synthetic-token', 'opaque-token', 'custom-secret', 'example.test'):
            self.assertNotIn(secret, row['diagnostic'])
            self.assertNotIn(secret, '\n'.join(logs.output))
        messages = self.client.get('/messages', params={'project': 'p'}).json()['messages']
        message = next(m for m in messages if m['id'] == first)
        self.assertIn(errors.MESSAGES['rate_limit'], message['content'])
        self.assertNotIn('429', message['content'])
        self.assertEqual(message['sender_type'], 'engine')
        self.assertEqual(message['need_reply'], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM wake_jobs').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 1)

    def test_thread_notification_retains_parent_and_turn(self):
        thread = self.conn.execute("INSERT INTO threads(project,title,creator,members,created_at,last_active_at,turn_profile) VALUES('p','topic','user','[\"user\",\"default\"]',?,?,'default')", (db.now_iso(), db.now_iso())).lastrowid
        parent = self.conn.execute("INSERT INTO messages(project,ts,sender,sender_type,content,thread_id,round,depth) VALUES('p',?,'user','user','test',?,2,1)", (db.now_iso(), thread)).lastrowid
        self.conn.commit()
        with self.assertLogs('engine.execution_errors'):
            mid = errors.report('default', 'p', thread, 'thread-run', 'HTTP 403', parent_id=parent)
        message = self.conn.execute('SELECT thread_id,parent_id,round,depth FROM messages WHERE id=?', (mid,)).fetchone()
        self.assertEqual(tuple(message), (thread, parent, 2, 2))
        self.assertEqual(self.conn.execute('SELECT turn_profile FROM threads WHERE id=?', (thread,)).fetchone()[0], 'default')

    def spawn(self, code, *, intentional=False, fail_start=False):
        from engine import project_rules
        session = wake.session_name('default', 'work', None, 'p')
        real_popen = subprocess.Popen
        def launch(*args, **kwargs):
            if fail_start:
                raise FileNotFoundError('missing executable')
            if intentional:
                wake.mark_intentional_stop('default', session)
            return real_popen([sys.executable, '-X', 'utf8', '-c', code], **kwargs)
        try:
            with patch.object(wake.subprocess, 'Popen', side_effect=launch), \
                 patch.object(wake, '_hermes_bin', return_value='hermes-test'), \
                 patch.object(wake, 'session_busy', return_value=False), \
                 patch.object(wake, '_workdir_for', return_value=str(self.root)), \
                 patch.object(project_rules, 'sync'):
                result = wake._spawn('default', 'work', 'test', None, 'p')
                for thread in threading.enumerate():
                    if thread.name == 'session-default':
                        thread.join(timeout=10)
                        self.assertFalse(thread.is_alive())
            return result
        finally:
            wake._intentional_stops.discard(session)
            wake._running_sessions.discard(session)
            wake._running_wakes.pop(session, None)

    def test_real_child_stderr_is_reported(self):
        result = self.spawn("import sys; print(\"No usable credentials found for provider 'ollama-cloud'. Set OLLAMA_API_KEY.\",file=sys.stderr); sys.exit(1)")
        self.assertTrue(result['ok'])
        self.assertEqual(self.conn.execute('SELECT category FROM execution_errors').fetchone()[0], 'configuration')
        text = self.conn.execute('SELECT content FROM messages').fetchone()[0]
        self.assertIn(errors.MESSAGES['configuration'], text)
        self.assertNotIn('OLLAMA', text)
        self.assertEqual(self.conn.execute('SELECT exit_code FROM wake_runs').fetchone()[0], 1)

    def test_spawn_failure_is_reported(self):
        self.assertFalse(self.spawn('', fail_start=True)['ok'])
        self.assertEqual(self.conn.execute('SELECT category FROM execution_errors').fetchone()[0], 'configuration')

    def test_success_and_intentional_stop_are_quiet(self):
        self.assertTrue(self.spawn('pass')['ok'])
        self.assertTrue(self.spawn('import sys;sys.exit(1)', intentional=True)['ok'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM execution_errors').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
