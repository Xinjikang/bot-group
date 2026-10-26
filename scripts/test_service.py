"""req#122 task#213 service 回归：.env 统一解析（剥引号/CR）+ 原生启停全链（不依赖 Git Bash）。

覆盖 node 1051 缺陷1（无 Git Windows 未贯穿启停，service.py 原生 Python 入口）+ 缺陷2（.env
引号/CRLF 解析回归，复用 config._parse_dotenv 统一剥引号/CR）。
"""

from __future__ import annotations

import os
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.maintenance import service  # noqa: E402


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


class LaunchTest(unittest.TestCase):
    def setUp(self):
        for name, value in (('_load_env', None), ('_running_instance', False),
                            ('do_start', 0), ('_claim_ui', {'action': 'open', 'reservation': 'test'}),
                            ('_release_ui', None)):
            patcher = mock.patch.object(service, name, return_value=value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        for name, value in (('host', '127.0.0.1'), ('port', 18765)):
            patcher = mock.patch.object(service.config, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(service.webbrowser, 'open', return_value=True)
        self.browser = patcher.start()
        self.addCleanup(patcher.stop)

    def test_cold_launch_opens_configured_url_after_start(self):
        self.do_start.side_effect = lambda: (self.browser.assert_not_called() or 0)
        self.assertEqual(service.do_launch(), 0)
        self.do_start.assert_called_once()
        self.browser.assert_called_once_with('http://127.0.0.1:18765/app/')

    def test_running_installation_opens_without_restart(self):
        self._running_instance.return_value = True
        self.assertEqual(service.do_launch(), 0)
        self.do_start.assert_not_called()
        self.browser.assert_called_once()

    def test_failure_keeps_browser_closed(self):
        self.do_start.return_value = 7
        self.assertEqual(service.do_launch(), 7)
        self.browser.assert_not_called()

    def test_other_installation_is_preserved(self):
        self._running_instance.side_effect = RuntimeError('实例不匹配')
        self.assertEqual(service.do_launch(), 1)
        self.do_start.assert_not_called()
        self.browser.assert_not_called()

    def test_browser_failure_preserves_running_service(self):
        for failure in (False, OSError('no browser')):
            with self.subTest(failure=failure):
                self.browser.side_effect = failure if isinstance(failure, Exception) else None
                self.browser.return_value = False
                self.assertEqual(service.do_launch(), 0)
                self._release_ui.assert_called_with('127.0.0.1', 18765, 'test')

    def test_existing_or_opening_page_is_reused(self):
        self._running_instance.return_value = True
        for action in ('reuse', 'pending'):
            self._claim_ui.return_value = {'action': action}
            self.assertEqual(service.do_launch(), 0)
        self.browser.assert_not_called()
        self.do_start.assert_not_called()

    def test_presence_failure_does_not_open_duplicate_page(self):
        self._claim_ui.side_effect = OSError('unavailable')
        self.assertEqual(service.do_launch(), 1)
        self.browser.assert_not_called()

    def test_cli_suppression_and_maintenance(self):
        with mock.patch.dict(os.environ, {'GROUP_SERVICE_MAINTENANCE': ''}):
            self.assertEqual(service.main(['start', '--no-browser']), 0)
        self.browser.assert_not_called()
        self._claim_ui.assert_not_called()
        self._running_instance.reset_mock()
        with mock.patch.dict(os.environ, {'GROUP_SERVICE_MAINTENANCE': '1'}):
            self.assertEqual(service.main(['start']), 0)
        self._running_instance.assert_not_called()
        self.browser.assert_not_called()
        self.assertEqual(service.main(['restart']), 0)
        self.browser.assert_not_called()

    def test_cli_data_directory_reaches_child_environment(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ):
            self.assertEqual(service.main(['start', '--data-dir', folder, '--no-browser']), 0)
            self.assertEqual(os.environ['GROUP_DATA_DIR'], str(Path(folder).resolve()))


class InstanceProbeTest(unittest.TestCase):
    def test_old_server_fallback_and_invalid_presence_reply(self):
        with mock.patch.object(service, '_ui_request', side_effect=service.urllib.error.HTTPError(
                'http://isolated', 404, 'missing', {}, None)):
            self.assertEqual(service._claim_ui('127.0.0.1', 1), {'action': 'open'})
        for result in ({}, {'action': 'invalid'}, []):
            with mock.patch.object(service, '_ui_request', return_value=result), self.assertRaises(ValueError):
                service._claim_ui('127.0.0.1', 1)

    def test_legacy_maintenance_requires_matching_parent_and_installation(self):
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.dict(os.environ, {'GROUP_SERVICE_MAINTENANCE': '', 'GROUP_RESTART_INSTANCE': ''}), \
             mock.patch.object(service.config, 'logs_dir', return_value=Path(folder)):
            path = Path(folder) / 'app-upgrade.json'
            receipt = {'state': 'running', 'helper_pid': os.getppid(),
                       'program_root': str(service.config.program_root().resolve()),
                       'db_path': str(Path(service.config.db_path()).resolve())}
            self.assertFalse(service._maintenance_start())
            path.write_text(json.dumps(receipt), encoding='utf-8')
            self.assertTrue(service._maintenance_start())
            for key, value in (('state', 'succeeded'), ('helper_pid', -1),
                               ('program_root', 'another-installation'), ('db_path', 'another-db')):
                with self.subTest(key=key):
                    path.write_text(json.dumps({**receipt, key: value}), encoding='utf-8')
                    self.assertFalse(service._maintenance_start())
            with mock.patch.dict(os.environ, {'GROUP_RESTART_INSTANCE': 'old-instance'}):
                self.assertTrue(service._maintenance_start())

    def test_authenticated_identity_and_path_match(self):
        from engine import auth
        root = Path(tempfile.gettempdir()) / 'program'
        database = root / 'data/group.db'
        health = {'status': 'ok', 'instance_id': 'current', 'program_root': str(root), 'db_path': str(database)}
        with mock.patch.object(service.config, 'program_root', return_value=root), \
             mock.patch.object(service.config, 'db_path', return_value=str(database)), \
             mock.patch.object(auth, 'configured', return_value=True), \
             mock.patch.object(auth, '_password', return_value='isolated'), \
             mock.patch.object(auth, 'login', return_value={'token': 'test-token'}), \
             mock.patch.object(service.urllib.request, 'build_opener') as factory:
            opener = factory.return_value
            opener.open.return_value = io.StringIO(json.dumps(health))
            self.assertTrue(service._running_instance('0.0.0.0', 18765))
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, 'http://127.0.0.1:18765/health')
            self.assertEqual(request.headers['Authorization'], 'Bearer test-token')
            for key in ('program_root', 'db_path', 'instance_id'):
                with self.subTest(key=key):
                    opener.open.return_value = io.StringIO(json.dumps({**health, key: ''}))
                    with self.assertRaises(RuntimeError):
                        service._running_instance('127.0.0.1', 18765)

    def test_local_browser_url(self):
        self.assertEqual(service._local_url('0.0.0.0', 8123), 'http://127.0.0.1:8123')
        self.assertEqual(service._local_url('::', 8123), 'http://[::1]:8123')
        self.assertEqual(service._local_url('localhost', 8123), 'http://localhost:8123')


if __name__ == "__main__":
    unittest.main()
