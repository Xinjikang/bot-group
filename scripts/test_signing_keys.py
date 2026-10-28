"""Isolated key rotation, recovery, HTTP revocation and pre-stop gates."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine import api, auth, config, db, signing_keys as keys, hermes_gateway_ledger as ledger
from scripts.unified_test_support import isolated_database


class SigningTests(unittest.TestCase):
    def setUp(self):
        fixture = isolated_database()
        database = fixture.__enter__()
        self.addCleanup(fixture.__exit__, None, None, None)
        for mocker in (patch.object(config, 'data_dir', return_value=Path(database).parent),
                       patch.object(keys, '_active_path', None),
                       patch.dict(os.environ, {'GROUP_ENGINE_KEY': ''})):
            mocker.start()
            self.addCleanup(mocker.stop)
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        app = FastAPI()
        app.include_router(api.router_api)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def run_token(self, session='A', thread=None):
        context = dict(profile='default', project='alpha', thread_id=thread,
                       session=session, wake_started_at=100.0)
        row = ledger.prepare(self.conn, session=session, project='alpha', profile='default',
            kind='chat' if thread else 'work', thread_id=thread, task_id=None, job_id=None,
            event_floor=0, source='', python='[]', home='', workdir='', session_id=session,
            profile_id='fixture', started_at=100.0)
        return row, {'X-Group-Profile': 'default', 'X-Group-Key': auth.execution_token('default', context)}

    def terminal(self, row):
        ledger.apply_receipt(self.conn, row['execution_id'], dict(admission_id=row['execution_id'],
            ref={'session_id': row['session_id'], 'profile_id': 'fixture'},
            authority_epoch=1, execution_generation=1, status='terminal', outcome='completed'))

    def test_two_restarts_preserve_a_b_terminal_revokes_all_routes(self):
        keys.initialize(self.conn)
        a, ah = self.run_token('A')
        b, bh = self.run_token('B', 7)
        generic = auth.bot_token('default')
        keys.initialize(self.conn)
        self.assertEqual(keys.read()[0], 2)
        keys.initialize(self.conn)
        self.assertEqual(keys.read()[0], 2)
        self.assertFalse(auth.verify_bot('default', generic))
        self.assertEqual(self.client.get('/commands', headers=ah).status_code, 200)
        self.assertEqual(self.client.get('/commands', headers=bh).status_code, 200)
        self.terminal(a)
        for method, url in (('get', '/commands'), ('get', '/members?project=alpha'),
                            ('post', '/task-tools/task_get'), ('post', '/msg'),
                            ('post', '/inbox/seen'), ('post', '/heartbeat')):
            response = getattr(self.client, method)(url, headers=ah)
            self.assertEqual(response.status_code, 401, (method, url, response.text))
        keys.initialize(self.conn)
        self.assertEqual(self.client.get('/commands', headers=ah).status_code, 401)
        self.assertEqual(self.client.get('/commands', headers=bh).status_code, 200)
        self.terminal(b)
        self.assertEqual(set(keys.read()[1]), {2})
        self.assertEqual(keys.read()[0], 2)

    def test_fresh_creation_keeps_generation_one_and_current_zero_refs(self):
        keys.initialize(self.conn)
        self.assertEqual(keys.read()[0], 1)
        keys.retire(self.conn)
        self.assertEqual(set(keys.read()[1]), {1})
        keys.initialize(self.conn)
        self.assertEqual(keys.read()[0], 2)
        keys.retire(self.conn)
        self.assertEqual(set(keys.read()[1]), {2})

    def test_missing_and_identity_generation_mismatch_fail_closed(self):
        keys.initialize(self.conn)
        row, headers = self.run_token()
        self.conn.execute('UPDATE hermes_executions SET signing_generation=99')
        self.conn.commit()
        self.assertEqual(self.client.get('/commands', headers=headers).status_code, 401)
        self.conn.execute("UPDATE hermes_executions SET signing_generation=1,project='beta'")
        self.conn.commit()
        self.assertEqual(self.client.get('/commands', headers=headers).status_code, 401)
        self.conn.execute('DELETE FROM hermes_executions')
        self.conn.commit()
        self.assertEqual(self.client.get('/commands', headers=headers).status_code, 401)

    def test_current_codex_path_works_old_generation_rejected(self):
        keys.initialize(self.conn)
        context = dict(profile='default', project='alpha', thread_id=None, session='native', wake_started_at=2)
        token = auth.execution_token('default', context)
        self.conn.execute("INSERT INTO wake_runs(session,started_at,profile,project,runtime) VALUES('native',2,'default','alpha','codex')")
        self.conn.commit()
        headers = {'X-Group-Profile': 'default', 'X-Group-Key': token}
        self.assertEqual(self.client.get('/commands', headers=headers).status_code, 200)
        keys.initialize(self.conn)
        self.assertEqual(self.client.get('/commands', headers=headers).status_code, 401)

    def test_legacy_env_import_preserves_existing_signature(self):
        with patch.dict(os.environ, {'GROUP_ENGINE_KEY': 'isolated-legacy-key'}):
            row, headers = self.run_token()
            self.conn.execute('UPDATE hermes_executions SET signing_generation=NULL')
            self.conn.commit()
            keys.preflight()
            keys.initialize(self.conn)
            self.assertEqual(keys.read()[0], 1)
            self.assertEqual(self.client.get('/commands', headers=headers).status_code, 200)
            self.assertEqual(ledger.read(self.conn, row['execution_id'])['signing_generation'], 1)

    def test_legacy_active_without_env_preflight_keeps_old_service(self):
        self.run_token()
        self.conn.execute('UPDATE hermes_executions SET signing_generation=NULL')
        self.conn.commit()
        from engine.maintenance import service
        with patch.object(service, 'venv_python', return_value=Mock(exists=lambda: True)), \
             patch.object(service, '_run_install_check', return_value=0), \
             patch.object(service, '_load_env'), patch.object(service, 'do_stop') as stop:
            self.assertEqual(service.do_start(), 1)
            stop.assert_not_called()
        self.assertFalse(keys.path().exists())
        self.assertEqual(len(ledger.active(self.conn)), 1)

    def test_missing_key_after_initialize_is_preserved_as_failure(self):
        keys.initialize(self.conn)
        self.run_token()
        keys.path().unlink()
        for action in (keys.preflight, lambda: keys.initialize(self.conn), lambda: auth.bot_token('default')):
            with self.assertRaises(keys.RecoveryError):
                action()
        self.assertFalse(keys.path().exists())
        self.assertEqual(len(ledger.active(self.conn)), 1)

    def test_damaged_key_never_replaced(self):
        keys.initialize(self.conn)
        keys.path().write_text('{damaged', encoding='utf-8')
        with self.assertRaises(keys.RecoveryError):
            keys.initialize(self.conn)
        self.assertEqual(keys.path().read_text(), '{damaged')

    def test_valid_json_with_changed_key_fingerprint_blocks_recovery(self):
        keys.initialize(self.conn)
        value = json.loads(keys.path().read_text())
        value['keys']['1'] = 'ff' * 32
        keys.path().write_text(json.dumps(value), encoding='utf-8')
        before = keys.path().read_bytes()
        with self.assertRaisesRegex(keys.RecoveryError, 'fingerprint'):
            keys.initialize(self.conn)
        self.assertEqual(keys.path().read_bytes(), before)
        with self.assertRaises(keys.RecoveryError):
            auth.bot_token('default')

    def test_failed_replace_preserves_old_file_and_generation(self):
        keys.initialize(self.conn)
        before = keys.path().read_bytes()
        with patch.object(keys.os, 'replace', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError):
                keys.initialize(self.conn)
        self.assertEqual(keys.path().read_bytes(), before)
        self.assertEqual(list(keys.path().parent.glob('*.tmp')), [])

    def test_retirement_write_failure_keeps_terminal_revocation(self):
        keys.initialize(self.conn)
        row, headers = self.run_token()
        keys.initialize(self.conn)
        with patch.object(keys.os, 'replace', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError):
                self.terminal(row)
        self.assertEqual(ledger.read(self.conn, row['execution_id'])['state'], 'terminal')
        self.assertEqual(self.client.get('/commands', headers=headers).status_code, 401)
        self.assertEqual(set(keys.read()[1]), {1, 2})
        keys.retire(self.conn)
        self.assertEqual(set(keys.read()[1]), {2})

    def test_first_create_commit_failure_recovers_same_key_without_rotation(self):
        proxy = Mock(wraps=self.conn)
        proxy.commit.side_effect = OSError('fixture commit failure')
        with self.assertRaises(OSError):
            keys.initialize(proxy)
        before = keys.path().read_bytes()
        keys.initialize(self.conn)
        self.assertEqual(keys.path().read_bytes(), before)
        self.assertEqual(keys.read()[0], 1)

    def test_reference_to_missing_generation_blocks_start(self):
        keys.initialize(self.conn)
        self.run_token()
        self.conn.execute('UPDATE hermes_executions SET signing_generation=9')
        self.conn.commit()
        before = keys.path().read_bytes()
        with self.assertRaises(keys.RecoveryError):
            keys.initialize(self.conn)
        self.assertEqual(keys.path().read_bytes(), before)

    def test_pure_verifier_uses_supplied_keys_only(self):
        keys.initialize(self.conn)
        _, headers = self.run_token()
        supplied = keys.read()[1]
        with patch.object(keys, 'current', side_effect=AssertionError('unexpected IO')), \
             patch.object(db, 'get_connection', side_effect=AssertionError('unexpected DB')):
            self.assertEqual(auth.execution_scope('default', headers['X-Group-Key'], supplied)['signing_generation'], 1)
            self.assertIsNone(auth.execution_scope('default', headers['X-Group-Key'], {2: b'wrong'}))

    def test_signing_after_rotation_uses_ledger_generation(self):
        keys.initialize(self.conn)
        row, headers = self.run_token()
        keys.initialize(self.conn)
        context = dict(profile='default', project='alpha', thread_id=None, session='A', wake_started_at=100.0)
        self.assertEqual(auth.execution_token('default', context, signing_generation=row['signing_generation']),
                         headers['X-Group-Key'])

    def test_http_signature_and_scope_share_one_key_snapshot(self):
        from engine.access import resolve_identity
        from starlette.requests import Request
        keys.initialize(self.conn)
        _, headers = self.run_token()
        ring = keys.read()
        request = Request({'type': 'http', 'method': 'GET', 'path': '/commands',
                           'headers': [], 'route': Mock(path='/commands')})
        with patch.object(auth, 'signing_keys', side_effect=[ring, AssertionError('second snapshot')]) as read:
            self.assertEqual(resolve_identity('default', None, headers['X-Group-Key'], request), ('default', 'bot'))
        self.assertEqual(read.call_count, 1)
        self.assertEqual(request.state.execution_scope['signing_generation'], 1)

    def test_upgrade_preflight_failure_precedes_stop(self):
        from engine.maintenance import upgrade
        self.run_token()
        self.conn.execute('UPDATE hermes_executions SET signing_generation=NULL')
        self.conn.commit()
        source = Path(config.data_dir()) / 'source-fixture'
        source.mkdir()
        with patch.object(upgrade, '_fetch_and_extract', return_value=source), \
             patch.object(upgrade, '_validate_release'), \
             patch.object(upgrade, '_current_release_manifest', return_value=set()), \
             patch.object(upgrade, '_safe_stop') as stop:
            self.assertEqual(upgrade.do_upgrade(target=Mock(source='fixture', sha256='fixture')), 1)
            stop.assert_not_called()

    def test_fresh_process_can_verify_file_and_token(self):
        keys.initialize(self.conn)
        _, headers = self.run_token()
        payload = json.dumps({'file': str(keys.path()), 'token': headers['X-Group-Key']})
        result = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c',
            "import json,sys; from pathlib import Path; from engine import auth,signing_keys; "
            "v=json.load(sys.stdin); g,k=signing_keys.read(Path(v['file'])); "
            "assert auth.execution_scope('default',v['token'],k)['signing_generation']==g; print('verified')"],
            input=payload, text=True, capture_output=True, cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'verified')

    def test_file_permissions_and_source_package_exclusion(self):
        keys.initialize(self.conn)
        if os.name == 'nt':
            result = subprocess.run(['powershell', '-NoProfile', '-Command',
                "$a=Get-Acl -LiteralPath $env:TEST_SIGNING_PATH; "
                "if (!$a.AreAccessRulesProtected -or $a.Access.Count -ne 1) {exit 1}"],
                env={**os.environ, 'TEST_SIGNING_PATH': str(keys.path())}, capture_output=True)
            self.assertEqual(result.returncode, 0)
        else:
            self.assertEqual(keys.path().stat().st_mode & 0o777, 0o600)
            self.assertEqual(keys.path().parent.stat().st_mode & 0o777, 0o700)
        from engine.maintenance.make_release import _managed_files
        root = keys.path().parent.parent
        nested = root / 'engine' / 'secrets'
        nested.mkdir(parents=True)
        (nested / 'fixture.key').write_text('synthetic-key')
        (root / 'engine' / 'keep.py').write_text('pass')
        files = _managed_files(root)
        self.assertIn('engine/keep.py', files)
        self.assertFalse(any('secrets' in Path(p).parts for p in files))


if __name__ == '__main__':
    unittest.main()
