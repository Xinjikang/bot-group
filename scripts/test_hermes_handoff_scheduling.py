"""Credential handoff failures preserve FIFO identity and independent scheduling."""
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

from engine import auth, browser_resources, hermes_gateway as gateway, hermes_gateway_sessions as sessions
from engine import hermes_session_auth as grants, queue, signing_keys, wake
from scripts import test_native_busy_scheduling as scheduling_fixture


class HandoffSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = scheduling_fixture.NativeBusySchedulingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.conn = self.fixture.conn

    def test_failed_handoff_skips_original_job_and_recovers_without_reset(self):
        f = self.fixture
        f._bind('hermes-a', session_id='native-a')
        f.native_states['native-a'] = {'running': False, 'pending': False}
        first = f._job('hermes-a')
        second = f._job('hermes-b')
        home = Path(f.tmp.name)
        session = wake.session_name('hermes-a', 'work', None, 'p')
        snapshot = dict(session_id='native-a', running=False, pending=[])
        remote = Mock()
        failed = [True]
        clock = [0.0]
        attempts = []
        observer_connection = gateway.connection

        def rpc(method, **kwargs):
            if method == 'runtime.describe':
                return dict(profile_id='fixture', capabilities=['durable-admission-v1', 'acp-session-mcp-v1'])
            if method == 'prompt.submit':
                return dict(status='queued', admission_id='accepted', authority_epoch=1,
                            execution_generation=None, ref={'profile_id': 'fixture', 'session_id': 'native-a'})
            raise AssertionError(method)

        def configure(*args, **kwargs):
            attempts.append(clock[0])
            if failed[0]:
                raise HTTPException(401, 'synthetic credential rejection')
            return snapshot

        def connection(profile, **kwargs):
            if kwargs.get('read_only'):
                return observer_connection(profile, **kwargs)
            return remote, home, ['fixture']

        def deliver(profile, kind, **kwargs):
            if profile != 'hermes-a':
                return f._fake_deliver(profile, kind, **kwargs)
            return gateway.spawn(profile, kind, 'fixture', None, 'p', bot={'id': 0},
                                 workdir=str(home), model_args=[], job_id=kwargs['job_id'])

        remote.rpc.side_effect = rpc
        with patch.object(auth, 'signing_keys', return_value=(2, {1: b'old', 2: b'new'})), \
             patch.object(gateway, 'connection', side_effect=connection), \
             patch.object(gateway, '_binding', return_value=snapshot), \
             patch.object(gateway, '_start_worker'), patch.object(gateway, '_publish'), \
             patch.object(gateway.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(grants, 'server', return_value={}), \
             patch.object(sessions, 'configure_tracked', side_effect=configure), \
             patch.object(signing_keys, 'retire'), patch.object(browser_resources, 'register'), \
             patch.object(wake, 'wake_system_prompt', return_value='fixture'), \
             patch.object(wake, 'deliver', side_effect=deliver):
            grants.grant(self.conn, project='p', profile='hermes-a', bot_id=0, kind='work',
                         thread_id=None, session=session, home=home, command=['fixture'],
                         workdir=str(home), model_args=[])
            self.conn.execute('UPDATE hermes_session_grants SET signing_generation=1')
            self.conn.commit()
            for _ in range(12):
                queue.process_one(self.conn)
                clock[0] += 0.5
            jobs = {r['id']: r for r in self.conn.execute('SELECT * FROM wake_jobs')}
            self.assertEqual(jobs[second['id']]['status'], 'done', f'handoff attempts: {attempts}')
            self.assertEqual(jobs[first['id']]['status'], 'queued')
            self.assertIsNone(jobs[first['id']]['started_at'])
            saved = self.conn.execute('SELECT * FROM hermes_session_grants').fetchone()
            self.assertEqual((saved['signing_generation'], saved['pending_generation'], saved['revoked']), (1, 2, 0))
            self.assertEqual(saved['session_id'], 'native-a')
            self.assertEqual(self.conn.execute('SELECT count(*) FROM hermes_executions').fetchone()[0], 0)
            failed[0] = False
            clock[0] += 31
            self.assertTrue(queue.process_one(self.conn))
            self.assertEqual(self.conn.execute('SELECT status FROM wake_jobs WHERE id=?',
                                              (first['id'],)).fetchone()[0], 'done')
            saved = self.conn.execute('SELECT * FROM hermes_session_grants').fetchone()
            self.assertEqual((saved['signing_generation'], saved['pending_generation'], saved['revoked']), (2, None, 0))
            self.assertEqual(saved['session_id'], 'native-a')
            self.assertEqual(sum(c.args[0] == 'prompt.submit' for c in remote.rpc.call_args_list), 1)


if __name__ == '__main__':
    unittest.main()
