"""req#99: shared scanner/action gate, real isolated source messages."""
import unittest
from unittest.mock import patch
from engine import db, requirement_gate, scanner, taskpool, wake
from scripts.unified_test_support import isolated_database, confirmed_requirement_fields
from scripts.test_task_brief import BRIEF


class RequirementGateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = isolated_database()
        self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        fields = confirmed_requirement_fields()
        self.rid = taskpool.req_create('alpha', 'default', 'Gate', **fields)['requirement_id']
        self.conn = db.get_connection()
        self.addCleanup(self.conn.close)
        # req#111：从 source_ref 解析真实来源消息 id（isolated_database 预插了 fixture 消息，
        # 不能按「第一条消息」取，否则会取到 fixture 消息而非 confirmed 插入的来源消息）。
        self.mid = int(fields['source_ref'].split('message_id=')[1])

    def update(self, **fields):
        self.conn.execute('UPDATE requirements SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?',
                          (*fields.values(), self.rid))
        self.conn.commit()

    def gate(self):
        return requirement_gate.check(self.conn, self.conn.execute('SELECT * FROM requirements WHERE id=?', (self.rid,)).fetchone())

    def create(self):
        return taskpool.task_create('alpha', 'architect', self.rid, [dict(title='Task', **BRIEF)])

    def assert_blocked(self, field):
        self.assertIn(field, [e['field'] for e in self.gate()['field_errors']])
        with patch.object(wake, 'enqueue') as enqueue:
            self.assertEqual(scanner.scan_requirements(self.conn), 0)
            enqueue.assert_not_called()
        self.assertEqual(wake._requirement_signal(self.conn, 'architect', 'work', 'alpha'), '')
        counts = [self.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in ('tasks', 'nodes', 'events')]
        self.assertEqual(self.create()['code'], 'requirement_decomposition_blocked')
        self.assertEqual(counts, [self.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in ('tasks', 'nodes', 'events')])

    def test_confirmed_source_passes_all_entry_points(self):
        self.assertIsNone(self.gate())
        with patch.object(wake, 'enqueue', return_value=True) as enqueue:
            self.assertGreater(scanner.scan_requirements(self.conn), 0)
            enqueue.assert_called()
        self.assertIn(f'#{self.rid}', wake._requirement_signal(self.conn, 'architect', 'work', 'alpha'))
        self.assertIn('task_ids', self.create())

    def test_undecided_strict_none_and_whitespace(self):
        for value in ('', ' ', '已解决', '范围待定', '无；待确认'):
            with self.subTest(value=value):
                self.update(undecided=value)
                self.assert_blocked('undecided')
        self.update(undecided=' 无 \n')
        self.assertIsNone(self.gate())

    def test_source_format_and_confirm_tail_ignored(self):
        # 格式非法（缺字段、负 ID、零 ID、超长 ID）被拒。
        for value in ('', 'thread_id=0', 'message_id=1', 'thread_id=-1; message_id=1',
                      'thread_id=0; message_id=0', 'thread_id=0; message_id=999999999999999999999'):
            with self.subTest(value=value):
                self.update(source_ref=value)
                self.assert_blocked('source_ref')
        # req#111：confirm 尾巴（direction/decompose）语义废弃、仅作旧数据兼容，不再区分方向。
        for value in (f'thread_id=0; message_id={self.mid}',
                      f'thread_id=0; message_id={self.mid}; confirm=direction',
                      f'thread_id=0; message_id={self.mid}; confirm=decompose'):
            with self.subTest(value=value):
                self.update(source_ref=value)
                self.assertIsNone(self.gate())

    def test_source_real_project_user_and_retraction(self):
        for column, value in [('project', 'beta'), ('sender_type', 'bot'), ('retract_by', 'user')]:
            with self.subTest(column=column):
                self.conn.execute(f'UPDATE messages SET {column}=? WHERE id=?', (value, self.mid))
                self.conn.commit()
                self.assert_blocked('source_ref')
                self.conn.execute("UPDATE messages SET project='alpha',sender_type='user',retract_by=NULL WHERE id=?", (self.mid,))
                self.conn.commit()
        self.update(source_ref='thread_id=0; message_id=9999')
        self.assert_blocked('source_ref')

    def test_thread_scope_and_message_link(self):
        tid = self.conn.execute("INSERT INTO threads(project,title,creator,members,created_at,last_active_at) VALUES('alpha','source','user','[]','now','now')").lastrowid
        self.conn.commit()
        self.update(source_ref=f'thread_id={tid}; message_id={self.mid}')
        self.assert_blocked('source_ref')
        self.conn.execute('UPDATE messages SET thread_id=? WHERE id=?', (tid, self.mid))
        self.conn.commit()
        self.assertIsNone(self.gate())
        self.conn.execute("UPDATE threads SET project='beta' WHERE id=?", (tid,))
        self.conn.commit()
        self.assert_blocked('source_ref')
        self.update(source_ref=f'thread_id=9999; message_id={self.mid}')
        self.assert_blocked('source_ref')

    def test_legacy_and_partial_upgrade(self):
        self.update(**dict.fromkeys(requirement_gate.FIELDS, ''), description='Original legacy body')
        self.assertIsNone(self.gate())
        self.update(background_goal='partial')
        self.assert_blocked('undecided')
        self.assert_blocked('source_ref')
        self.update(background_goal='', description='')
        self.assert_blocked('undecided')

    def test_creation_rechecks_after_scan(self):
        with patch.object(wake, 'enqueue', return_value=True):
            self.assertGreater(scanner.scan_requirements(self.conn), 0)
        self.conn.execute("UPDATE messages SET retract_by='user' WHERE id=?", (self.mid,))
        self.conn.commit()
        self.assert_blocked('source_ref')


if __name__ == '__main__':
    unittest.main()
