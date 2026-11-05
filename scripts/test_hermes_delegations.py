"""Isolated native delegation waits, current-owner writes and independent scheduling."""
import json
from pathlib import Path
import sqlite3
import time
import unittest
from contextlib import closing
from unittest.mock import Mock, patch

from engine import db, heartbeat, hermes_delegations as delegations, hermes_gateway as gateway
from engine import hermes_gateway_ledger as ledger, queue, router, wake
from scripts import test_hermes_session_reuse as session_fixture


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = session_fixture.SessionReuseTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.conn = self.fixture.conn
        for target, name, value in [(wake, '_running_sessions', set()), (wake, '_running_wakes', {}),
                                    (heartbeat, '_beats', {})]:
            p = patch.object(target, name, value); p.start(); self.addCleanup(p.stop)
        self.row = self.fixture.run_turn()
        self.eid = self.row['execution_id']
        self.conn.execute("INSERT INTO requirements(id,project,title,created_at,updated_at) VALUES(1,'alpha','fixture',?,?)",
                          (db.now_iso(), db.now_iso()))
        self.conn.execute("INSERT INTO tasks(id,project,requirement_id,title,status,claimed_by,created_at) "
                          "VALUES(1,'alpha',1,'fixture','in_progress','default',?)", (db.now_iso(),))
        self.conn.execute('UPDATE hermes_executions SET task_id=1 WHERE execution_id=?', (self.eid,))
        self.conn.commit()
        self.native_path = Path(self.row['home']) / 'state.db'
        with closing(sqlite3.connect(self.native_path)) as native:
            native.execute('CREATE TABLE session_admissions(seq INTEGER PRIMARY KEY, admission_id TEXT, '
                'request_id TEXT, target_session_id TEXT, status TEXT, generation INTEGER, owner_epoch INTEGER)')

    def current(self):
        return ledger.read(self.conn, self.eid)

    def receipt(self, aid='root', generation=1, dispatched=(), outcome='completed'):
        messages = []
        if dispatched:
            payload = dict(status='dispatched', mode='background', delegation_id=dispatched[0])
            if len(dispatched) > 1:
                payload['delegation_id'] = 'display-only-batch'
                payload['units'] = [{'delegation_id': d, 'task_indexes': [i]} for i, d in enumerate(dispatched)]
            messages = [dict(role='tool', name='delegate_task', content=json.dumps(payload))]
        return dict(admission_id=aid, ref={'session_id': 'sid', 'profile_id': 'fixture'},
                    status='terminal', outcome=outcome, execution_generation=generation, authority_epoch=1,
                    result={'messages': messages})

    def wait(self, ids=('d1',)):
        gateway._publish(self.current())
        row = ledger.apply_receipt(self.conn, self.eid, self.receipt(dispatched=ids))
        gateway._sync_delegation_state(self.conn, row)
        return row

    def callback(self, did='d1', aid='child-result', generation=2, sid='sid', request=None):
        request = request or json.dumps(['async_delegation', did, ''])
        with closing(sqlite3.connect(self.native_path)) as native:
            native.execute('INSERT INTO session_admissions(admission_id,request_id,target_session_id,status,generation,owner_epoch) '
                           "VALUES(?,?,?,'terminal',?,1)", (aid, request, sid, generation))
            native.commit()

    def test_wait_clears_thinking_keeps_writes_and_never_retries_task(self):
        gateway._publish(self.current())
        self.fixture.client.post('/heartbeat', headers=self.fixture.headers,
                                 json={'profile': 'default', 'thread_id': None, 'pid': 321})
        self.wait()
        self.assertEqual(self.current()['state'], 'waiting_delegation')
        self.assertTrue(gateway.active(self.row['session']))
        self.assertEqual(wake.active_runs(), [])
        beat = self.fixture.client.post('/heartbeat', headers=self.fixture.headers,
                                       json={'profile': 'default', 'thread_id': None, 'pid': 321})
        self.assertTrue(beat.json()['idle'], beat.text)
        with patch.object(gateway, 'connection', side_effect=AssertionError('HTTP read queried gateway')):
            from engine import api
            self.assertEqual(api.get_thinking(), {'thinking': [], 'runs': []})
        response = self.fixture.send()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events WHERE event_type='execution.failed'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT status FROM tasks WHERE id=1').fetchone()[0], 'in_progress')

    def test_completed_child_delivery_gap_remains_waiting_then_rpc_settles_callback(self):
        self.wait()
        remote = Mock()
        self.assertFalse(delegations.poll(self.conn, self.current(), remote))
        remote.rpc.assert_not_called()
        self.assertEqual(self.current()['state'], 'waiting_delegation')
        self.callback()
        self.assertTrue(delegations.poll(self.conn, self.current(), remote))
        remote.rpc.return_value = self.receipt('child-result', 2)
        final = ledger.reconcile(remote, self.conn, self.eid)
        self.assertEqual(final['state'], 'terminal')
        self.assertFalse(delegations.pending(self.conn, final))
        self.assertEqual(remote.rpc.call_args.kwargs['include_result'], True)
        with patch.object(wake, '_handle_task_exit') as finish:
            gateway._finish(self.conn, final, remote)
        finish.assert_called_once()

    def test_independent_units_and_nested_dispatch_wait_for_all_callbacks(self):
        self.wait(('d1', 'd2'))
        self.assertEqual({r[0] for r in self.conn.execute('SELECT delegation_id FROM hermes_delegations')}, {'d1', 'd2'})
        remote = Mock()
        self.callback('d1', 'a1')
        delegations.poll(self.conn, self.current(), remote)
        ledger.apply_receipt(self.conn, self.eid, self.receipt('a1', 2, ('d3',)))
        self.assertEqual(self.current()['state'], 'waiting_delegation')
        self.callback('d2', 'a2', 3)
        delegations.poll(self.conn, self.current(), remote)
        ledger.apply_receipt(self.conn, self.eid, self.receipt('a2', 3))
        self.assertEqual(self.current()['state'], 'waiting_delegation')
        self.callback('d3', 'a3', 4)
        delegations.poll(self.conn, self.current(), remote)
        ledger.apply_receipt(self.conn, self.eid, self.receipt('a3', 4))
        self.assertEqual(self.current()['state'], 'terminal')

    def test_recovery_discovers_already_completed_callback_without_event_stream(self):
        self.wait()
        self.callback('d1', 'foreign', sid='other-session')
        self.callback('unrelated', 'other-delegation')
        self.callback('d1', 'ours')
        wake._running_sessions.clear(); wake._running_wakes.clear()
        with patch.object(gateway, '_start_worker') as start:
            gateway.recover()
        start.assert_called_once_with(self.eid)
        self.assertEqual(wake.active_runs(), [])
        self.assertTrue(delegations.poll(self.conn, self.current(), Mock()))
        self.assertEqual(self.current()['admission_id'], 'ours')

    def test_task_handoff_revokes_late_writes_and_stops_old_wait(self):
        self.wait()
        self.conn.execute("UPDATE tasks SET status='pending',claimed_by='',next_bot='architect' WHERE id=1")
        self.conn.commit()
        self.assertEqual(self.fixture.send().status_code, 409)
        remote = Mock()
        remote.rpc.side_effect = lambda method, **kw: ({'running': False, 'pending': []} if method == 'session.resume'
            else self.receipt() if method == 'prompt.receipt' else {'delegations': 1})
        self.assertTrue(delegations.poll(self.conn, self.current(), remote))
        self.assertEqual(self.current()['cancel_reason'], 'superseded')
        with patch.object(wake, '_handle_task_exit') as failure:
            gateway._finish(self.conn, self.current(), remote)
        failure.assert_not_called()

    def test_user_stop_revokes_wait_writes(self):
        self.wait()
        ledger.cancel(self.conn, self.eid)
        self.assertEqual(self.fixture.send().status_code, 409)

    def _unknown_callback_stop(self, *, expired=False, lost=False):
        self.wait()
        self.callback()
        delegations.poll(self.conn, self.current(), Mock())
        before = ledger.cancel(self.conn, self.eid, now=time.time() - (151 if expired else 0))
        unknown = dict(admission_id='child-result', status='unknown', execution_generation=2)
        resolved = False

        def rpc(method, **kwargs):
            nonlocal resolved
            if method == 'process.stop':
                return {}
            if method == 'session.resume':
                self.assertTrue(kwargs['omit_messages'])
                return dict(running=False, pending=[] if resolved else [unknown])
            if method == 'prompt.resolve_unknown':
                self.assertEqual(kwargs['session_id'], 'sid')
                self.assertEqual(kwargs['admission_id'], 'child-result')
                self.assertEqual(kwargs['execution_generation'], 2)
                resolved = True
                if lost:
                    raise TimeoutError('resolution response lost')
                return self.receipt('child-result', 2, outcome='cancelled')
            if method == 'prompt.receipt':
                self.assertTrue(resolved)
                self.assertEqual(kwargs['admission_id'], 'child-result')
                return self.receipt('child-result', 2, outcome='cancelled')
            raise AssertionError(method)

        remote = Mock()
        remote.rpc.side_effect = rpc
        if lost:
            with self.assertRaises(TimeoutError):
                delegations.poll(self.conn, self.current(), remote)
            self.assertTrue(gateway.active(self.row['session']))
            self.assertEqual(self.current()['admission_id'], 'child-result')
        with patch.object(gateway, 'connection', return_value=(remote, self.row['home'], [])), \
             patch.object(gateway.sessions, 'attach', return_value={'messages': []}), \
             patch.object(wake, '_handle_task_exit') as failed:
            gateway._watch(self.eid, retry_manual=expired)
        failed.assert_not_called()
        self.assertFalse(gateway.active(self.row['session']))
        self.assertEqual(self.current()['state'], 'terminal')
        self.assertEqual(self.current()['outcome'], 'cancelled')
        self.assertEqual(ledger.cleanup_pending(self.conn, self.row['session']), [])
        for key in ('cancel_deadline', 'reconcile_deadline', 'admission_id', 'submission_id'):
            self.assertEqual(self.current()[key], before[key])
        methods = [call.args[0] for call in remote.rpc.call_args_list]
        self.assertEqual(methods.count('prompt.resolve_unknown'), 1)
        self.assertNotIn('prompt.submit', methods)

    def test_unknown_callback_user_stop_releases_execution(self):
        self._unknown_callback_stop()

    def test_unknown_callback_manual_retry_releases_execution(self):
        self._unknown_callback_stop(expired=True)

    def test_unknown_callback_lost_resolution_response_recovers_on_manual_retry(self):
        self._unknown_callback_stop(expired=True, lost=True)

    def test_unknown_stop_preserves_foreign_admission_and_requires_user_intent(self):
        self.wait()
        self.callback()
        delegations.poll(self.conn, self.current(), Mock())
        remote = Mock()
        for reason, aid, generation in [('user', 'foreign', 3),
                                         ('superseded', 'child-result', 2),
                                         ('user', 'child-result', None)]:
            with self.subTest(reason=reason, admission=aid, generation=generation):
                self.conn.execute('UPDATE hermes_executions SET cancel_reason=? WHERE execution_id=?',
                                  (reason, self.eid))
                self.conn.commit()
                remote.reset_mock()
                remote.rpc.return_value = dict(running=False, pending=[dict(
                    status='unknown', admission_id=aid, execution_generation=generation)])
                self.assertFalse(delegations.poll(self.conn, self.current(), remote))
                self.assertNotIn('prompt.resolve_unknown', [c.args[0] for c in remote.rpc.call_args_list])
                self.assertTrue(gateway.active(self.row['session']))

    def test_fast_callback_handoff_is_recorded_before_old_wait_cleanup(self):
        self.wait()
        self.callback()
        self.conn.execute("UPDATE tasks SET status='pending',claimed_by='',next_bot='architect' WHERE id=1")
        self.conn.commit()
        remote = Mock()
        remote.rpc.side_effect = lambda method, **kw: ({'running': False, 'pending': []} if method == 'session.resume'
            else self.receipt('child-result', 2) if method == 'prompt.receipt' else {})
        self.assertTrue(delegations.poll(self.conn, self.current(), remote))
        self.assertEqual(self.current()['state'], 'terminal')
        self.assertEqual(self.current()['admission_id'], 'child-result')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM hermes_native_turns WHERE terminal=1').fetchone()[0], 2)

    def test_revoked_grant_ends_owned_wait(self):
        self.wait()
        self.conn.execute('UPDATE hermes_session_grants SET revoked=1')
        self.conn.commit()
        self.assertFalse(delegations.owned(self.conn, self.current()))

    def test_unknown_observation_recovers_direct_message_admission(self):
        self.wait()
        ledger.uncertain(self.conn, self.eid, 'native database unavailable')
        delegations.poll(self.conn, self.current(), Mock())
        self.assertEqual(self.current()['state'], 'waiting_delegation')
        self.message()
        self.assertIsNotNone(delegations.direct_wait(self.conn, 'default', 'alpha'))

    def test_direct_delivery_rechecks_wait_after_callback_settles(self):
        self.wait()
        mid = self.message()
        old = self.current()
        self.callback()
        delegations.poll(self.conn, old, Mock())
        ledger.apply_receipt(self.conn, self.eid, self.receipt('child-result', 2))
        self.assertFalse(delegations.enqueue_direct(self.conn, old, mid, 'late signal'))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM hermes_delegation_inputs').fetchone()[0], 0)

    def test_missing_native_database_preserves_wait_for_background_recovery(self):
        self.wait()
        self.native_path.unlink()
        with self.assertRaises(sqlite3.OperationalError):
            delegations.poll(self.conn, self.current(), Mock())
        self.assertFalse(self.native_path.exists())
        self.assertEqual(self.current()['state'], 'waiting_delegation')

    def message(self, recipient='default'):
        mid = self.conn.execute("INSERT INTO messages(project,ts,sender,sender_type,content,mentions,need_reply) "
            "VALUES('alpha',?,'user','user','direct request',?,1)", (db.now_iso(), json.dumps([recipient]))).lastrowid
        self.conn.execute('INSERT INTO inbox(message_id,profile) VALUES(?,?)', (mid, recipient))
        self.conn.commit()
        return mid

    def test_direct_message_outbox_is_idempotent_and_retains_task(self):
        self.wait()
        self.assertIsNone(delegations.direct_wait(self.conn, 'default', 'alpha'))
        mid = self.message()
        row, found = delegations.direct_wait(self.conn, 'default', 'alpha')
        self.assertEqual(found, mid)
        delegations.enqueue_direct(self.conn, row, mid, 'synthetic direct signal')
        delegations.enqueue_direct(self.conn, row, mid, 'duplicate')
        self.assertIsNone(delegations.direct_wait(self.conn, 'default', 'alpha'))
        remote = Mock()
        delegations.poll(self.conn, self.current(), remote)
        delegations.poll(self.conn, self.current(), remote)
        ids = [c.kwargs['submission_id'] for c in remote.rpc.call_args_list]
        self.assertEqual(len(ids), 2); self.assertEqual(ids[0], ids[1])
        self.callback(aid='direct', request=ids[0])
        delegations.poll(self.conn, self.current(), remote)
        ledger.apply_receipt(self.conn, self.eid, self.receipt('direct', 2))
        self.assertEqual(self.current()['task_id'], 1)
        self.assertEqual(self.current()['state'], 'waiting_delegation')

    def test_waiting_work_does_not_block_other_bot_direct_message(self):
        self.wait()
        wake.enqueue(self.conn, 'default', 'work', 'scan:task', project='alpha')
        mid = self.message('architect')
        wake.enqueue(self.conn, 'architect', 'work', 'scan:group', trigger_msg_id=mid, project='alpha')
        with patch.object(wake, 'find_bot_processes', return_value=[]), \
             patch.object(wake, 'deliver', return_value={'ok': True}) as deliver:
            self.assertTrue(queue.process_one(self.conn))
        self.assertEqual(deliver.call_args.args[:2], ('architect', 'work'))
        self.assertEqual(self.conn.execute("SELECT status FROM wake_jobs WHERE profile='default'").fetchone()[0], 'queued')

    def test_new_direct_message_is_deliverable_while_prior_input_remains_unread(self):
        self.wait()
        first = self.message()
        delegations.enqueue_direct(self.conn, self.current(), first, 'first input')
        second = self.message()
        selected = delegations.direct_wait(self.conn, 'default', 'alpha')
        self.assertIsNotNone(selected)
        self.assertEqual(selected[1], second)

    def test_retracted_direct_message_is_rechecked_before_enqueue(self):
        self.wait()
        mid = self.message()
        row, _ = delegations.direct_wait(self.conn, 'default', 'alpha')
        self.conn.execute('UPDATE messages SET retract_at=? WHERE id=?', (db.now_iso(), mid))
        self.conn.commit()
        self.assertFalse(delegations.enqueue_direct(self.conn, row, mid, 'stale input'))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM hermes_delegation_inputs').fetchone()[0], 0)

    def test_queued_task_scan_can_deliver_actual_direct_message_during_wait(self):
        self.wait()
        mid = self.message()
        wake.enqueue(self.conn, 'default', 'work', 'scan:task', project='alpha')

        def deliver(profile, kind, **kwargs):
            return gateway.spawn(profile, kind, None, kwargs['thread_id'], kwargs['project'],
                                 bot={}, workdir='', model_args=[], trigger_msg_id=kwargs['trigger_msg_id'])

        with patch.object(wake, 'deliver', side_effect=deliver), \
             patch.object(wake, '_finalize_injection_signal', return_value='direct signal') as signal, \
             patch.object(gateway, '_start_worker'), \
             patch.object(gateway, 'connection', side_effect=AssertionError('Queue queried native gateway')):
            self.assertTrue(queue.process_one(self.conn))
        self.assertEqual(signal.call_args.args[4:6], (mid, 1))
        self.assertEqual(self.conn.execute('SELECT message_id FROM hermes_delegation_inputs').fetchone()[0], mid)
        self.assertEqual(self.conn.execute('SELECT status FROM wake_jobs').fetchone()[0], 'done')

    def test_final_output_wait_claim_does_not_create_delegation(self):
        receipt = self.receipt()
        receipt['result']['final_response'] = '{"is_waiting_delegation":true}'
        row = ledger.apply_receipt(self.conn, self.eid, receipt)
        self.assertEqual(row['state'], 'terminal')
        self.assertFalse(delegations.pending(self.conn, row))

    def topic(self):
        for tid in (10, 20):
            self.conn.execute("INSERT INTO threads(id,project,title,creator,members,status,created_at,last_active_at,"
                "turn_profile,turn_wake_at,round) VALUES(?,'alpha','fixture','user',?,'open',?,?,'default',?,1)",
                (tid, json.dumps(['default', 'architect', 'user']), db.now_iso(), db.now_iso(), db.now_iso(-900)))
        session = wake.session_name('default', 'chat', 10, 'alpha')
        self.conn.execute("UPDATE session_bindings SET kind='chat',thread_id=10,title=? WHERE session_id='sid'", (session,))
        self.conn.execute("UPDATE hermes_executions SET kind='chat',thread_id=10,session=?,round=1,task_id=NULL WHERE execution_id=?",
                          (session, self.eid))
        self.conn.execute('UPDATE wake_runs SET session=?,thread_id=10 WHERE started_at=?', (session, self.row['started_at']))
        self.conn.commit()
        self.row = self.current()

    def test_topic_wait_snapshot_survives_refresh_and_resumes_same_execution(self):
        from engine import api, events, reasoning, session_adapter
        self.topic()
        self.wait()
        with patch.object(gateway, 'connection', side_effect=AssertionError('Snapshot queried gateway')), \
             patch.object(events.bus, 'publish') as publish:
            snapshot = api.get_thinking()
            self.assertEqual(snapshot['thinking'], [])
            self.assertEqual(len(snapshot['runs']), 1)
            run = snapshot['runs'][0]
            self.assertEqual((run['session'], run['thread_id'], run['wake_started_at'], run['state']),
                             (self.row['session'], 10, self.row['started_at'], 'waiting_delegation'))
            with patch.object(wake, '_running_wakes', {}):
                self.assertEqual(api.get_thinking(), snapshot)
            publish.assert_not_called()
        # The parent's final message finishes a native turn, while the topic
        # execution stays open until its callback has completed.
        with patch.object(session_adapter, 'compression_tip', return_value='sid'), \
             patch.object(reasoning, 'read_reasoning', return_value={
                 'entries': [{'id': 1, 'finished': True}], 'latest_id': 1}):
            result = api.get_reasoning('default', self.row['session'], self.row['started_at'])
            self.assertEqual(result['execution_state'], 'waiting_delegation')
            self.assertFalse(result['entries'][0]['finished'])
            self.assertIsNone(result['process_ended_at'])
        self.callback()
        remote = Mock()
        delegations.poll(self.conn, self.current(), remote)
        gateway._sync_delegation_state(self.conn, self.current())
        resumed = api.get_thinking()['runs'][0]
        self.assertEqual(resumed['state'], 'submitting')
        self.assertEqual(resumed['wake_started_at'], run['wake_started_at'])
        final = ledger.apply_receipt(self.conn, self.eid, self.receipt('child-result', 2))
        with patch.object(wake, '_handle_task_exit'), patch.object(heartbeat, 'on_exit'):
            gateway._finish(self.conn, final, remote)
        self.assertEqual(api.get_thinking()['runs'], [])

    def test_waiting_topic_does_not_advance_on_timeout_or_block_another_topic(self):
        self.topic()
        self.wait()
        wake.enqueue(self.conn, 'default', 'chat', 'scan:group', thread_id=20, project='alpha')
        with patch.object(wake, 'find_bot_processes', return_value=[]), \
             patch.object(wake, 'deliver', return_value={'ok': True}) as deliver:
            self.assertTrue(queue.process_one(self.conn))
            self.assertEqual(deliver.call_args.kwargs['thread_id'], 20)
            self.conn.execute('UPDATE threads SET turn_wake_at=? WHERE id=20', (db.now_iso(),)); self.conn.commit()
            router.advance_timed_out_turns(self.conn)
        self.assertEqual(self.conn.execute('SELECT turn_profile FROM threads WHERE id=10').fetchone()[0], 'default')
        self.assertEqual(self.conn.execute('SELECT turn_skipped FROM threads WHERE id=10').fetchone()[0], '[]')


if __name__ == '__main__':
    unittest.main()
