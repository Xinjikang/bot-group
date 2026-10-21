"""req#110 task#188：列目录接口（网页内目录选择器后端）。

覆盖：①返回当前路径+子目录列表（含可导航性）；②无权限/空/大目录分页正常处理；
③只返回目录不暴露文件；④鉴权（管理员/回环/同源）；根视图（盘符）。

运行：python -B -m unittest scripts.test_directory_list -q
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from engine import api, directory_list
from engine.directory_list import list_directory


class ListDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # 目录结构：sub_a/、sub_b/、中文 目录/、空目录 empty/，加一个文件 file.txt
        (self.root / "sub_a").mkdir()
        (self.root / "sub_b").mkdir()
        (self.root / "中文 目录").mkdir()
        (self.root / "empty").mkdir()
        (self.root / "file.txt").write_text("x", encoding="utf-8")
        (self.root / "sub_a" / "nested").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_returns_path_and_subdirs_with_navigable(self):
        out = list_directory(str(self.root))
        self.assertIsNone(out.get("error"))
        self.assertEqual(out["path"], str(self.root))
        self.assertEqual(out["parent"], str(Path(self.root).parent))
        names = {e["name"] for e in out["entries"]}
        # 只列目录，不列文件 file.txt
        self.assertEqual(names, {"sub_a", "sub_b", "中文 目录", "empty"})
        self.assertEqual(out["total"], 4)
        self.assertFalse(out["has_more"])
        for e in out["entries"]:
            self.assertTrue(e["navigable"])
            self.assertTrue(e["path"].startswith(str(self.root)))

    def test_empty_directory(self):
        out = list_directory(str(self.root / "empty"))
        self.assertEqual(out["entries"], [])
        self.assertEqual(out["total"], 0)
        self.assertFalse(out["has_more"])

    def test_not_found_and_not_directory(self):
        self.assertEqual(list_directory(str(self.root / "ghost"))["error"], "not_found")
        self.assertEqual(list_directory(str(self.root / "file.txt"))["error"], "not_directory")

    def test_permission_denied_returns_error_not_raise(self):
        with patch("engine.directory_list.os.scandir", side_effect=PermissionError):
            out = list_directory(str(self.root))
        self.assertEqual(out["error"], "permission_denied")
        self.assertEqual(out["entries"], [])

    def test_large_directory_pagination(self):
        big = self.root / "big"
        big.mkdir()
        for i in range(25):
            (big / f"d{i:02d}").mkdir()
        out = list_directory(str(big), offset=0, limit=10)
        self.assertEqual(out["total"], 25)
        self.assertEqual(len(out["entries"]), 10)
        self.assertTrue(out["has_more"])
        self.assertEqual(out["entries"][0]["name"], "d00")
        # 第二页
        out2 = list_directory(str(big), offset=10, limit=10)
        self.assertEqual(out2["entries"][0]["name"], "d10")
        # 跨过结尾
        out3 = list_directory(str(big), offset=20, limit=10)
        self.assertEqual(len(out3["entries"]), 5)
        self.assertFalse(out3["has_more"])

    def test_limit_clamped(self):
        big = self.root / "big2"
        big.mkdir()
        for i in range(5):
            (big / f"d{i}").mkdir()
        out = list_directory(str(big), limit=99999)
        self.assertEqual(out["limit"], directory_list.MAX_LIMIT)

    def test_root_returns_drives_on_windows(self):
        out = list_directory("")
        self.assertEqual(out["path"], "")
        if os.name == "nt":
            # Windows 盘符视图：entries 是 C:\\ 等盘符
            self.assertTrue(all(e["path"].endswith(":\\") for e in out["entries"]))
        else:
            self.assertEqual([e["path"] for e in out["entries"]], ["/"])


class DirectoryListApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "sub").mkdir()
        (self.root / "f.txt").write_text("x", encoding="utf-8")
        self.app = FastAPI()
        self.app.include_router(api.router_api)
        self.app.dependency_overrides[api._resolve_identity] = lambda: ('user', 'user')

    def tearDown(self):
        self.tmp.cleanup()

    def test_api_returns_directory_listing(self):
        client = TestClient(self.app, client=('127.0.0.1', 50000))
        r = client.get('/local/directory-list', params={'path': str(self.root)})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual({e["name"] for e in body["entries"]}, {"sub"})

    def test_api_rejects_bot_and_dev_remote(self):
        self.app.dependency_overrides[api._resolve_identity] = lambda: ('bot', 'bot')
        client = TestClient(self.app, client=('127.0.0.1', 50000))
        self.assertEqual(client.get('/local/directory-list').status_code, 403)
        # dev 身份远程访问（非回环）→ 403
        self.app.dependency_overrides[api._resolve_identity] = lambda: ('', 'dev')
        remote = TestClient(self.app, client=('192.0.2.1', 50000))
        self.assertEqual(remote.get('/local/directory-list').status_code, 403)
        # dev 身份回环 → 放行
        local = TestClient(self.app, client=('127.0.0.1', 50000))
        self.assertEqual(local.get('/local/directory-list').status_code, 200)

    def test_api_cross_origin_rejected(self):
        client = TestClient(self.app, client=('127.0.0.1', 50000))
        r = client.get('/local/directory-list',
                       headers={'Origin': 'https://example.com'})
        self.assertEqual(r.status_code, 403)


if __name__ == '__main__':
    unittest.main()
