"""MCP retry budgets, durable receipts, isolation and atomic business writes."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from engine import api, auth, db, mcp_http, mcp_operations, taskpool
from scripts.unified_test_support import isolated_database, REQUIREMENT_FIELDS


class OperationTest(unittest.TestCase):
    def setUp(self):
        fixture = isolated_database()
        self.path = fixture.__enter__()
        self.addCleanup(fixture.__exit__, None, None, None)
        self.app = FastAPI()
        self.app.include_router(api.router_api)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.headers = {'X-Group-Profile': 'default', 'X-Group-Key': auth.bot_token('default')}
        self.body = {'arguments': {'project': 'alpha', 'title': 'Once', **REQUIREMENT_FIELDS}}
        mcp_http._pending.clear()
        self.addCleanup(mcp_http._pending.clear)

    def post(self, operation_id='a' * 32, body=None, path='/task-tools/req_create'):
        return self.client.post(path, headers={**self.headers, 'X-Group-Operation-Id': operation_id},
                                json=self.body if body is None else body)

    def count(self, table):
        conn = db.get_connection()
        try:
            return conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
        finally:
            conn.close()

    def test_duplicate_and_restart_return_same_result(self):
        first = self.post()
        self.assertEqual(first.status_code, 200, first.text)
        self.assertIn('requirement_id', first.json())
        fresh_app = FastAPI()
        fresh_app.include_router(api.router_api)
        with TestClient(fresh_app) as restarted:
            second = restarted.post('/task-tools/req_create', json=self.body,
                                    headers={**self.headers, 'X-Group-Operation-Id': 'a' * 32})
            receipt = restarted.get('/mcp-operations/' + 'a' * 32, headers=self.headers).json()
        self.assertEqual(first.json(), second.json())
        self.assertEqual(receipt['result'], first.json())
        self.assertEqual(self.count('requirements'), 1)

    def test_conflicting_payload_and_concurrent_duplicate(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            replies = list(executor.map(lambda _: self.post(), range(2)))
        self.assertTrue(any(r.status_code == 200 for r in replies))
        self.assertTrue(all(r.status_code in (200, 409) for r in replies))
        self.assertEqual(self.count('requirements'), 1)
        changed = {'arguments': {**self.body['arguments'], 'title': 'Changed'}}
        self.assertEqual(self.post(body=changed).status_code, 409)

    def test_receipt_owner_and_membership_isolation(self):
        self.post()
        other = {'X-Group-Profile': 'dev', 'X-Group-Key': auth.bot_token('dev')}
        self.assertEqual(self.client.get('/mcp-operations/' + 'a' * 32, headers=other).json()['state'], 'unknown')
        conn = db.get_connection()
        conn.execute("UPDATE project_members SET active=0 WHERE project='alpha' AND profile='default'")
        conn.commit()
        conn.close()
        self.assertEqual(self.client.get('/mcp-operations/' + 'a' * 32, headers=self.headers).status_code, 403)
        self.assertEqual(self.post().status_code, 403)

    def test_execution_receipts_do_not_cross_successors_or_revocation(self):
        conn = db.get_connection()
        conn.execute("INSERT INTO wake_runs(session,started_at,project,profile) VALUES('exec',1,'alpha','default')")
        conn.commit()
        conn.close()
        context = {'profile': 'default', 'project': 'alpha', 'thread_id': None,
                   'session': 'exec', 'wake_started_at': 1}
        self.headers['X-Group-Key'] = auth.execution_token('default', context)
        self.assertEqual(self.post().status_code, 200)
        old_headers = dict(self.headers)
        conn = db.get_connection()
        conn.execute("UPDATE wake_runs SET ended_at=2 WHERE session='exec'")
        conn.execute("INSERT INTO wake_runs(session,started_at,project,profile) VALUES('exec',3,'alpha','default')")
        conn.commit()
        conn.close()
        self.headers['X-Group-Key'] = auth.execution_token('default', {**context, 'wake_started_at': 3})
        self.assertEqual(self.client.get('/mcp-operations/' + 'a' * 32, headers=self.headers).json()['state'], 'unknown')
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.count('requirements'), 2)
        self.assertEqual(self.client.get('/mcp-operations/' + 'a' * 32, headers=old_headers).status_code, 409)

    def test_failure_after_business_commit_leaves_unknown_and_rolls_back(self):
        original = taskpool.req_create

        def interrupted(*args, **kwargs):
            result = original(*args, **kwargs)
            self.assertIn('requirement_id', result)
            raise RuntimeError('simulated crash before receipt')

        with patch('engine.task_api._validated_function', return_value=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'simulated crash'):
                self.post()
        self.assertEqual(self.count('requirements'), 0)
        self.assertEqual(self.post().json()['code'], 'operation_unknown')
        self.assertEqual(self.count('requirements'), 0)

    def test_message_reply_loss_is_reconciled_without_duplicate(self):
        initial_messages = self.count('messages')
        conn = db.get_connection()
        conn.execute("INSERT INTO wake_runs(session,started_at,project,profile) VALUES('op-work',1,'alpha','default')")
        conn.commit()
        conn.close()
        body = {'project': 'alpha', 'thread_id': None, 'text': '@architect evidence', 'need_reply': False,
                'execution': {'session': 'op-work', 'wake_started_at': 1}}
        calls = []

        async def transport(method, url, *, headers, body=None, params=None):
            calls.append(method)
            response = self.client.request(method, url.replace('http://test-engine', ''),
                                           headers=headers, json=body, params=params)
            if method == 'POST':
                self.assertEqual(response.status_code, 200, response.text)
                raise httpx.ReadError('reply lost after commit')
            return response

        with patch.object(mcp_http, '_send', side_effect=transport):
            result = mcp_http.request('POST', 'http://test-engine', '/msg', headers=self.headers, body=body)
        self.assertIn('message_id', result)
        self.assertEqual(calls, ['POST', 'GET'])
        self.assertEqual(self.count('messages'), initial_messages + 1)
        self.assertEqual(self.count('wake_jobs'), 1)

    def test_external_effect_failure_never_replayed(self):
        effects = []

        def external(body, request=None):
            effects.append('sent')
            raise RuntimeError('external reply lost')

        wrapped = mcp_operations.recorded(external)
        request = Request({'type': 'http', 'method': 'POST', 'path': '/inbox/seen',
                           'headers': [(b'x-group-operation-id', b'c' * 32)],
                           'server': ('testserver', 80), 'scheme': 'http', 'query_string': b''})
        request.state.identity = ('default', 'bot')
        with self.assertRaises(RuntimeError):
            wrapped(body={}, request=request)
        response = wrapped(body={}, request=request)
        self.assertEqual(json.loads(response.body)['code'], 'operation_unknown')
        self.assertEqual(effects, ['sent'])


class RetryTest(unittest.TestCase):
    def setUp(self):
        mcp_http._pending.clear()
        self.addCleanup(mcp_http._pending.clear)
        self.headers = {'X-Group-Profile': 'test', 'X-Group-Key': 'synthetic'}

    def call(self):
        return mcp_http.request('POST', 'http://test-engine', '/msg', headers=self.headers, body={'text': 'test'})

    def test_connect_retry_backoff_same_id_and_five_attempt_limit(self):
        send = AsyncMock(side_effect=httpx.ConnectError('offline'))
        sleep = AsyncMock()
        with patch.object(mcp_http, '_send', send), patch.object(mcp_http.asyncio, 'sleep', sleep):
            result = self.call()
        self.assertEqual(result['code'], 'engine_offline')
        self.assertEqual(send.call_count, 5)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2, 4, 8])
        self.assertEqual(len({c.kwargs['headers']['X-Group-Operation-Id'] for c in send.call_args_list}), 1)

    def test_connect_recovery_in_same_client(self):
        send = AsyncMock(side_effect=[httpx.ConnectError('offline'), httpx.Response(200, json={'message_id': 7})])
        with patch.object(mcp_http, '_send', send), patch.object(mcp_http.asyncio, 'sleep', AsyncMock()):
            self.assertEqual(self.call(), {'message_id': 7})
        self.assertEqual(send.call_count, 2)

    def test_sync_tool_inside_running_event_loop(self):
        async def tool():
            return self.call()

        with patch.object(mcp_http, '_send', AsyncMock(return_value=httpx.Response(200, json={'message_id': 7}))):
            self.assertEqual(asyncio.run(tool()), {'message_id': 7})

    def test_unknown_keeps_id_and_next_call_only_queries(self):
        send = AsyncMock(side_effect=lambda *a, **k: httpx.Response(500) if a[0] == 'POST' else
                         httpx.Response(200, json={'state': 'unknown'}))
        with patch.object(mcp_http, '_send', send), patch.object(mcp_http.asyncio, 'sleep', AsyncMock()):
            first = self.call()
            second = self.call()
        self.assertEqual(first['operation_id'], second['operation_id'])
        self.assertEqual(sum(c.args[0] == 'POST' for c in send.call_args_list), 1)
        self.assertEqual(sum(c.args[0] == 'GET' for c in send.call_args_list), 10)

    def test_total_deadline_cancels_stalled_request(self):
        cancelled = []

        async def stalled(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

        with patch.object(mcp_http, '_send', side_effect=stalled), patch.object(mcp_http, 'BUDGET', 0.02):
            result = self.call()
        self.assertEqual(result['code'], 'operation_unknown')
        self.assertEqual(cancelled, [True])

    def test_explicit_unaccepted_retries_and_auth_denial_stops(self):
        send = AsyncMock(side_effect=[httpx.Response(503, headers={'X-Group-Not-Accepted': '1'}),
                                    httpx.Response(403, json={'detail': 'denied'})])
        with patch.object(mcp_http, '_send', send), patch.object(mcp_http.asyncio, 'sleep', AsyncMock()):
            self.assertIn('403', self.call()['error'])
        self.assertEqual(send.call_count, 2)


if __name__ == '__main__':
    unittest.main()
