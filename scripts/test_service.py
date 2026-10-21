"""req#122 task#213 service 回归：.env 统一解析（剥引号/CR）+ 原生启停全链（不依赖 Git Bash）。

覆盖 node 1051 缺陷1（无 Git Windows 未贯穿启停，service.py 原生 Python 入口）+ 缺陷2（.env
引号/CRLF 解析回归，复用 config._parse_dotenv 统一剥引号/CR）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import service  # noqa: E402


class EnvLoadTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name)
        self._p = mock.patch.object(service.config, "data_dir", return_value=self._data_dir)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        for key in ("GROUP_PORT", "GROUP_HOST", "GROUP_DIR"):
            os.environ.pop(key, None)
        self._tmp.cleanup()

    def _write_env(self, text):
        (self._data_dir / ".env").write_text(text, encoding="utf-8")

    def test_strips_quotes_and_crlf(self):
        # 缺陷2：LF/CRLF、单双引号、含空格路径，统一 dotenv 解析剥引号/CR，不落字面值。
        self._write_env('GROUP_PORT="18042"\r\nGROUP_HOST=\'127.0.0.1\'\r\nexport GROUP_DIR=C:/my data\r\n')
        service._load_env()
        self.assertEqual(os.environ["GROUP_PORT"], "18042")
        self.assertEqual(os.environ["GROUP_HOST"], "127.0.0.1")
        self.assertEqual(os.environ["GROUP_DIR"], "C:/my data")

    def test_env_priority_over_dotenv(self):
        # 环境 > .env：进程环境已定义键不被 .env 覆盖。
        os.environ["GROUP_PORT"] = "9999"
        self._write_env('GROUP_PORT="18042"\n')
        service._load_env()
        self.assertEqual(os.environ["GROUP_PORT"], "9999")


class ServiceFlowTest(unittest.TestCase):
    def test_start_missing_venv(self):
        with mock.patch.object(service, "venv_python", return_value=Path("/nonexistent/python")):
            self.assertEqual(service.do_start(), 1)

    def test_start_check_failure_returns_1(self):
        # 旧环境（venv 存在但就绪检查未过）不继续启动：do_start 返回 1 且不触发停机/启动
        with mock.patch.object(service, "venv_python", return_value=Path(sys.executable)), \
                mock.patch.object(service, "_run_install_check", return_value=1), \
                mock.patch.object(service, "do_stop") as do_stop, \
                mock.patch.object(service.subprocess, "run") as run:
            self.assertEqual(service.do_start(), 1)
        do_stop.assert_not_called()
        run.assert_not_called()

    def test_stop_missing_venv(self):
        with mock.patch.object(service, "venv_python", return_value=Path("/nonexistent/python")):
            self.assertEqual(service.do_stop(), 1)

    def test_start_full_chain(self):
        # 缺陷1：安装→启动→健康检查全链，仅调 Python 入口（不探测 Git Bash）。
        with mock.patch.object(service, "venv_python", return_value=Path(sys.executable)), \
                mock.patch.object(service, "_run_install_check", return_value=0), \
                mock.patch.object(service, "do_stop", return_value=0), \
                mock.patch.object(service.config, "host", return_value="127.0.0.1"), \
                mock.patch.object(service.config, "port", return_value=18042), \
                mock.patch.object(service.subprocess, "run", return_value=mock.Mock(returncode=0)), \
                mock.patch.object(service, "_health_check", return_value=0):
            self.assertEqual(service.do_start(), 0)

    def test_stop_full_chain(self):
        with mock.patch.object(service, "venv_python", return_value=Path(sys.executable)), \
                mock.patch.object(service, "_load_env"), \
                mock.patch.object(service.config, "engine_url", return_value="http://127.0.0.1:18042"), \
                mock.patch.object(service.config, "host", return_value="127.0.0.1"), \
                mock.patch.object(service.config, "port", return_value=18042), \
                mock.patch.object(service.subprocess, "run", return_value=mock.Mock(returncode=0)):
            self.assertEqual(service.do_stop(), 0)

    def test_inherited_callback_url_cannot_redirect_stop(self):
        with mock.patch.object(service, "venv_python", return_value=Path(sys.executable)), \
             mock.patch.object(service, "_load_env"), \
             mock.patch.object(service.config, "engine_url", return_value="http://127.0.0.1:8180"), \
             mock.patch.object(service.config, "host", return_value="127.0.0.1"), \
             mock.patch.object(service.config, "port", return_value=18042), \
             mock.patch.object(service.subprocess, "run") as run:
            self.assertEqual(service.do_stop(), 1)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
