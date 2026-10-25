"""Read-only version discovery, release selection and HTTP authorization."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import app_updates, auth, version
from engine.maintenance import releases, upgrade
from scripts.test_user_display_name import UserNameAPI


class AppUpdateTests(unittest.TestCase):
    def test_real_index_selection_and_version_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entry = dict(version='v1.2.0', source='source.zip', sha256='a' * 64,
                         manifest='manifest.json', manifest_sha256='b' * 64)
            index = root / 'releases.json'
            index.write_text(json.dumps({'releases': [entry,
                {**entry, 'version': 'v99.0.0', 'draft': True},
                {**entry, 'version': 'v98.0.0', 'prerelease': True}]}))
            with patch.object(upgrade.config, 'program_root', return_value=root), \
                 patch.dict('os.environ', {'RELEASE_INDEX_URL': index.as_uri()}):
                unknown = app_updates.check_update()
                self.assertEqual(unknown['status'], 'unknown')
                self.assertEqual(unknown['version_label'], '源码副本 · 缺少版本信息')
                self.assertFalse(unknown['update_available'])
                for version, available in [('v1.1.0', True), ('1.2.0', False), ('v2.0.0', False), ('invalid', False)]:
                    (root / 'VERSION').write_text(version)
                    result = app_updates.check_update()
                    self.assertEqual(result['current_version'], version if releases.version_key(version) else None)
                    self.assertEqual(result['latest_version'], 'v1.2.0')
                    self.assertEqual(result['update_available'], available)

    def test_failure_then_retry_and_default_source(self):
        with patch.object(upgrade.config, '_pick', return_value=releases.DEFAULT_INDEX_URL), \
             patch.object(releases, 'fetch_index', side_effect=OSError('offline')) as fetch:
            self.assertEqual(app_updates.check_update()['status'], 'unavailable')
            fetch.assert_called_once_with(releases.DEFAULT_INDEX_URL)
        target = releases.Release('v1.2.0', 's', 'a' * 64)
        with patch.object(upgrade, 'discover_release', return_value=target), \
             patch.object(app_updates, 'version_info', return_value={'current_version': 'v1.0.0'}):
            self.assertTrue(app_updates.check_update()['update_available'])

    def test_git_metadata_failure_does_not_use_stale_package_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.git').mkdir()
            (root / 'VERSION').write_text('v0.1.0')
            with patch.object(version.config, 'program_root', return_value=root), \
                 patch.object(version, '_git', side_effect=OSError('git missing')):
                result = app_updates.version_info()
                self.assertIsNone(result['current_version'])
                self.assertIn('读取失败', result['version_label'])


class GitVersionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        self.git('init', '-q')
        self.git('config', 'user.name', 'Version Test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.root / 'file.txt').write_text('first')
        self.git('add', '.')
        self.git('-c', 'commit.gpgsign=false', 'commit', '-qm', 'first')
        self.addCleanup(patch.stopall)
        patch.object(version.config, 'program_root', return_value=self.root).start()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], env=self.env,
                                       text=True, encoding='utf-8', timeout=10).strip()

    def test_no_tags_then_exact_tag_then_dirty(self):
        result = app_updates.version_info()
        self.assertEqual(result['version_label'], '开发版 · ' + self.git('rev-parse', '--short=12', 'HEAD'))
        with patch.object(upgrade, 'discover_release', return_value=releases.Release('v1.0.0', '', '')):
            self.assertEqual(app_updates.check_update()['status'], 'development')
        self.git('tag', 'v1.2.3')
        self.assertEqual(app_updates.version_info()['version_label'], 'v1.2.3')
        (self.root / 'file.txt').write_text('dirty')
        self.assertTrue(app_updates.version_info()['dirty'])
        self.assertIn('本地有修改', app_updates.version_info()['version_label'])

    def test_ahead_prerelease_detached_and_worktree(self):
        self.git('-c', 'tag.gpgsign=false', 'tag', '-a', 'v1.2.3', '-m', 'release')
        self.git('-c', 'commit.gpgsign=false', 'commit', '--allow-empty', '-qm', 'next')
        self.git('tag', 'v2.0.0-rc1')
        self.git('tag', 'v999-bad')
        self.git('checkout', '--detach', '-q')
        result = app_updates.version_info()
        self.assertEqual(result['current_version'], 'v1.2.3')
        self.assertEqual(result['commits_since_tag'], 1)
        self.assertIn('v1.2.3 +1', result['version_label'])
        linked = self.root / 'linked'
        self.git('worktree', 'add', '--detach', str(linked), 'v1.2.3')
        with patch.object(version.config, 'program_root', return_value=linked):
            self.assertEqual(app_updates.version_info()['version_label'], 'v1.2.3')

    def test_plain_directory_under_repository_does_not_inherit_parent_version(self):
        self.git('tag', 'v9.0.0')
        copy = self.root / 'copy'
        copy.mkdir()
        (copy / 'VERSION').write_text('v1.0.0')
        with patch.object(version.config, 'program_root', return_value=copy):
            result = app_updates.version_info()
            self.assertEqual(result['version_source'], 'release')
            self.assertEqual(result['current_version'], 'v1.0.0')

    def test_http_administrator_boundary_and_no_store(self):
        with UserNameAPI() as fixture, patch.object(app_updates, 'check_update', return_value={'status': 'ok'}) as check:
            for path in ('/system/version', '/system/updates'):
                response = fixture.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['cache-control'], 'no-store')
                denied = fixture.client.get(path, headers={'X-Group-Profile': 'bot-a', 'X-Group-Key': auth.bot_token('bot-a')})
                self.assertEqual(denied.status_code, 403)
                with patch.object(auth, '_password', return_value='test-password'):
                    self.assertEqual(fixture.client.get(path).status_code, 401)
            check.assert_called_once()


if __name__ == '__main__':
    unittest.main()
