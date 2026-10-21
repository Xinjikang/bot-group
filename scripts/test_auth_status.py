"""req#122 task#212 鉴权模式探测接口测试（验收 6）。

覆盖：auth.configured() 未设/已设密码返回正确；GET /auth/status 端点返回
{"configured": bool} 且不泄露密码本身。全程 patch 环境变量，不触碰真实配置。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import auth


class AuthStatusTest(unittest.TestCase):
    def setUp(self):
        self._orig = os.environ.get('GROUP_ADMIN_PASSWORD')

    def tearDown(self):
        if self._orig is None:
            os.environ.pop('GROUP_ADMIN_PASSWORD', None)
        else:
            os.environ['GROUP_ADMIN_PASSWORD'] = self._orig

    def test_configured_false_when_password_unset(self):
        with patch.dict(os.environ, {'GROUP_ADMIN_PASSWORD': ''}):
            self.assertFalse(auth.configured())

    def test_configured_true_when_password_set(self):
        with patch.dict(os.environ, {'GROUP_ADMIN_PASSWORD': 'secret'}):
            self.assertTrue(auth.configured())

    def test_auth_status_endpoint_reports_configuration_only(self):
        from fastapi.testclient import TestClient
        from engine.main import app
        client = TestClient(app)
        with patch.dict(os.environ, {'GROUP_ADMIN_PASSWORD': ''}):
            r = client.get('/auth/status')
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {'configured': False})
        with patch.dict(os.environ, {'GROUP_ADMIN_PASSWORD': 'secret'}):
            r = client.get('/auth/status')
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json(), {'configured': True})
            # 探测接口绝不回传密码明文
            self.assertNotIn('secret', r.text)


if __name__ == '__main__':
    unittest.main()
