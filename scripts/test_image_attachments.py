"""Image upload, atomic binding, project scope, and durable reads on isolated data."""
import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from engine import api, auth, attachments, db


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'GROUP_DB': str(Path(self.tmp.name) / 'group.db'),
                                          'GROUP_ATTACHMENTS_DIR': str(Path(self.tmp.name) / 'attachments'),
                                          'GROUP_ADMIN_PASSWORD': ''})
        self.env.start()
        conn = db.get_connection()
        db.init_schema(conn)
        conn.executemany("INSERT INTO projects(name,root,created_at) VALUES(?, '', 'now')", [('one',), ('two',)])
        conn.executemany("INSERT INTO members(profile,display_name) VALUES(?,?)", [('alice','Alice'), ('bob','Bob')])
        conn.execute("INSERT INTO threads(id,project,title,creator,members,created_at,last_active_at) VALUES(1,'one','Images','user','[\"alice\"]','now','now')")
        from scripts.project_test_support import enable_members
        enable_members(conn, ('one', 'two'))
        conn.commit()
        conn.close()
        app = FastAPI()
        app.include_router(api.router_api)
        self.client = TestClient(app, client=('127.0.0.1', 4321))
        self.wake = patch.object(api.router.wake, 'enqueue', return_value=False)
        self.wake.start()
        buffer = io.BytesIO()
        Image.new('RGB', (32, 24), 'red').save(buffer, format='PNG')
        self.png = buffer.getvalue()

    def tearDown(self):
        self.client.close()
        self.wake.stop()
        self.env.stop()
        self.tmp.cleanup()

    def upload(self, project='one'):
        response = self.client.post('/attachments', params={'project': project, 'filename': '../test.png'}, content=self.png)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def send(self, aid, **kwargs):
        return self.client.post('/msg', json={'sender': 'user', 'text': '', 'thread_id': 1,
                                              'attachment_ids': [aid], **kwargs})

    def test_topic_image_only_persists_and_is_discoverable(self):
        item = self.upload()
        self.assertEqual(item['filename'], 'test.png')
        self.assertEqual(item['sha256'], hashlib.sha256(self.png).hexdigest())
        self.assertEqual(self.send(item['id']).status_code, 200)
        conn = db.get_connection()
        conn.execute(
            "INSERT INTO wake_jobs(profile,project,kind,trigger,thread_id,status,enqueued_at,chat_context_until) "
            "VALUES('alice','one','chat','scan:group',1,'done','now','2099-01-01T00:00:00')")
        conn.commit()
        conn.close()
        for url in ['/messages?project=one&limit=100', '/thread?id=1', '/inbox?profile=alice&kind=chat&thread_id=1']:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, response.text)
            messages = response.json()['messages']
            self.assertEqual(messages[0]['attachments'][0]['id'], item['id'])
        self.assertEqual(self.client.get('/messages?project=two&limit=100').json()['messages'], [])
        path = next(attachments.root().iterdir())
        os.utime(path, (1, 1))
        response = self.client.get('/attachments/' + item['id'], headers={'X-Group-Profile': 'alice', 'X-Group-Key': auth.bot_token('alice')})
        self.assertEqual(response.content, self.png)
        self.assertEqual(self.client.get('/attachments/' + item['id'], headers={'X-Group-Profile': 'bob', 'X-Group-Key': auth.bot_token('bob')}).status_code, 403)
        path.unlink()
        self.assertEqual(self.client.get('/attachments/' + item['id']).status_code, 410)

    def test_wrong_project_rolls_back_message_and_retry_can_bind(self):
        item = self.upload('two')
        self.assertEqual(self.send(item['id']).status_code, 400)
        self.assertEqual(self.client.get('/messages').json()['messages'], [])
        sent = self.send(item['id'], text='@alice inspect', thread_id=None, project='two')
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertEqual(self.send(item['id']).status_code, 400)
        self.assertEqual(self.client.get('/messages').json()['count'], 1)

    def test_attachment_access_tracks_current_project_membership(self):
        item = self.upload()
        self.assertEqual(self.send(item['id']).status_code, 200)
        headers = {'X-Group-Profile': 'alice', 'X-Group-Key': auth.bot_token('alice')}
        url = '/attachments/' + item['id']
        self.assertEqual(self.client.get(url, headers=headers).content, self.png)
        for change in (
            "UPDATE project_members SET active=0 WHERE project='one' AND profile='alice'",
            "UPDATE projects SET archived=1 WHERE name='one'",
            "UPDATE projects SET removed=1 WHERE name='one'",
        ):
            with self.subTest(change=change):
                conn = db.get_connection()
                conn.execute("UPDATE project_members SET active=1 WHERE project='one'")
                conn.execute("UPDATE projects SET archived=0,removed=0 WHERE name='one'")
                conn.execute(change)
                conn.commit()
                conn.close()
                self.assertEqual(self.client.get(url, headers=headers).status_code, 403)
                self.assertEqual(self.client.get(url).content, self.png)

    def test_local_admin_image_topic_and_idempotent_retry(self):
        item = self.upload()
        body = {'project': 'one', 'title': 'Image topic', 'members': ['alice'],
                'attachment_ids': [item['id']], 'request_id': 'local-image-topic'}
        response = self.client.post('/thread', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        retry = self.client.post('/thread', json=body)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(response.json()['thread_id'], retry.json()['thread_id'])
        conn = db.get_connection()
        row = conn.execute("SELECT owner,message_id FROM attachments WHERE id=?", (item['id'],)).fetchone()
        self.assertEqual(row['owner'], 'user')
        self.assertIsNotNone(row['message_id'])
        self.assertEqual(conn.execute("SELECT creator FROM threads WHERE id=?",
                                     (response.json()['thread_id'],)).fetchone()[0], 'user')
        conn.close()

    def test_auth_invalid_format_size_duplicates_and_closed_topic(self):
        with patch.object(api.auth, '_password', return_value='configured'):
            self.assertEqual(self.client.post('/attachments?project=one', content=self.png).status_code, 401)
            self.assertEqual(self.client.get('/attachments/anything').status_code, 401)
        for data, status in [(b'<svg/>', 400), (b'x' * (attachments.MAX_BYTES + 1), 413)]:
            self.assertEqual(self.client.post('/attachments?project=one', content=data).status_code, status)
        item = self.upload()
        self.assertEqual(self.send(item['id'], attachment_ids=[item['id'], item['id']]).status_code, 400)
        conn = db.get_connection()
        conn.execute("UPDATE threads SET status='closed' WHERE id=1")
        conn.commit()
        conn.close()
        self.assertEqual(self.send(item['id']).status_code, 400)
        self.assertEqual(self.client.get('/messages').json()['count'], 0)

    def test_multiple_images_keep_selected_order_and_partial_binding_rolls_back(self):
        first, second = self.upload(), self.upload()
        response = self.send(first['id'], attachment_ids=[second['id'], 'missing'])
        self.assertEqual(response.status_code, 400)
        response = self.send(first['id'], attachment_ids=[second['id'], first['id']])
        self.assertEqual(response.status_code, 200, response.text)
        items = self.client.get('/thread?id=1').json()['messages'][0]['attachments']
        self.assertEqual([item['id'] for item in items], [second['id'], first['id']])


if __name__ == '__main__':
    unittest.main()
