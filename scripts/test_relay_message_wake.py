"""Remote direct-message admission, withdrawal and scanner recovery."""
import unittest
from unittest.mock import patch

from engine import api, db, hermes_gateway_ledger as ledger, queue, scanner, wake
from scripts.unified_test_support import isolated_database


class RemoteMessageWakeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(isolated_database())
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO relay_nodes(member,created_at,state,direction) VALUES('dev',0,'online','dial')")
        self.conn.commit()
        self.enterContext(patch.object(wake, 'session_busy', return_value=False))

    def message(self):
        mid = self.conn.execute("""INSERT INTO messages(project,ts,sender,sender_type,content,mentions,need_reply)
            VALUES('alpha',?,'user','user','test','["dev"]',1)""", (db.now_iso(),)).lastrowid
        self.conn.execute("INSERT INTO inbox(message_id,profile) VALUES(?,'dev')", (mid,))
        self.conn.commit()
        return mid

    def withdraw(self, mid):
        with patch.object(api, '_resolve_identity', return_value=('user', 'user')), patch.object(api.events.bus, 'publish'):
            api.post_message_retract(mid, {})

    def pending(self):
        return [r['message_id'] for r in wake._snapshot_pending(self.conn, 'dev', 'work', None, 'alpha')]

    def test_withdraw_only_input(self):
        mid = self.message()
        wake.enqueue(self.conn, 'dev', 'work', '@msg', mid, project='alpha')
        self.withdraw(mid)
        with patch.object(wake, 'deliver') as deliver:
            queue.process_one(self.conn)
            deliver.assert_not_called()

    def test_coalesced_second_message_survives(self):
        first, second = self.message(), self.message()
        self.assertTrue(wake.enqueue(self.conn, 'dev', 'work', '@msg', first, project='alpha'))
        self.assertFalse(wake.enqueue(self.conn, 'dev', 'work', '@msg', second, project='alpha'))
        self.withdraw(first)
        with patch.object(wake, 'deliver', return_value={'ok': True}) as deliver:
            queue.process_one(self.conn)
            deliver.assert_called_once()
        self.assertEqual(self.pending(), [second])

    def test_delivered_unread_does_not_wake_again_and_failed_delivery_can_retry(self):
        first = self.message()
        row = ledger.prepare(self.conn, session='dev-alpha-work', project='alpha', profile='dev', kind='work',
            thread_id=None, task_id=None, job_id=None, event_floor=0, source='', python='', home='', workdir='',
            session_id='session', profile_id='dev', relay_message_ids=[first])
        self.assertEqual(scanner.scan_inbox(self.conn), 0)
        self.conn.execute("UPDATE hermes_executions SET state='terminal',outcome='completed' WHERE execution_id=?",
                          (row['execution_id'],))
        self.conn.commit()
        self.assertEqual(scanner.scan_inbox(self.conn), 0)
        self.assertEqual(self.pending(), [])
        status = self.conn.execute("SELECT status FROM inbox WHERE message_id=?", (first,)).fetchone()[0]
        self.assertEqual(status, 'pending')
        second = self.message()
        self.assertEqual(self.pending(), [second])
        self.assertEqual(scanner.scan_inbox(self.conn), 1)
        self.withdraw(second)
        self.conn.execute("UPDATE hermes_executions SET outcome='failed' WHERE execution_id=?", (row['execution_id'],))
        self.conn.commit()
        self.assertEqual(self.pending(), [first])
