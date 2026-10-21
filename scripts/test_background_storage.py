"""Real background API, validation, authorization and atomic replacement tests."""
import io
import unittest
from unittest.mock import patch
from PIL import Image
from engine import api, auth, appearance
from scripts.background_test_support import BackgroundAPI


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.fixture = BackgroundAPI()
        self.fixture.__enter__()
        self.addCleanup(self.fixture.__exit__, None, None, None)
        self.client = self.fixture.client
        self.url = '/appearance/background'
        stream = io.BytesIO()
        Image.new('RGB', (64, 64), '#778899').save(stream, 'JPEG')
        self.photo = stream.getvalue()

    def test_save_read_replace_reset_and_stale_migration(self):
        self.assertEqual(self.client.get(self.url).headers['x-background-configured'], 'false')
        self.assertEqual(self.client.put(self.url, content=self.photo).status_code, 200)
        reply = self.client.get(self.url)
        self.assertEqual(reply.content, self.photo)
        self.assertEqual(reply.headers['content-type'], 'image/jpeg')
        self.assertEqual(reply.headers['cache-control'], 'private, no-store')
        self.assertEqual(self.client.put(self.url, content=self.photo).status_code, 200)
        self.assertEqual(len(list(appearance.path().parent.iterdir())), 1)
        self.assertEqual(self.client.delete(self.url).status_code, 200)
        self.assertEqual(appearance.path().stat().st_size, 0)
        self.assertEqual(self.client.get(self.url).status_code, 204)
        self.assertEqual(self.client.get(self.url).headers['x-background-configured'], 'true')
        self.assertEqual(self.client.put(self.url, content=self.photo, headers={'If-None-Match': '*'}).status_code, 412)

    def test_invalid_oversized_and_io_failure_preserve_previous(self):
        self.client.put(self.url, content=self.photo)
        for content, expected in [(b'', 400), (b'broken', 400), (b'x' * (appearance.MAX_BYTES + 1), 413)]:
            self.assertEqual(self.client.put(self.url, content=content).status_code, expected)
            self.assertEqual(self.client.get(self.url).content, self.photo)
        with patch.object(appearance.os, 'replace', side_effect=OSError('disk full')):
            self.assertEqual(self.client.put(self.url, content=self.photo).status_code, 507)
            self.assertEqual(self.client.delete(self.url).status_code, 507)
        self.assertEqual(self.client.get(self.url).content, self.photo)
        self.assertEqual(len(list(appearance.path().parent.iterdir())), 1)

    def test_identity_and_origin(self):
        for method in ('get', 'put', 'delete'):
            self.assertEqual(self.client.request(method, self.url, headers={'X-Group-Profile': 'bot', 'X-Group-Key': auth.bot_token('bot')}).status_code, 403)
        self.assertEqual(self.client.put(self.url, content=self.photo, headers={'Origin': 'https://other.test'}).status_code, 403)
        with patch.object(api.auth, '_password', return_value='configured'), patch.object(api.auth, 'verify_token', return_value=True):
            self.assertEqual(self.client.get(self.url).status_code, 401)
            self.assertEqual(self.client.put(self.url, content=self.photo, headers={'Authorization': 'Bearer test'}).status_code, 200)
            self.assertEqual(self.client.get(self.url, headers={'Authorization': 'Bearer test'}).content, self.photo)


if __name__ == '__main__':
    unittest.main()
