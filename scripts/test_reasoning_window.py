"""Reasoning window regression tests; in-memory database only.
Run: python -B -m unittest discover -s scripts -p test_reasoning_window.py -v
"""
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import reasoning

class MemoryDB(sqlite3.Connection):
    def close(self): pass

class ReasoningWindowTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(":memory:",factory=MemoryDB)
        self.c.row_factory=sqlite3.Row
        self.c.executescript("""CREATE TABLE sessions(id TEXT,title TEXT,started_at REAL);
        CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,reasoning TEXT,reasoning_content TEXT,tool_calls TEXT,tool_name TEXT,timestamp REAL,active INTEGER,content TEXT);
        INSERT INTO sessions VALUES('s','bot-work',1);
        INSERT INTO sessions VALUES('other','other-work',1);""")
        self.add(1,99.9,"old")
        self.add(2,100.125,"current start")
        self.add(3,101,"current")
        self.add(4,200,"next wake")
        self.add(5,99.95,"late old row")
        self.add(6,102,"late current row")
        self.add(7,103,"other session",session="other")
    def tearDown(self):
        sqlite3.Connection.close(self.c)
    def add(self,i,ts,text,session="s"):
        self.c.execute("INSERT INTO messages VALUES(?,?,'assistant',?,NULL,NULL,NULL,?,1,NULL)",(i,session,text,ts))
    def read(self,start=100.125,end=200,after=0):
        with patch.object(reasoning,"_profile_state_db",return_value=Path("present")),patch.object(Path,"exists",return_value=True),patch.object(reasoning.sqlite3,"connect",return_value=self.c):
            return reasoning.read_reasoning("bot","bot-work",after,wake_started_at=start,wake_ended_at=end)
    def test_zero_cursor_after_refresh_stays_in_window(self):
        self.assertEqual([e['id'] for e in self.read()['entries']],[2,3,6])
    def test_incremental_cursor_and_late_old_row(self):
        result=self.read(after=3)
        self.assertEqual([e['id'] for e in result['entries']],[6])
        self.assertEqual(result['latest_id'],6)
    def test_next_wake_has_independent_cursor(self):
        self.assertEqual([e['id'] for e in self.read(start=200,end=None)['entries']],[4])
    def test_no_current_rows_does_not_fall_back_to_history(self):
        self.assertEqual(self.read(start=300,end=None)['entries'],[])
    def test_missing_start_is_rejected(self):
        with self.assertRaises(TypeError):reasoning.read_reasoning('bot','bot-work')

    def test_send_result_is_a_boundary_even_without_reasoning(self):
        import json
        content = '<untrusted_tool_result>\n' + json.dumps({"result": json.dumps({"message_id": 42})}) + '\n</untrusted_tool_result>'
        self.c.execute("INSERT INTO messages VALUES(8,'s','tool',NULL,NULL,NULL,'mcp__task_queue__group_send',104,1,?)", (content,))
        result = self.read(after=7)
        self.assertEqual(result['entries'][0]['reply_message_id'], 42)
        self.assertEqual(result['latest_id'], 8)

    def test_inbox_message_ids_do_not_split_reasoning(self):
        self.c.execute("INSERT INTO messages VALUES(8,'s','tool',NULL,NULL,NULL,'mcp__task_queue__group_read_inbox',104,1,?)", ('{"message_id":42}',))
        self.assertEqual(self.read(after=7)['entries'], [])

    def test_final_assistant_marks_completion(self):
        self.c.execute("INSERT INTO messages VALUES(8,'s','assistant',NULL,NULL,NULL,NULL,104,1,'done')")
        self.assertTrue(self.read(after=7)['entries'][0]['finished'])

    def test_assistant_compaction_summary_advances_cursor_without_finishing(self):
        self.c.execute("INSERT INTO messages VALUES(8,'s','assistant',NULL,NULL,NULL,NULL,150,1,?)",
                       ('[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns',))
        compacted = self.read(end=None, after=7)
        self.assertEqual(compacted, {'entries': [], 'latest_id': 8})
        self.add(9, 151, 'continue after compaction')
        self.c.execute("INSERT INTO messages VALUES(10,'s','tool',NULL,NULL,NULL,'mcp__task_queue__group_send',152,1,?)",
                       ('{"message_id":42}',))
        self.c.execute("INSERT INTO messages VALUES(11,'s','assistant',NULL,NULL,NULL,NULL,153,1,'done')")
        completed = self.read(after=compacted['latest_id'])
        self.assertEqual([e['id'] for e in completed['entries']], [9, 10, 11])
        self.assertFalse(completed['entries'][0]['finished'])
        self.assertEqual(completed['entries'][1]['reply_message_id'], 42)
        self.assertTrue(completed['entries'][2]['finished'])

    def test_failed_send_does_not_create_reply(self):
        self.assertIsNone(reasoning._sent_message_id('{"error":"missing thread_id"}'))

    def test_missed_next_wake_event_is_bounded_by_user_turn(self):
        self.c.execute("INSERT INTO messages VALUES(8,'s','user',NULL,NULL,NULL,NULL,100.2,1,'wake one')")
        self.c.execute("INSERT INTO messages VALUES(9,'s','user',NULL,NULL,NULL,NULL,200,1,'wake two')")
        result = self.read(end=None)
        self.assertNotIn(4, [entry['id'] for entry in result['entries']])

    def test_compaction_summary_before_copied_wake_does_not_cut_window(self):
        # Production ordering: summary has the smaller ID but a later timestamp.
        self.c.execute("INSERT INTO messages VALUES(8,'s','user',NULL,NULL,NULL,NULL,150,1,?)",
                       ('[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns',))
        self.c.execute("INSERT INTO messages VALUES(9,'s','user',NULL,NULL,NULL,NULL,100.2,1,'wake one')")
        self.c.execute("INSERT INTO messages VALUES(10,'s','user',NULL,NULL,NULL,NULL,200,1,'wake two')")
        self.assertEqual([e['id'] for e in self.read(end=None)['entries']], [2, 3, 6])

    def test_recorded_end_ignores_intermediate_user_rows(self):
        self.c.execute("INSERT INTO messages VALUES(8,'s','user',NULL,NULL,NULL,NULL,100.2,1,'wake')")
        self.c.execute("INSERT INTO messages VALUES(9,'s','user',NULL,NULL,NULL,NULL,100.8,1,'injected context')")
        self.assertEqual([e['id'] for e in self.read()['entries']], [2, 3, 6])

if __name__=='__main__':unittest.main()
