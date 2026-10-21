"""Isolated picker API/native-process tests; never opens a desktop dialog."""
import subprocess
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from engine import api, directory_picker


class PickerTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(api.router_api)
        app.dependency_overrides[api._resolve_identity] = lambda: ('user', 'user')
        self.app = app
        self.client = TestClient(app, client=('127.0.0.1', 50000))

    def test_selected_and_cancelled(self):
        for selected in ('E:\\项目\\test', None):
            with patch.object(directory_picker, 'choose_directory', return_value=selected) as picker:
                result = self.client.post('/local/directory-picker', json={'initial': 'E:\\existing'})
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json(), {'path': selected})
                picker.assert_called_once_with('E:\\existing')

    def test_remote_bot_and_cross_origin_rejected(self):
        with patch.object(directory_picker, 'choose_directory') as picker:
            remote = TestClient(self.app, client=('192.0.2.1', 50000))
            self.assertEqual(remote.post('/local/directory-picker', json={}).status_code, 403)
            self.assertEqual(self.client.post('/local/directory-picker', json={},
                headers={'Origin': 'https://example.com'}).status_code, 403)
            self.app.dependency_overrides[api._resolve_identity] = lambda: ('bot', 'bot')
            self.assertEqual(self.client.post('/local/directory-picker', json={}).status_code, 403)
            picker.assert_not_called()

    @unittest.skipUnless(directory_picker.os.name == 'nt', 'Windows process boundary')
    def test_native_path_is_data_and_lock_releases_after_timeout(self):
        initial = 'E:\\中文\\$example;test'
        with patch.object(directory_picker.subprocess, 'run',
                          return_value=subprocess.CompletedProcess([], 0, 'E:\\中文', '')) as run:
            self.assertEqual(directory_picker.choose_directory(initial), 'E:\\中文')
            self.assertEqual(run.call_args.kwargs['env']['GROUP_PICKER_INITIAL'], initial)
            self.assertNotIn(initial, run.call_args.args[0])
        with patch.object(directory_picker.subprocess, 'run',
                          side_effect=subprocess.TimeoutExpired('powershell', 180)):
            with self.assertRaises(HTTPException) as error:
                directory_picker.choose_directory('')
            self.assertEqual(error.exception.status_code, 408)
        self.assertFalse(directory_picker._lock.locked())


if __name__ == '__main__':
    unittest.main()
