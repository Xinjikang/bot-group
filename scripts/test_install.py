"""req#122 task#213 install 回归：就绪检查（check）判据、状态文件、依赖摘要变化。

隔离 + mock，不真的建 venv/装依赖/启服务/碰生产库。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parent
_PROGRAM_ROOT = _SCRIPTS_DIR.parent
for _p in (str(_PROGRAM_ROOT), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from engine.maintenance import install  # noqa: E402


class InstallCheckTest(unittest.TestCase):
    """do_check 就绪检查判据：venv + 状态 + Python 版本 + requirements 摘要 + 锁定清单。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name)
        self._lock = self._data_dir / "requirements.lock"
        self._lock.write_text("fastapi==0.110.0\n", encoding="utf-8")
        # 隔离：data_dir 指向临时目录、venv 解释器 mock 成存在的当前解释器、LOCKFILE 指临时文件
        self._p_data = mock.patch.object(install.config, "data_dir", return_value=self._data_dir)
        self._p_data.start()
        self._p_venv = mock.patch.object(install, "venv_python", return_value=Path(sys.executable))
        self._p_venv.start()
        self._p_lock = mock.patch.object(install, "LOCKFILE", self._lock)
        self._p_lock.start()

    def tearDown(self):
        self._p_lock.stop()
        self._p_venv.stop()
        self._p_data.stop()
        self._tmp.cleanup()

    def _write_state(self, **overrides):
        data = {
            "python_version": install.current_python_version(),
            "requirements_sha256": install._sha256(install.REQUIREMENTS),
            "lock_sha256": install._sha256(install.LOCKFILE),
            "installed_at": "2026-10-16T00:00:00",
        }
        data.update(overrides)
        sp = install.state_path()
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(data), encoding="utf-8")

    def test_check_no_state_returns_1(self):
        # 未安装（无状态文件）→ 提示 install
        self.assertEqual(install.do_check(), 1)

    def test_check_ready_returns_0(self):
        self._write_state()
        self.assertEqual(install.do_check(), 0)

    def test_check_requirements_changed_returns_1(self):
        self._write_state(requirements_sha256="deadbeef")
        self.assertEqual(install.do_check(), 1)

    def test_check_lock_changed_returns_1(self):
        # 锁定清单内容变化（requirements.lock 被改但文件还在）→ 提示重跑 install
        self._write_state(lock_sha256="deadbeef")
        self.assertEqual(install.do_check(), 1)

    def test_check_python_mismatch_returns_1(self):
        self._write_state(python_version="2.7.0")
        self.assertEqual(install.do_check(), 1)

    def test_check_missing_lock_returns_1(self):
        self._write_state()
        # 锁定清单缺失 → 提示重跑 install
        self._lock.unlink()
        self.assertEqual(install.do_check(), 1)


class FreshInstallLocationTest(unittest.TestCase):
    def test_first_config_and_service_share_root_before_database_exists(self):
        from engine.maintenance import service

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "program"
            root.mkdir()
            appdata = Path(tmp) / "user-data"
            appdata.mkdir()
            install.config.apply_cli(host=None, port=None, data_dir=None)
            install.config._dotenv_cache.clear()
            try:
                with mock.patch.object(install.config, "program_root", return_value=root), \
                     mock.patch.dict(os.environ, {"LOCALAPPDATA": str(appdata)}, clear=True):
                    env_path = install.first_config()
                    self.assertEqual(env_path, root / ".env")
                    self.assertTrue(env_path.is_file())
                    self.assertFalse((root / "group.db").exists())
                    self.assertEqual(install.state_path(), root / "install.json")
                    custom = "GROUP_ENGINE_PORT=18765\nGROUP_ADMIN_PASSWORD=test-only\n"
                    env_path.write_text(custom, encoding="utf-8")
                    install.first_config()
                    self.assertEqual(env_path.read_text(encoding="utf-8"), custom)
                    service._load_env()
                    self.assertEqual(install.config.port(), 18765)
                    self.assertEqual(os.environ["GROUP_ADMIN_PASSWORD"], "test-only")
                    self.assertEqual(install.config.db_path(), str(root / "data" / "group.db"))
                    self.assertEqual(list(appdata.iterdir()), [])
            finally:
                install.config._dotenv_cache.clear()
                install.config.apply_cli(host=None, port=None, data_dir=None)


class InstallStateTest(unittest.TestCase):
    """state_path / 状态写入落数据目录，摘要跟代码绑定。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name)
        self._p = mock.patch.object(install.config, "data_dir", return_value=self._data_dir)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def test_state_path_in_data_dir(self):
        self.assertEqual(install.state_path(), self._data_dir / "install.json")

    def test_check_python_minimum(self):
        # MIN_PYTHON 拦截行为：低于下限的解释器明确报错（task#215：lock 集合要求 >=3.11，
        # PATH 3.10 会在 pip 阶段装一半失败，check 前置拦截）。当前解释器达标时返回版本串。
        import sys as _sys
        v = _sys.version_info
        if (v.major, v.minor) < install.MIN_PYTHON:
            with self.assertRaises(RuntimeError):
                install.check_python()
        else:
            self.assertEqual(install.check_python(), install.current_python_version())


class InstallFlowTest(unittest.TestCase):
    """install 全链关键路径：发布锁定 pip 输入、失败重装失效旧状态、runtime 真实探测。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name)
        self._lock = self._data_dir / "requirements.lock"
        self._lock.write_text("fastapi==0.110.0\n", encoding="utf-8")
        self._p_data = mock.patch.object(install.config, "data_dir", return_value=self._data_dir)
        self._p_data.start()
        self._p_lock = mock.patch.object(install, "LOCKFILE", self._lock)
        self._p_lock.start()

    def tearDown(self):
        self._p_lock.stop()
        self._p_data.stop()
        self._tmp.cleanup()

    def test_pip_install_uses_lockfile(self):
        # 发布锁定：requirements.lock 随版本交付，作为 pip 安装输入（不事后 freeze）
        with mock.patch.object(install.subprocess, "run") as run:
            install.pip_install()
        args = run.call_args[0][0]
        self.assertEqual(args[-1], str(self._lock))

    def test_pip_install_falls_back_to_requirements(self):
        # 缺 lock 时回退 requirements.txt（开发环境未锁定）
        self._lock.unlink()
        with mock.patch.object(install.subprocess, "run") as run:
            install.pip_install()
        args = run.call_args[0][0]
        self.assertEqual(args[-1], str(install.REQUIREMENTS))

    def test_do_install_invalidates_old_state(self):
        # 失败重装：do_install 开始前失效旧状态，失败后 check != 0
        sp = install.state_path()
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text("{}", encoding="utf-8")
        with mock.patch.object(install, "check_python", side_effect=RuntimeError("boom")):
            rc = install.do_install()
        self.assertEqual(rc, 1)
        self.assertFalse(sp.exists())

    def test_detect_runtime_reports_missing_bin(self):
        # runtime 真实探测：环境变量空 + PATH 找不到可执行文件 → bin 为空（缺项报告）
        with mock.patch.dict(os.environ, {"HERMES_HOME": "", "HERMES_BIN": "", "CODEX_HOME": ""}), \
                mock.patch.object(install, "_find_executable", return_value=""), \
                mock.patch('engine.hermes_runtime.executable', side_effect=FileNotFoundError('missing executable')):
            runtime = install.detect_runtime()
        self.assertEqual(runtime["hermes"]["bin"], "")
        self.assertEqual(runtime["codex"]["bin"], "")


class DetectRuntimeCodexHomeTest(unittest.TestCase):
    """CODEX_HOME 探测回归（req#133 #254）。

    e48560e 之前 detect_runtime 在 CODEX_HOME 非空时引用未定义变量 `exe`（原本想从
    hermes 的可执行名派生），直接抛 NameError：install 与启动检查都失败、引擎起不来。
    既有用例只覆盖 CODEX_HOME 为空的分支，所以这个缺陷漏检。本组按实际实现覆盖
    CODEX_HOME 设置/未设置、候选存在/缺失、PATH 回退、Windows 后缀与其他平台分支。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._home = Path(self._tmp.name)
        # hermes 侧不参与本组断言：诊断固定返回空结构，避免读真实 HERMES_HOME
        self._p_hermes = mock.patch(
            "engine.hermes_runtime.diagnostic",
            return_value={"bin": "", "home": "", "source": "missing"})
        self._p_hermes.start()

    def tearDown(self):
        self._p_hermes.stop()
        self._tmp.cleanup()

    def _touch(self, name: str) -> str:
        p = self._home / name
        p.write_text("", encoding="utf-8")
        return str(p)

    def _detect(self, *, codex_home, platform="win32", path_bin=""):
        env = {"CODEX_HOME": codex_home}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(install.sys, "platform", platform), \
                mock.patch.object(install, "_find_executable", return_value=path_bin):
            return install.detect_runtime()

    def test_codex_home_exe_found_without_name_error(self):
        # 缺陷复现点：CODEX_HOME 非空 + Windows 候选存在
        exe = self._touch("codex.exe")
        runtime = self._detect(codex_home=str(self._home))
        self.assertEqual(runtime["codex"]["bin"], exe)
        self.assertEqual(runtime["codex"]["home"], str(self._home))

    def test_windows_suffix_candidates_cmd_and_bat(self):
        for name in ("codex.cmd", "codex.bat"):
            with self.subTest(name=name):
                for old in self._home.iterdir():
                    old.unlink()
                target = self._touch(name)
                runtime = self._detect(codex_home=str(self._home))
                self.assertEqual(runtime["codex"]["bin"], target)

    def test_windows_plain_name_without_suffix_not_matched(self):
        # Windows 只枚举 codex.exe/.cmd/.bat；裸 codex 不是有效候选（与实现一致）
        self._touch("codex")
        self.assertEqual(self._detect(codex_home=str(self._home))["codex"]["bin"], "")

    def test_codex_home_missing_falls_back_to_path(self):
        path_bin = self._touch("path-codex.exe")
        runtime = self._detect(codex_home=str(self._home), path_bin=path_bin)
        self.assertEqual(runtime["codex"]["bin"], path_bin)

    def test_missing_everywhere_reports_empty(self):
        runtime = self._detect(codex_home=str(self._home), path_bin="")
        self.assertEqual(runtime["codex"]["bin"], "")
        self.assertEqual(runtime["codex"]["home"], str(self._home))

    def test_codex_home_unset_uses_path_only(self):
        path_bin = self._touch("path-codex.exe")
        runtime = self._detect(codex_home="", path_bin=path_bin)
        self.assertEqual(runtime["codex"]["bin"], path_bin)
        self.assertEqual(runtime["codex"]["home"], "")

    def test_non_windows_uses_plain_codex(self):
        target = self._touch("codex")
        runtime = self._detect(codex_home=str(self._home), platform="linux")
        self.assertEqual(runtime["codex"]["bin"], target)

    def test_non_windows_ignores_windows_suffixes(self):
        # 其他平台分支只认裸 codex，codex.exe 不应被当成候选
        self._touch("codex.exe")
        self.assertEqual(self._detect(codex_home=str(self._home), platform="linux")["codex"]["bin"],
                         "")

    def test_do_check_with_codex_home_set_returns_0(self):
        """启动检查（do_check）在 CODEX_HOME 非空时走 detect_runtime，不得抛 NameError。

        e48560e 之前该路径直接抛异常，启动脚本退出码 1、引擎起不来；本用例覆盖
        「安装/启动检查调用探测函数」的路径，而不只是函数本身。
        """
        data_dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        lock = data_dir / "requirements.lock"
        lock.write_text("fastapi==0.110.0\n", encoding="utf-8")
        out = io.StringIO()
        with mock.patch.object(install.config, "data_dir", return_value=data_dir), \
                mock.patch.object(install, "venv_python", return_value=Path(sys.executable)), \
                mock.patch.object(install, "LOCKFILE", lock), \
                mock.patch.dict(os.environ, {"CODEX_HOME": str(self._home)}), \
                mock.patch.object(install.sys, "platform", "win32"), \
                mock.patch.object(install, "_find_executable", return_value=""), \
                redirect_stdout(out), redirect_stderr(io.StringIO()):
            install.write_state()
            rc = install.do_check()
        self.assertEqual(rc, 0)
        self.assertIn("Hermes/Codex", out.getvalue())


class InstallVenvVersionTest(unittest.TestCase):
    """就绪记录/检查统一 venv 实际解释器版本（node 1066 启动回归：install.bat 用 PATH Python 3.10.4
    建 venv，但 venv 已是 3.11.15，写调用方版本导致 check 恒失败）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name)
        self._lock = self._data_dir / "requirements.lock"
        self._lock.write_text("fastapi==0.110.0\n", encoding="utf-8")
        self._p_data = mock.patch.object(install.config, "data_dir", return_value=self._data_dir)
        self._p_data.start()
        self._p_venv = mock.patch.object(install, "venv_python", return_value=Path(sys.executable))
        self._p_venv.start()
        self._p_lock = mock.patch.object(install, "LOCKFILE", self._lock)
        self._p_lock.start()

    def tearDown(self):
        self._p_lock.stop()
        self._p_venv.stop()
        self._p_data.stop()
        self._tmp.cleanup()

    def _write_state(self, python_version):
        data = {
            "python_version": python_version,
            "requirements_sha256": install._sha256(install.REQUIREMENTS),
            "lock_sha256": install._sha256(install.LOCKFILE),
            "installed_at": "2026-10-16T00:00:00",
        }
        sp = install.state_path()
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(data), encoding="utf-8")

    def test_write_state_records_venv_version_not_caller(self):
        # 记录 venv 实际版本（3.11.15），不是调用方（PATH Python 3.10.4）
        with mock.patch.object(install, "venv_python_version", return_value="3.11.15"):
            install.write_state()
        state = json.loads(install.state_path().read_text(encoding="utf-8"))
        self.assertEqual(state["python_version"], "3.11.15")

    def test_check_matches_venv_version_returns_0(self):
        self._write_state(python_version="3.11.15")
        with mock.patch.object(install, "venv_python_version", return_value="3.11.15"):
            self.assertEqual(install.do_check(), 0)

    def test_check_venv_version_mismatch_returns_1(self):
        self._write_state(python_version="3.10.4")
        with mock.patch.object(install, "venv_python_version", return_value="3.11.15"):
            self.assertEqual(install.do_check(), 1)


class InstallRepairTest(unittest.TestCase):
    """旧环境迁移（已有 .venv 但无 install.json）：可操作提示 + repair 验证迁移 + 半安装拒绝。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._data_dir = Path(self._tmp.name)
        self._lock = self._data_dir / "requirements.lock"
        self._lock.write_text("fastapi==0.110.0\n", encoding="utf-8")
        self._p_data = mock.patch.object(install.config, "data_dir", return_value=self._data_dir)
        self._p_data.start()
        self._p_venv = mock.patch.object(install, "venv_python", return_value=Path(sys.executable))
        self._p_venv.start()
        self._p_lock = mock.patch.object(install, "LOCKFILE", self._lock)
        self._p_lock.start()

    def tearDown(self):
        self._p_lock.stop()
        self._p_venv.stop()
        self._p_data.stop()
        self._tmp.cleanup()

    def test_check_missing_state_hint_mentions_repair(self):
        # 旧环境（venv 存在但无 install.json）：提示迁移路径，而非只叫重跑 install
        buf = io.StringIO()
        with redirect_stderr(buf):
            self.assertEqual(install.do_check(), 1)
        self.assertIn("repair", buf.getvalue())

    def test_repair_migrates_old_env_without_reinstall(self):
        # repair 验证 venv 依赖 + 记录 venv 实际版本，不重装
        with mock.patch.object(install, "venv_python_version", return_value="3.11.15"), \
                mock.patch.object(install, "selfcheck"), \
                mock.patch.object(install, "verify_lock_versions", return_value=True):
            self.assertEqual(install.do_repair(), 0)
        state = json.loads(install.state_path().read_text(encoding="utf-8"))
        self.assertEqual(state["python_version"], "3.11.15")

    def test_repair_fails_on_lock_drift(self):
        # 缺陷：lock 漂移（如 fastapi==999.999.999）不得盖就绪章（node 1071 P1）
        with mock.patch.object(install, "venv_python_version", return_value="3.11.15"), \
                mock.patch.object(install, "selfcheck"), \
                mock.patch.object(install, "verify_lock_versions", return_value=False):
            self.assertEqual(install.do_repair(), 1)
        self.assertFalse(install.state_path().exists())

    def test_verify_lock_versions_requires_lock(self):
        with mock.patch.object(install, "LOCKFILE", Path("/nonexistent/lock")):
            self.assertFalse(install.verify_lock_versions())

    def test_repair_fails_when_selfcheck_broken(self):
        # 半安装/缺依赖：selfcheck 失败不写就绪（保持可重装）
        with mock.patch.object(install, "venv_python_version", return_value="3.11.15"), \
                mock.patch.object(install, "selfcheck", side_effect=subprocess.CalledProcessError(1, "pip")):
            self.assertEqual(install.do_repair(), 1)
        self.assertFalse(install.state_path().exists())

    def test_repair_fails_when_venv_below_min(self):
        with mock.patch.object(install, "venv_python_version", return_value="3.9.0"):
            self.assertEqual(install.do_repair(), 1)
        self.assertFalse(install.state_path().exists())

    def test_repair_fails_when_venv_missing(self):
        with mock.patch.object(install, "venv_python", return_value=Path("/nonexistent/python")):
            self.assertEqual(install.do_repair(), 1)


class BatEntryTest(unittest.TestCase):
    """实际 .bat 入口透传：install.bat 支持 repair 子命令，start/stop.bat 探测 Python 调 service.py。"""

    def test_install_bat_passthrough_repair(self):
        text = (install.PROGRAM_ROOT / "install.bat").read_text(encoding="utf-8")
        self.assertIn("tools\\install.py %CMD%", text)
        self.assertIn('if "%CMD%"=="" set "CMD=install"', text)

    def test_start_bat_calls_service(self):
        text = (install.PROGRAM_ROOT / "start.bat").read_text(encoding="utf-8")
        self.assertIn("tools\\service.py restart", text)

    def test_stop_bat_calls_service(self):
        text = (install.PROGRAM_ROOT / "stop.bat").read_text(encoding="utf-8")
        self.assertIn("tools\\service.py stop", text)


if __name__ == "__main__":
    unittest.main()
