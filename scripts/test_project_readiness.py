"""req#101: additive upgrade, unchanged registration and ready task dispatch."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from engine import api, db, taskpool, scanner, wake
from scripts.unified_test_support import isolated_database, confirmed_requirement_fields
from scripts.test_task_brief import BRIEF


def snapshot(conn):
    """Compare every application table, excluding the newly introduced column."""
    result = {}
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        table = row[0]
        columns = [r['name'] for r in conn.execute(f'PRAGMA table_info("{table}")') if r['name'] != 'readiness_status']
        values = [tuple(r) for r in conn.execute('SELECT ' + ','.join('"' + c + '"' for c in columns) + f' FROM "{table}"')]
        result[table] = sorted(values, key=repr)
    return result


class ProjectReadinessTests(unittest.TestCase):
    def setUp(self):
        fixture = isolated_database()
        fixture.__enter__()
        self.addCleanup(fixture.__exit__, None, None, None)
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        app = FastAPI()
        app.include_router(api.router_api)
        app.dependency_overrides[api._resolve_identity] = lambda: ('user', 'user')
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_upgrade_retains_all_configuration_and_history_idempotently(self):
        rid = taskpool.req_create('alpha', 'default', 'Existing task', **confirmed_requirement_fields())['requirement_id']
        tid = taskpool.task_create('alpha', 'architect', rid, [dict(title='Develop', next_bot='dev', **BRIEF)])['task_ids'][0]
        role_id = self.conn.execute("INSERT INTO project_roles(project,name,created_at) VALUES('alpha','existing role','old')").lastrowid
        self.conn.execute('UPDATE project_members SET role=?,role_id=? WHERE project=? AND profile=?',
                          ('existing role', role_id, 'alpha', 'dev'))
        self.conn.execute("INSERT INTO project_permissions VALUES('alpha',?,'task.update')", (f'role:{role_id}',))
        self.conn.execute("INSERT INTO settings(key,project,value,updated_at) VALUES('owner_profile','alpha','default','old')")
        self.conn.execute("UPDATE projects SET display_name='Kept name',description='Kept description',worktree_root='/existing/trees',directory_mode='worktree' WHERE name='alpha'")
        self.conn.execute("UPDATE projects SET archived=1 WHERE name='beta'")
        self.conn.commit()
        self.conn.execute('ALTER TABLE projects DROP COLUMN readiness_status')
        self.conn.commit()
        before = snapshot(self.conn)
        for _ in range(2):
            db.init_schema(self.conn)
            self.assertEqual(snapshot(self.conn), before)
            self.assertEqual([r[0] for r in self.conn.execute('SELECT readiness_status FROM projects')], ['ready', 'ready'])
        self.assertEqual(taskpool.task_claim('alpha', 'dev')['task']['id'], tid)

    def test_ready_project_still_scans_and_claims(self):
        rid = taskpool.req_create('alpha', 'default', 'Dispatch', **confirmed_requirement_fields())['requirement_id']
        tid = taskpool.task_create('alpha', 'architect', rid, [dict(title='Develop', next_bot='dev', **BRIEF)])['task_ids'][0]
        with patch.object(wake, 'enqueue', return_value=True) as enqueue:
            self.assertGreater(scanner.scan_tasks(self.conn)['claimable'], 0)
            enqueue.assert_called()
        self.assertEqual(taskpool.task_claim('alpha', 'dev')['task']['id'], tid)

    def test_state_contract_retains_incomplete_states_on_reinitialization(self):
        for state in ('pending', 'initializing', 'failed', 'ready'):
            self.conn.execute("UPDATE projects SET readiness_status=? WHERE name='alpha'", (state,))
            self.conn.commit()
            db.init_schema(self.conn)
            self.assertEqual(self.conn.execute("SELECT readiness_status FROM projects WHERE name='alpha'").fetchone()[0], state)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE projects SET readiness_status='unknown' WHERE name='alpha'")
        self.conn.rollback()

    def test_failed_additive_migration_rolls_back_column(self):
        self.conn.execute('ALTER TABLE projects DROP COLUMN readiness_status')
        self.conn.commit()
        migrate = db._migrate_add_column

        def fail_after_add(conn, table, column, declaration):
            migrate(conn, table, column, declaration)
            if table == 'projects' and column == 'readiness_status':
                raise RuntimeError('Injected migration failure')

        with patch.object(db, '_migrate_add_column', side_effect=fail_after_add):
            with self.assertRaises(RuntimeError):
                db.init_schema(self.conn)
        self.assertNotIn('readiness_status', {r['name'] for r in self.conn.execute('PRAGMA table_info(projects)')})
        db.init_schema(self.conn)
        self.assertEqual(self.conn.execute("SELECT readiness_status FROM projects WHERE name='alpha'").fetchone()[0], 'ready')

    def test_registration_keeps_old_response_files_and_empty_membership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'AGENTS.md').write_bytes(b'Existing rules\r\n')
            (root / 'README.md').write_bytes(b'Existing readme')
            (root / 'docs').mkdir()
            (root / 'docs' / 'custom.md').write_bytes(b'Preserve documents')
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            body = dict(name='legacy-id', display_name='Existing project', root=directory, description='Keep',
                        owner_profile='default', directory_mode='shared', worktree_root='')
            response = self.client.post('/projects', json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {'project': 'legacy-id', 'root': directory})
            row = self.conn.execute("SELECT * FROM projects WHERE name='legacy-id'").fetchone()
            self.assertEqual(row['readiness_status'], 'ready')
            for field in ('display_name', 'root', 'description', 'directory_mode', 'worktree_root'):
                self.assertEqual(row[field], body[field])
            self.assertEqual({str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}, before)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM project_members WHERE project='legacy-id'").fetchone()[0], 0)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM project_permissions WHERE project='legacy-id'").fetchone()[0], 0)
            audit = self.conn.execute("SELECT detail FROM project_config_events WHERE project='legacy-id'").fetchone()
            self.assertEqual(json.loads(audit[0])['root'], directory)
            self.assertEqual(self.client.post('/projects', json=body).status_code, 400)

    def test_registration_requires_existing_directory_and_generates_only_id(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / 'missing'
            result = self.client.post('/projects', json={'display_name': 'Missing', 'root': str(missing)})
            self.assertEqual(result.status_code, 400)
            self.assertFalse(missing.exists())
            result = self.client.post('/projects', json={'display_name': 'Empty directory', 'root': directory})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertTrue(result.json()['project'].startswith('p_'))
            self.assertEqual(set(result.json()), {'project', 'root'})
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
