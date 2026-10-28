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
        # 收紧 active：terminal 即释放执行占用（cleanup_pending 不算 active）。
        self.assertEqual(ledger.active(self.conn), [])
        self.assertEqual(len(ledger.cleanup_pending(self.conn)), 1)
        ledger.finish_cleanup(self.conn, self.eid)
        ledger.finish_cleanup(self.conn, self.eid)
        self.assertEqual(ledger.active(self.conn), [])
        self.assertEqual(ledger.cleanup_pending(self.conn), [])

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
        ledger.cancel(self.conn, self.eid, now=115)
        ledger.uncertain(self.conn, self.eid, 'lost', now=266)
        self.assertEqual(self.accept()['state'], 'manual')
        self.assertEqual(self.accept('terminal')['state'], 'terminal')

    def test_long_execution_disconnect_recovers_original_admission(self):
        self.accept()
        before = dict(ledger.read(self.conn, self.eid))
        ledger.uncertain(self.conn, self.eid, 'offline', now=300)
        self.assertEqual(ledger.read(self.conn, self.eid)['state'], 'unknown')
        row = ledger.apply_receipt(self.conn, self.eid, self.receipt('unknown'), now=301)
        self.assertEqual(row['state'], 'unknown')
        remote = Mock()
        remote.rpc.return_value = self.receipt('terminal')
        row = ledger.reconcile(remote, self.conn, self.eid, now=302)
        self.assertEqual(row['state'], 'terminal')
        for key in ('admission_id', 'submission_id', 'reconcile_deadline', 'execution_deadline'):
            self.assertEqual(row[key], before[key])
        remote.rpc.assert_called_once_with('prompt.receipt', session_id='sid', admission_id='a', timeout=30.0)

    def test_unreachable_execution_still_has_original_execution_budget(self):
        self.accept()
        ledger.uncertain(self.conn, self.eid, 'offline', now=3700)
        self.assertEqual(ledger.read(self.conn, self.eid)['state'], 'manual')

    def test_transport_loss_preserves_admission(self):
        self.accept()
        client = Mock()
        client.rpc.side_effect = ConnectionError('lost')
        with self.assertRaises(ConnectionError):
            ledger.reconcile(client, self.conn, self.eid)
        self.assertEqual(ledger.read(self.conn, self.eid)['admission_id'], 'a')
        self.assertEqual(client.rpc.call_count, 1)

    def test_terminal_sets_cleanup_deadline(self):
        self.accept('terminal')
        row = ledger.read(self.conn, self.eid)
        self.assertEqual(row['cleanup_pending'], 1)
        self.assertEqual(row['cleanup_deadline'], 110 + ledger.CLEANUP_BUDGET_SECONDS)
        self.assertEqual(row['retry_count'], 0)

    def test_cleanup_retry_persists_count_and_backoff(self):
        self.accept('terminal')
        first = ledger.register_cleanup_retry(self.conn, self.eid, 'offline', now=120)
        self.assertEqual(first, 1)
        row = ledger.read(self.conn, self.eid)
        self.assertEqual(row['retry_count'], 1)
        self.assertEqual(row['next_retry_at'], 120 + 1.0)
        second = ledger.register_cleanup_retry(self.conn, self.eid, 'offline', now=130)
        self.assertEqual(second, 2)
        self.assertEqual(ledger.read(self.conn, self.eid)['next_retry_at'], 130 + 2.0)

    def test_cleanup_exhaustion_marks_failed_and_keeps_pending(self):
        self.accept('terminal')
        attempts = 0
        for i in range(ledger.CLEANUP_MAX_ATTEMPTS):
            attempts = ledger.register_cleanup_retry(self.conn, self.eid, 'offline', now=100 + i * 10)
        self.assertEqual(attempts, ledger.CLEANUP_MAX_ATTEMPTS)
        ledger.mark_cleanup_failed(self.conn, self.eid, 'offline')
        row = ledger.read(self.conn, self.eid)
        self.assertEqual(row['retry_count'], ledger.CLEANUP_MAX_ATTEMPTS)
        self.assertEqual(row['cleanup_pending'], 1)
        self.assertIn('cleanup_failed after 5 attempts', row['detail'])
        # 耗尽后仍是清理待办（会话禁用保留），但不占执行占用。
        self.assertEqual(ledger.active(self.conn), [])
        self.assertEqual(len(ledger.cleanup_pending(self.conn)), 1)

    def test_finish_cleanup_resets_retry_fields(self):
        self.accept('terminal')
        ledger.register_cleanup_retry(self.conn, self.eid, 'offline', now=120)
        ledger.finish_cleanup(self.conn, self.eid)
        row = ledger.read(self.conn, self.eid)
        self.assertEqual(row['cleanup_pending'], 0)
        self.assertEqual(row['retry_count'], 0)
        self.assertIsNone(row['next_retry_at'])
        self.assertIsNone(row['cleanup_deadline'])

    def test_register_cleanup_retry_ignores_non_terminal(self):
        self.accept('started')
        self.assertEqual(ledger.register_cleanup_retry(self.conn, self.eid, 'x', now=120), 0)
        self.assertEqual(ledger.read(self.conn, self.eid)['retry_count'], 0)

    def test_reconcile_rpc_timeout_caps_to_remaining_budget(self):
        self.accept()
        ledger.cancel(self.conn, self.eid, now=115)  # reconcile_deadline=115+150=265
        client = Mock()
        client.rpc.side_effect = [self.receipt(), {'execution_state': 'idle'}]
        ledger.reconcile(client, self.conn, self.eid, now=240)  # remaining=25 < 30
        self.assertEqual(client.rpc.call_args_list[0].kwargs['timeout'], 25.0)
        self.assertEqual(client.rpc.call_args_list[1].kwargs['timeout'], 25.0)

    def test_reconcile_rpc_timeout_caps_to_default_thirty(self):
        self.accept()
        ledger.cancel(self.conn, self.eid, now=115)
        client = Mock()
        client.rpc.side_effect = [self.receipt(), {'execution_state': 'idle'}]
        ledger.reconcile(client, self.conn, self.eid, now=180)  # remaining=85 → cap 30
        self.assertEqual(client.rpc.call_args_list[0].kwargs['timeout'], 30.0)

    def test_reconcile_rpc_timeout_zero_when_budget_exhausted(self):
        self.accept()
        ledger.cancel(self.conn, self.eid, now=115)
        client = Mock()
        client.rpc.side_effect = [self.receipt(), {'execution_state': 'idle'}]
        ledger.reconcile(client, self.conn, self.eid, now=270)  # remaining=0
        self.assertEqual(client.rpc.call_args_list[0].kwargs['timeout'], 0.0)

    def test_reconcile_rpc_timeout_keeps_thirty_when_not_cancelled(self):
        # 非取消 running 执行运行超 reconcile_deadline（started_at+150）时，
        # 有界超时裁剪不得生效：正常轮询保持 30s（reviewer #255 高危回归）。
        self.accept()  # state=running，无 cancel_reason，reconcile_deadline=250
        client = Mock()
        client.rpc.side_effect = [self.receipt()]
        ledger.reconcile(client, self.conn, self.eid, now=260)  # now > 250，但未取消
        self.assertEqual(client.rpc.call_args_list[0].kwargs['timeout'], 30.0)


if __name__ == '__main__':
    unittest.main()
