"""Isolated real API bridge for browser background tests; never contacts production."""
import tempfile
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from engine import api, appearance


class BackgroundAPI:
    def __enter__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = patch.object(appearance.config, 'data_dir', return_value=Path(self.temp.name))
        self.password = patch.object(api.auth, '_password', return_value='')
        self.directory.start()
        self.password.start()
        app = FastAPI()
        app.include_router(api.router_api)
        self.client = TestClient(app, client=('127.0.0.1', 4321))
        self.fail = False
        return self

    def route(self, route):
        request = route.request
        if self.fail and request.method in ('PUT', 'DELETE'):
            route.fulfill(status=507, json={'detail': '服务端图片保存失败'})
            return
        response = self.client.request(request.method, '/appearance/background',
            content=request.post_data_buffer, headers={key: value for key, value in request.headers.items()
                if key in ('authorization', 'if-none-match')})
        route.fulfill(status=response.status_code, body=response.content, headers=dict(response.headers))

    def __exit__(self, *args):
        self.client.close()
        self.password.stop()
        self.directory.stop()
        self.temp.cleanup()
