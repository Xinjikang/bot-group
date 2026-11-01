"""req#138 task#272 node1642：异常进程回收控制流回归（身份语义 + 补偿定位 + 清理门禁）。

manager node1642 固定断言：A 不存在（NoSuchProcess）与查询失败（AccessDenied/OSError）分开，
仅 NoSuchProcess 可报已退出，其余 unknown + 总失败；登记缺稳定身份拒杀。B 首次根发现前异常
（live_run 查询失败）标记 DSH 树未定位、总失败。C fx_close 异常必须使总 passed=false。测试
实际调用 _kill_registered / _start_and_capture / _run_segment 注入故障，不复制被测逻辑。
"""
import unittest
from unittest import mock
import tempfile

import scripts.verify_dsh_stop_tree as v


def _ident(name="harness.exe", ct=1000):
    return ("ok", name, ct)


class KillRegistryTests(unittest.TestCase):
    def _reg(self, pid=71001, name="harness.exe", ct=1000, state="ok"):
        reg = v._ResourceRegistry()
        if state == "ok":
            with mock.patch.object(v, "_process_identity", return_value=("ok", name, ct)):
                reg.register(pid)
        elif state == "unknown":
            with mock.patch.object(v, "_process_identity", return_value=("unknown",)):
                reg.register(pid)
        return reg

    def test_same_name_pid_reuse_create_time_mismatch(self):
        # 同名 PID 复用（create_time 不同）拒杀，不 taskkill——name 不能作创建身份。
        reg = self._reg(name="node.exe", ct=1000)
        with mock.patch.object(v, "_process_identity", return_value=_ident("node.exe", 2000)), \
             mock.patch.object(v.subprocess, "run") as run:
            rep = v._kill_registered(reg)
        self.assertEqual(rep[71001], "identity_mismatch")
        run.assert_not_called()

    def test_name_change_identity_mismatch(self):
        reg = self._reg(name="harness.exe", ct=1000)
        with mock.patch.object(v, "_process_identity", return_value=_ident("notepad.exe", 1000)), \
             mock.patch.object(v.subprocess, "run") as run:
            rep = v._kill_registered(reg)
        self.assertEqual(rep[71001], "identity_mismatch")
        run.assert_not_called()

    def test_access_denied_is_unknown_not_exited(self):
        # A：AccessDenied/OSError（查询失败）→ identity_unknown（总失败），不误报 already_exited。
        reg = self._reg()
        with mock.patch.object(v, "_process_identity", return_value=("unknown",)), \
             mock.patch.object(v.subprocess, "run") as run:
            rep = v._kill_registered(reg)
        self.assertEqual(rep[71001], "identity_unknown")
        run.assert_not_called()

    def test_unknown_registration_refuses_kill(self):
        # A：登记时身份不可读（known=False），后续任意可读身份也不 taskkill，拒杀并失败。
        reg = self._reg(state="unknown")
        with mock.patch.object(v, "_process_identity", return_value=_ident("node.exe", 2000)), \
             mock.patch.object(v.subprocess, "run") as run:
            rep = v._kill_registered(reg)
        self.assertEqual(rep[71001], "identity_unknown")
        run.assert_not_called()

    def test_killed_after_success(self):
        reg = self._reg()
        with mock.patch.object(v, "_process_identity", side_effect=[
            _ident("harness.exe", 1000),  # kill 前身份核验
            ("exited",),                   # kill 后退出
        ]), mock.patch.object(v.subprocess, "run", return_value=mock.Mock(returncode=0)):
            rep = v._kill_registered(reg)
        self.assertEqual(rep[71001], "killed")

    def test_taskkill_failure_rc1(self):
        reg = self._reg()
        with mock.patch.object(v, "_process_identity", return_value=_ident()), \
             mock.patch.object(v.subprocess, "run", return_value=mock.Mock(returncode=1)):
            rep = v._kill_registered(reg)
        self.assertEqual(rep[71001], "kill_failed_rc=1")

    def test_already_exited_skipped(self):
        reg = self._reg()
        with mock.patch.object(v, "_process_identity", return_value=("exited",)), \
             mock.patch.object(v.subprocess, "run") as run:
            rep = v._kill_registered(reg)
        self.assertEqual(rep[71001], "already_exited")
        run.assert_not_called()

    def test_still_alive_after_kill(self):
        reg = self._reg()
        with mock.patch.object(v, "_process_identity", return_value=_ident()), \
             mock.patch.object(v.subprocess, "run", return_value=mock.Mock(returncode=0)):
            rep = v._kill_registered(reg)
        self.assertEqual(rep[71001], "still_alive")

    def test_kill_ok_false_on_failure(self):
        # kill_ok：identity_unknown/identity_mismatch/kill_failed/still_alive 均不算通过。
        ok = lambda s: all(x in ("killed", "already_exited") for x in s)
        self.assertFalse(ok(["kill_failed_rc=1"]))
        self.assertFalse(ok(["identity_mismatch"]))
        self.assertFalse(ok(["identity_unknown"]))
        self.assertFalse(ok(["still_alive"]))
        self.assertTrue(ok(["killed", "already_exited"]))


class StartAndCaptureTests(unittest.TestCase):
    def _fx(self):
        fx = mock.Mock()
        fx.post_msg.return_value = {}
        fx.live_run.side_effect = RuntimeError("db query failed")
        fx.engine.proc.pid = 9999
        return fx

    def test_live_run_exception_compensates_dsh_descendants(self):
        # B：补偿回收——反查到引擎后代（DSH 树）则登记 + 回收，不标 dsh_not_located。
        fx = self._fx()
        reg = v._ResourceRegistry()
        with mock.patch.object(v, "_compensate_dsh_descendants", return_value=True) as comp:
            cap = v._start_and_capture(fx, mock.Mock(), reg)
        self.assertFalse(cap["ok"])
        self.assertFalse(reg.dsh_not_located)  # 补偿定位成功，不标未回收
        comp.assert_called_once_with(fx, reg)

    def test_live_run_exception_no_dsh_sets_not_located(self):
        # B：补偿回收——反查不到 DSH 树（引擎已退出）则标 dsh_not_located 总失败，不假报回收。
        fx = self._fx()
        reg = v._ResourceRegistry()
        with mock.patch.object(v, "_compensate_dsh_descendants", return_value=False):
            cap = v._start_and_capture(fx, mock.Mock(), reg)
        self.assertFalse(cap["ok"])
        self.assertTrue(reg.dsh_not_located)

    def test_compensate_dsh_descendants_registers_engine_children(self):
        # 实际调用 _compensate_dsh_descendants：引擎后代（DSH 树）登记进 registry。
        fx = mock.Mock()
        fx.engine.proc.pid = 100
        reg = v._ResourceRegistry()
        snap = {100: (1, "python.exe"), 200: (100, "cmd.exe"), 300: (200, "harness.exe")}
        with mock.patch.object(v, "_process_tree", return_value=snap), \
             mock.patch.object(v, "_process_identity", side_effect=[
                 ("ok", "cmd.exe", 200), ("ok", "harness.exe", 300)]):
            located = v._compensate_dsh_descendants(fx, reg)
        self.assertTrue(located)
        self.assertEqual(sorted(reg.pids()), [200, 300])

    def test_compensate_dsh_descendants_no_engine_pid(self):
        fx = mock.Mock()
        fx.engine.proc.pid = None
        reg = v._ResourceRegistry()
        self.assertFalse(v._compensate_dsh_descendants(fx, reg))


class RunSegmentTests(unittest.TestCase):
    def test_fx_close_error_sets_passed_false(self):
        # C：实际调用 _run_segment，stop 成功后 fx.close 抛 OSError → passed=false。
        fake_fx = mock.Mock()
        fake_fx.engine.proc.pid = 9999
        fake_fx.close.side_effect = OSError("close failed")
        cap = {
            "ok": True,
            "run": {"session": "s1"},
            "live_roots": [9999],
            "root_identity_before": {9999: "harness.exe"},
            "finder_before": "resolved",
            "seen": {},
            "spawner_child_pid": None,
            "spawner_child_observed": False,
            "spawner_call_id": None,
        }
        stop_r = {"passed": True, "other_bot_alive": True}
        args = mock.Mock()
        args.test_directory = tempfile.mkdtemp()
        args.source_home = "E:/x"
        args.entry = "dsh.cmd"
        args.node = "node"
        args.host = "127.0.0.1"
        args.port = 18042
        with mock.patch.object(v, "EngineFixture", return_value=fake_fx), \
             mock.patch.object(v, "_copy_home"), \
             mock.patch.object(v, "_inject_spawner"), \
             mock.patch.object(v, "_process_identity", return_value=("ok", "python.exe", 5000)), \
             mock.patch.object(v, "_start_and_capture", return_value=cap), \
             mock.patch.object(v, "_stop_and_verify", return_value=stop_r), \
             mock.patch.object(v, "_stop_pass", return_value=True), \
             mock.patch.object(v, "_kill_registered", return_value={9999: "killed"}):
            r = v._run_segment(args, "stop")
        self.assertFalse(r["passed"])
        self.assertIn("fx_close_error", r)


if __name__ == "__main__":
    unittest.main()
