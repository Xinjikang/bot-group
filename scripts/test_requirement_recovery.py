"""Requirement recovery through isolated SQLite, queue claims and injection."""

import unittest
from unittest.mock import patch

from engine import db, queue, scanner, taskpool, wake
from scripts.test_task_brief import BRIEF
from scripts.unified_test_support import confirmed_requirement_fields, isolated_database


class RequirementRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = isolated_database()
        self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        self.conn.execute("DELETE FROM project_permissions WHERE permission='task.create' AND profile='default'")
        self.conn.commit()
        self.deliveries = []
        delivery = patch.object(wake, 'deliver', side_effect=self._deliver)
        delivery.start()
        self.addCleanup(delivery.stop)
        busy = patch.object(wake, 'session_busy', return_value=False)
        busy.start()
        self.addCleanup(busy.stop)

    def _deliver(self, profile, kind, **kwargs):
        signal = wake._finalize_injection_signal(profile, kind, kwargs['thread_id'],
            kwargs['project'], kwargs['trigger_msg_id'], kwargs['task_id'], ['model-recorder'])
        self.deliveries.append({'profile': profile, 'task_id': kwargs['task_id'], 'signal': signal})
        return {'ok': True}

    def _requirement(self, partial=False, blocked=False, project='alpha', next_bot='dev'):
        rid = taskpool.req_create(project, 'default', 'Recovery',
                                  **confirmed_requirement_fields(project))['requirement_id']
        tid = None
        if partial:
            created = taskpool.task_create(project, 'architect', rid,
                [{**BRIEF, 'title': 'Partial work', 'next_bot': next_bot}])
            self.assertNotIn('error', created)
            tid = created['task_ids'][0]
            if blocked:
                self.assertTrue(taskpool.admin_task_block(project, tid)['blocked'])
            self.conn.execute("UPDATE requirements SET decompose_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (rid,))
            self.conn.commit()
        return rid, tid

    def _context(self, profile='architect', project='alpha'):
        return wake._requirement_signal(self.conn, profile, 'work', project)

    def _job(self):
        return self.conn.execute('SELECT * FROM wake_jobs ORDER BY id DESC LIMIT 1').fetchone()

    def _process_cancelled(self):
        self.assertTrue(queue.process_one(self.conn))
        self.assertEqual(self._job()['status'], 'cancelled')
        self.assertIn('No actionable requirement', self._job()['detail'])
        self.assertEqual(self.deliveries, [])

    def _message(self, project='alpha', profile='architect'):
        mid = self.conn.execute(
            "INSERT INTO messages(project,ts,sender,sender_type,content,mentions) "
            "VALUES(?,?,'user','user','Merged real message','[]')", (project, db.now_iso())).lastrowid
        self.conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(?,?,'pending')", (mid, profile))
        self.conn.commit()
        return mid

    def _owner(self, profile='default'):
        self.conn.execute("INSERT OR REPLACE INTO settings(project,key,value,updated_at) "
                          "VALUES('alpha','owner_profile',?,?)", (profile, db.now_iso()))
        self.conn.execute("DELETE FROM project_permissions WHERE project='alpha' AND permission='task.create'")
        self.conn.commit()

    def test_blocked_partial_stays_quiet_then_explicit_resume_recovers(self):
        rid, tid = self._requirement(partial=True, blocked=True)
        for _ in range(2):
            self.assertEqual(scanner.scan_requirements(self.conn), 0)
            self.assertEqual(self._context(), '')
            self.assertFalse(queue.process_one(self.conn))
        self.assertTrue(taskpool.admin_task_resume('alpha', tid)['resumed'])
        self.assertEqual(scanner.scan_requirements(self.conn), 1)
        self.assertTrue(queue.process_one(self.conn))
        signal = self.deliveries[0]['signal']
        for text in (f'#{rid}', 'task_list/task_get', '复用已有任务', 'complete=true', '收件箱为空'):
            self.assertIn(text, signal)
        self.assertNotIn('你有新消息', signal)

    def test_existing_queue_is_cancelled_after_child_blocks(self):
        _, tid = self._requirement(partial=True)
        self.assertEqual(scanner.scan_requirements(self.conn), 1)
        self.assertTrue(taskpool.admin_task_block('alpha', tid)['blocked'])
        self._process_cancelled()
        self.assertEqual(scanner.scan_requirements(self.conn), 0)

    def test_partial_unblocked_requirement_keeps_recovery_context(self):
        rid, _ = self._requirement(partial=True)
        self.assertEqual(scanner.scan_requirements(self.conn), 1)
        self.assertTrue(queue.process_one(self.conn))
        self.assertIn(f'#{rid}', self.deliveries[0]['signal'])
        self.assertIsNone(self.deliveries[0]['task_id'])
        self.assertIn('恢复拆解', self.deliveries[0]['signal'])

    def test_active_lease_and_finished_decomposition_are_quiet(self):
        rid, _ = self._requirement(partial=True)
        for state, expiry in [('decomposing', '2999-01-01T00:00:00+00:00'), ('done', '')]:
            with self.subTest(state=state):
                self.conn.execute('UPDATE requirements SET decompose_state=?,decompose_expires_at=? WHERE id=?', (state, expiry, rid))
                self.conn.commit()
                self.assertEqual(scanner.scan_requirements(self.conn), 0)
                self.assertEqual(self._context(), '')

    def test_empty_expiry_recovers_partial_decomposition(self):
        rid, _ = self._requirement(partial=True)
        self.conn.execute("UPDATE requirements SET decompose_expires_at='' WHERE id=?", (rid,))
        self.conn.commit()
        self.assertEqual(scanner.scan_requirements(self.conn), 1)
        self.assertIn(f'#{rid}', self._context())

    def test_requirement_lifecycle_change_cancels_queued_job(self):
        rid, _ = self._requirement()
        for status, archived in [('done', 0), ('cancelled', 0), ('blocked', 0), ('open', 1)]:
            with self.subTest(status=status, archived=archived):
                self.conn.execute("UPDATE requirements SET status='open',archived=0 WHERE id=?", (rid,))
                self.conn.commit()
                self.assertEqual(scanner.scan_requirements(self.conn), 1)
                self.conn.execute('UPDATE requirements SET status=?,archived=? WHERE id=?', (status, archived, rid))
                self.conn.commit()
                self._process_cancelled()
                self.assertEqual(scanner.scan_requirements(self.conn), 0)

    def test_source_retraction_cancels_and_records_gate(self):
        rid, _ = self._requirement()
        self.assertEqual(scanner.scan_requirements(self.conn), 1)
        self.conn.execute("UPDATE messages SET retract_by='user' WHERE project='alpha'")
        self.conn.commit()
        self._process_cancelled()
        self.assertEqual(scanner.scan_requirements(self.conn), 0)
        reason = self.conn.execute('SELECT gate_reason FROM requirements WHERE id=?', (rid,)).fetchone()[0]
        self.assertIn('来源', reason)

    def test_permission_removed_after_enqueue_cancels(self):
        self._requirement()
        self.assertEqual(scanner.scan_requirements(self.conn), 1)
        self.conn.execute("DELETE FROM project_permissions WHERE permission='task.create'")
        self.conn.commit()
        self._process_cancelled()

    def test_disabled_member_drops_job_and_recovery_context(self):
        self._requirement()
        self.assertEqual(scanner.scan_requirements(self.conn), 1)
        self.conn.execute("UPDATE project_members SET active=0 WHERE project='alpha' AND profile='architect'")
        self.conn.commit()
        self.assertEqual(self._context(), '')
        queue.process_one(self.conn)
        self.assertEqual(self.deliveries, [])
        self.assertEqual(scanner.scan_requirements(self.conn), 0)

    def test_project_readiness_and_archive_gate_context_and_scan(self):
        self._requirement()
        for readiness, archived in [('pending', 0), ('ready', 1)]:
            with self.subTest(readiness=readiness, archived=archived):
                self.conn.execute('UPDATE projects SET readiness_status=?,archived=? WHERE name=?', (readiness, archived, 'alpha'))
                self.conn.commit()
                self.assertEqual(scanner.scan_requirements(self.conn), 0)
                self.assertEqual(self._context(), '')

    def test_stale_requirement_preserves_merged_message(self):
        rid, _ = self._requirement()
        scanner.scan_requirements(self.conn)
        self.conn.execute("UPDATE requirements SET status='done' WHERE id=?", (rid,))
        self.conn.commit()
        mid = self._message()
        self.assertTrue(queue.process_one(self.conn))
        self.assertEqual(self._job()['status'], 'done')
        self.assertIn('Merged real message', self.deliveries[0]['signal'])
        self.assertEqual(self.conn.execute('SELECT status FROM inbox WHERE message_id=?', (mid,)).fetchone()[0], 'pending')

    def test_stale_requirement_preserves_merged_eligible_task(self):
        rid, _ = self._requirement()
        scanner.scan_requirements(self.conn)
        task_rid, tid = self._requirement(partial=True, next_bot='architect')
        self.conn.execute("UPDATE requirements SET status='done' WHERE id=?", (rid,))
        self.conn.execute("UPDATE requirements SET decompose_state='done' WHERE id=?", (task_rid,))
        self.conn.commit()
        self.assertEqual(self._context(), '')
        self.assertTrue(queue.process_one(self.conn))
        self.assertEqual(self.deliveries[0]['task_id'], tid)
        self.assertIn(f'任务 #{tid}', self.deliveries[0]['signal'])
        self.assertNotIn('你有新消息', self.deliveries[0]['signal'])

    def test_other_project_or_member_messages_do_not_preserve_stale_job(self):
        rid, _ = self._requirement()
        scanner.scan_requirements(self.conn)
        self.conn.execute("UPDATE requirements SET status='done' WHERE id=?", (rid,))
        self.conn.commit()
        self._message(project='beta')
        self._message(profile='dev')
        self._process_cancelled()

    def test_owner_without_grant_gets_configuration_action(self):
        rid, _ = self._requirement()
        self._owner()
        self.assertEqual(scanner.scan_requirements(self.conn), 1)
        self.assertEqual(self._context(), '')
        self.assertTrue(queue.process_one(self.conn))
        self.assertEqual(self.deliveries[0]['profile'], 'default')
        for text in (f'#{rid}', '配置协调', '联系管理员', 'task.create'):
            self.assertIn(text, self.deliveries[0]['signal'])
        self.assertNotIn('complete=true', self.deliveries[0]['signal'])

    def test_owner_job_cancels_after_candidate_becomes_available(self):
        self._requirement()
        self._owner()
        self.assertEqual(scanner.scan_requirements(self.conn), 1)
        self.conn.execute("INSERT INTO project_permissions VALUES('alpha','architect','task.create')")
        self.conn.commit()
        self._process_cancelled()
        self.assertEqual(scanner.scan_requirements(self.conn), 1)
        self.assertEqual(self._job()['profile'], 'architect')

    def test_owner_change_invalidates_previous_coordination(self):
        self._requirement()
        self._owner()
        scanner.scan_requirements(self.conn)
        self._owner('dev')
        self._process_cancelled()
        self.assertEqual(scanner.scan_requirements(self.conn), 1)
        self.assertEqual(self._job()['profile'], 'dev')

    def test_disabled_owner_is_not_scheduled(self):
        self._requirement()
        self._owner()
        self.conn.execute("UPDATE project_members SET active=0 WHERE project='alpha' AND profile='default'")
        self.conn.commit()
        self.assertEqual(scanner.scan_requirements(self.conn), 0)
        self.assertEqual(self._context('default'), '')

    def test_role_grant_is_used_for_selection_and_context(self):
        rid, _ = self._requirement()
        role_id = self.conn.execute("SELECT role_id FROM project_members WHERE project='alpha' AND profile='architect'").fetchone()[0]
        self.conn.execute("DELETE FROM project_permissions WHERE permission='task.create'")
        self.conn.execute("INSERT INTO project_permissions VALUES('alpha',?,'task.create')", (f'role:{role_id}',))
        self.conn.commit()
        self.assertEqual(scanner.scan_requirements(self.conn), 1)
        self.assertIn(f'#{rid}', self._context())

    def test_claim_time_change_is_rechecked_before_delivery(self):
        rid, _ = self._requirement()
        scanner.scan_requirements(self.conn)

        def finish_requirement(*args):
            other = db.get_connection()
            try:
                other.execute("UPDATE requirements SET decompose_state='done' WHERE id=?", (rid,))
                other.commit()
            finally:
                other.close()
            return False

        with patch.object(wake, 'session_busy', side_effect=finish_requirement):
            self._process_cancelled()

    def test_new_requirement_preserves_coalesced_job(self):
        rid, _ = self._requirement()
        scanner.scan_requirements(self.conn)
        self.conn.execute("UPDATE requirements SET status='done' WHERE id=?", (rid,))
        self.conn.commit()
        replacement, _ = self._requirement()
        self.assertTrue(queue.process_one(self.conn))
        self.assertIn(f'#{replacement}', self.deliveries[0]['signal'])

    def test_coalesced_message_trigger_keeps_partial_recovery_on_fallback(self):
        rid, _ = self._requirement(partial=True)
        self._message()
        wake.enqueue(self.conn, 'architect', 'work', '@msg', project='alpha')
        self.assertEqual(scanner.scan_requirements(self.conn), 0)
        with patch.object(wake, '_fits_command_budget', return_value=False):
            self.assertTrue(queue.process_one(self.conn))
        self.assertIn(f'#{rid}', self.deliveries[0]['signal'])
        self.assertIn('upper_id=', self.deliveries[0]['signal'])
        self.assertIn('complete=true', self.deliveries[0]['signal'])


if __name__ == '__main__':
    unittest.main()
