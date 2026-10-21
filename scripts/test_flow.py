"""Unified taskpool workflow and project isolation; python scripts/test_flow.py."""
import sys
import unittest

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.unified_test_support import REQUIREMENT_FIELDS, confirmed_requirement_fields, role_id_of
from engine import db, taskpool
from scripts.unified_test_support import isolated_database
from scripts.test_task_brief import BRIEF


class UnifiedFlowTests(unittest.TestCase):
    def test_workflow_in_shared_tables(self):
        with isolated_database():
            rid = taskpool.req_create('alpha', 'default', 'workflow', **confirmed_requirement_fields('alpha'))['requirement_id']
            created = taskpool.task_create('alpha', 'architect', rid, [
                {'title': 'first', 'next_bot': 'dev', **BRIEF},
                {'title': 'second', 'next_bot': 'dev', 'priority': 1, **BRIEF},
            ])
            first, second = created['task_ids']
            claimed = taskpool.task_claim('alpha', 'dev')
            self.assertEqual(claimed['task']['id'], first)
            self.assertEqual(claimed['task']['project'], 'alpha')
            self.assertEqual(claimed['nodes'][0]['type'], 'decompose')
            self.assertEqual(taskpool.task_claim('alpha', 'dev')['reason'], 'already_in_progress')
            for actor, stage, next_bot in (
                ('dev', 'dev', 'reviewer'), ('reviewer', 'review', 'architect'),
                ('architect', 'merge', 'default'),
            ):
                self.assertTrue(taskpool.task_log('alpha', actor, first, stage, next_bot=next_bot,
                                                  result=f'{stage}: verified isolated workflow fixture',
                                                  expected_role_id=role_id_of('alpha', next_bot))['logged'])
                if next_bot != 'default':
                    self.assertTrue(taskpool.task_claim('alpha', next_bot)['claimed'])
            self.assertTrue(taskpool.task_complete('alpha', 'default', first)['completed'])
            self.assertIn('error', taskpool.req_complete('alpha', 'default', rid))
            self.assertEqual(taskpool.task_claim('alpha', 'dev')['task']['id'], second)
            taskpool.task_log('alpha', 'dev', second, 'dev', next_bot='default',
                              result='Implemented and verified second fixture task',
                              expected_role_id=role_id_of('alpha', 'default'))
            taskpool.task_complete('alpha', 'default', second)
            self.assertTrue(taskpool.req_complete('alpha', 'default', rid)['completed'])
            self.assertEqual(taskpool.req_list('beta')['count'], 0)
            other = taskpool.req_create('beta', 'default', 'other', **confirmed_requirement_fields('beta'))['requirement_id']
            self.assertNotEqual(rid, other)
            self.assertIn('error', taskpool.req_status('beta', rid))
            conn = db.get_connection()
            try:
                self.assertEqual({r[0] for r in conn.execute('SELECT project FROM requirements')}, {'alpha', 'beta'})
                self.assertEqual([r[0] for r in conn.execute('SELECT type FROM nodes WHERE task_id=? ORDER BY id', (first,))],
                                 ['decompose', 'dev', 'review', 'merge', 'accept'])
            finally:
                conn.close()

    def test_empty_result_preserves_ownership_and_history_then_allows_custom_stage(self):
        with isolated_database():
            rid = taskpool.req_create('alpha', 'default', 'handoff', **confirmed_requirement_fields('alpha'))['requirement_id']
            tid = taskpool.task_create('alpha', 'architect', rid, [
                {'title': 'work', 'next_bot': 'dev', **BRIEF},
            ])['task_ids'][0]
            self.assertTrue(taskpool.task_claim('alpha', 'dev')['claimed'])
            conn = db.get_connection()
            try:
                before = list(conn.iterdump())
                args = dict(next_bot='reviewer', expected_role_id=role_id_of('alpha', 'reviewer'))
                for fields in ({}, {'result': ''}, {'result': ' \t\r\n\u3000'}, {'result': None}, {'result': 42}):
                    with self.subTest(fields=fields):
                        rejected = taskpool.task_log('alpha', 'dev', tid, 'custom-check', **args, **fields)
                        self.assertEqual(rejected['code'], 'task_result_required')
                        self.assertEqual(list(conn.iterdump()), before)
                        task = conn.execute('SELECT status,claimed_by,next_bot FROM tasks WHERE id=?', (tid,)).fetchone()
                        self.assertEqual(tuple(task), ('in_progress', 'dev', 'dev'))
                result = '通过：核对自定义环节成果；未验证：生产环境。'
                logged = taskpool.task_log('alpha', 'dev', tid, 'custom-check', result=result, **args)
                self.assertTrue(logged['logged'], logged)
                node = conn.execute('SELECT type,result FROM nodes WHERE id=?', (logged['node_id'],)).fetchone()
                self.assertEqual(tuple(node), ('custom-check', result))
                self.assertTrue(taskpool.task_claim('alpha', 'reviewer')['claimed'])
            finally:
                conn.close()

    def test_blocking_is_scoped_to_project(self):
        with isolated_database():
            rid = taskpool.req_create('alpha', 'default', 'block', **confirmed_requirement_fields('alpha'))['requirement_id']
            tid = taskpool.task_create('alpha', 'architect', rid, [{'title': 'work', 'next_bot': 'dev', **BRIEF}])['task_ids'][0]
            taskpool.task_claim('alpha', 'dev')
            self.assertTrue(taskpool.task_block('alpha', 'dev', tid, 'dependency')['blocked'])
            self.assertEqual(taskpool.task_list('beta', 'dev')['tasks'], [])


if __name__ == '__main__':
    unittest.main()
