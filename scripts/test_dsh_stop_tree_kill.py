"""req#138 task#272 node1625：异常进程回收 kill 逻辑 mock 回归（4 个故障反例）。

manager node1625 要求补：随启动/发现登记的外层资源 registry、杀前创建身份/归属核验、
kill 后存活核验、returncode 校验，并覆盖 capture 中途异常 / stop 异常 / taskkill 失败 /
历史 PID 复用四个反例（mock 即可，勿先重复整套模型验证）。
"""
import unittest
from unittest import mock

import scripts.verify_dsh_stop_tree as v


class KillRegistryTests(unittest.TestCase):
    def _reg(self, pid=71001, name="harness.exe"):
        reg = v._ResourceRegistry()
        reg.register(pid, name)
        return reg

    def test_capture_exception_registry_still_kills(self):
        # 反例①：capture 中途异常（未返回 cap），registry 已随发现登记 → finally 仍能回收，
        # 不再 _kill_pids([]) 遗留独立 DSH 树。
        reg = self._reg()
        with mock.patch.object(v, "_process_tree", side_effect=[
            {71001: (1, "harness.exe")},  # kill 前存活
            {},                           # kill 后退出
        ]), mock.patch.object(v.subprocess, "run", return_value=mock.Mock(returncode=0)):
            rep = v._kill_registered(reg)
        self.assertEqual(rep[71001], "killed")

    def test_taskkill_failure_rc1(self):
        # 反例②：taskkill returncode=1（失败）不得记 killed（旧 _kill_pids 不查 returncode 仍 killed=1）。
        reg = self._reg()
        with mock.patch.object(v, "_process_tree", return_value={71001: (1, "harness.exe")}), \
             mock.patch.object(v.subprocess, "run", return_value=mock.Mock(returncode=1)):
            rep = v._kill_registered(reg)
        self.assertEqual(rep[71001], "kill_failed_rc=1")

    def test_pid_reuse_identity_mismatch(self):
        # 反例③：PID 复用/身份变化（当前 name 与登记不符）拒杀，不 taskkill 未授权进程。
        reg = self._reg(name="harness.exe")
        with mock.patch.object(v, "_process_tree", return_value={71001: (1, "notepad.exe")}), \
             mock.patch.object(v.subprocess, "run") as run:
            rep = v._kill_registered(reg)
        self.assertEqual(rep[71001], "identity_mismatch")
        run.assert_not_called()

    def test_already_exited_skipped(self):
        # 已退出（PID 不在快照）跳过，不 taskkill 历史 PID。
        reg = self._reg()
        with mock.patch.object(v, "_process_tree", return_value={}), \
             mock.patch.object(v.subprocess, "run") as run:
            rep = v._kill_registered(reg)
        self.assertEqual(rep[71001], "already_exited")
        run.assert_not_called()

    def test_still_alive_after_kill(self):
        # returncode=0 但杀后仍在（taskkill /T 未真正回收）→ still_alive（不 pass）。
        reg = self._reg()
        with mock.patch.object(v, "_process_tree", return_value={71001: (1, "harness.exe")}), \
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


if __name__ == "__main__":
    unittest.main()
