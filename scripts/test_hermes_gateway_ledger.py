"""Crash, cancellation races and authoritative settlement of gateway admissions."""
import sqlite3
import unittest
from unittest.mock import Mock

from engine import hermes_gateway_ledger as ledger


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        ledger.initialize(self.conn)
        self.row = ledger.prepare(self.conn, session='logical', project='p', profile='b', kind='work',
            thread_id=None, task_id=1, job_id=2, event_floor=0, source='/src', python='/python',
            home='/home', workdir='/project', session_id='sid', profile_id='pid', started_at=100)
        self.eid = self.row['execution_id']

    def receipt(self, status='started', **changes):
        return {'admission_id': 'a', 'ref': {'session_id': 'sid', 'profile_id': 'pid'},
                'status': status, 'execution_generation': 7, 'authority_epoch': 3,
                'outcome': 'completed' if status == 'terminal' else None, **changes}

    def accept(self, status='started', **changes):
        return ledger.apply_receipt(self.conn, self.eid, self.receipt(status, **changes), now=110)

    def test_write_ahead_identity_has_fixed_deadlines(self):
        self.assertTrue(self.row['submission_id'])
        self.assertEqual(self.row['submit_deadline'], 130)
        self.assertEqual(self.row['reconcile_deadline'], 250)
        self.assertEqual(self.row['execution_deadline'], 3700)
        self.assertFalse(self.conn.in_transaction)

    def test_foreign_receipt_cannot_change_identity(self):
        self.accept()
        for change in ({'admission_id': 'foreign'}, {'ref': {'session_id': 'other', 'profile_id': 'pid'}},
                       {'authority_epoch': 2}, {'execution_generation': 6}):
            with self.assertRaises(ValueError):
                self.accept('terminal', **change)
        self.assertEqual(ledger.read(self.conn, self.eid)['state'], 'running')

    def test_terminal_is_absorbing_and_cleanup_is_durable(self):
        self.accept('terminal')
        self.accept('started')
        self.assertEqual(ledger.active(self.conn)[0]['state'], 'terminal')
        ledger.finish_cleanup(self.conn, self.eid)
        ledger.finish_cleanup(self.conn, self.eid)
        self.assertEqual(ledger.active(self.conn), [])

    def test_cancel_retries_keep_original_reason_and_deadlines(self):
        first = ledger.cancel(self.conn, self.eid, 'user', now=115)
        again = ledger.cancel(self.conn, self.eid, 'timeout', now=160)
        self.assertEqual(first['cancel_deadline'], again['cancel_deadline'])
        self.assertEqual(first['reconcile_deadline'], again['reconcile_deadline'])
        self.assertEqual(again['cancel_reason'], 'user')

    def test_cancel_race_uses_started_generation_and_keeps_slot(self):
        self.accept()
        ledger.cancel(self.conn, self.eid, now=115)
        client = Mock()
        client.rpc.side_effect = [self.receipt(), {'execution_state': 'idle'}]
        row = ledger.reconcile(client, self.conn, self.eid)
        self.assertEqual(row['state'], 'cancelling')
        self.assertIsNone(row['ended_at'])
        self.assertEqual(client.rpc.call_args.args, ('session.interrupt',))
        self.assertEqual(client.rpc.call_args.kwargs['execution_generation'], 7)

    def test_queued_cancel_can_return_started(self):
        self.accept('queued')
        ledger.cancel(self.conn, self.eid, now=115)
        client = Mock()
        client.rpc.side_effect = [self.receipt('queued'), self.receipt('started')]
        row = ledger.reconcile(client, self.conn, self.eid)
        self.assertEqual(row['state'], 'cancelling')
        self.assertIsNone(row['ended_at'])

    def test_missing_submit_receipt_never_resubmits(self):
        client = Mock()
        row = ledger.reconcile(client, self.conn, self.eid, now=251)
        self.assertEqual(row['state'], 'manual')
        client.rpc.assert_not_called()
        self.assertEqual(len(ledger.active(self.conn)), 1)

    def test_manual_hold_survives_nonterminal_receipt(self):
        self.accept()
        ledger.uncertain(self.conn, self.eid, 'lost', now=251)
        self.assertEqual(self.accept()['state'], 'manual')
        self.assertEqual(self.accept('terminal')['state'], 'terminal')

    def test_transport_loss_preserves_admission(self):
        self.accept()
        client = Mock()
        client.rpc.side_effect = ConnectionError('lost')
        with self.assertRaises(ConnectionError):
            ledger.reconcile(client, self.conn, self.eid)
        self.assertEqual(ledger.read(self.conn, self.eid)['admission_id'], 'a')
        self.assertEqual(client.rpc.call_count, 1)


if __name__ == '__main__':
    unittest.main()
