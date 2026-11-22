"""Isolated regression for resuming a task through its existing Hermes work execution."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
import time
import unittest
from unittest.mock import Mock, patch

from engine import db, hermes_delegations, hermes_gateway as gateway, hermes_gateway_ledger as ledger
from engine import queue, wake
from scripts import test_hermes_session_reuse as session_fixture


class ResumeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = session_fixture.SessionReuseTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.conn = self.fixture.conn
        self.row = self.fixture.run_turn()
        self.execution = self.row['execution_id']
        self.conn.execute("INSERT INTO requirements(id,project,title,created_at,updated_at) "
                          "VALUES(991,'alpha','fixture',?,?)", (db.now_iso(), db.now_iso()))
        old = db.now_iso(seconds_offset=-10800)
        self.conn.execute("INSERT INTO tasks(id,project,requirement_id,title,status,claimed_by,created_at,claimed_at) "
                          "VALUES(991,'alpha',991,'recovery fixture','in_progress','default',?,?)", (old, old))
        self.conn.execute("UPDATE hermes_executions SET state='manual' WHERE execution_id=?", (self.execution,))
        self.conn.commit()
        self.assertIsNone(self.row['task_id'])
        self.assertTrue(wake.enqueue(self.conn, 'default', 'work', 'scan:task', project='alpha'))
        self.job_id = self.conn.execute('SELECT id FROM wake_jobs').fetchone()[0]
        self.enterContext(patch.object(wake, '_running_sessions', {self.fixture.session}))

    def test_manual_catch_up_reuses_cue_and_original_execution(self):
        with patch.object(gateway, '_start_worker') as worker, patch.object(wake, 'deliver') as deliver:
            self.assertTrue(queue.process_one(self.conn))
            worker.assert_called_once_with(self.execution)
            deliver.assert_not_called()
        with closing(db.get_connection()) as observer:
            cue = observer.execute('SELECT * FROM hermes_task_recoveries').fetchone()
            self.assertEqual((cue['execution_id'], cue['task_id'], cue['job_id']),
                             (self.execution, 991, self.job_id))
            self.assertEqual(observer.execute('SELECT status FROM wake_jobs WHERE id=?', (self.job_id,)).fetchone()[0],
                             'queued', 'durable input alone is not successful recovery')
            self.assertEqual(observer.execute('SELECT COUNT(*) FROM hermes_executions').fetchone()[0], 1)

    def cue(self):
        return self.conn.execute('SELECT * FROM hermes_task_recoveries ORDER BY rowid DESC LIMIT 1').fetchone()

    def current(self):
        return ledger.read(self.conn, self.execution)

    def reserve(self):
        with patch.object(gateway, '_start_worker') as worker:
            self.assertTrue(queue.process_one(self.conn))
            worker.assert_called_once_with(self.execution)
        return self.cue()

    def test_active_or_recently_progressing_task_does_not_start_recovery(self):
        with patch.object(gateway, '_start_worker') as worker:
            self.conn.execute("UPDATE hermes_executions SET state='running' WHERE execution_id=?", (self.execution,))
            self.conn.commit()
            self.assertFalse(queue.process_one(self.conn))
            self.conn.execute("UPDATE hermes_executions SET state='manual' WHERE execution_id=?", (self.execution,))
            self.conn.execute("UPDATE tasks SET claimed_at=? WHERE id=991", (db.now_iso(),))
            self.conn.commit()
            self.assertFalse(queue.process_one(self.conn))
            self.conn.execute("UPDATE tasks SET claimed_at=?,claimed_by='architect' WHERE id=991",
                              (db.now_iso(seconds_offset=-10800),))
            self.conn.commit()
            self.assertFalse(queue.process_one(self.conn))
            worker.assert_not_called()

    def snapshot(self, pending=(), running=False):
        return {'session_id': self.row['session_id'], 'running': running, 'pending': list(pending)}

    def receipt(self, status, *, aid='recovery-admission', input_id=None):
        return {'ref': {'profile_id': self.row['profile_id'], 'session_id': self.row['session_id']},
                'admission_id': aid, 'status': status, 'authority_epoch': 1,
                'execution_generation': None if status == 'queued' else 3, 'input_id': input_id}

    def poll(self, client):
        return hermes_delegations.poll(self.conn, self.current(), client, task_cue=self.cue())

    def task_tool(self, operation, **arguments):
        return self.fixture.client.post('/task-tools/' + operation, headers=self.fixture.headers, json={
            'arguments': {'project': 'alpha', 'task_id': 991, **arguments},
            'execution': {**self.fixture.context, 'credential_type': 'session'},
        })

    def test_started_receipt_opens_current_session_for_original_task_node(self):
        cue = self.reserve()
        client = Mock()
        client.rpc.side_effect = [self.snapshot(), self.receipt('queued')]
        self.assertFalse(self.poll(client))
        self.assertEqual(self.task_tool('task_get').status_code, 409)
        self.assertEqual(self.current()['state'], 'manual')
        client.rpc.side_effect = [self.snapshot([self.receipt('started', input_id=cue['request_id'])], running=True)]
        self.assertTrue(self.poll(client))
        sent = [call.kwargs for call in client.rpc.call_args_list if call.args == ('prompt.submit',)]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0], {'session_id': 'sid', 'submission_id': cue['request_id'], 'text': cue['text'], 'timeout': 5})
        self.assertIn('任务 #991', sent[0]['text'])
        self.assertEqual(self.current()['task_id'], 991)
        self.assertEqual(self.conn.execute('SELECT status FROM wake_jobs WHERE id=?', (self.job_id,)).fetchone()[0], 'queued')
        self.assertEqual(self.task_tool('task_get').json()['task']['id'], 991)
        role = self.conn.execute("SELECT role_id FROM project_members WHERE project='alpha' AND profile='architect'").fetchone()[0]
        response = self.task_tool('task_log', type='dev', result='current B accepted recovery',
                                  next_bot='architect', expected_role_id=role)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('error', response.json(), response.text)
        self.assertEqual(self.cue()['state'], 'accepted')
        self.assertEqual(self.conn.execute('SELECT status FROM wake_jobs WHERE id=?', (self.job_id,)).fetchone()[0], 'done')
        self.assertEqual(tuple(self.conn.execute('SELECT task_id,actor,result FROM nodes WHERE task_id=991').fetchone()),
                         (991, 'default', 'current B accepted recovery'))

    def test_first_task_get_observes_the_native_start_without_a_worker_poll(self):
        cue = self.reserve()
        client = Mock()
        client.rpc.side_effect = [self.snapshot(), self.receipt('queued')]
        self.assertFalse(self.poll(client))
        # This is the first task request from B, while the worker is still parked.
        client.rpc.side_effect = [self.snapshot([self.receipt('started', input_id=cue['request_id'])], running=True)]
        with patch.object(gateway, 'connection', return_value=(client, self.row['home'], [])), \
                patch.object(gateway, '_start_worker') as worker:
            response = self.task_tool('task_get')
        worker.assert_called_once_with(self.execution)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['task']['id'], 991)
        self.assertEqual(self.current()['task_id'], 991)
        self.assertEqual([call.args[0] for call in client.rpc.call_args_list],
                         ['session.resume', 'prompt.submit', 'session.resume'])

    def test_first_read_cannot_adopt_an_old_receipt_or_a_task_that_changed_owner(self):
        cue = self.reserve()
        client = Mock()
        client.rpc.side_effect = [self.snapshot(), self.receipt('queued')]
        self.assertFalse(self.poll(client))
        old = self.receipt('started', aid='older-admission')
        ready = self.receipt('started', input_id=cue['request_id'])

        def move(*args, **kwargs):
            with closing(db.get_connection()) as other:
                other.execute('PRAGMA busy_timeout=0')
                other.execute('BEGIN IMMEDIATE')
                other.execute("UPDATE tasks SET claimed_by='architect' WHERE id=991")
                other.commit()
            return self.snapshot([ready], running=True)

        # Run the owner change precisely while the second HTTP request queries Hermes.
        client.rpc.side_effect = [self.snapshot([old], running=True)]
        with patch.object(gateway, 'connection', return_value=(client, self.row['home'], [])), \
                patch.object(gateway, '_start_worker') as worker:
            self.assertEqual(self.task_tool('task_get').status_code, 409)
            self.assertEqual(self.cue()['state'], 'pending')
            client.rpc.side_effect = move
            self.assertEqual(self.task_tool('task_get').status_code, 409)
            worker.assert_not_called()
        self.assertEqual(self.current()['state'], 'manual')
        self.assertEqual(self.cue()['state'], 'abandoned')

    def test_terminal_before_first_observation_keeps_the_wake_unaccepted(self):
        self.reserve()
        client = Mock()
        terminal = {**self.receipt('terminal'), 'outcome': 'completed'}
        client.rpc.side_effect = [self.snapshot(), self.receipt('queued'), self.snapshot(), terminal]
        self.assertFalse(self.poll(client))
        self.poll(client)
        db.recover_on_startup(self.conn)
        self.assertFalse(queue.process_one(self.conn))
        self.assertEqual(self.cue()['state'], 'unaccepted')
        self.assertEqual(self.task_tool('task_get').status_code, 409)
        self.assertEqual(self.conn.execute('SELECT status FROM wake_jobs WHERE id=?', (self.job_id,)).fetchone()[0], 'queued')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM nodes WHERE task_id=991').fetchone()[0], 0)
        self.assertEqual(sum(call.args == ('prompt.submit',) for call in client.rpc.call_args_list), 1)

    def test_unaccepted_exit_reminds_once_and_can_be_released_by_user(self):
        self.reserve()
        client = Mock()
        terminal = {**self.receipt('terminal'), 'outcome': 'completed'}
        client.rpc.side_effect = [self.snapshot(), self.receipt('queued'), self.snapshot(), terminal]
        self.assertFalse(self.poll(client))
        self.poll(client)
        notice = self.cue()['notice_message_id']
        self.assertIsNotNone(notice)
        self.assertEqual(self.current()['state'], 'terminal')
        row = self.conn.execute('SELECT content FROM messages WHERE id=?', (notice,)).fetchone()
        self.assertIn('未在原任务追加节点', row['content'])
        self.assertNotIn('/release-recovery', row['content'])
        ledger.apply_receipt(self.conn, self.execution, terminal)
        db.recover_on_startup(self.conn)
        self.assertEqual(self.cue()['notice_message_id'], notice)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM messages WHERE sender='engine' AND content LIKE '%#991 自动续办受阻%'").fetchone()[0], 1)
        self.assertFalse(wake.enqueue(self.conn, 'default', 'work', '@msg', project='alpha'))
        self.conn.rollback()
        body = {'project': 'alpha', 'sender': 'user', 'text': f'/release-recovery {notice}'}
        with patch.object(session_fixture.auth, 'configured', return_value=False):
            premature = self.fixture.client.post('/msg', json=body)
            self.assertEqual(premature.status_code, 400, premature.text)
            self.assertEqual(self.cue()['state'], 'unaccepted')
            self.assertTrue(gateway._finish(self.conn, self.current(), client))
            denied = self.fixture.client.post('/msg', headers=self.fixture.headers, json=body)
            self.assertNotEqual(denied.status_code, 200)
            wrong_project = self.fixture.client.post('/msg', json={**body, 'project': 'xjk'})
            self.assertEqual(wrong_project.status_code, 400)
            res = self.fixture.client.post('/msg', json=body)
            self.assertTrue(res.json().get('ok'), res.text)
            self.assertTrue(self.fixture.client.post('/msg', json=body).json().get('ok'))
        self.assertEqual(self.cue()['state'], 'released')
        self.assertEqual(self.conn.execute('SELECT status FROM wake_jobs WHERE id=?', (self.job_id,)).fetchone()[0], 'cancelled')
        self.assertEqual(self.conn.execute('SELECT status FROM tasks WHERE id=991').fetchone()[0], 'blocked')
        self.assertEqual(sum(call.args == ('prompt.submit',) for call in client.rpc.call_args_list), 1)

    def test_terminal_after_handoff_does_not_notify_or_replay_the_old_wake(self):
        self.reserve()
        client = Mock()
        client.rpc.side_effect = [self.snapshot(), self.receipt('started')]
        self.assertTrue(self.poll(client))
        self.conn.execute("UPDATE tasks SET status='pending',next_bot='architect',claimed_by='' WHERE id=991")
        self.conn.commit()
        receipt = {**self.receipt('terminal'), 'outcome': 'completed'}
        ledger.apply_receipt(self.conn, self.execution, receipt)
        self.assertIsNone(self.cue()['notice_message_id'])
        self.assertEqual(self.cue()['state'], 'unaccepted')
        self.assertEqual(self.conn.execute('SELECT status FROM wake_jobs WHERE id=?', (self.job_id,)).fetchone()[0], 'cancelled')
        self.assertEqual(self.conn.execute('SELECT next_bot FROM tasks WHERE id=991').fetchone()[0], 'architect')

    def test_notice_transition_keeps_original_message_and_syncs_state_after_watermark(self):
        from engine import events
        self.reserve()
        client = Mock()
        unknown = self.receipt('unknown')
        terminal = {**self.receipt('terminal'), 'outcome': 'completed'}
        client.rpc.side_effect = [self.snapshot(), self.receipt('queued'), self.snapshot([unknown]),
                                  self.snapshot(), terminal]
        with patch.object(events.bus, 'publish') as publish, \
                patch.object(session_fixture.auth, 'configured', return_value=False):
            self.assertFalse(self.poll(client))
            self.assertFalse(self.poll(client))
            mid = self.cue()['notice_message_id']
            api = self.fixture.client
            original = api.get('/messages', params={'project': 'alpha'}).json()['messages'][-1]
            release = {'project': 'alpha', 'sender': 'user', 'text': f'/release-recovery {mid}'}
            self.assertEqual(api.post('/msg', json=release).status_code, 400)
            publish.reset_mock()
            self.assertTrue(self.poll(client))
            self.assertEqual(api.get('/messages', params={'project': 'alpha'}).json()['messages'][-1], original)
            params = {'project': 'alpha', 'since_id': mid, 'recovery_notices': True}
            delta = api.get('/messages', params=params).json()
            self.assertEqual(delta['messages'], [])
            self.assertEqual(delta['recovery_notices'], {str(mid): 'cleaning'})
            publish.assert_any_call('recovery.notice.changed', {'project': 'alpha', 'message_id': mid})
            self.assertFalse(any(c.args[0] == 'message.new' for c in publish.call_args_list))
            self.assertEqual(api.post('/msg', json=release).status_code, 400)
            self.assertTrue(gateway._finish(self.conn, self.current(), client))
            self.assertEqual(api.get('/messages', params=params).json()['recovery_notices'], {str(mid): 'releasable'})
            self.assertEqual(api.get('/messages', params={**params, 'project': 'beta'}).json()['recovery_notices'], {})
            publish.reset_mock()
            ledger.apply_receipt(self.conn, self.execution, terminal)
            ledger.apply_receipt(self.conn, self.execution, unknown)
            db.recover_on_startup(self.conn)
            publish.assert_not_called()
            self.assertEqual(api.get('/messages', params=params).json()['recovery_notices'], {str(mid): 'releasable'})
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM inbox WHERE message_id=? AND profile='user' AND status='pending'", (mid,)).fetchone()[0], 1)
            self.assertTrue(api.post('/msg', json=release).json().get('ok'))
            self.assertTrue(api.post('/msg', json=release).json().get('ok'))
            self.assertEqual(api.get('/messages', params=params).json()['recovery_notices'], {str(mid): 'released'})
            self.assertEqual(api.get('/messages', params={'project': 'alpha'}).json()['messages'][-1], original)
            self.assertEqual(self.conn.execute("SELECT status FROM inbox WHERE message_id=? AND profile='user'", (mid,)).fetchone()[0], 'seen')
        self.assertEqual(sum(c.args == ('prompt.submit',) for c in client.rpc.call_args_list), 1)

    def test_unknown_preserves_wake_and_notifies_user_once_across_restart(self):
        cue = self.reserve()
        client = Mock()
        client.rpc.return_value = self.snapshot([self.receipt('unknown', aid='prior-native-turn')])
        self.assertFalse(self.poll(client))
        notice = self.cue()['notice_message_id']
        self.assertIsNotNone(notice)
        self.assertEqual(self.current()['state'], 'manual')
        db.recover_on_startup(self.conn)
        self.assertFalse(self.poll(client))
        self.assertEqual(self.cue()['request_id'], cue['request_id'])
        self.assertEqual(self.cue()['notice_message_id'], notice)
        self.assertEqual([call.args for call in client.rpc.call_args_list], [('session.resume',)] * 2)
        self.assertEqual(self.conn.execute('SELECT status FROM wake_jobs WHERE id=?', (self.job_id,)).fetchone()[0], 'queued')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM messages WHERE sender='engine' "
                                           "AND content LIKE '%#991 自动续办受阻%' ").fetchone()[0], 1)
        self.assertEqual(tuple(self.conn.execute('SELECT profile,status FROM inbox WHERE message_id=?', (notice,)).fetchone()),
                         ('user', 'pending'))
        content = self.conn.execute('SELECT content FROM messages WHERE id=?', (notice,)).fetchone()[0]
        for marker in ('#991', 'unknown', '核对旧执行结果'):
            self.assertIn(marker, content)

    def test_handoff_during_native_check_prevents_submission(self):
        self.reserve()
        client = Mock()

        def native_check(method, **kwargs):
            self.assertEqual(method, 'session.resume')
            with closing(db.get_connection()) as other:
                other.execute("UPDATE tasks SET status='pending',next_bot='architect',claimed_by='' WHERE id=991")
                other.commit()
            return self.snapshot()

        client.rpc.side_effect = native_check
        self.assertFalse(self.poll(client))
        self.assertEqual(client.rpc.call_count, 1)
        self.assertEqual(self.cue()['state'], 'abandoned')
        self.assertEqual(self.current()['state'], 'manual')

    def test_parallel_queue_workers_reserve_the_same_stall_once(self):
        def tick(_):
            with closing(db.get_connection()) as conn:
                return queue.process_one(conn)
        with patch.object(gateway, '_start_worker') as worker, ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(tick, range(2))), [False, True])
            worker.assert_called_once_with(self.execution)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM hermes_task_recoveries').fetchone()[0], 1)

    def test_native_busy_keeps_process_inputs_and_other_wakes_intact(self):
        self.conn.execute("UPDATE hermes_executions SET state='waiting_delegation',execution_deadline=? WHERE execution_id=?",
                          (time.time() - 1, self.execution))
        self.conn.execute('INSERT INTO hermes_delegations(execution_id,delegation_id) VALUES(?,?)',
                          (self.execution, 'process:proc_in_flight'))
        self.conn.commit()
        cue = self.reserve()
        self.assertIn('proc_in_flight', cue['text'])
        client = Mock()
        client.rpc.return_value = self.snapshot([self.receipt('started', aid='callback-active')], running=True)
        self.assertFalse(self.poll(client))
        self.assertEqual([call.args for call in client.rpc.call_args_list], [('session.resume',)])
        self.assertTrue(wake.enqueue(self.conn, 'architect', 'work', '@msg', project='alpha'))
        with patch.object(wake, 'session_busy', side_effect=lambda member, *_: member == 'default'), \
                patch.object(wake, 'deliver', return_value={'ok': True}) as deliver:
            self.assertTrue(queue.process_one(self.conn))
            self.assertEqual(deliver.call_args.args[0], 'architect')
        self.assertEqual(self.conn.execute('SELECT consumed FROM hermes_delegations').fetchone()[0], 0)

    def test_lost_submit_response_reuses_the_admitted_input_on_restart(self):
        cue = self.reserve()
        client = Mock()
        client.rpc.side_effect = [self.snapshot(), TimeoutError('response lost after admission')]
        with self.assertRaises(TimeoutError):
            self.poll(client)
        db.recover_on_startup(self.conn)
        self.conn.execute('UPDATE hermes_task_recoveries SET next_check_at=0')
        self.conn.commit()
        self.assertEqual(self.reserve()['request_id'], cue['request_id'])
        client.rpc.side_effect = [self.snapshot([self.receipt('started', input_id=cue['request_id'])], running=True)]
        self.assertTrue(self.poll(client))
        self.assertEqual(sum(call.args == ('prompt.submit',) for call in client.rpc.call_args_list), 1)
        self.assertEqual(self.current()['admission_id'], 'recovery-admission')
        late = self.receipt('started', aid='older-callback-admission')
        with self.assertRaises(ValueError):
            ledger.apply_receipt(self.conn, self.execution, late)
        self.assertEqual(self.current()['admission_id'], 'recovery-admission')

    def test_fast_followup_is_not_lost_in_epoch_float_rounding(self):
        fixed_epoch = 1795246400.0
        claimed = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(fixed_epoch - 10800))
        self.conn.execute('UPDATE tasks SET claimed_at=? WHERE id=991', (claimed,))
        self.conn.commit()
        with patch.object(gateway.time, 'time', return_value=fixed_epoch):
            cue = self.reserve()
        client = Mock()
        terminal = {**self.receipt('terminal'), 'outcome': 'completed'}
        client.rpc.side_effect = [self.snapshot(), self.receipt('queued'),
                                  self.snapshot([self.receipt('started', input_id=cue['request_id'])], running=True),
                                  terminal]
        # An epoch-sized time plus 0.2 rounds differently than a delta of 0.2.
        # A cached OS wall-clock tick can be the same before and after the commit.
        with patch.object(gateway.time, 'time', return_value=1795246400.0), \
                patch.object(gateway.time, 'sleep'), \
                patch.object(gateway, 'connection', return_value=(client, self.row['home'], [])), \
                patch.object(gateway, '_finish', return_value=True):
            gateway._watch(self.execution)
        self.assertEqual(self.cue()['state'], 'unaccepted')
        self.assertEqual(self.current()['state'], 'terminal')
        self.assertEqual([call.args[0] for call in client.rpc.call_args_list],
                         ['session.resume', 'prompt.submit', 'session.resume', 'prompt.receipt'])
        ledger.apply_receipt(self.conn, self.execution, terminal)
        db.recover_on_startup(self.conn)
        self.assertEqual(self.cue()['state'], 'unaccepted')
        self.assertFalse(queue.process_one(self.conn))
        self.assertEqual(self.conn.execute('SELECT status FROM wake_jobs WHERE id=?', (self.job_id,)).fetchone()[0], 'queued')

    def test_task_claim_uses_the_adopted_task_not_the_first_pending_task(self):
        self.conn.execute("UPDATE tasks SET status='pending',next_bot='default',claimed_by='',"
                          "description='fixture',design_basis='fixture',acceptance_criteria='fixture',"
                          "briefing_status='ready' WHERE id=991")
        self.conn.execute("INSERT INTO tasks(id,project,requirement_id,title,status,next_bot,created_at,description,"
                          "design_basis,acceptance_criteria,briefing_status) "
                          "VALUES(990,'alpha',991,'other task','pending','default',?,'fixture','fixture','fixture','ready')",
                          (db.now_iso(),))
        self.conn.execute("UPDATE wake_jobs SET trigger='retry:task:991' WHERE id=?", (self.job_id,))
        self.conn.commit()
        self.reserve()
        client = Mock()
        client.rpc.side_effect = [self.snapshot(), self.receipt('started')]
        self.assertTrue(self.poll(client))
        response = self.fixture.client.post('/task-tools/task_claim', headers=self.fixture.headers,
            json={'arguments': {'project': 'alpha'},
                  'execution': {**self.fixture.context, 'credential_type': 'session'}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['claimed'], response.text)
        self.assertEqual(response.json()['task']['id'], 991)
        self.assertEqual(self.conn.execute('SELECT status FROM tasks WHERE id=990').fetchone()[0], 'pending')

    def test_new_admission_can_stall_and_have_a_new_recovery_id(self):
        first = self.reserve()
        client = Mock()
        client.rpc.side_effect = [self.snapshot(), self.receipt('started')]
        self.assertTrue(self.poll(client))
        self.conn.execute("UPDATE hermes_executions SET state='manual' WHERE execution_id=?", (self.execution,))
        self.conn.commit()
        second = self.reserve()
        self.assertNotEqual(first['request_id'], second['request_id'])
        self.assertEqual(first['job_id'], second['job_id'])
        self.assertEqual(first['execution_id'], second['execution_id'])


if __name__ == '__main__':
    unittest.main()