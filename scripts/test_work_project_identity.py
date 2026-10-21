"""Session parsing and MCP project provenance without spawning Hermes."""
import ast
import re
import unittest
from pathlib import Path
from unittest.mock import Mock
from scripts import test_read_inbox_batch as fixture
from engine import api


class WorkProjectIdentityTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(Path('mcp/server.py').read_text(encoding='utf-8'))
        funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in
                 {'_work_project', '_session_resolve', 'group_read_inbox', 'group_send'}]
        for node in funcs:
            node.decorator_list = []
        self.scope = {'re': re, '_profile': lambda: 'fe-dev-01',
                      '_execution_context': lambda: {},
                      '_parent_cmdline': lambda: 'hermes chat -c "fe-dev-01-bot-group-work" -q wake',
                      '_start_heartbeat': lambda: None, '_get': Mock(return_value={'messages': []}),
                      '_post': Mock(return_value={})}
        exec(compile(ast.Module(body=funcs, type_ignores=[]), 'mcp/server.py', 'exec'), self.scope)

    def test_hyphenated_profile_and_project(self):
        self.assertEqual(self.scope['_work_project'](), ('bot-group', ''))
        self.scope['group_read_inbox']()
        self.assertEqual(self.scope['_get'].call_args.args[1]['project'], 'bot-group')

    def test_send_before_read_and_after_empty_read(self):
        self.scope['_execution_context'] = lambda: dict(profile='fe-dev-01', project='bot-group',
            thread_id=None, session='fe-dev-01-bot-group-work', wake_started_at=100)
        for read_first in (False, True):
            if read_first:
                self.scope['group_read_inbox']()
            self.scope['group_send']('@default reply', False)
            self.assertEqual(self.scope['_post'].call_args.args[1]['project'], 'bot-group')

    def test_malformed_identity_blocks_read_and_send_even_explicit_project(self):
        for session in ('other-bot-group-work', 'fe-dev-01--work', 'work'):
            with self.subTest(session=session):
                self.scope['_parent_cmdline'] = lambda: f'hermes -c {session}'
                self.assertIn('error', self.scope['group_read_inbox']())
                self.assertIn('error', self.scope['group_send']('@default reply', False))
        self.scope['_get'].assert_not_called()
        self.scope['_post'].assert_not_called()

    def test_foreign_response_errors_without_changing_destination(self):
        self.scope['_execution_context'] = lambda: dict(profile='fe-dev-01', project='bot-group',
            thread_id=None, session='fe-dev-01-bot-group-work', wake_started_at=100)
        self.scope['_get'].return_value = {'messages': [{'project': 'other', 'thread_id': None}]}
        self.assertIn('error', self.scope['group_read_inbox']())
        self.scope['group_send']('@default reply', True)
        self.assertEqual(self.scope['_post'].call_args.args[1]['project'], 'bot-group')

    def test_explicit_send_does_not_change_next_default(self):
        self.scope['_execution_context'] = lambda: dict(profile='fe-dev-01', project='bot-group',
            thread_id=None, session='fe-dev-01-bot-group-work', wake_started_at=100)
        with self.assertRaises(TypeError):
            self.scope['group_send']('@default handoff', True, project='other')
        self.scope['_post'].assert_not_called()
        self.scope['group_send']('@default reply', False)
        self.assertEqual(self.scope['_post'].call_args.args[1]['project'], 'bot-group')


class WorkInboxScopeTests(unittest.TestCase):
    setUp = fixture.ReadInboxBatchTests.setUp
    tearDown = fixture.ReadInboxBatchTests.tearDown

    def test_missing_project_errors(self):
        with self.assertRaises(api.HTTPException) as error:
            api.get_inbox('default', kind='work', x_group_profile='default')
        self.assertEqual(error.exception.status_code, 400)

    def test_empty_project_reports_zero_despite_other_pending(self):
        self.conn.execute("UPDATE inbox SET status='seen' WHERE message_id IN (1,3)")
        self.conn.commit()
        result = api.get_inbox('default', kind='work', project='bot-group', x_group_profile='default')
        self.assertEqual(result['messages'], [])
        self.assertEqual(result['pending_total'], 0)
        self.assertEqual(result['pending_thread'], 0)
        self.assertEqual(self.conn.execute("SELECT status FROM inbox WHERE message_id=2").fetchone()[0], 'pending')


if __name__ == '__main__':
    unittest.main()
