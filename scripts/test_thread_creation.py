"""Isolated HTTP/SQLite regressions for topic creation retries."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from engine import api
from scripts import test_project_import as imports


class ThreadCreationTests(unittest.TestCase):
    setUp = imports.ImportTests.setUp

    def prepare(self):
        project = self.client.post('/projects/import', json=self.body).json()['project']
        profile = self.conn.execute('SELECT profile FROM project_members WHERE project=? LIMIT 1', (project,)).fetchone()[0]
        self.topic = dict(project=project, title='Topic', opening='Hello', members=[profile], request_id='attempt-1')
        identity = patch.object(api, '_resolve_identity', return_value=('user', 'user'))
        identity.start()
        self.addCleanup(identity.stop)

    def test_concurrent_retry_creates_one_topic_and_delivery(self):
        self.prepare()
        with patch.object(api.events.bus, 'publish') as publish:
            with ThreadPoolExecutor(max_workers=2) as pool:
                replies = list(pool.map(lambda _: self.client.post('/thread', json=self.topic), range(2)))
        self.assertEqual([r.status_code for r in replies], [200, 200])
        self.assertEqual(replies[0].json()['thread_id'], replies[1].json()['thread_id'])
        self.assertEqual(publish.call_count, 1)
        for table in ('threads', 'inbox', 'wake_jobs', 'thread_create_requests'):
            self.assertEqual(self.conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 1, table)
        tid = replies[0].json()['thread_id']
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM messages WHERE thread_id=?', (tid,)).fetchone()[0], 1)

    def test_changed_payload_conflicts_and_fresh_attempt_is_allowed(self):
        self.prepare()
        first = self.client.post('/thread', json=self.topic).json()
        self.assertEqual(self.client.post('/thread', json={**self.topic, 'opening': 'Changed'}).status_code, 409)
        second = self.client.post('/thread', json={**self.topic, 'request_id': 'attempt-2'}).json()
        self.assertNotEqual(first['thread_id'], second['thread_id'])
        legacy = {k: v for k, v in self.topic.items() if k != 'request_id'}
        self.assertEqual(self.client.post('/thread', json=legacy).status_code, 200)

    def test_opening_images_atomic_binding_and_retry(self):
        import io
        import os
        from PIL import Image
        self.prepare()
        with patch.dict(os.environ, {'GROUP_ATTACHMENTS_DIR': str(self.base / 'images')}):
            data = io.BytesIO()
            Image.new('RGB', (8, 8), 'red').save(data, format='PNG')
            uploaded = self.client.post('/attachments', params={'project': self.topic['project'], 'filename': 'opening.png'}, content=data.getvalue())
            self.assertEqual(uploaded.status_code, 200, uploaded.text)
            aid = uploaded.json()['id']
            invalid = {**self.topic, 'attachment_ids': [aid, 'missing']}
            self.assertGreaterEqual(self.client.post('/thread', json=invalid).status_code, 400)
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM threads').fetchone()[0], 0)
            self.assertIsNone(self.conn.execute('SELECT message_id FROM attachments WHERE id=?', (aid,)).fetchone()[0])
            body = {**self.topic, 'opening': '', 'attachment_ids': [aid]}
            result = self.client.post('/thread', json=body)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertTrue(self.client.post('/thread', json=body).json()['replayed'])
            self.assertEqual(self.client.post('/thread', json={**self.topic, 'attachment_ids': []}).status_code, 409)
            messages = self.client.get('/thread', params={'id': result.json()['thread_id']}).json()['messages']
            self.assertEqual(messages[0]['attachments'][0]['id'], aid)
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM threads').fetchone()[0], 1)

    def test_validation_failure_can_retry_and_invalid_keys_rejected(self):
        self.prepare()
        for key in ('', ' ' * 2, 12, 'a' * 129):
            self.assertEqual(self.client.post('/thread', json={**self.topic, 'request_id': key}).status_code, 400)
        with patch.object(api.router, 'validate_mention_scope', side_effect=api.router.RouteError('invalid', 400)):
            self.assertEqual(self.client.post('/thread', json=self.topic).status_code, 400)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM thread_create_requests').fetchone()[0], 0)
        self.assertEqual(self.client.post('/thread', json=self.topic).status_code, 200)


if __name__ == '__main__':
    unittest.main()
