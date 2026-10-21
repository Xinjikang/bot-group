"""Sandbox hints read isolated member settings without changing configuration."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine import codex_sandbox_notice as notices


class CodexSandboxNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.candidate = {'host_type': 'codex', 'profile': 'default', 'config_root': str(self.home)}
        platform = patch.object(notices.sys, 'platform', 'win32')
        platform.start()
        self.addCleanup(platform.stop)

    def write(self, content, name='config.toml'):
        path = self.home / name
        path.write_text(content, encoding='utf-8')
        return path

    def notice(self):
        before = {p: p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        result = notices.for_candidate(self.candidate)
        self.assertEqual(before, {p: p.read_bytes() for p in self.home.rglob('*') if p.is_file()})
        return result

    def test_missing_selection_is_uncertain_and_permits_joining(self):
        self.write('sandbox_mode = "workspace-write"\napproval_policy = "never"\n')
        result = self.notice()
        self.assertEqual(result['code'], 'unspecified')
        self.assertIn('默认设置', result['message'])
        self.assertIn('继续加入', result['message'])

    def test_known_implementations_and_feature_selection(self):
        for implementation in ('elevated', 'unelevated', 'mxc'):
            with self.subTest(implementation=implementation):
                self.write(f'[windows]\nsandbox = "{implementation}"\n')
                self.assertIsNone(self.notice())
        for feature in ('prefer_mxc', 'experimental_windows_sandbox', 'elevated_windows_sandbox'):
            with self.subTest(feature=feature):
                self.write(f'[features]\n{feature} = true\n')
                self.assertIsNone(self.notice())

    def test_named_configuration_inherits_and_overrides(self):
        self.candidate['profile'] = 'worker'
        self.write('[windows]\nsandbox = "elevated"\n[profiles.worker.windows]\nsandbox = "unknown"\n')
        self.assertEqual(self.notice()['code'], 'unrecognized')
        self.write('model = "example"\n', 'worker.config.toml')
        self.assertIsNone(self.notice())
        self.write('[windows]\nsandbox = "unknown"\n', 'worker.config.toml')
        self.assertEqual(self.notice()['code'], 'unrecognized')

    def test_default_selector_is_resolved(self):
        self.write('profile = "worker"\n[profiles.worker.windows]\nsandbox = "elevated"\n')
        self.assertIsNone(self.notice())

    def test_independent_home_does_not_inherit_primary_home(self):
        self.write('[windows]\nsandbox = "elevated"\n')
        child = self.home / 'profiles' / 'worker'
        child.mkdir(parents=True)
        (child / 'config.toml').write_text('', encoding='utf-8')
        self.candidate.update(profile='worker', config_root=str(child))
        self.assertEqual(self.notice()['code'], 'unspecified')

    def test_full_access_is_reported_even_with_implementation(self):
        for setting in ('sandbox_mode = "danger-full-access"', 'default_permissions = ":danger-full-access"'):
            with self.subTest(setting=setting):
                self.write(setting + '\n[windows]\nsandbox = "elevated"\n')
                self.assertEqual(self.notice()['code'], 'disabled')

    def test_invalid_or_unreadable_configuration_returns_safe_hint(self):
        for content in ('[broken', 'windows = false', 'features = false', 'profile = "../private"'):
            with self.subTest(content=content):
                self.write(content)
                self.assertEqual(self.notice()['code'], 'unreadable')
        with patch.object(notices, '_read', side_effect=PermissionError('secret path')):
            result = notices.for_candidate(self.candidate)
            self.assertEqual(result['code'], 'unreadable')
            self.assertNotIn('secret path', result['message'])

    def test_missing_file_bom_and_size_limit(self):
        self.assertEqual(self.notice()['code'], 'unspecified')
        self.write('\ufeff[windows]\nsandbox = "elevated"\n')
        self.assertIsNone(self.notice())
        self.write('#' * (2 * 1024 * 1024 + 1))
        self.assertEqual(self.notice()['code'], 'unreadable')

    def test_other_platforms_and_hermes_skip_config_reads(self):
        with patch.object(notices, '_read', side_effect=AssertionError('unexpected read')):
            self.candidate['host_type'] = 'hermes'
            self.assertIsNone(self.notice())
            self.candidate['host_type'] = 'codex'
            with patch.object(notices.sys, 'platform', 'linux'):
                self.assertIsNone(self.notice())


if __name__ == '__main__':
    unittest.main()
