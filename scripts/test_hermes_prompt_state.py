"""Rule deduplication across creation, admissions, recovery and session replacement."""
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from engine import db, hermes_gateway as gateway, hermes_gateway_ledger as ledger
from engine import hermes_prompt_state as prompts, session_adapter, wake
from engine.hermes_connections import Pool
from engine.hermes_gateway_client import GatewayTransportError
from scripts.test_hermes_gateway_sessions import _snapshot
from scripts.unified_test_support import isolated_database


class PromptLedgerTests(unittest.TestCase):
    def setUp(self):
        fixture = isolated_database()
        fixture.__enter__()
        self.addCleanup(fixture.__exit__, None, None, None)
        self.conn = db.get_connection()
        self.addCleanup(lambda: self.conn.close())
        self.counter = 0

    def prepare(self, prompt, *, profile='pid', sid='sid'):
        self.counter += 1
        return ledger.prepare(self.conn, session='logical-' + str(self.counter), project='alpha',
            profile='default', kind='work', thread_id=None, task_id=None, job_id=None, event_floor=0,
            source='', python='[]', home='fixture', workdir='fixture', session_id=sid, profile_id=profile,
            prompt_hash=prompts.pending_hash(self.conn, profile, sid, prompt))

    def receipt(self, row, status='started', **overrides):
        return {'admission_id': 'a-' + row['execution_id'], 'status': status,
                'ref': {'profile_id': row['profile_id'], 'session_id': row['session_id']},
                'authority_epoch': 1, 'execution_generation': 1,
                'outcome': 'completed' if status == 'terminal' else None, **overrides}

    def accept(self, row, status='started'):
        return ledger.apply_receipt(self.conn, row['execution_id'], self.receipt(row, status))

    def test_initial_system_and_aba_use_latest_user_hash(self):
        prompts.created(self.conn, 'pid', 'sid', 'A'); self.conn.commit()
        self.assertIsNone(prompts.pending_hash(self.conn, 'pid', 'sid', 'A'))
        self.accept(self.prepare('B'))
        self.assertEqual(prompts.pending_hash(self.conn, 'pid', 'sid', 'A'), prompts.digest('A'))
        self.accept(self.prepare('A'))
        self.assertIsNone(prompts.pending_hash(self.conn, 'pid', 'sid', 'A'))
        row = self.conn.execute('SELECT * FROM hermes_prompt_state').fetchone()
        self.assertEqual((row['system_hash'], row['user_hash']), (prompts.digest('A'), prompts.digest('A')))

    def test_old_session_records_only_user_layer_after_acceptance(self):
        row = self.prepare('A')
        self.assertIsNotNone(prompts.pending_hash(self.conn, 'pid', 'sid', 'A'))
        self.accept(row, 'queued')
        state = self.conn.execute('SELECT * FROM hermes_prompt_state').fetchone()
        self.assertIsNone(state['system_hash'])
        self.assertEqual(state['user_hash'], prompts.digest('A'))

    def test_reopen_and_admission_reconciliation_commit_pending_hash(self):
        row = self.prepare('A')
        self.accept(row, 'unknown')
        self.assertIsNotNone(prompts.pending_hash(self.conn, 'pid', 'sid', 'A'))
        self.conn.close(); self.conn = db.get_connection()
        remote = Mock()
        remote.rpc.return_value = self.receipt(row, 'terminal')
        ledger.reconcile(remote, self.conn, row['execution_id'])
        self.assertIsNone(prompts.pending_hash(self.conn, 'pid', 'sid', 'A'))
        self.assertEqual([c.args[0] for c in remote.rpc.call_args_list], ['prompt.receipt'])

    def test_lost_first_receipt_holds_without_resend_or_marking_hash(self):
        row = self.prepare('A')
        ledger.uncertain(self.conn, row['execution_id'], 'lost')
        remote = Mock()
        ledger.reconcile(remote, self.conn, row['execution_id'], now=row['reconcile_deadline']+1)
        remote.rpc.assert_not_called()
        self.assertEqual(ledger.read(self.conn, row['execution_id'])['state'], 'manual')
        self.assertIsNotNone(prompts.pending_hash(self.conn, 'pid', 'sid', 'A'))

    def test_unsubmitted_execution_keeps_previous_version(self):
        self.accept(self.prepare('A'))
        row = self.prepare('B')
        ledger.not_submitted(self.conn, row['execution_id'], 'configuration failed')
        self.assertIsNone(prompts.pending_hash(self.conn, 'pid', 'sid', 'A'))
        self.assertIsNotNone(prompts.pending_hash(self.conn, 'pid', 'sid', 'B'))

    def test_stale_and_duplicate_receipts_do_not_overwrite_latest(self):
        first, second = self.prepare('A'), self.prepare('B')
        self.accept(second)
        self.accept(first)
        self.accept(first, 'terminal')
        self.assertIsNone(prompts.pending_hash(self.conn, 'pid', 'sid', 'B'))

    def test_invalid_receipt_and_failed_commit_preserve_pending_hash(self):
        row = self.prepare('A')
        invalid = self.receipt(row, ref={'profile_id': 'wrong', 'session_id': 'sid'})
        with self.assertRaises(ValueError): ledger.apply_receipt(self.conn, row['execution_id'], invalid)
        original = prompts.accepted
        def fail_after_projection(conn, eid):
            original(conn, eid)
            raise RuntimeError('fixture transaction abort')
        with patch.object(prompts, 'accepted', side_effect=fail_after_projection), self.assertRaises(RuntimeError):
            self.accept(row)
        self.assertIsNone(ledger.read(self.conn, row['execution_id'])['admission_id'])
        self.assertIsNotNone(prompts.pending_hash(self.conn, 'pid', 'sid', 'A'))
        self.accept(row)
        self.assertIsNone(prompts.pending_hash(self.conn, 'pid', 'sid', 'A'))

    def test_profile_and_new_session_are_independent(self):
        self.accept(self.prepare('A'))
        for profile, sid in (('other', 'sid'), ('pid', 'new')):
            self.assertIsNotNone(prompts.pending_hash(self.conn, profile, sid, 'A'))
        prompts.created(self.conn, 'pid', 'new', 'B'); self.conn.commit()
        self.assertIsNone(prompts.pending_hash(self.conn, 'pid', 'new', 'B'))

    def test_rewind_restores_previous_accepted_version(self):
        self.accept(self.prepare('A'))
        second = self.prepare('B'); self.accept(second)
        prompts.withdraw(self.conn, second['execution_id'], 'rewound'); self.conn.commit()
        self.accept(second, 'terminal')
        self.assertIsNone(prompts.pending_hash(self.conn, 'pid', 'sid', 'A'))
        self.assertIsNotNone(prompts.pending_hash(self.conn, 'pid', 'sid', 'B'))

    def test_queued_cancellation_withdraws_update_before_it_reaches_conversation(self):
        self.accept(self.prepare('A'))
        second = self.prepare('B')
        ledger.apply_receipt(self.conn, second['execution_id'], self.receipt(second, 'queued', execution_generation=None))
        self.assertIsNone(prompts.pending_hash(self.conn, 'pid', 'sid', 'B'))
        cancelled = self.receipt(second, 'terminal', execution_generation=None, outcome='cancelled')
        ledger.apply_receipt(self.conn, second['execution_id'], cancelled)
        self.assertIsNone(prompts.pending_hash(self.conn, 'pid', 'sid', 'A'))
        self.assertIsNotNone(prompts.pending_hash(self.conn, 'pid', 'sid', 'B'))

    def test_schema_initialization_preserves_existing_versions(self):
        self.accept(self.prepare('A'))
        ledger.initialize(self.conn)
        self.assertIsNone(prompts.pending_hash(self.conn, 'pid', 'sid', 'A'))


class WakePromptTests(unittest.TestCase):
    def setUp(self):
        fixture = isolated_database(); fixture.__enter__()
        self.addCleanup(fixture.__exit__, None, None, None)
        self.conn = db.get_connection(); self.addCleanup(self.conn.close)
        self.bot = {'id': db._ensure_hermes_bot(self.conn, 'default')}
        self.conn.commit()
        self.home = Path(db.db_path()).parent
        self.remote = Mock(endpoint={'profile_id': 'pid'}, closed=False)
        self.remote.rpc.side_effect = self.rpc
        self.pool = Pool(opener=lambda *a, **kw: self.remote,
                         revision=lambda: ('fixture', 'sha'), pinned=lambda _: False)
        self.addCleanup(self.pool.stop)
        self.created, self.sent, self.prompt, self.lost = [], [], 'A', False
        patches = [patch.object(gateway, 'connection', side_effect=lambda *a, **kw:
                        (self.pool.acquire(self.home, ['fixture']), self.home, ['fixture'])),
                   patch.object(wake, 'wake_system_prompt', side_effect=lambda *a: self.prompt),
                   patch.object(wake, '_mcp_server_config', return_value={'command':'fixture','args':[],'env':{}}),
                   patch('engine.browser_resources.register', side_effect=self.resources),
                   patch.object(gateway, '_publish'), patch.object(gateway, '_start_worker')]
        for p in patches: p.start(); self.addCleanup(p.stop)

    def resources(self, conn, env, **kwargs):
        env.update({key: 'fixture' for key in ('BH_RUNTIME_DIR','BH_RUNTIME_DIR_SHARED','BH_TMP_DIR','BH_TMP_DIR_SHARED','BU_NAME')})

    def rpc(self, method, **params):
        if method == 'runtime.describe':
            return {'profile_id':'pid','capabilities':['durable-admission-v1','acp-session-mcp-v1']}
        if method == 'session.list': return {'sessions': []}
        if method == 'session.create':
            self.created.append(params)
            return {**_snapshot(), 'session_id': 'sid-' + str(len(self.created))}
        if method == 'session.resume': return {**_snapshot(), 'session_id': params['session_id']}
        if method == 'session.mutate': return {}
        if method == 'prompt.submit':
            self.sent.append(params)
            if self.lost: raise GatewayTransportError('fixture lost receipt')
            return {'admission_id': params['submission_id'], 'ref': {'profile_id':'pid','session_id':params['session_id']},
                    'status':'terminal','outcome':'completed','authority_epoch':1,'execution_generation':1}
        raise AssertionError(method)

    def spawn(self, signal='event'):
        result = gateway.spawn('default','work',signal,None,'alpha',bot=self.bot,workdir='/w',model_args=[])
        for row in ledger.cleanup_pending(self.conn): ledger.finish_cleanup(self.conn, row['execution_id'])
        return result

    def test_failed_handoff_keeps_next_wake_queued_without_execution(self):
        from engine import auth
        ring = (1, {1: b'old'})
        with patch.object(auth, 'signing_keys', side_effect=lambda: ring):
            self.assertTrue(self.spawn()['ok'])
            ring = (2, {1: b'old', 2: b'new'})
            def fail_configuration(method, **params):
                if method == 'session.mutate' and params['operation'] == 'editor_mcp':
                    raise GatewayTransportError('fixture handoff unavailable')
                return self.rpc(method, **params)
            self.remote.rpc.side_effect = fail_configuration
            result = self.spawn()
            self.assertTrue(result['busy'], result)
            self.assertEqual(len(self.sent), 1)
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM hermes_executions').fetchone()[0], 1)
            self.assertEqual(self.conn.execute('SELECT pending_generation FROM hermes_session_grants').fetchone()[0], 2)
            self.remote.rpc.side_effect = self.rpc
            self.assertTrue(self.spawn()['ok'])
            self.assertEqual(len(self.sent), 2)

    def test_new_session_and_aba_send_rules_exactly_when_needed(self):
        for index, prompt in enumerate(('A','A','B','B','A','A')):
            self.prompt = prompt
            self.assertTrue(self.spawn(signal='event-' + str(index))['ok'])
        self.assertEqual(self.created[0]['client_env']['HERMES_EPHEMERAL_SYSTEM_PROMPT'], 'A')
        self.assertEqual([r['text'] for r in self.sent],
                         ['event-0','event-1','B\n\nevent-2','event-3','A\n\nevent-4','event-5'])

    def test_existing_session_missing_client_env_is_backfilled_once(self):
        self.conn.execute("INSERT INTO session_bindings(project,bot_id,profile,kind,thread_id,session_id,runtime,updated_at) "
                          "VALUES('alpha',?,'default','work',0,'legacy','hermes',?)", (self.bot['id'], db.now_iso()))
        self.conn.commit()
        self.spawn(); self.spawn()
        self.assertEqual([r['text'] for r in self.sent], ['A\n\nevent','event'])
        self.assertEqual(self.created, [])
        self.assertIsNone(self.conn.execute('SELECT system_hash FROM hermes_prompt_state').fetchone()[0])

    def test_lost_receipt_blocks_next_wake_and_keeps_user_hash_unknown(self):
        self.spawn(); self.prompt = 'B'; self.lost = True
        self.spawn()
        self.assertTrue(self.spawn()['busy'])
        self.assertEqual(len(self.sent), 2)
        self.assertIsNotNone(prompts.pending_hash(self.conn, 'pid', 'sid-1', 'B'))

    def test_migrated_session_backfills_user_rules_without_claiming_system_identity(self):
        def legacy_rpc(method, **params):
            if method == 'session.list': return {'sessions': [{'session_id': 'old-cli'}]}
            if method == 'session.resume' and params['session_id'] == 'old-cli':
                return {**_snapshot(source='cli'), 'session_id': 'old-cli'}
            return self.rpc(method, **params)
        self.remote.rpc.side_effect = legacy_rpc
        with patch.object(gateway.sessions, 'migrate_legacy', return_value={**_snapshot(), 'session_id':'migrated'}):
            self.spawn(); self.spawn()
        self.assertEqual([r['text'] for r in self.sent], ['A\n\nevent', 'event'])
        row = self.conn.execute("SELECT * FROM hermes_prompt_state WHERE session_id='migrated'").fetchone()
        self.assertIsNone(row['system_hash'])
        self.assertEqual(row['user_hash'], prompts.digest('A'))

    def test_new_command_creates_fresh_system_rules(self):
        self.spawn(); self.prompt = 'B'
        with patch.object(gateway, 'enabled', return_value=True), patch.object(wake, '_workdir_for', return_value='/w'):
            result = session_adapter.switch_session(self.conn, 'alpha', 'default', 'work', None, 'new-title',
                                                    busy_check=lambda *args: False)
        self.assertTrue(result['ok'], result)
        self.assertEqual(self.created[-1]['client_env']['HERMES_EPHEMERAL_SYSTEM_PROMPT'], 'B')
        self.spawn()
        self.assertEqual(self.sent[-1]['text'], 'event')
        self.assertIsNone(prompts.pending_hash(self.conn, 'pid', result['session_id'], 'B'))


if __name__ == '__main__': unittest.main()
