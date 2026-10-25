"""Project provenance from inbox to MCP reply, using an isolated database."""
import ast
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from engine import api


class ReplyProjectTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(Path('mcp/server.py').read_text(encoding='utf-8'))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in {'group_send', 'group_read_inbox'}]
        for node in functions:
            node.decorator_list = []
        self.scope = {'_inbox_project': None, '_post': Mock(return_value={}),
                      '_execution_context': lambda: dict(project='xjk', session='architect-xjk-work', wake_started_at=100),
                      '_work_project': lambda: ('xjk', ''),
                      '_get': Mock(), '_profile': lambda: 'architect',
                      '_start_heartbeat': lambda: None,
                      '_session_resolve': lambda: ('', None, '父进程命令行查询失败或进程已退出，无法确定会话身份')}
        exec(compile(ast.Module(body=functions, type_ignores=[]), 'mcp/server.py', 'exec'), self.scope)

    def test_reply_uses_session_project_and_explicit_destination_wins(self):
        # req#60：group_send 收掉 thread_id，work 进程点对点用 project 定向。
        for project in ('cicp', 'xjk'):
            self.scope['_execution_context'] = lambda: dict(project=project, session=f'architect-{project}-work', wake_started_at=100)
            self.scope['_work_project'] = lambda: (project, '')
            self.scope['_session_resolve'] = lambda: ('work', None, '')
            self.scope['_get'].return_value = {'messages': [{'project': project, 'thread_id': None}]}
            self.scope['group_read_inbox']()
            self.scope['group_send']('@user reply', True)
            self.assertEqual(self.scope['_post'].call_args.args[1]['project'], project)
        self.scope['_post'].reset_mock()
        with self.assertRaises(TypeError):
            self.scope['group_send']('@user reply', True, project='cicp')
        self.scope['_post'].assert_not_called()

    def test_missing_context_fails_and_topic_remains_authoritative(self):
        # req#60：解析失败 → 报错不发（fail-closed）；chat 进程 thread_id 按进程身份自动填。
        self.assertIn('error', self.scope['group_send']('@user reply', True))
        self.scope['_post'].assert_not_called()
        self.scope['_session_resolve'] = lambda: ('chat', 42, '')
        self.scope['group_send']('topic reply', True)
        self.assertEqual(self.scope['_post'].call_args.args[1],
                         {'text': 'topic reply', 'thread_id': 42, 'need_reply': True, 'project': 'xjk',
                          'execution': {'session': 'architect-xjk-work', 'wake_started_at': 100}})

    def test_need_reply_required_no_default(self):
        """req#57：need_reply 去默认值改必填——不传缺参报 TypeError。"""
        self.scope['_session_resolve'] = lambda: ('work', None, '')
        self.scope['_get'].return_value = {'messages': [{'project': 'xjk', 'thread_id': None}]}
        self.scope['group_read_inbox']()
        with self.assertRaises(TypeError):
            self.scope['group_send']('@user reply')  # 缺 need_reply → 必填报错

    def test_work_process_forces_kind_work(self):
        """req#52 根因B：work 进程不传 kind → 强制 kind=work，不按 profile 猜 chat。"""
        self.scope['_session_resolve'] = lambda: ('work', None, '')
        self.scope['_get'].return_value = {'messages': [{'project': 'xjk', 'thread_id': None}]}
        self.scope['group_read_inbox']()
        # _get 以位置参数调用：_get(path, params)，取第一次（/inbox）调用
        kind_arg = self.scope['_get'].call_args_list[0].args[1]['kind']
        self.assertEqual(kind_arg, 'work')

    def test_chat_process_forces_kind_chat(self):
        """req#52 根因B：chat 进程不传 kind → 强制 kind=chat。"""
        self.scope['_session_resolve'] = lambda: ('chat', 100, '')
        self.scope['_get'].return_value = {'messages': [{'project': 'xjk', 'thread_id': 100}]}
        self.scope['group_read_inbox']()
        kind_arg = self.scope['_get'].call_args_list[0].args[1]['kind']
        self.assertEqual(kind_arg, 'chat')
        # chat 进程须自动带出自己的 thread_id
        self.assertEqual(self.scope['_get'].call_args_list[0].args[1]['thread_id'], 100)

    def test_unresolved_session_errors(self):
        """req#54 task#99：解析失败 → 直接报错，不落 work、不调 /inbox。"""
        self.scope['_session_resolve'] = lambda: ('', None, '父进程命令行无 -c 会话标记（未通过引擎正常叫醒），无法确定会话身份')
        result = self.scope['group_read_inbox']()
        self.assertIn('error', result)
        self.assertIn('无法确定会话身份', result['error'])
        self.assertIn('未通过引擎正常叫醒', result['error'])
        self.scope['_get'].assert_not_called()

    def test_inbox_backend_error_is_returned_unchanged(self):
        self.scope['_session_resolve'] = lambda: ('chat', 100, '')
        error = {'error': 'HTTP 409: no active chat window'}
        self.scope['_get'].return_value = error
        self.assertEqual(self.scope['group_read_inbox'](), error)
        self.scope['_get'].assert_called_once_with('/inbox',
            {'profile': 'architect', 'limit': 1, 'kind': 'chat', 'thread_id': 100})
        self.assertIsNone(self.scope['_inbox_project'])

    def test_thread_backend_error_is_returned_unchanged(self):
        self.scope['_session_resolve'] = lambda: ('chat', 100, '')
        error = {'error': 'HTTP 404: thread missing'}
        self.scope['_get'].side_effect = [
            {'messages': [{'project': 'bot-group', 'thread_id': 100}]}, error]
        self.assertEqual(self.scope['group_read_inbox'](), error)

    def test_valid_empty_inbox_still_returns_empty(self):
        self.scope['_session_resolve'] = lambda: ('chat', 100, '')
        self.scope['_get'].return_value = {'messages': [], 'pending_total': 0}
        self.assertEqual(self.scope['group_read_inbox']()['kind'], 'empty')

    def test_api_rejects_unscoped_point_to_point_before_database_write(self):
        with patch.object(api, '_resolve_identity', return_value=('architect', 'bot')), patch.object(api, '_conn') as connection:
            with self.assertRaises(api.HTTPException) as error:
                api.post_msg({'text': '@user reply', 'thread_id': None})
            self.assertEqual(error.exception.status_code, 400)
            connection.assert_not_called()

    def test_inbox_exposes_source_project(self):
        class Connection(sqlite3.Connection):
            def close(self):
                pass
        conn = sqlite3.connect(':memory:', factory=Connection)
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE attachments(message_id INTEGER, position INTEGER)')
        conn.executescript("""
            CREATE TABLE messages(id INTEGER, project TEXT, ts TEXT, sender TEXT,
                sender_type TEXT, content TEXT, mentions TEXT, thread_id INTEGER, need_reply INTEGER);
            CREATE TABLE inbox(id INTEGER, message_id INTEGER, profile TEXT, status TEXT);
            INSERT INTO messages VALUES(1, 'cicp', 'now', 'user', 'user', 'hello', '["architect"]', NULL, 1);
            INSERT INTO inbox VALUES(1, 1, 'architect', 'pending');
        """)
        try:
            with patch.object(api, '_conn', return_value=conn):
                result = api.get_inbox('architect', kind='work', x_group_profile=None, project='cicp')
            self.assertEqual(result['messages'][0]['project'], 'cicp')
        finally:
            sqlite3.Connection.close(conn)


if __name__ == '__main__':
    unittest.main()
