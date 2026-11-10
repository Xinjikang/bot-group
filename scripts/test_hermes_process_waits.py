"""Notifying terminal processes retain one logical execution through native callbacks."""
import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

from engine import hermes_delegations as waits, hermes_gateway as gateway
from engine import hermes_gateway_ledger as ledger, wake
from engine import hermes_background as background
from scripts import test_hermes_delegations as delegation_fixture


class ProcessWaitTests(unittest.TestCase):
    current = delegation_fixture.DelegationTests.current
    receipt = delegation_fixture.DelegationTests.receipt

    def setUp(self):
        delegation_fixture.DelegationTests.setUp(self)
        with closing(sqlite3.connect(self.native_path)) as native:
            native.execute('ALTER TABLE session_admissions ADD COLUMN principal_id TEXT')
            native.execute('ALTER TABLE session_admissions ADD COLUMN payload_json TEXT')

    def native(self, aid, pid='proc_one', *, kind='completion', generation=2,
               trusted=True, sid='sid', extras=()):
        identity = json.dumps([kind, pid, 100.0 if kind == 'completion' else 'event'])
        identities = [identity, *[json.dumps(['completion', item, 100.0]) for item in extras]]
        payload = {'native_text_v1': {'automation': {'identity': identity, 'identities': identities}}}
        with closing(sqlite3.connect(self.native_path)) as native:
            native.execute('INSERT INTO session_admissions(admission_id,request_id,target_session_id,'
                'status,generation,owner_epoch,principal_id,payload_json) VALUES(?,?,?,\'terminal\',?,1,?,?)',
                (aid, identity, sid, generation, 'automation:fixture' if trusted else 'client:fixture', json.dumps(payload)))
            native.commit()

    def wait(self, pids=('proc_one',), extra=()):
        receipt = self.receipt()
        receipt['result']['messages'] = [dict(role='tool', name='terminal',
            content=json.dumps(dict(session_id=pid, notify_on_complete=True, exit_code=0))) for pid in pids] + list(extra)
        with closing(sqlite3.connect(self.native_path)) as native:
            native.execute('INSERT INTO session_admissions(admission_id,request_id,target_session_id,'
                'status,generation,owner_epoch,principal_id,payload_json) '
                "VALUES('root','root-input','sid','terminal',1,1,'client:fixture','{}')")
            native.commit()
        row = ledger.apply_receipt(self.conn, self.eid, receipt)
        gateway._sync_delegation_state(self.conn, row)
        return row

    def test_parent_wait_preserves_task_and_shows_work_background_state(self):
        row = self.wait()
        self.assertEqual(row['state'], 'waiting_delegation')
        self.assertTrue(waits.owned(self.conn, row))
        self.assertIsNone(self.conn.execute('SELECT ended_at FROM wake_runs WHERE session=?',
                                           (row['session'],)).fetchone()[0])
        from engine import api, reasoning, session_adapter
        with patch.object(gateway, 'connection', side_effect=AssertionError('HTTP contacted native gateway')):
            runs = api.get_thinking()['runs']
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]['background_kind'], 'process')
        self.assertEqual(runs[0]['kind'], 'work')
        self.assertEqual(self.conn.execute('SELECT status FROM tasks WHERE id=1').fetchone()[0], 'in_progress')
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events WHERE event_type='execution.failed'").fetchone()[0], 0)
        with patch.object(session_adapter, 'compression_tip', return_value='sid'), \
             patch.object(reasoning, 'read_reasoning', return_value={
                 'entries': [{'id': 1, 'finished': True}], 'latest_id': 1}):
            result = api.get_reasoning('default', row['session'], row['started_at'])
        self.assertEqual(result['background_kind'], 'process')
        self.assertEqual(result['execution_state'], 'waiting_delegation')
        self.assertFalse(result['entries'][0]['finished'])
        self.assertIsNone(result['process_ended_at'])

    def test_native_completion_settles_original_execution_once(self):
        self.assertEqual(self.wait()['state'], 'waiting_delegation')
        self.native('completion')
        remote = Mock()
        self.assertTrue(waits.poll(self.conn, self.current(), remote))
        remote.rpc.return_value = self.receipt('completion', 2)
        final = ledger.reconcile(remote, self.conn, self.eid)
        self.assertEqual(final['state'], 'terminal')
        self.assertFalse(waits.pending(self.conn, final))
        self.assertFalse(waits.adopt(self.conn, final, waits.discover_turns(self.conn, final)))
        with patch.object(wake, '_handle_task_exit') as finish:
            gateway._finish(self.conn, final, remote)
        finish.assert_called_once()

    def test_watch_notice_preserves_wait_then_batched_completion_settles_both(self):
        self.assertEqual(self.wait(('proc_one', 'proc_two'))['state'], 'waiting_delegation')
        remote = Mock()
        self.native('watch', kind='watch_match')
        self.assertTrue(waits.poll(self.conn, self.current(), remote))
        row = ledger.apply_receipt(self.conn, self.eid, self.receipt('watch', 2))
        self.assertEqual(row['state'], 'waiting_delegation')
        self.native('both', generation=3, extras=('proc_two',))
        self.assertTrue(waits.poll(self.conn, self.current(), remote))
        row = ledger.apply_receipt(self.conn, self.eid, self.receipt('both', 3))
        self.assertEqual(row['state'], 'terminal')
        self.assertFalse(waits.pending(self.conn, row))

    def test_stale_foreign_and_client_callbacks_do_not_settle_wait(self):
        self.native('stale')
        self.assertEqual(self.wait()['state'], 'waiting_delegation')
        self.native('foreign', sid='other-session')
        self.native('client-forgery', trusted=False)
        self.native('other-process', pid='proc_other')
        remote = Mock()
        self.assertFalse(waits.poll(self.conn, self.current(), remote))
        remote.rpc.assert_not_called()
        self.native('ours')
        self.assertTrue(waits.poll(self.conn, self.current(), remote))
        self.assertEqual(self.current()['admission_id'], 'ours')

    def test_process_killed_in_parent_has_no_pending_completion(self):
        killed = dict(role='tool', name='process_manage',
                      content=json.dumps(dict(session_id='proc_one', status='killed')))
        row = self.wait(extra=(killed,))
        self.assertEqual(row['state'], 'terminal')
        self.assertFalse(waits.pending(self.conn, row))

    def test_mixed_process_and_delegation_wait_for_both_callbacks(self):
        delegated = self.receipt(dispatched=('child',))['result']['messages']
        row = self.wait(extra=delegated)
        self.assertEqual(waits.background_kind(self.conn, row), 'mixed')
        self.native('process-complete')
        self.assertTrue(waits.poll(self.conn, row, Mock()))
        row = ledger.apply_receipt(self.conn, self.eid, self.receipt('process-complete', 2))
        self.assertEqual(row['state'], 'waiting_delegation')
        self.assertEqual(waits.background_kind(self.conn, row), 'delegation')
        delegation_fixture.DelegationTests.callback(self, 'child', 'child-complete', 3)
        self.assertTrue(waits.poll(self.conn, row, Mock()))
        row = ledger.apply_receipt(self.conn, self.eid, self.receipt('child-complete', 3))
        self.assertEqual(row['state'], 'terminal')

    def test_missing_native_origin_keeps_wait_instead_of_adopting(self):
        row = self.wait()
        with closing(sqlite3.connect(self.native_path)) as native:
            native.execute("DELETE FROM session_admissions WHERE admission_id='root'")
            native.commit()
        self.native('unproven')
        with self.assertRaises(sqlite3.OperationalError):
            waits.poll(self.conn, row, Mock())
        self.assertEqual(self.current()['state'], 'waiting_delegation')

    def test_malformed_tool_messages_are_ignored(self):
        receipt = self.receipt()
        receipt['result']['messages'] = None
        row = ledger.apply_receipt(self.conn, self.eid, receipt)
        self.assertEqual(row['state'], 'terminal')

    def test_wait_survives_engine_recovery_and_stop_remains_available(self):
        self.assertEqual(self.wait()['state'], 'waiting_delegation')
        with patch.object(gateway, '_start_worker') as start:
            gateway.recover()
        start.assert_called_once_with(self.eid)
        row = ledger.cancel(self.conn, self.eid, 'user')
        remote = Mock()
        remote.rpc.side_effect = lambda method, **kw: (dict(running=False, pending=[])
            if method == 'session.resume' else self.receipt(outcome='cancelled')
            if method == 'prompt.receipt' else {})
        self.assertTrue(waits.poll(self.conn, row, remote))
        self.assertEqual(self.current()['state'], 'terminal')
        self.assertFalse(waits.pending(self.conn, self.current()))


class ProcessProofTests(unittest.TestCase):
    def test_callback_requires_structured_matching_automation_descriptor(self):
        raw = json.dumps(['completion', 'proc_one', 100.0])
        item = {'request_id': raw}
        for value in ({}, {'native_text_v1': 'invalid'}, {'local_automation_v1': []},
                      {'local_automation_v1': {'identities': 'invalid'}},
                      {'local_automation_v1': {'identities': []}}):
            with self.subTest(value=value):
                self.assertEqual(background.callback_waits(item, 'automation:test', json.dumps(value), {'proc_one'}), [])
        descriptor = json.dumps({'local_automation_v1': {'identity': raw}})
        self.assertEqual(background.callback_waits(item, 'automation:test', descriptor, {'proc_one'}), [('process:proc_one', 1)])

    def test_process_identity_rejects_invalid_completion_epoch(self):
        for token in (None, True, '100', float('inf'), float('nan'), 10 ** 500, {}):
            with self.subTest(token=token):
                self.assertIsNone(background.identity(json.dumps(['completion', 'proc_one', token])))

    def test_explicit_notification_disable_and_tool_ownership(self):
        def message(name, value):
            return dict(role='tool', name=name, content=json.dumps(value))
        result = background.process_updates([
            None, message('delegate_task', dict(session_id='proc_foreign', notify_on_complete=True)),
            message('terminal', dict(session_id='proc_one', notify_on_complete=True)),
            message('process_manage', dict(session_id='proc_one', notify_on_complete=False)),
            message('terminal', dict(session_id='unowned', notify_on_complete=True))])
        self.assertEqual(result, {'process:proc_one': False})


if __name__ == '__main__':
    unittest.main()
