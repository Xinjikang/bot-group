"""Isolated database relocation, recovery, custom-path and legacy-updater checks."""

from contextlib import closing, ExitStack
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from engine import config, db
from engine.maintenance import database_layout as layout


class DatabaseLayoutTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.stack.enter_context(patch.object(config, 'program_root', return_value=self.root))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch.object(layout.socket, 'create_connection', side_effect=ConnectionRefusedError))
        config.apply_cli(host=None, port=None, data_dir=None)
        config._dotenv_cache.clear()
        self.addCleanup(config._dotenv_cache.clear)
        self.source = self.root / 'group.db'
        self.target = self.root / 'data/group.db'

    def seed(self, path=None):
        path = path or self.source
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY, body TEXT)')
            db.execute("INSERT INTO messages VALUES(1, 'preserved message')")
            db.commit()

    def assert_data(self, path):
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone(), ('ok',))
            self.assertEqual(db.execute('SELECT * FROM messages').fetchall(), [(1, 'preserved message')])

    def test_fresh_install_and_existing_attachments(self):
        self.assertEqual(Path(config.db_path()), self.target)
        self.assertEqual(config.attachments_dir(), self.root / 'attachments')
        self.assertIsNone(layout.migrate())
        self.assertTrue(self.target.parent.is_dir())
        self.assertFalse(self.target.exists())

    def test_migrate_default_and_repeat_without_changing_data(self):
        self.seed()
        (self.root / 'attachments').mkdir()
        (self.root / 'attachments/a.txt').write_text('attachment')
        self.assertEqual(Path(config.db_path()), self.source)
        result = layout.migrate()
        self.assertFalse(self.source.exists())
        self.assert_data(self.target)
        self.assert_data(Path(result['backup']))
        self.assertEqual(Path(config.db_path()), self.target)
        self.assertEqual(layout.migrate()['id'], result['id'])
        self.assertEqual((self.root / 'attachments/a.txt').read_text(), 'attachment')

    def test_custom_database_and_data_directory_remain_in_place(self):
        self.seed()
        custom = self.root / 'custom.sqlite'
        self.seed(custom)
        with patch.dict(os.environ, {'GROUP_DB': str(custom)}):
            self.assertIsNone(layout.migrate())
            self.assertEqual(Path(config.db_path()), custom)
        with patch.dict(os.environ, {'GROUP_DATA_DIR': str(self.root / 'custom')}):
            self.assertIsNone(layout.migrate())
            self.assertEqual(Path(config.db_path()), self.root / 'custom/group.db')
        self.assertTrue(self.source.exists())

    def test_explicit_root_override_resolves_after_migration_in_new_process_environment(self):
        self.seed()
        (self.root / '.env').write_text('GROUP_DB=' + self.source.as_posix() + '\n')
        with patch.dict(os.environ, {'GROUP_DB': str(self.source)}):
            layout.migrate()
        config._dotenv_cache.clear()
        self.assertEqual(Path(config.db_path()), self.target)
        self.assertEqual(config.attachments_dir(), self.root / 'attachments')

    def test_conflicting_databases_are_preserved(self):
        self.seed()
        self.seed(self.target)
        with self.assertRaisesRegex(RuntimeError, '同时存在'):
            layout.migrate()
        self.assert_data(self.source)
        self.assert_data(self.target)

    def test_running_engine_blocks_migration(self):
        self.seed()
        with patch.object(layout.socket, 'create_connection', side_effect=None), self.assertRaisesRegex(RuntimeError, '停止引擎'):
            layout.migrate()
        self.assert_data(self.source)
        self.assertFalse(self.target.exists())

    def test_corrupt_source_does_not_publish_target(self):
        self.source.write_bytes(b'not a sqlite database')
        with self.assertRaises(sqlite3.DatabaseError):
            layout.migrate()
        self.assertEqual(self.source.read_bytes(), b'not a sqlite database')
        self.assertFalse(self.target.exists())

    def test_publish_failure_restores_source_and_allows_retry(self):
        self.seed()
        replace = os.replace
        def fail_stage(src, dst):
            if Path(src).suffix == '.pending' and Path(dst) == self.target:
                raise OSError('simulated publication failure')
            return replace(src, dst)
        with patch.object(layout.os, 'replace', side_effect=fail_stage), self.assertRaises(OSError):
            layout.migrate()
        self.assert_data(self.source)
        self.assertFalse(self.target.exists())
        layout.migrate()
        self.assert_data(self.target)

    def test_interrupted_published_snapshot_finishes_on_retry(self):
        self.seed()
        result = layout.migrate()
        result['state'] = 'prepared'
        layout._write(layout._paths()[2], result)
        with self.assertRaisesRegex(RuntimeError, '尚未完成'):
            config.db_path()
        self.assertEqual(layout.migrate()['state'], 'complete')
        self.assert_data(self.target)

    def test_rollback_is_bound_to_upgrade_and_preserves_failed_target(self):
        self.seed()
        upgrade_backup = self.root / 'backups/upgrade-test'
        result = layout.migrate(upgrade_backup=upgrade_backup)
        layout.rollback(self.root / 'backups/other')
        self.assert_data(self.target)
        layout.rollback(upgrade_backup)
        self.assert_data(self.source)
        self.assertFalse(self.target.exists())
        self.assertTrue(Path(result['backup']).with_suffix('.failed.db').exists())
        self.assertEqual(Path(config.db_path()), self.source)

    def test_legacy_gui_helper_finishes_before_next_launch_migration(self):
        self.seed()
        logs = self.root / 'logs'
        logs.mkdir()
        receipt = {'state': 'running', 'helper_pid': os.getppid(), 'program_root': str(self.root)}
        path = logs / 'app-upgrade.json'
        path.write_text(json.dumps(receipt))
        self.assertIsNone(layout.migrate())
        self.assert_data(self.source)
        receipt['state'] = 'succeeded'
        path.write_text(json.dumps(receipt))
        layout.migrate()
        self.assert_data(self.target)

    def test_current_gui_upgrade_migrates_within_upgrade(self):
        self.seed()
        logs = self.root / 'logs'
        logs.mkdir()
        (logs / 'app-upgrade.json').write_text(json.dumps({
            'state': 'running', 'helper_pid': os.getppid(), 'program_root': str(self.root),
            'database_layout_version': 1}))
        layout.migrate(upgrade_backup=self.root / 'backups/upgrade-test')
        self.assert_data(self.target)

    def test_idle_connection_preserves_wal_without_blocking_writes_or_checkpoints(self):
        self.seed()
        layout.migrate()
        wal = Path(str(self.target) + '-wal')
        with db.keep_wal_open() as keeper:
            self.assertFalse(keeper.in_transaction)
            for index in range(3):
                with closing(db.get_connection()) as writer:
                    writer.execute('INSERT INTO messages VALUES(?, ?)', (index + 2, 'new'))
                    writer.commit()
                    self.assertEqual(writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0], 0)
                self.assertTrue(wal.exists())
                self.assertFalse(keeper.in_transaction)
        self.assertFalse(wal.exists())

    def test_live_write_transaction_blocks_checkpoint_and_migration(self):
        self.seed()
        with closing(sqlite3.connect(self.source)) as writer:
            writer.execute("INSERT INTO messages VALUES(2, 'uncommitted')")
            with self.assertRaises((RuntimeError, sqlite3.OperationalError)):
                layout.migrate()
            writer.rollback()
        self.assert_data(self.source)
        self.assertFalse(self.target.exists())

    def test_upgrade_rollback_restores_binding_and_snapshot(self):
        from engine.maintenance import upgrade
        self.seed()
        upgrade_backup = self.root / 'backups/upgrade-test'
        upgrade._backup(upgrade_backup)
        layout.migrate(upgrade_backup=upgrade_backup)
        with patch.object(upgrade, '_stop_independent', return_value=0), \
             patch.object(upgrade, '_installer', return_value=self.root / 'absent-installer'):
            self.assertEqual(upgrade._rollback(upgrade_backup), 0)
        self.assert_data(self.source)
        self.assertEqual(Path(config.db_path()), self.source)

    def test_corrupt_receipt_blocks_start_without_creating_database(self):
        self.target.parent.mkdir()
        (self.target.parent / 'database-layout.json').write_text('[]')
        with self.assertRaisesRegex(RuntimeError, '记录格式损坏'):
            config.db_path()
        self.assertFalse(self.target.exists())

    def test_prepared_receipt_recovers_archived_source_before_retry(self):
        self.seed()
        result = layout.migrate()
        self.target.unlink()
        result['state'] = 'prepared'
        layout._write(layout._paths()[2], result)
        layout.migrate()
        self.assert_data(self.target)


if __name__ == '__main__':
    unittest.main()
