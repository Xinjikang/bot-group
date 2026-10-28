"""req#122 task#212 配置底座：统一配置解析层测试。

覆盖：默认值、环境变量覆盖、命令行覆盖、.env 解析、新旧安装的根目录数据布局、
db/attachments/engine_url 派生。全程 patch 环境变量，不触碰真实配置与生产库。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import config


class ConfigDefaultsTest(unittest.TestCase):
    def setUp(self):
        # 清掉可能污染默认值断言的相关环境键，并重置 .env 缓存。
        self._clear = ('GROUP_ENGINE_HOST', 'HOST', 'GROUP_ENGINE_PORT', 'PORT',
                       'GROUP_DATA_DIR', 'DATA_DIR', 'GROUP_DB', 'GROUP_ATTACHMENTS_DIR',
                       'GROUP_ENGINE_URL', 'XDG_GROUP_ENGINE_URL', 'LOCALAPPDATA')
        config._dotenv_cache.clear()
        config.apply_cli(host=None, port=None, data_dir=None)
        self._orig = {k: os.environ.get(k) for k in self._clear}
        for k in self._clear:
            os.environ.pop(k, None)
        self._tmp = tempfile.TemporaryDirectory()
        self._root_patch = patch.object(config, 'program_root', return_value=Path(self._tmp.name))
        self._root_patch.start()

    def tearDown(self):
        self._root_patch.stop()
        self._tmp.cleanup()
        config._dotenv_cache.clear()
        config.apply_cli(host=None, port=None, data_dir=None)
        for k, v in self._orig.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_host_and_port(self):
        self.assertEqual(config.host(), '127.0.0.1')
        self.assertEqual(config.port(), 8180)

    def test_env_overrides_host_and_port(self):
        with patch.dict(os.environ, {'GROUP_ENGINE_PORT': '9999', 'GROUP_ENGINE_HOST': '0.0.0.0'}):
            self.assertEqual(config.port(), 9999)
            self.assertEqual(config.host(), '0.0.0.0')

    def test_dotenv_fallback_when_env_absent(self):
        # 无进程环境变量时，.env 的 PORT 生效；进程环境优先于 .env。
        # 显式数据目录中的配置参与端口解析。
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / '.env').write_text('PORT=7777\nHOST=127.0.0.1\n', encoding='utf-8')
            with patch.dict(os.environ, {'GROUP_DATA_DIR': str(root)}):
                config._dotenv_cache.clear()
                self.assertEqual(config.port(), 7777)
                with patch.dict(os.environ, {'GROUP_ENGINE_PORT': '8888'}, clear=False):
                    config._dotenv_cache.clear()
                    self.assertEqual(config.port(), 8888)

    def test_cli_beats_env_and_default(self):
        with patch.dict(os.environ, {'GROUP_ENGINE_PORT': '9999'}):
            self.assertEqual(config.port(7000), 7000)
            self.assertEqual(config.host('0.0.0.0'), '0.0.0.0')

    def test_group_db_override_priority(self):
        with patch.dict(os.environ, {'GROUP_DB': 'C:/tmp/x/group.db'}):
            self.assertEqual(config.db_path(), 'C:/tmp/x/group.db')

    def test_data_dir_explicit_override(self):
        with patch.dict(os.environ, {'GROUP_DATA_DIR': 'C:/tmp/mydata'}):
            self.assertEqual(config.data_dir(), Path('C:/tmp/mydata').resolve())
            self.assertEqual(config.db_path(), str(Path('C:/tmp/mydata').resolve() / 'group.db'))

    def test_fresh_install_uses_program_root_and_its_dotenv(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as appdata:
            root = Path(d)
            (root / '.env').write_text('GROUP_ENGINE_PORT=18765\n', encoding='utf-8')
            with patch.object(config, 'program_root', return_value=root), \
                 patch.dict(os.environ, {'LOCALAPPDATA': appdata}):
                self.assertFalse((root / 'group.db').exists())
                self.assertEqual(config.data_dir(), root)
                self.assertEqual(config.port(), 18765)
                self.assertEqual(config.db_path(), str(root / 'data' / 'group.db'))
                self.assertEqual(config.attachments_dir(), root / 'attachments')
                self.assertEqual(config.backups_dir(), root / 'backups')
                self.assertEqual(config.logs_dir(), root / 'logs')
                self.assertEqual(list(Path(appdata).iterdir()), [])

    def test_data_dir_cli_overrides_explicit_environment(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.dict(os.environ, {'GROUP_DATA_DIR': str(root / 'env-data')}):
                self.assertEqual(config.data_dir(root / 'cli-data'), root / 'cli-data')
                config.apply_cli(data_dir=root / 'cli-data')
                self.assertEqual(config.data_dir(), root / 'cli-data')

    def test_existing_database_keeps_program_root_default(self):
        # 程序根已有 group.db 时继续使用根目录。
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'group.db').write_bytes(b'')
            with patch.object(config, 'program_root', return_value=root):
                self.assertEqual(config.data_dir(), root)

    def test_explicit_directory_wins_over_existing_root_data(self):
        root = Path(self._tmp.name)
        (root / 'group.db').write_bytes(b'')
        (root / '.env').write_text('PORT=8181\n', encoding='utf-8')
        custom = root / 'custom'
        custom.mkdir()
        (custom / '.env').write_text('PORT=18888\n', encoding='utf-8')
        with patch.dict(os.environ, {'DATA_DIR': str(custom)}):
            self.assertEqual(config.data_dir(), custom)
            self.assertEqual(config.port(), 18888)
            with patch.dict(os.environ, {'GROUP_DATA_DIR': str(root / 'preferred')}):
                self.assertEqual(config.data_dir(), root / 'preferred')

    def test_attachments_dir_override_and_default(self):
        with patch.dict(os.environ, {'GROUP_ATTACHMENTS_DIR': 'C:/tmp/atts'}):
            self.assertEqual(config.attachments_dir(), Path('C:/tmp/atts').resolve())
        with patch.dict(os.environ, {'GROUP_DB': 'C:/tmp/x/group.db'}):
            self.assertEqual(config.attachments_dir(), Path('C:/tmp/x').resolve() / 'attachments')

    def test_engine_url_override_and_derived(self):
        with patch.dict(os.environ, {'GROUP_ENGINE_URL': 'http://127.0.0.1:9000/'}):
            self.assertEqual(config.engine_url(), 'http://127.0.0.1:9000')
        with patch.dict(os.environ, {'GROUP_ENGINE_PORT': '9999'}, clear=False):
            self.assertEqual(config.engine_url(), 'http://127.0.0.1:9999')

    def test_backups_and_logs_live_under_data_dir(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with patch.object(config, 'program_root', return_value=root), \
                 patch.dict(os.environ, {'GROUP_DATA_DIR': str(root / 'data')}):
                self.assertEqual(config.backups_dir(), (root / 'data' / 'backups'))
                self.assertEqual(config.logs_dir(), (root / 'data' / 'logs'))


if __name__ == '__main__':
    unittest.main()
