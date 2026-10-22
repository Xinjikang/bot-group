"""Isolated task HTTP boundary and database-independent MCP client regressions."""

import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine import api, auth, db, task_api, taskpool
from scripts.unified_test_support import isolated_database, REQUIREMENT_FIELDS, confirmed_requirement_fields
from scripts.test_task_brief import BRIEF


class TaskApiTest(unittest.TestCase):
    def setUp(self):
        self.fixture = isolated_database()
        self.path = self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        app = FastAPI()
        app.include_router(api.router_api)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {"X-Group-Profile": "default", "X-Group-Key": auth.bot_token("default")}

    def call(self, operation, arguments, execution=None, actor="default"):
        return self.client.post("/task-tools/" + operation, headers={"X-Group-Profile": actor, "X-Group-Key": auth.bot_token(actor)},
                                json={"arguments": arguments, "execution": execution})

    def test_create_query_and_permission_checks(self):
        args = {"project": "alpha", "title": "HTTP requirement", **REQUIREMENT_FIELDS}
        denied = self.call("req_create", args, actor="dev")
        self.assertEqual(denied.json()["code"], "permission_denied")
        created = self.call("req_create", args)
        self.assertEqual(created.status_code, 200, created.text)
        rid = created.json()["requirement_id"]
        result = self.call("req_status", {"project": "alpha", "requirement_id": rid})
        self.assertEqual(result.status_code, 200, result.text)
        other = self.call("req_status", {"project": "beta", "requirement_id": rid})
        self.assertIn("error", other.json())
        conn = db.get_connection()
        conn.execute("UPDATE project_members SET active=0 WHERE profile='default' AND project='alpha'")
        conn.commit()
        conn.close()
        self.assertEqual(self.call("req_list", {"project": "alpha"}).json()["code"], "permission_denied")

    def test_payload_cannot_choose_actor_or_database(self):
        for extra in ({"role": "architect"}, {"GROUP_DB": "elsewhere"}, {"db_path": "elsewhere"}):
            with self.subTest(extra=extra):
                response = self.call("req_list", {"project": "alpha", **extra})
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.call("_conn", {}).status_code, 404)
        self.assertEqual(self.call("task_get", {"project": "alpha", "task_id": "invalid"}).status_code, 422)
        self.assertEqual(self.client.post("/task-tools/project_list", json={"arguments": {}}).status_code, 401)

    def test_execution_validation_and_work_only_guard(self):
        conn = db.get_connection()
        tid = conn.execute("INSERT INTO threads(project,title,creator,members,created_at,last_active_at) "
                           "VALUES('alpha','Test','user','[\"default\"]',?,?)",
                           (db.now_iso(), db.now_iso())).lastrowid
        conn.execute("INSERT INTO wake_runs(session,started_at,project,profile,thread_id) VALUES(?,?,?,?,?)",
                     ("test-chat", 100, "alpha", "default", tid))
        conn.commit()
        conn.close()
        context = {"profile": "default", "project": "alpha", "session": "test-chat",
                   "wake_started_at": 100, "thread_id": tid}
        self.assertEqual(self.call("req_list", {"project": "alpha"}, context).status_code, 200)
        self.assertEqual(self.call("task_claim", {"project": "alpha"}, context).status_code, 403)
        self.assertEqual(self.call("req_list", {"project": "beta"}, context).status_code, 403)
        self.assertEqual(self.call("req_list", {"project": "alpha"}, {**context, "wake_started_at": 101}).status_code, 409)
        conn = db.get_connection()
        conn.execute("UPDATE wake_runs SET ended_at=200 WHERE session='test-chat'")
        conn.commit()
        conn.close()
        self.assertEqual(self.call("req_list", {"project": "alpha"}, context).status_code, 409)

    def test_project_list_scope_and_admin_mutations(self):
        conn = db.get_connection()
        conn.execute("UPDATE project_members SET active=0 WHERE profile='default' AND project='beta'")
        conn.commit()
        conn.close()
        result = self.call("project_list", {}).json()
        self.assertEqual([p["id"] for p in result["projects"]], ["alpha"])
        for path, body in (("/projects", {"name": "extra", "root": str(Path(self.path).parent)}),
                           ("/projects/alpha/archive", {"archived": True}),
                           ("/projects/alpha/remove", {})):
            self.assertEqual(self.client.post(path, json=body, headers=self.headers).status_code, 403)

    def test_membership_revoked_between_boundary_and_write(self):
        rid = taskpool.req_create('alpha', 'default', 'Claim race',
                                  **confirmed_requirement_fields())['requirement_id']
        tid = taskpool.task_create('alpha', 'architect', rid,
                                  [{'title': 'Work', 'next_bot': 'default', **BRIEF}],
                                  complete=True)['task_ids'][0]
        original = task_api._validated_function

        def revoke(operation):
            function = original(operation)
            conn = db.get_connection()
            conn.execute("UPDATE project_members SET active=0 WHERE profile='default' AND project='alpha'")
            conn.commit()
            conn.close()
            return function

        with patch.object(task_api, '_validated_function', side_effect=revoke):
            result = self.call('task_claim', {'project': 'alpha'})
        self.assertEqual(result.status_code, 403, result.text)
        conn = db.get_connection()
        self.assertEqual(conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0], 'pending')
        conn.close()

    def test_decomposition_credential_cannot_be_reused_by_another_member(self):
        rid = self.call('req_create', {'project': 'alpha', 'title': 'Owner', **REQUIREMENT_FIELDS}).json()['requirement_id']
        first = taskpool.task_create('alpha', 'architect', rid,
                                    [{'title': 'Draft', 'draft': True}], credential='exec:architect-work')
        self.assertNotIn('error', first)
        copied = self.call('task_create', {'project': 'alpha', 'requirement_id': rid,
                           'tasks': [{'title': 'Other draft', 'draft': True}],
                           'credential': 'exec:architect-work'})
        self.assertEqual(copied.json().get('code'), 'decompose_credential_stale', copied.text)
        edited = self.call('task_update', {'project': 'alpha', 'task_id': first['task_ids'][0],
                           'title': 'Changed', 'credential': 'exec:architect-work'})
        self.assertEqual(edited.json().get('code'), 'decompose_credential_stale', edited.text)
        status = self.call('req_status', {'project': 'alpha', 'requirement_id': rid}).json()
        self.assertNotIn('decompose_credential', status['requirement'])
        self.assertNotIn('decompose_stale_credentials', status['requirement'])

    def test_manual_decomposition_normalizes_optional_credentials(self):
        for supplied in ({}, {'credential': ''}, {'credential': None}):
            with self.subTest(supplied=supplied):
                rid = self.call('req_create', {'project': 'alpha', 'title': 'Manual decomposition',
                               **confirmed_requirement_fields()}).json()['requirement_id']
                response = self.call('task_create', {'project': 'alpha', 'requirement_id': rid,
                                     'tasks': [{'title': 'Draft', 'draft': True}], **supplied})
                self.assertEqual(response.status_code, 200, response.text)
                created = response.json()
                credential = created['credential']
                self.assertTrue(credential)
                args = {'project': 'alpha', 'task_id': created['task_ids'][0], 'title': 'Updated'}
                for actor, values in [('default', supplied), ('architect', supplied),
                                      ('architect', {'credential': credential})]:
                    denied = self.call('task_update', {**args, **values}, actor=actor)
                    self.assertEqual(denied.status_code, 200, denied.text)
                    self.assertEqual(denied.json().get('code'), 'decompose_credential_stale')
                updated = self.call('task_update', {**args, 'credential': credential, 'publish': True, **BRIEF})
                self.assertTrue(updated.json().get('updated'), updated.text)
                finished = self.call('task_create', {'project': 'alpha', 'requirement_id': rid,
                                     'tasks': [], 'complete': True, 'credential': credential})
                self.assertEqual(finished.json().get('decompose_state'), 'done', finished.text)

    def test_legacy_empty_lock_requires_owner_to_reacquire(self):
        rid = self.call('req_create', {'project': 'alpha', 'title': 'Legacy lock',
                       **confirmed_requirement_fields()}).json()['requirement_id']
        created = taskpool.task_create('alpha', 'default', rid, [{'title': 'Draft', 'draft': True}])
        conn = db.get_connection()
        try:
            conn.execute("UPDATE requirements SET decompose_credential='' WHERE id=?", (rid,))
            conn.commit()
        finally:
            conn.close()
        args = {'project': 'alpha', 'task_id': created['task_ids'][0], 'title': 'Updated', 'credential': ''}
        for actor in ('default', 'architect'):
            self.assertEqual(self.call('task_update', args, actor=actor).json().get('code'),
                             'decompose_credential_stale')
        retried = self.call('task_create', {'project': 'alpha', 'requirement_id': rid,
                            'tasks': [{'title': 'Next draft', 'draft': True}], 'credential': ''}).json()
        self.assertTrue(retried.get('credential'), retried)
        updated = self.call('task_update', {**args, 'credential': retried['credential']})
        self.assertTrue(updated.json().get('updated'), updated.text)

    def test_invalid_manual_credential_types_are_rejected(self):
        for credential in (0, False, [], {}):
            for operation, args in [('task_create', {'requirement_id': 1, 'tasks': []}),
                                    ('task_update', {'task_id': 1})]:
                with self.subTest(credential=credential, operation=operation):
                    response = self.call(operation, {'project': 'alpha', **args, 'credential': credential})
                    self.assertEqual(response.status_code, 422, response.text)

    def execution(self, chat=False, session='bound-work', start=100):
        conn = db.get_connection()
        tid = None
        if chat:
            tid = conn.execute("INSERT INTO threads(project,title,creator,members,created_at,last_active_at) "
                               "VALUES('alpha','Chat','user','[\"default\"]','now','now')").lastrowid
        conn.execute("INSERT INTO wake_runs(session,started_at,project,profile,thread_id) VALUES(?,?,?,?,?)",
                     (session, start, 'alpha', 'default', tid))
        conn.commit()
        conn.close()
        return {'profile': 'default', 'project': 'alpha', 'session': session,
                'wake_started_at': start, 'thread_id': tid}

    def bound_call(self, context, operation, arguments, **body):
        return self.client.post('/task-tools/' + operation,
                                headers={'X-Group-Profile': 'default',
                                         'X-Group-Key': auth.execution_token('default', context)},
                                json={'arguments': arguments, **body})

    def test_bound_chat_cannot_drop_execution_to_use_work_operations(self):
        context = self.execution(chat=True)
        for operation in ('task_claim', 'task_log', 'task_complete', 'task_block'):
            for payload in ({}, {'execution': None}, {'execution': context}):
                with self.subTest(operation=operation, payload=payload):
                    response = self.bound_call(context, operation, {'project': 'alpha'}, **payload)
                    self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.bound_call(context, 'req_list', {'project': 'alpha'}).status_code, 200)
        self.assertEqual(self.bound_call(context, 'req_list', {'project': 'beta'}).status_code, 403)
        self.assertEqual(self.bound_call(context, 'req_list', {'project': 'alpha'},
                                        execution={**context, 'thread_id': None}).status_code, 403)

    def test_bound_credential_rejects_tampering_and_stale_execution(self):
        context = self.execution()
        token = auth.execution_token('default', context)
        self.assertTrue(auth.verify_bot('default', token))
        self.assertFalse(auth.verify_bot('architect', token))
        self.assertFalse(auth.verify_bot('default', token[:-1] + ('0' if token[-1] != '0' else '1')))
        self.assertEqual(self.bound_call(context, 'task_claim', {'project': 'alpha'}).status_code, 200)
        conn = db.get_connection()
        conn.execute("UPDATE wake_runs SET ended_at=101 WHERE session='bound-work'")
        conn.commit()
        conn.close()
        self.assertEqual(self.bound_call(context, 'req_list', {'project': 'alpha'}).status_code, 409)
        self.assertEqual(self.call('req_list', {'project': 'alpha'}).status_code, 200)

    def test_bound_execution_rechecked_in_write_transaction(self):
        context = self.execution()
        original = task_api._validated_function

        def end_execution(operation):
            function = original(operation)
            conn = db.get_connection()
            conn.execute("UPDATE wake_runs SET ended_at=101 WHERE session='bound-work'")
            conn.commit()
            conn.close()
            return function

        with patch.object(task_api, '_validated_function', side_effect=end_execution):
            result = self.bound_call(context, 'req_create',
                                     {'project': 'alpha', 'title': 'Late write', **REQUIREMENT_FIELDS})
        self.assertEqual(result.status_code, 409, result.text)
        conn = db.get_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM requirements").fetchone()[0], 0)
        conn.close()
        manual = self.call('req_create', {'project': 'alpha', 'title': 'Manual write', **REQUIREMENT_FIELDS})
        self.assertIn('requirement_id', manual.json())

    def test_bound_member_cannot_send_as_another_execution(self):
        context = self.execution()
        other = self.execution(session='other-work', start=200)
        headers = {'X-Group-Profile': 'default', 'X-Group-Key': auth.execution_token('default', context)}
        body = {'project': 'alpha', 'thread_id': None, 'text': '@architect Work update', 'need_reply': False,
                'execution': {'session': context['session'], 'wake_started_at': context['wake_started_at']}}
        with patch.object(api.router.wake, 'enqueue', return_value=False):
            self.assertEqual(self.client.post('/msg', headers=headers,
                json={**body, 'execution': {'session': other['session'], 'wake_started_at': other['wake_started_at']}}).status_code, 403)
            valid = self.client.post('/msg', headers=headers, json=body)
        self.assertEqual(valid.status_code, 200, valid.text)

    def test_bound_execution_controls_decomposition_and_topic_proxy(self):
        context = self.execution(chat=True)
        rid = self.call('req_create', {'project': 'alpha', 'title': 'Draft', **REQUIREMENT_FIELDS}).json()['requirement_id']
        arguments = {'project': 'alpha', 'requirement_id': rid,
                     'tasks': [{'title': 'Draft', 'draft': True}]}
        self.assertEqual(self.bound_call(context, 'task_create',
                                        {**arguments, 'credential': 'exec:other'}).status_code, 403)
        first = self.bound_call(context, 'task_create', arguments)
        self.assertEqual(first.status_code, 200, first.text)
        tid = first.json()['task_ids'][0]
        conn = db.get_connection()
        conn.execute("UPDATE threads SET task_id=? WHERE id=?", (tid, context['thread_id']))
        conn.commit()
        conn.close()
        headers = {'X-Group-Profile': 'default', 'X-Group-Key': auth.execution_token('default', context)}
        body = {'thread_id': context['thread_id'], 'task_id': tid, 'fields': {'title': 'Revised'}}
        self.assertEqual(self.client.post('/thread/task-update', headers=headers,
                                         json={**body, 'credential': 'exec:other'}).status_code, 403)
        revised = self.client.post('/thread/task-update', headers=headers, json=body)
        self.assertEqual(revised.status_code, 200, revised.text)
        conn = db.get_connection()
        conn.execute("UPDATE wake_runs SET ended_at=101 WHERE session='bound-work'")
        conn.commit()
        conn.close()
        self.assertEqual(self.client.post('/thread/task-update', headers=headers, json=body).status_code, 409)

    def test_mcp_uses_http_even_with_invalid_database_environment(self):
        source = Path(__file__).resolve().parents[1] / "mcp" / "server.py"
        spec = importlib.util.spec_from_file_location("api_mcp_test", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        poison = str(Path(self.path).parent / "must-not-exist" / "group.db")
        def post(url, *, json, headers, timeout):
            with patch.dict(os.environ, {"GROUP_DB": self.path}):
                return self.client.post(url.replace("http://test-engine", ""), json=json, headers=headers)
        with patch.dict(os.environ, {"GROUP_DB": poison, "TASK_QUEUE_ROLE": "default",
                                    "HERMES_PROFILE": "default", "GROUP_ENGINE_URL": "http://test-engine",
                                    "GROUP_ENGINE_TOKEN": auth.bot_token("default"),
                                    "XDG_GROUP_ENGINE_EXECUTION": ""}), patch.object(module.httpx, "post", side_effect=post):
            result = module.req_create("alpha", "Via HTTP", **REQUIREMENT_FIELDS)
            self.assertIn("requirement_id", result)
            self.assertFalse(Path(poison).parent.exists())
            self.assertFalse(hasattr(module, "taskpool"))
        with patch.object(module.httpx, "post", side_effect=httpx.ConnectError("offline")), \
             patch.dict(os.environ, {"TASK_QUEUE_ROLE": "default", "XDG_GROUP_ENGINE_EXECUTION": ""}):
            self.assertIn("API unavailable", module.task_list("alpha")["error"])


if __name__ == "__main__":
    unittest.main()
