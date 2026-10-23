"""Read-only version discovery, release selection and HTTP authorization."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import app_updates, auth
from scripts import releases, upgrade
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
                self.assertEqual(unknown['version_label'], '开发版 / 版本未知')
                self.assertFalse(unknown['update_available'])
                for version, available in [('v1.1.0', True), ('1.2.0', False), ('v2.0.0', False), ('invalid', False)]:
                    (root / 'VERSION').write_text(version)
                    result = app_updates.check_update()
                    self.assertEqual(result['current_version'], version)
                    self.assertEqual(result['latest_version'], 'v1.2.0')
                    self.assertEqual(result['update_available'], available)

    def test_failure_then_retry_and_default_source(self):
        with patch.object(upgrade.config, '_pick', return_value=releases.DEFAULT_INDEX_URL), \
             patch.object(releases, 'fetch_index', side_effect=OSError('offline')) as fetch:
            self.assertEqual(app_updates.check_update()['status'], 'unavailable')
            fetch.assert_called_once_with(releases.DEFAULT_INDEX_URL)
        target = releases.Release('v1.2.0', 's', 'a' * 64)
        with patch.object(upgrade, 'discover_release', return_value=target), \
             patch.object(upgrade, 'current_version', return_value='v1.0.0'):
            self.assertTrue(app_updates.check_update()['update_available'])

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
