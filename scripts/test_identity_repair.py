import os
import unittest
from unittest.mock import patch

from engine import db
from scripts import test_project_roles
from tools.repair_hermes_identity import repair


class RepairTests(unittest.TestCase):
    setUp = test_project_roles.ProjectRoleTests.setUp
    join = test_project_roles.ProjectRoleTests.join

    def test_worker_environment_does_not_rewrite_registered_roots(self):
        member = self.join(host='hermes')
        before = tuple(self.conn.execute('SELECT id,config_root FROM bots WHERE host_type=?', ('hermes',)).fetchone())
        for root in ('worker/profiles/dev', 'different-install'):
            with patch.dict(os.environ, {'HERMES_HOME': str(self.root / root)}):
                db.init_schema(self.conn)
        self.assertEqual(tuple(self.conn.execute('SELECT id,config_root FROM bots WHERE host_type=?', ('hermes',)).fetchone()), before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM bots WHERE host_type='hermes'").fetchone()[0], 1)

    def test_repair_preserves_id_and_archives_conflicting_binding(self):
        member = self.join(host='hermes')
        keep = self.conn.execute('SELECT bot_id FROM members WHERE profile=?', (member,)).fetchone()[0]
        old_root = str(self.root / 'legacy')
        old = self.conn.execute("INSERT INTO bots(host_type,profile,config_root,runtime,display_name,role) VALUES('hermes','default',?,'hermes','Old','')", (old_root,)).lastrowid
        for bot_id, session in ((keep, 'current'), (old, 'historical')):
            self.conn.execute("INSERT INTO session_bindings(project,bot_id,profile,kind,thread_id,session_id,updated_at) VALUES('p',?,?,'work',0,?,'now')", (bot_id, member, session))
        self.conn.commit()
        with self.conn:
            result = repair(self.conn, self.root / 'hermes', [old_root])
        self.assertEqual(result, [{'profile': 'default', 'from': old, 'to': keep}])
        self.assertEqual(self.conn.execute('SELECT session_id FROM session_bindings').fetchone()[0], 'current')
        self.assertIn('historical', self.conn.execute("SELECT payload FROM identity_repair_archive WHERE source_table='session_bindings'").fetchone()[0])
        self.assertEqual(self.conn.execute('SELECT bot_id FROM project_members').fetchone()[0], keep)
        with self.conn:
            self.assertEqual(repair(self.conn, self.root / 'hermes', [old_root]), [])


if __name__ == '__main__':
    unittest.main()
