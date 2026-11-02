"""Real worker teardown, workspace-list reproduction and signing fallback rejection."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from engine import auth, config, db, hermes_connections as connections
from engine import hermes_gateway as gateway, hermes_session_auth as grants, signing_keys
from scripts.runtime_test_support import isolated_runtime


class RuntimeIsolationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='isolation-regression-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.sentinel = self.root / 'production-substitute'
        self.case = self.root / 'test-runtime'
        self.sentinel.mkdir()
        self.case.mkdir()
        runtime = isolated_runtime(self.sentinel)
        runtime.__enter__()
        self.addCleanup(runtime.__exit__, None, None, None)
        self.database = Path(config.db_path())
        self.keyfile = signing_keys.path()
        conn = db.get_connection()
        try:
            db.init_schema(conn)
            conn.execute('INSERT INTO hermes_session_grants VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('fixture', 7, 'fixture-profile', 'work', 0, 'fixture-session', 'fixture-sid',
                 0, 20, None, 0, 0, str(self.sentinel / 'hermes'), '[]', str(self.sentinel), '[]'))
            digest = hashlib.sha256(b'fixture-generation-20').hexdigest()
            conn.execute('CREATE TABLE bot_signing_state (initialized INTEGER PRIMARY KEY, generation INTEGER, digests TEXT)')
            conn.execute('INSERT INTO bot_signing_state VALUES(1,20,?)', (json.dumps({'20': digest}),))
            conn.commit()
        finally:
            conn.close()
        self.keyfile.parent.mkdir(parents=True)
        self.keyfile.write_text(json.dumps({'generation': 20, 'keys': {
            '20': b'fixture-generation-20'.hex()}}), encoding='utf-8')
        self.before = self.snapshot()

    def snapshot(self):
        with closing(sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True)) as conn:
            dump = '\n'.join(conn.iterdump())
        return (dump, hashlib.sha256(self.database.read_bytes()).hexdigest(),
                hashlib.sha256(self.keyfile.read_bytes()).hexdigest())

    def assert_substitute_unchanged(self):
        after = self.snapshot()
        evidence = os.environ.get('GROUP_TEST_REPORT')
        if evidence:
            names = ('database_content_sha256', 'database_file_sha256', 'signing_file_sha256')
            def digests(snapshot):
                return dict(zip(names, (hashlib.sha256(snapshot[0].encode()).hexdigest(), *snapshot[1:])))
            (Path(evidence) / (self._testMethodName + '.json')).write_text(json.dumps({
                'before': digests(self.before), 'after': digests(after),
                'unchanged': self.before == after,
            }, indent=2), encoding='utf-8')
        self.assertEqual(after, self.before)

    def test_workspace_request_stops_owned_workers_before_environment_restoration(self):
        from scripts.test_codex_driver_regressions import CodexDriverRegressions
        pool_type = connections.Pool
        pools = []

        def make(*args, **kwargs):
            pool = pool_type(*args, **kwargs)
            pools.append(pool)
            return pool

        result = unittest.TestResult()
        with patch.object(connections, 'Pool', side_effect=make), \
             patch.object(gateway, 'enabled', return_value=True):
            CodexDriverRegressions('test_workspace_list_handles_codex_without_native_session').run(result)
        self.assertTrue(result.wasSuccessful(), (result.errors, result.failures))
        self.assertTrue(any(pool.worker is not None for pool in pools))
        self.assertTrue(all(pool.stopped for pool in pools))
        self.assertTrue(all(not t.is_alive() for pool in pools for t in pool.threads))
        self.assertEqual(Path(config.db_path()), self.database)
        self.assert_substitute_unchanged()

    def test_maintenance_completion_precedes_environment_restoration(self):
        entered, release = threading.Event(), threading.Event()
        observed = []
        with isolated_runtime(self.case):
            pool = connections._POOL

            def maintenance():
                entered.set()
                if not release.wait(8):
                    raise TimeoutError('test callback was not released')
                observed.append(config.db_path())
                conn = db.get_connection()
                conn.execute('CREATE TABLE owned_marker (value TEXT)')
                conn.commit()
                conn.close()

            pool.maintenance = maintenance
            pool.start()
            self.assertTrue(entered.wait(5))

            def finish():
                pool.stop_event.wait(5)
                release.set()

            releaser = threading.Thread(target=finish)
            releaser.start()
        releaser.join(2)
        self.assertFalse(releaser.is_alive())
        self.assertEqual(observed, [str(self.case / 'group.db')])
        self.assertTrue(all(not t.is_alive() for t in pool.threads))
        self.assertEqual(Path(config.db_path()), self.database)
        self.assert_substitute_unchanged()

    def test_failure_and_resume_reclaim_all_owned_pools(self):
        pools = []
        with self.assertRaisesRegex(RuntimeError, 'fixture failed'):
            with isolated_runtime(self.case):
                for _ in range(2):
                    pools.append(connections._POOL)
                    connections._POOL.start()
                    connections.stop()
                    connections.resume()
                pools.append(connections._POOL)
                connections._POOL.start()
                raise RuntimeError('fixture failed')
        self.assertTrue(all(p.stopped and all(not t.is_alive() for t in p.threads) for p in pools))
        self.assert_substitute_unchanged()

    def test_runtime_rejects_database_path_escape(self):
        with isolated_runtime(self.case), patch.dict(os.environ, {'GROUP_DB': str(self.database)}):
            with self.assertRaisesRegex(AssertionError, 'escaped'):
                db.get_connection()
        self.assert_substitute_unchanged()

    def test_uninitialized_keys_cannot_create_or_reconfigure_persistent_grant(self):
        self.assertEqual(auth.signing_keys()[0], 0)
        remote = Mock()
        conn = db.get_connection()
        try:
            row = conn.execute('SELECT * FROM hermes_session_grants').fetchone()
            with self.assertRaisesRegex(signing_keys.RecoveryError, 'initialized'):
                grants.grant(conn, project='fixture', profile='fixture-profile', bot_id=7,
                    kind='work', thread_id=0, session='fixture-session', home=self.sentinel,
                    command=[], workdir=str(self.sentinel), model_args={})
            with self.assertRaisesRegex(signing_keys.RecoveryError, 'initialized'):
                grants.configure(conn, row, remote, {'running': False, 'pending': False})
            with patch.object(db, 'get_connection') as connect:
                with self.assertRaisesRegex(signing_keys.RecoveryError, 'initialized'):
                    grants.maintain()
                connect.assert_not_called()
        finally:
            conn.close()
        remote.rpc.assert_not_called()
        self.assert_substitute_unchanged()

    @unittest.skipUnless(os.environ.get('GROUP_TEST_SANDBOX'), 'requires isolated test runner')
    def test_runner_blocks_sqlite_and_child_environment_escape(self):
        sandbox = Path(os.environ['GROUP_TEST_SANDBOX'])
        outside = sandbox.parent / 'forbidden-test-database.db'
        self.assertFalse(outside.exists())
        for target in (str(outside), outside.as_uri() + '?mode=rwc'):
            with self.assertRaisesRegex(RuntimeError, 'database outside sandbox'):
                sqlite3.connect(target, uri=target.startswith('file:'))
        with self.assertRaisesRegex(RuntimeError, 'child runtime outside sandbox'):
            subprocess.run([sys.executable, '-c', 'raise SystemExit(99)'],
                           env={**os.environ, 'GROUP_DB': str(outside)}, check=True)
        self.assertFalse(outside.exists())
        self.assert_substitute_unchanged()

    @unittest.skipUnless(os.environ.get('GROUP_TEST_SANDBOX'), 'requires isolated test runner')
    def test_runner_blocks_unowned_endpoint(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            endpoint = listener.getsockname()
        with socket.socket() as client:
            with self.assertRaisesRegex(RuntimeError, 'unowned endpoint'):
                client.connect(endpoint)
        self.assert_substitute_unchanged()

    def test_recovery_still_rejects_unknown_referenced_signing_generation(self):
        conn = db.get_connection()
        try:
            conn.execute('UPDATE hermes_session_grants SET pending_generation=0')
            conn.commit()
            with self.assertRaisesRegex(signing_keys.RecoveryError, 'referenced signing generation unavailable'):
                signing_keys._validate(conn, self.keyfile)
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
