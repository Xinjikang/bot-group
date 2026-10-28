"""#255 老库迁移回归：清理状态机字段补齐 + 唯一索引收紧 + 数据保留（reviewer #255 次要①）。"""
import sqlite3
import unittest

from engine import db

# 老库 schema：宽口径唯一索引（state!='terminal' OR cleanup_pending=1）+ 无清理状态机新列。
_OLD_SCHEMA = """
CREATE TABLE hermes_executions (
    execution_id TEXT PRIMARY KEY, session TEXT NOT NULL, started_at REAL NOT NULL,
    project TEXT NOT NULL, profile TEXT NOT NULL, kind TEXT NOT NULL, thread_id INTEGER,
    parent_id INTEGER, round INTEGER, task_id INTEGER, job_id INTEGER, event_floor INTEGER,
    source TEXT NOT NULL, python TEXT NOT NULL, home TEXT NOT NULL, workdir TEXT NOT NULL,
    session_id TEXT NOT NULL, profile_id TEXT NOT NULL, submission_id TEXT NOT NULL UNIQUE,
    admission_id TEXT, generation INTEGER, authority_epoch INTEGER,
    state TEXT NOT NULL DEFAULT 'submitting', cancel_reason TEXT, submit_deadline REAL NOT NULL,
    cancel_deadline REAL, reconcile_deadline REAL NOT NULL, execution_deadline REAL,
    outcome TEXT, ended_at REAL, cleanup_pending INTEGER NOT NULL DEFAULT 0,
    rewind_target INTEGER, detail TEXT NOT NULL DEFAULT '', UNIQUE(session, started_at)
);
CREATE UNIQUE INDEX hermes_execution_active
ON hermes_executions(session) WHERE state != 'terminal' OR cleanup_pending=1;
"""


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript(_OLD_SCHEMA)
        self.conn.execute(
            "INSERT INTO hermes_executions(execution_id,session,started_at,project,profile,kind,"
            "source,python,home,workdir,session_id,profile_id,submission_id,submit_deadline,"
            "reconcile_deadline,state,cleanup_pending) "
            "VALUES('e1','s',100,'p','b','work','','','','','sid','pid','sub',130,250,'terminal',1)")
        self.conn.commit()

    def test_migration_adds_columns_tightens_index_and_preserves_data(self):
        db.init_schema(self.conn)
        cols = {r['name'] for r in self.conn.execute('PRAGMA table_info(hermes_executions)')}
        self.assertTrue({'retry_count', 'next_retry_at', 'cleanup_deadline'} <= cols)
        idx_sql = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='hermes_execution_active'").fetchone()['sql']
        self.assertNotIn('cleanup_pending', idx_sql)      # 索引收紧为窄口径
        self.assertIn("state != 'terminal'", idx_sql)
        row = self.conn.execute("SELECT * FROM hermes_executions WHERE execution_id='e1'").fetchone()
        self.assertEqual(row['cleanup_pending'], 1)       # 数据保留
        self.assertEqual(row['retry_count'], 0)          # 新列默认值


if __name__ == '__main__':
    unittest.main()
