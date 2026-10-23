"""Release comparison and failure states use isolated files and a fake index."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine import update_check


class UpdateCheckTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.patch = patch.object(update_check.config, 'program_root', return_value=self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        update_check._cached = None

    def entry(self, version, **extra):
        return {'version': version, 'source': 'https://example.test/source.zip', 'sha256': 'a' * 64,
                'manifest': 'https://example.test/manifest.json', 'manifest_sha256': 'b' * 64, **extra}

    def test_numeric_formal_release_and_coalescing(self):
        (self.root / 'VERSION').write_text('v1.9.0')
        with patch.object(update_check.releases, 'fetch_index', return_value=[
            self.entry('v1.10.0'), self.entry('v2.0.0', prerelease=True), self.entry('v3.0.0', draft=True)
        ]) as fetch:
            result = update_check.check()
            self.assertTrue(result['update_available'])
            self.assertEqual(result['latest_version'], 'v1.10.0')
            self.assertEqual(update_check.check(), result)
            self.assertEqual(fetch.call_count, 1)
            (self.root / 'VERSION').write_text('v1.10.0')
            self.assertFalse(update_check.check()['update_available'])
            (self.root / 'VERSION').write_text('v1.11.0')
            self.assertFalse(update_check.check()['update_available'])

    def test_unknown_and_failed_lookup_are_explicit(self):
        with patch.object(update_check.releases, 'fetch_index', return_value=[self.entry('v1.0.0')]):
            result = update_check.check()
            self.assertEqual(result['status'], 'unknown_version')
            self.assertFalse(result['update_available'])
        update_check._cached = None
        with patch.object(update_check.releases, 'fetch_index', side_effect=RuntimeError('private-token')):
            result = update_check.check()
            self.assertEqual(result['status'], 'unavailable')
            self.assertNotIn('private-token', str(result))
        with patch.object(update_check.releases, 'fetch_index', return_value=[self.entry('v1.0.0')]) as fetch:
            self.assertEqual(update_check.check()['status'], 'unknown_version')
            self.assertEqual(fetch.call_count, 1)


if __name__ == '__main__':
    unittest.main()
