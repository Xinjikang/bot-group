"""Binding writes and recovery use isolated SQLite and mocked Hermes sessions."""
import tempfile
import unittest
from unittest.mock import patch
from engine import db, session_adapter as adapter


class BindingColumnOrderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patched = patch.object(db, 'db_path', return_value=tmp.name + '/group.db')
        patched.start()
        self.addCleanup(patched.stop)
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        db.init_schema(self.conn)

    def binding(self):
        return self.conn.execute('SELECT * FROM session_bindings').fetchone()

    def resolve(self):
        return adapter.resolve_or_create(self.conn, 'p', 'dev', 'work', None, 'dev-p-work')

    def test_legacy_and_new_session_survive_second_wake(self):
        for existing in ('legacy-session', None):
            with self.subTest(existing=existing):
                self.conn.execute('DELETE FROM session_bindings')
                self.conn.commit()
                sid = existing or 'new-session'
                with patch.object(adapter, 'resolve_session_by_title', return_value=existing), \
                     patch.object(adapter, 'create_session', return_value=sid), \
                     patch.object(adapter, 'session_exists', return_value=True), \
                     patch.object(adapter, 'compression_tip', side_effect=lambda p, s: s):
                    self.assertEqual(self.resolve(), sid)
                    self.assertEqual((self.binding()['session_id'], self.binding()['generation']), (sid, 0))
                    self.assertEqual(self.resolve(), sid)

    def corrupt_binding(self):
        with patch.object(adapter, 'resolve_session_by_title', return_value='real-session'):
            self.resolve()
        self.conn.execute("UPDATE session_bindings SET session_id='0', generation='real-session', switching=1")
        self.conn.commit()

    def test_recovery_preserves_real_session_and_new_can_increment(self):
        self.corrupt_binding()
        with patch.object(adapter, 'session_exists', return_value=True), \
             patch.object(adapter, 'compression_tip', side_effect=lambda p, s: s), \
             patch.object(adapter, 'create_session', return_value='next-session'):
            self.assertEqual(self.resolve(), 'real-session')
            self.assertEqual(self.binding()['generation'], 0)
            self.assertEqual(self.binding()['switching'], 0)
            result = adapter.switch_session(self.conn, 'p', 'dev', 'work', None, 'dev-p-work')
            self.assertTrue(result['ok'])
            self.assertEqual(result['generation'], 1)

    def test_new_directly_recovers_shifted_binding(self):
        self.corrupt_binding()
        with patch.object(adapter, 'session_exists', return_value=True), \
             patch.object(adapter, 'create_session', return_value='next-session'):
            result = adapter.switch_session(self.conn, 'p', 'dev', 'work', None, 'dev-p-work')
            self.assertTrue(result['ok'])
            self.assertEqual(self.binding()['session_id'], 'next-session')
            self.assertEqual(self.binding()['generation'], 1)

    def test_unknown_session_is_preserved_for_manual_recovery(self):
        self.corrupt_binding()
        with patch.object(adapter, 'session_exists', return_value=False):
            with self.assertRaises(adapter.SessionBindingInvalid):
                self.resolve()
        self.assertEqual(self.binding()['session_id'], '0')

