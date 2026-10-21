"""req#99: independent fields, legacy migration, atomic validation and update."""
import sqlite3
import unittest
from unittest.mock import patch

from engine import db, taskpool
from scripts.unified_test_support import isolated_database, REQUIREMENT_FIELDS


class RequirementFieldTests(unittest.TestCase):
    def setUp(self):
        self.fixture = isolated_database()
        self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        conn = db.get_connection()
        db.init_schema(conn)
        conn.close()
        # Complete existing member backfills before taking zero-write snapshots.
        conn = db.get_connection()
        db.init_schema(conn)
        conn.close()

    def create(self, **overrides):
        return taskpool.req_create('alpha', 'default', **dict(
            dict(title='Structured requirement', **REQUIREMENT_FIELDS), **overrides))

    def dump(self):
        conn = db.get_connection()
        try:
            return list(conn.iterdump())
        finally:
            conn.close()

    def test_round_trip_independent_columns(self):
        rid = self.create()['requirement_id']
        req = taskpool.req_status('alpha', rid)['requirement']
        for param, (column, _) in taskpool._REQUIREMENT_FIELDS.items():
            self.assertEqual(req[column], 'Structured requirement' if param == 'title' else REQUIREMENT_FIELDS[param])
        self.assertEqual(req['description'], '')
        self.assertEqual(taskpool.req_list('alpha')['requirements'][0], req)

    def test_missing_and_invalid_fields_report_each_hint_without_writes(self):
        before = self.dump()
        result = taskpool.req_create('alpha', 'default')
        self.assertEqual({e['field'] for e in result['field_errors']}, set(taskpool._REQUIREMENT_FIELDS))
        for field in taskpool._REQUIREMENT_FIELDS:
            for value in ('', ' \n ', None, 42, {}, []):
                with self.subTest(field=field, value=value):
                    result = self.create(**{field: value})
                    self.assertEqual(result['code'], 'requirement_fields_invalid')
                    self.assertEqual([e['field'] for e in result['field_errors']], [field])
                    self.assertIn(taskpool._REQUIREMENT_FIELDS[field][1], result['error'])
        self.assertEqual(self.dump(), before)

    def test_update_all_fields_and_reject_mixed_patch_atomically(self):
        rid = self.create()['requirement_id']
        values = {key: f'changed {key}' for key in taskpool._REQUIREMENT_FIELDS}
        self.assertTrue(taskpool.req_update('alpha', 'default', rid, **values)['updated'])
        req = taskpool.req_status('alpha', rid)['requirement']
        for field, (column, _) in taskpool._REQUIREMENT_FIELDS.items():
            self.assertEqual(req[column], values[field])
        before = self.dump()
        self.assertIn('error', taskpool.req_update('alpha', 'default', rid, title='replace', scope=''))
        self.assertIn('error', taskpool.req_update('alpha', 'default', rid))
        self.assertEqual(self.dump(), before)

    def test_partial_update_preserves_legacy_and_other_fields(self):
        conn = db.get_connection()
        conn.execute("INSERT INTO requirements(id,project,title,description,created_at,updated_at) VALUES(100,'alpha','legacy','original text','old','old')")
        conn.commit()
        conn.close()
        self.assertTrue(taskpool.req_update('alpha', 'default', 100, background='new goal')['updated'])
        req = taskpool.req_status('alpha', 100)['requirement']
        self.assertEqual(req['description'], 'original text')
        self.assertEqual(req['title'], 'legacy')
        self.assertEqual(req['scope'], '')
        self.assertEqual(req['background_goal'], 'new goal')

    def test_project_permission_and_cross_project_boundaries(self):
        rid = self.create()['requirement_id']
        before = self.dump()
        self.assertEqual(taskpool.req_update('alpha', 'dev', rid, title='bad')['code'], 'permission_denied')
        self.assertIn('error', taskpool.req_update('beta', 'default', rid, title='bad'))
        self.assertIn('error', taskpool.req_update('alpha', 'default', 9999, title='bad'))
        self.assertEqual(self.dump(), before)

    def test_event_failure_rolls_back_create_and_update(self):
        rid = self.create()['requirement_id']
        before = self.dump()
        with patch.object(taskpool, '_log_event', side_effect=RuntimeError('event failure')):
            with self.assertRaises(RuntimeError):
                self.create()
            with self.assertRaises(RuntimeError):
                taskpool.req_update('alpha', 'default', rid, title='must roll back')
        self.assertEqual(self.dump(), before)

    def test_legacy_schema_migration_is_idempotent(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE requirements(id INTEGER PRIMARY KEY, project TEXT, title TEXT, version TEXT DEFAULT '', description TEXT DEFAULT '', priority INTEGER DEFAULT 0, status TEXT DEFAULT 'open', project_root TEXT DEFAULT '', created_at TEXT, updated_at TEXT)")
        conn.execute("INSERT INTO requirements(id,project,title,description,created_at,updated_at) VALUES(1,'alpha','legacy','preserve\nverbatim','old','old')")
        conn.commit()
        for _ in range(2):
            db.init_schema(conn)
            row = dict(conn.execute('SELECT * FROM requirements').fetchone())
            self.assertEqual(row['description'], 'preserve\nverbatim')
            self.assertEqual(row['title'], 'legacy')
            for column, _ in list(taskpool._REQUIREMENT_FIELDS.values())[1:]:
                self.assertEqual(row[column], '')


if __name__ == '__main__':
    unittest.main()
