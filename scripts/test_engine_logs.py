"""Isolated log tail, rotation and administrator API regression checks."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import engine_logs, auth
from scripts.test_user_display_name import UserNameAPI


class EngineLogTests(unittest.TestCase):
    def test_tail_missing_unicode_limits_and_rotation(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
                engine_logs.config, 'logs_dir', return_value=Path(directory)):
            path = Path(directory) / 'engine.log'
            self.assertFalse(engine_logs.snapshot()['available'])
            path.write_bytes(b'')
            self.assertEqual(engine_logs.snapshot()['lines'], 0)
            path.write_text('\n'.join(f'{i} 中文🌟' for i in range(1100)), encoding='utf-8')
            result = engine_logs.snapshot()
            self.assertEqual(result['lines'], 1000)
            self.assertTrue(result['truncated'])
            self.assertTrue(result['text'].startswith('100 中文🌟'))
            path.write_bytes(('中文\n' * 60000).encode() + b'last\xff')
            result = engine_logs.snapshot()
            self.assertLessEqual(len(result['text'].encode()), engine_logs.MAX_BYTES + 3)
            self.assertTrue(result['text'].endswith('last�'))
            path.rename(path.with_suffix('.log.1'))
            path.write_text('new engine', encoding='utf-8')
            self.assertEqual(engine_logs.snapshot()['text'], 'new engine')

    def test_http_auth_cache_and_read_errors(self):
        with UserNameAPI() as fixture, patch.object(engine_logs, 'snapshot', return_value={
                'text': 'sample', 'lines': 1, 'available': True, 'truncated': False}) as read:
            response = fixture.client.get('/system/logs')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['text'], 'sample')
            self.assertEqual(response.headers['cache-control'], 'no-store')
            read.reset_mock()
            response = fixture.client.get('/system/logs', headers={
                'X-Group-Profile': 'bot-a', 'X-Group-Key': auth.bot_token('bot-a')})
            self.assertEqual(response.status_code, 403)
            with patch.object(auth, '_password', return_value='configured'):
                self.assertEqual(fixture.client.get('/system/logs').status_code, 401)
                with patch.object(auth, 'verify_token', return_value=True):
                    self.assertEqual(fixture.client.get('/system/logs', headers={
                        'Authorization': 'Bearer test'}).status_code, 200)
            read.assert_called_once()
            read.side_effect = PermissionError('private path')
            response = fixture.client.get('/system/logs')
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('private path', response.text)


if __name__ == '__main__':
    unittest.main()
