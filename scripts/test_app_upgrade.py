"""Administrator upgrade handoff, pinned targets and recovery boundaries."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from engine import app_upgrade, auth, config, restart
from tools import releases, update_engine, upgrade
from scripts.test_user_display_name import UserNameAPI


class AppUpgradeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, GROUP_DATA_DIR=str(self.root / 'data'), GROUP_DB=str(self.root / 'data/group.db')).start()
        patch.object(config, 'program_root', return_value=self.root).start()
        (self.root / 'VERSION').write_text('v1.0.0')
        (self.root / 'release_manifest.json').write_text(json.dumps({'program_files': ['VERSION']}))
        self.target = releases.Release('v1.1.0', 'https://example.invalid/code.zip', 'a' * 64,
                                       'https://example.invalid/manifest.json', 'b' * 64)
        patch.object(upgrade, 'discover_release', return_value=self.target).start()
        self.spawn = patch.object(app_upgrade.subprocess, 'Popen').start()

    def begin(self, id='a' * 32):
        return app_upgrade.begin(id, 'v1.1.0', 'old-instance')

    def test_handoff_pins_target_and_base_python_and_deduplicates(self):
        first = self.begin()
        self.assertEqual(self.begin()['id'], first['id'])
        self.assertEqual(self.begin('b' * 32)['id'], first['id'])
        self.spawn.assert_called_once()
        self.assertEqual(first['target']['sha256'], 'a' * 64)
        command = self.spawn.call_args.args[0]
        self.assertEqual(Path(command[0]), app_upgrade.base_python())
        self.assertIn('update_engine.py', command[-2])
        env = self.spawn.call_args.kwargs['env']
        self.assertEqual(env['GROUP_DB'], first['db_path'])
        self.assertEqual(env['GROUP_DATA_DIR'], str(config.data_dir()))
        with self.assertRaisesRegex(ValueError, '正在升级'):
            restart.Restart('old-instance').begin()

    def test_git_manifest_missing_and_version_change_rejected_before_spawn(self):
        (self.root / '.git').mkdir()
        with patch.object(app_upgrade, 'version_info', return_value={'version_source': 'git'}):
            with self.assertRaisesRegex(ValueError, 'Git'):
                self.begin()
        (self.root / '.git').rmdir()
        with self.assertRaisesRegex(ValueError, '版本已变化'):
            app_upgrade.begin('a' * 32, 'v9.9.9', 'old')
        (self.root / 'release_manifest.json').unlink()
        with self.assertRaisesRegex(ValueError, '清单'):
            self.begin()
        self.spawn.assert_not_called()

    def test_restart_and_spawn_failure_and_dead_helper(self):
        with patch.object(restart, 'status', return_value={'state': 'restarting'}):
            with self.assertRaisesRegex(ValueError, '正在重启'):
                self.begin()
        self.spawn.side_effect = OSError('launch failed')
        with self.assertRaises(OSError):
            self.begin()
        self.assertEqual(app_upgrade.status()['state'], 'failed')
        app_upgrade.write_receipt({'state': 'running', 'created_at': time.time() - 60, 'helper_pid': 999})
        with patch.object(app_upgrade, '_pid_alive', return_value=False):
            self.assertEqual(app_upgrade.status()['state'], 'failed')

    def test_helper_pins_release_and_restarts_only_safe_outcomes(self):
        operation = self.begin()
        for phase, result, starts in [('checking', 0, True), ('rolled_back', 1, True),
                                      ('rollback_failed', 1, False), ('downloading', 1, False),
                                      ('backing_up', 1, True)]:
            with self.subTest(phase=phase):
                app_upgrade.write_receipt(operation)
                def execute(*, target, progress):
                    self.assertEqual(target, self.target)
                    if phase != 'downloading':
                        progress('backing_up')
                    progress(phase)
                    return result
                with patch.object(upgrade, 'do_upgrade', side_effect=execute), \
                     patch.object(upgrade, '_admin_headers', return_value={}), \
                     patch.object(upgrade, 'current_version', return_value='v1.1.0'), \
                     patch.object(update_engine, 'start_and_verify', return_value='new') as start:
                    self.assertEqual(update_engine.run(operation['id']), result)
                    self.assertEqual(start.called, starts)
                receipt = app_upgrade.read_receipt()
                self.assertEqual(receipt['state'], 'failed' if result else 'succeeded')

    def test_api_authorization_and_confirmation_contract(self):
        with UserNameAPI() as fixture, patch.object(app_upgrade, 'begin', return_value={'id': 'a' * 32}) as begin:
            fixture.app.state.server = Mock()
            fixture.app.state.instance_id = 'old'
            payload = {'request_id': 'a' * 32, 'version': 'v1.1.0'}
            denied = fixture.client.post('/system/upgrade', json=payload,
                headers={'X-Group-Profile': 'bot-a', 'X-Group-Key': auth.bot_token('bot-a')})
            self.assertEqual(denied.status_code, 403)
            with patch.object(auth, '_password', return_value='secret'):
                self.assertEqual(fixture.client.post('/system/upgrade', json=payload).status_code, 401)
                self.assertEqual(fixture.client.get('/system/upgrade').status_code, 401)
            self.assertEqual(fixture.client.post('/system/upgrade', json={'version': 'v1.1.0'}).status_code, 422)
            response = fixture.client.post('/system/upgrade', json=payload)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            begin.assert_called_once_with('a' * 32, 'v1.1.0', 'old')

    def test_new_service_start_failure_rolls_back_matching_backup(self):
        operation = self.begin()
        backup = self.root / 'backup'
        backup.mkdir()
        (backup / 'upgrade_target.json').write_text(json.dumps({'target': operation['target']}))
        def execute(*, target, progress):
            progress('backing_up')
            progress('checking')
            return 0
        with patch.object(upgrade, 'do_upgrade', side_effect=execute), \
             patch.object(upgrade, '_admin_headers', return_value={}), \
             patch.object(upgrade, 'latest_backup', return_value=backup), \
             patch.object(upgrade, '_rollback', return_value=0) as rollback, \
             patch.object(update_engine, 'start_and_verify', side_effect=[RuntimeError('startup'), 'restored']):
            self.assertEqual(update_engine.run(operation['id']), 1)
        rollback.assert_called_once_with(backup)
        receipt = app_upgrade.read_receipt()
        self.assertEqual(receipt['new_instance_id'], 'restored')
        self.assertIn('已恢复原版本并启动', receipt['error'])
