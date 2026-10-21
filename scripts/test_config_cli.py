"""req#122 task#212 关单核验退修回归（node 1018）。

缺陷1：config._dotenv 应读数据目录 .env（非程序根 .env），GROUP_DATA_DIR 下 .env
配 GROUP_ENGINE_PORT 应生效。
缺陷2：main --host/--port 应贯穿到 wake._engine_url()（config 模块级 CLI 覆盖状态）。

隔离目录 + mock ManagedServer.run，不启真实服务、不碰生产库。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import config


def _reset_config_state() -> None:
    """清空模块级 CLI 覆盖状态、.env 缓存与遗留环境键，避免跨测试串扰。"""
    config._dotenv_cache.clear()
    config.apply_cli(host=None, port=None, data_dir=None)
    # main() 会写 os.environ['GROUP_DATA_DIR']（供子进程继承），测试需清掉避免污染后续用例
    os.environ.pop("GROUP_DATA_DIR", None)
    os.environ.pop("DATA_DIR", None)


class DataDirDotenvTest(unittest.TestCase):
    """缺陷1：.env 落数据目录，data_dir/.env 的端口配置应生效。"""

    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, {k: '' for k in (
            'GROUP_ENGINE_PORT', 'PORT', 'GROUP_ENGINE_URL', 'XDG_GROUP_ENGINE_URL')})
        self._env.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        _reset_config_state()

    def tearDown(self) -> None:
        _reset_config_state()
        self._env.stop()
        self._tmp.cleanup()

    def test_data_dir_dotenv_port_applied(self) -> None:
        # GROUP_DATA_DIR 指向临时目录，目录内 .env 写 GROUP_ENGINE_PORT=18042
        (self.data_dir / ".env").write_text("GROUP_ENGINE_PORT=18042\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"GROUP_DATA_DIR": str(self.data_dir)}):
            # data_dir 由环境变量决定，.env 落该目录；port 读 data_dir/.env（非程序根 .env）
            self.assertEqual(config.data_dir(), self.data_dir.resolve())
            self.assertEqual(config.port(), 18042)

    def test_data_dir_no_dotenv_falls_back_default(self) -> None:
        # 临时目录无 .env，端口回落默认 8180（不读程序根 .env 污染）
        with mock.patch.dict(os.environ, {"GROUP_DATA_DIR": str(self.data_dir)}):
            self.assertEqual(config.port(), 8180)


class CliThroughTest(unittest.TestCase):
    """缺陷2：main --host/--port 贯穿到 engine_url 派生（wake/MCP 回调）。"""

    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, {k: '' for k in (
            'GROUP_ENGINE_PORT', 'PORT', 'GROUP_ENGINE_URL', 'XDG_GROUP_ENGINE_URL')})
        self._env.start()
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        _reset_config_state()

    def tearDown(self) -> None:
        _reset_config_state()
        self._env.stop()
        self._tmp.cleanup()

    def test_apply_cli_port_through_engine_url(self) -> None:
        # 直接验证模块级 CLI 覆盖状态贯穿 engine_url
        config.apply_cli(host="127.0.0.1", port=18042, data_dir=str(self.data_dir))
        self.assertEqual(config.port(), 18042)
        self.assertEqual(config.engine_url(), "http://127.0.0.1:18042")

    def test_main_cli_port_through_wake_engine_url(self) -> None:
        from engine import main as main_mod
        from engine import wake

        # 模拟 main(['--host','127.0.0.1','--port','18042','--data-dir', tmp])，mock run 不启真实服务
        with mock.patch.object(main_mod, "ManagedServer") as MockServer:
            main_mod.main(['--host', '127.0.0.1', '--port', '18042',
                           '--data-dir', str(self.data_dir)])
            uvicorn_config = MockServer.call_args[0][0]
            # uvicorn.Config.port 应为 18042
            self.assertEqual(uvicorn_config.port, 18042)
            # wake._engine_url() 读 CLI 覆盖状态 → http://127.0.0.1:18042
            self.assertEqual(wake._engine_url(), "http://127.0.0.1:18042")


if __name__ == "__main__":
    unittest.main()
