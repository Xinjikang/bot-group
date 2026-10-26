"""UI presence arbitration and administrator access boundaries."""
import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from engine import auth
from engine.ui_presence import Presence, peer_key, router


class PresenceTest(unittest.TestCase):
    def test_latest_page_and_disconnect(self):
        state = Presence()
        old, old_queue = state.connect('loopback')
        new, new_queue = state.connect('loopback')
        for _ in range(100):
            self.assertEqual(state.activate('loopback'), {'action': 'reuse'})
        self.assertEqual(new_queue.qsize(), 1)
        self.assertTrue(old_queue.empty())
        state.disconnect(new)
        state.activate('loopback')
        self.assertEqual(old_queue.get_nowait(), 'activate')
        state.disconnect(old)
        self.assertEqual(state.activate('loopback')['action'], 'open')

    def test_reservation_expiry_release_and_connection(self):
        now = [0]
        state = Presence(clock=lambda: now[0])
        first = state.activate('loopback')
        state.release('remote', first['reservation'])
        state.release('loopback', 'wrong')
        self.assertEqual(state.activate('loopback')['action'], 'pending')
        state.release('loopback', first['reservation'])
        self.assertEqual(state.activate('loopback')['action'], 'open')
        now[0] = 11
        self.assertEqual(state.activate('loopback')['action'], 'open')
        key, _ = state.connect('loopback')
        state.disconnect(key)
        self.assertEqual(state.activate('loopback')['action'], 'open')

    def test_remote_page_and_loopback_aliases(self):
        state = Presence()
        state.connect('192.0.2.1')
        self.assertEqual(state.activate('loopback')['action'], 'open')
        for host in ('127.0.0.1', '::1', '::ffff:127.0.0.1'):
            self.assertEqual(peer_key(host), 'loopback')
        self.assertEqual(peer_key('192.0.2.1'), '192.0.2.1')


class PresenceAccessTest(unittest.TestCase):
    def test_access_and_release(self):
        app = FastAPI()
        app.include_router(router)
        with patch.dict(os.environ, {'GROUP_ADMIN_PASSWORD': 'test', 'GROUP_SECRET': 'test'}), TestClient(app) as client:
            for path, method in (('/admin/ui/activate', client.post), ('/admin/ui/open/token', client.delete)):
                self.assertEqual(method(path).status_code, 401)
                self.assertEqual(method(path, headers={'X-Group-Profile': 'bot-test',
                    'X-Group-Key': auth.bot_token('bot-test')}).status_code, 403)
            headers = {'Authorization': 'Bearer ' + auth.login('test')['token']}
            first = client.post('/admin/ui/activate', headers=headers).json()
            self.assertEqual(first['action'], 'open')
            self.assertEqual(client.post('/admin/ui/activate', headers=headers).json()['action'], 'pending')
            self.assertEqual(client.delete('/admin/ui/open/' + first['reservation'], headers=headers).status_code, 200)
            self.assertEqual(client.post('/admin/ui/activate', headers=headers).json()['action'], 'open')
            for site in ('cross-site', 'same-site', 'none', ''):
                self.assertEqual(client.get('/desktop/events', headers={'Sec-Fetch-Site': site}).status_code, 403)


if __name__ == '__main__':
    unittest.main()
