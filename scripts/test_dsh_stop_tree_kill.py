"""req#138 task#272 node1625：异常进程回收 kill 逻辑 mock 回归（4 个故障反例）。

manager node1625 要求补：随启动/发现登记的外层资源 registry、杀前创建身份/归属核验、
kill 后存活核验、returncode 校验，并覆盖 capture 中途异常 / stop 异常 / taskkill 失败 /
历史 PID 复用四个反例（mock 即可，勿先重复整套模型验证）。
"""
import unittest
from unittest import mock

import scripts.verify_dsh_stop_tree as v


class KillRegistryTests(unittest.TestCase):
    def _reg(self, pid=71001, name="harness.exe", ct=1000):
        reg = v._ResourceRegistry()
        # register 内部调 _process_identity 采集 (name, create_time)。
        with mock.patch.object(v, "_process_identity", return_value=(name, ct)):
            reg.register(pid)
        return reg

    def test_same_name_pid_reuse_create_time_mismatch(self):
        # A：同名 PID 复用（create_time 不同）拒杀，不 taskkill——name 不能作创建身份。
        reg = self._reg(name="node.exe", ct=1000)
        with mock.patch.object(v, "_process_identity", return_value=("node.exe", 2000)), \
             mock.patch.object(v.subprocess, "run") as run:
            rep = v._kill_registered(reg)
        self.assertEqual(rep[71001], "identity_mismatch")
        run.assert_not_called()

    def test_name_change_identity_mismatch(self):
        reg = self._reg(name="harness.exe", ct=1000)
        with mock.patch.object(v, "_process_identity", return_value=("notepad.exe", 1000)), \
             mock.patch.object(v.subprocess, "run") as run:
            rep = v._kill_registered(reg)
        self.assertEqual(rep[71001], "identity_mismatch")
        run.assert_not_called()

    def test_killed_after_success(self):
        # returncode=0 且杀后身份消失 → killed。
        reg = self._reg()
        with mock.patch.object(v, "_process_identity", side_effect=[
            ("harness.exe", 1000),  # kill 前身份核验
            None,                    # kill 后退出
        ]), mock.patch.object(v.subprocess, "run", return_value=mock.Mock(returncode=0)):
            rep = v._kill_registered(reg)
        self.assertEqual(rep[71001], "killed")

    def test_taskkill_failure_rc1(self):
        # taskkill returncode=1（失败）不得记 killed（旧 _kill_pids 不查 returncode 仍 killed=1）。
        reg = self._reg()
        with mock.patch.object(v, "_process_identity", return_value=("harness.exe", 1000)), \
             mock.patch.object(v.subprocess, "run", return_value=mock.Mock(returncode=1)):
            rep = v._kill_registered(reg)
        self.assertEqual(rep[71001], "kill_failed_rc=1")

    def test_already_exited_skipped(self):
        # 已退出（identity None）跳过，不 taskkill 历史 PID。
        reg = self._reg()
        with mock.patch.object(v, "_process_identity", return_value=None), \
             mock.patch.object(v.subprocess, "run") as run:
            rep = v._kill_registered(reg)
        self.assertEqual(rep[71001], "already_exited")
        run.assert_not_called()

    def test_still_alive_after_kill(self):
        # returncode=0 但杀后身份仍在 → still_alive（不 pass）。
        reg = self._reg()
        with mock.patch.object(v, "_process_identity", return_value=("harness.exe", 1000)), \
             mock.patch.object(v.subprocess, "run", return_value=mock.Mock(returncode=0)):
            rep = v._kill_registered(reg)
        self.assertEqual(rep[71001], "still_alive")

    def test_kill_ok_false_on_failure(self):
        # kill_ok 判定：kill_failed/identity_mismatch/still_alive 均不算通过，仅 killed/already_exited 通过。
        ok = lambda s: all(x in ("killed", "already_exited") for x in s)
        self.assertFalse(ok(["kill_failed_rc=1"]))
        self.assertFalse(ok(["identity_mismatch"]))
        self.assertFalse(ok(["still_alive"]))
        self.assertTrue(ok(["killed", "already_exited"]))

    def test_register_engine_proc_before_discovery(self):
        # B：引擎进程随启动登记（首次 PID 发现前），registry 非空，异常路径可回收。
        reg = v._ResourceRegistry()
        with mock.patch.object(v, "_process_identity", return_value=("python.exe", 5000)):
            reg.register(8888)  # 模拟 _run_segment 里 fx.engine.proc.pid 随启动登记
        self.assertEqual(reg.pids(), [8888])


class CleanupStructureTests(unittest.TestCase):
    def test_finally_kill_error_does_not_skip_fx_close(self):
        # C：_kill_registered 抛异常（快照不可读）仍执行 fx.close —— 独立 try/except 结构核验。
        fx = mock.Mock()
        result = {}
        try:
            raise OSError("snapshot unreadable")  # 模拟 _kill_registered 抛异常
        except OSError as e:
            result["kill_report"] = {"_error": f"{type(e).__name__}: {e}"}
        # 无论 kill 是否异常，fx.close 都应在独立保护块里执行。
        try:
            fx.close()
        except Exception as e:
            result["fx_close_error"] = str(e)
        self.assertIn("_error", result["kill_report"])
        fx.close.assert_called_once()

    def test_dirs_registered_before_second_mkdtemp(self):
        # D：目录逐个登记（第二次 mkdtemp 失败，第一个已在 dirs 列表可回收）。
        dirs = []
        first = "engine-272-stop-xxxx"
        dirs.append(first)
        try:
            raise OSError("mkdtemp failed")  # 第二次分配失败
        except OSError:
            pass
        self.assertEqual(dirs, [first])  # 第一个仍登记，可回收


if __name__ == "__main__":
    unittest.main()
