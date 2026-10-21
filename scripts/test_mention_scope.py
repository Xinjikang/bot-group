"""Project roster and mention boundaries using an in-memory database."""
import ast
import json
import re
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from engine import api, router
from scripts import test_member_disabled as fixture


class MentionScopeTests(fixture._Base):
    def setUp(self):
        super().setUp()
        self.conn.execute("INSERT INTO projects(name,root,created_at) VALUES('other','.','now')")
        self.conn.execute("INSERT INTO project_members(project,profile,active) SELECT 'other',profile,1 FROM members WHERE profile<>'user'")
        self.conn.execute("UPDATE threads SET members=?,turn_profile='a' WHERE id=1",
                          (json.dumps(['a', 'b']),))
        # req#86 兼容期：pm 主键 (project,bot_id) 对 NULL bot_id 不去重，裸
        # INSERT OR REPLACE 会产生同 profile 双行——先 UPDATE 既有行，无行才插入。
        self.conn.execute(
            "INSERT INTO project_members(project,profile,active) SELECT ?,?,'0' "
            "WHERE NOT EXISTS (SELECT 1 FROM project_members WHERE project=? AND profile='c')",
            (self.PROJECT, 'c', self.PROJECT),
        )
        self.conn.execute(
            "UPDATE project_members SET active=0 WHERE project=? AND profile='c'", (self.PROJECT,)
        )
        self.conn.commit()

    def snapshot(self):
        return {table: [tuple(r) for r in self.conn.execute(f'SELECT * FROM {table}')]
                for table in ('messages', 'inbox', 'wake_jobs', 'threads')}

    def test_roster_filters_project_and_preserves_admin_view(self):
        result = api.get_members(self.PROJECT, active_only=True)
        self.assertEqual([m['profile'] for m in result['members']], ['a', 'b', 'user'])
        self.assertEqual(result['count'], 3)
        self.assertEqual(api.get_members('other', active_only=True)['count'], 4)
        self.assertEqual(api.get_members(self.PROJECT)['count'], 4)
        with self.assertRaises(api.HTTPException):
            api.get_members(active_only=True)

    def test_rejected_messages_have_no_partial_effect(self):
        for tid, text, reason in ((None, '@b @c help', '项目'),
                                  (1, '@b @c help', '话题')):
            with self.subTest(tid=tid):
                before = self.snapshot()
                with self.assertRaises(router.RouteError) as error:
                    router.route_message(self.conn, 'a', text, 'bot', tid, self.PROJECT)
                self.assertEqual(error.exception.status, 400)
                self.assertIn('@c', str(error.exception))
                self.assertIn(reason, str(error.exception))
                self.assertEqual(self.snapshot(), before)
                self.conn.rollback()

    def test_enabled_project_bot_outside_topic_rejected(self):
        self.conn.execute("UPDATE project_members SET active=1")
        self.conn.commit()
        with self.assertRaisesRegex(router.RouteError, '话题'):
            router.route_message(self.conn, 'a', '@c help', 'bot', 1, self.PROJECT)

    def test_topic_project_authoritative_and_disabled_member_rejected(self):
        # req#86 兼容期：pm 主键对 NULL bot_id 不去重，改 UPDATE 既有行（防双行）。
        self.conn.execute("UPDATE project_members SET active=0 WHERE project=? AND profile='b'",
                          (self.PROJECT,))
        self.conn.commit()
        with self.assertRaisesRegex(router.RouteError, '项目'):
            router.route_message(self.conn, 'a', '@b help', 'bot', 1, 'other')

    def test_valid_delivery_and_other_project(self):
        result = router.route_message(self.conn, 'a', '@b help', 'bot', 1, self.PROJECT)
        self.assertEqual(result['mentions'], ['b'])
        result = router.route_message(self.conn, 'a', '@c help', 'bot', None, 'other')
        self.assertEqual(result['mentions'], ['c'])

    def test_user_remains_addressable(self):
        self.assertEqual(router.validate_mention_scope(self.conn, '@user help', self.PROJECT, ['a']),
                         ['user'])

    def test_invalid_mention_does_not_reopen_settled_topic(self):
        self.conn.execute("UPDATE threads SET status='settled'")
        self.conn.commit()
        before = self.snapshot()
        with self.assertRaises(router.RouteError):
            router.route_message(self.conn, 'user', '@c help', 'user', 1, self.PROJECT)
        self.assertEqual(self.snapshot(), before)

    def test_http_error_including_settle_and_opening(self):
        from engine import db
        db.init_schema(self.conn)
        self.conn.execute('INSERT INTO wake_runs(session,started_at,project,profile,thread_id) VALUES(?,?,?,?,?)',
                          ('a-topic', 100, self.PROJECT, 'a', 1))
        self.conn.commit()
        with patch.object(api, '_resolve_identity', return_value=('a', 'bot')):
            for text in ('@c help', '[收口] @c next'):
                before = self.snapshot()
                with self.assertRaises(api.HTTPException) as error:
                    api.post_msg({'thread_id': 1, 'text': text, 'project': self.PROJECT, 'need_reply': False,
                                  'execution': {'session': 'a-topic', 'wake_started_at': 100}}, 'a', None)
                self.assertEqual(error.exception.status_code, 400)
                self.assertIn('@c', error.exception.detail)
                self.assertEqual(self.snapshot(), before)
                self.conn.rollback()
            with self.assertRaises(api.HTTPException) as error:
                api.post_thread({'project': self.PROJECT, 'title': 'new', 'members': ['a', 'b'],
                                 'opening': '@c help'}, 'a', None)
            self.assertEqual(error.exception.status_code, 400)
            self.assertEqual(self.snapshot(), before)


class MemberToolTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(Path('mcp/server.py').read_text(encoding='utf-8'))
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                     and n.name in ('_current_project', 'group_list_members', 'group_create_thread')]
        for fn in functions:
            fn.decorator_list = []
        self.scope = {'re': re, '_profile': lambda: 'fe-dev-01', '_execution_context': lambda: {},
                      '_get': Mock(return_value={}), '_post': Mock(return_value={})}
        exec(compile(ast.Module(body=functions, type_ignores=[]), 'mcp/server.py', 'exec'), self.scope)

    def test_work_and_chat_use_current_project(self):
        for suffix in ('work', 'chat-123'):
            self.scope['_parent_cmdline'] = lambda: f'hermes -c "fe-dev-01-bot-group-{suffix}"'
            self.scope['group_list_members']()
            self.scope['_get'].assert_called_with('/members', {'project': 'bot-group', 'active_only': True})

    def test_invalid_session_fails_without_global_query(self):
        for session in ('', 'other-bot-group-work', 'fe-dev-01--work', 'fe-dev-01-bot-group-chat-x'):
            self.scope['_parent_cmdline'] = lambda: f'hermes -c "{session}"'
            self.assertIn('error', self.scope['group_list_members']())
        self.scope['_get'].assert_not_called()

    def test_codex_context_and_custom_role_filter(self):
        self.scope['_execution_context'] = lambda: {'project': 'custom-project', 'thread_id': 41}
        self.scope['_parent_cmdline'] = Mock(side_effect=AssertionError('Codex needs no Hermes command line'))
        self.scope['group_list_members']('质量把关人')
        self.scope['_get'].assert_called_with('/members',
            {'project': 'custom-project', 'active_only': True, 'role': '质量把关人'})

    def test_invalid_execution_context_has_no_roster_fallback(self):
        self.scope['_execution_context'] = lambda: {'error': 'invalid execution context'}
        self.assertIn('error', self.scope['group_list_members']())
        self.scope['_get'].assert_not_called()

    def test_create_thread_uses_current_project_and_fails_closed(self):
        self.scope['_execution_context'] = lambda: {'project': 'custom-project'}
        self.scope['group_create_thread']('title', ['fe-dev-01'], 'opening')
        self.scope['_post'].assert_called_once_with('/thread', {'title': 'title', 'members': ['fe-dev-01'],
                                                   'opening': 'opening', 'project': 'custom-project'})
        self.scope['_post'].reset_mock()
        self.scope['_execution_context'] = lambda: {'error': 'invalid project'}
        self.assertIn('error', self.scope['group_create_thread']('title', [], 'opening'))
        self.scope['_post'].assert_not_called()


if __name__ == '__main__':
    unittest.main()
