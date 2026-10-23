"""req#122 task#213 bootstrap 回归：SHA256 校验、解压、落地持久安装目录、install 执行。

隔离 + mock，不真的联网下载/启服务/碰生产库。覆盖 node 1046 缺陷1（持久安装目录）与缺陷2（无 Git Windows，
纯 Python 标准库实现）。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from tools import bootstrap  # noqa: E402


def _make_source_tree(root: Path) -> None:
    """构造一个最小完整项目包：install.sh + requirements.txt + scripts/install.py。"""
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "install.py").write_text("pass\n", encoding="utf-8")
    (root / "install.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")


def _zip_tree(src: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w") as z:
        for root, _dirs, files in os.walk(src):
            for f in files:
                full = Path(root) / f
                z.write(full, full.relative_to(src))


class BootstrapVerifyTest(unittest.TestCase):
    def test_verify_sha256_mismatch_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "src.zip"
            p.write_bytes(b"hello")
            with self.assertRaises(RuntimeError):
                bootstrap._verify("deadbeef", p)

    def test_verify_sha256_match_passes(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "src.zip"
            p.write_bytes(b"hello")
            bootstrap._verify(bootstrap._sha256_file(p), p)  # 不抛异常


class BootstrapLandTest(unittest.TestCase):
    def test_repeat_install_merges_without_removing_user_files(self):
        with tempfile.TemporaryDirectory() as folder:
            src, dst = Path(folder) / 'src', Path(folder) / 'installed'
            _make_source_tree(src)
            bootstrap._land(src, dst)
            (dst / 'scripts' / 'user.txt').write_text('USER')
            (dst / '.env').write_text('KEEP')
            bootstrap._land(src, dst)
            self.assertEqual((dst / 'scripts' / 'user.txt').read_text(), 'USER')
            self.assertEqual((dst / '.env').read_text(), 'KEEP')

    def test_different_source_fails_before_any_write(self):
        with tempfile.TemporaryDirectory() as folder:
            src, dst = Path(folder) / 'src', Path(folder) / 'installed'
            _make_source_tree(src)
            bootstrap._land(src, dst)
            (src / 'new.txt').write_text('NEW')
            (src / 'scripts' / 'install.py').write_text('CHANGED')
            with self.assertRaises(RuntimeError):
                bootstrap._land(src, dst)
            self.assertFalse((dst / 'new.txt').exists())
            self.assertEqual((dst / 'scripts' / 'install.py').read_text(), 'pass\n')

    def test_package_cannot_overwrite_persistent_data(self):
        with tempfile.TemporaryDirectory() as folder:
            src, dst = Path(folder) / 'src', Path(folder) / 'installed'
            _make_source_tree(src)
            (src / 'group.db').write_text('PACKAGE')
            dst.mkdir()
            (dst / 'group.db').write_text('USER')
            with self.assertRaises(RuntimeError):
                bootstrap._land(src, dst)
            self.assertEqual((dst / 'group.db').read_text(), 'USER')

    def test_land_copies_source_to_install_dir(self):
        # 缺陷1：落地到持久安装目录（install 后 venv/源码都在这里，不随临时目录删除）
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "src"
            _make_source_tree(src)
            install_dir = d / "installed"
            bootstrap._land(src, install_dir)
            self.assertTrue((install_dir / "install.sh").exists())
            self.assertTrue((install_dir / "scripts" / "install.py").exists())
            self.assertTrue((install_dir / "requirements.txt").exists())

    def test_find_source_root_inside_top_level_dir(self):
        # 归档包一层顶层目录：定位到含 install.sh 的源码根
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            top = d / "bot-group-v1.0"
            _make_source_tree(top)
            self.assertEqual(bootstrap._find_source_root(d), top)

    def test_find_source_root_flat(self):
        # 归档直接是项目文件（无顶层目录）
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            _make_source_tree(d)
            self.assertEqual(bootstrap._find_source_root(d), d)


class BootstrapMainTest(unittest.TestCase):
    def test_tools_layout_downloads_and_executes_installer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            src = root / 'src'
            (src / 'tools').mkdir(parents=True)
            (src / 'tools/install.py').write_text(
                "from pathlib import Path\nPath(__file__).with_suffix('.ran').write_text('ok')\n")
            archive = root / 'source.zip'
            _zip_tree(src, archive)
            target = root / 'installed'
            with mock.patch.dict(os.environ, {'BOOTSTRAP_URL': archive.as_uri(),
                    'BOOTSTRAP_SHA256': bootstrap._sha256_file(archive), 'INSTALL_DIR': str(target)}):
                self.assertEqual(bootstrap.main(), 0)
            self.assertEqual((target / 'tools/install.ran').read_text(), 'ok')
            self.assertFalse((target / 'scripts').exists())

    def test_main_full_flow_lands_and_runs_install(self):
        # 完整流程：下载(mock)→校验(真实)→解压(真实 zipfile)→落地(真实)→install(mock subprocess)
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = d / "src"
            _make_source_tree(src)
            zip_path = d / "src.zip"
            _zip_tree(src, zip_path)
            sha = bootstrap._sha256_file(zip_path)
            install_dir = d / "installed"
            with mock.patch.dict(os.environ, {
                "BOOTSTRAP_URL": "https://example.com/src.zip",
                "BOOTSTRAP_SHA256": sha,
                "INSTALL_DIR": str(install_dir),
            }), mock.patch.object(
                bootstrap, "_download", side_effect=lambda url, dest: shutil.copyfile(zip_path, dest)
            ), mock.patch.object(bootstrap.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
                rc = bootstrap.main()
            self.assertEqual(rc, 0)
            # 落地到持久目录，install 在持久目录执行（非临时目录）
            self.assertTrue((install_dir / "scripts" / "install.py").exists())
            call = run.call_args[0][0]
            self.assertEqual(call[1], str(install_dir / "scripts" / "install.py"))
            self.assertEqual(call[2], "install")


if __name__ == "__main__":
    unittest.main()
