"""Only synthetic child trees are terminated; no production PID discovery."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import process_tree


class TreeTests(unittest.TestCase):
    def test_old_launcher_walk_includes_grandchildren_and_protects_engine(self):
        graph = {10: (1, 'pythonw.exe'), 20: (10, 'nohup.exe'), 30: (20, 'pythonw.exe'),
                 40: (30, 'python.exe'), 50: (40, 'mcp.exe'), 60: (10, 'python.exe')}
        roots, descendants = process_tree.plan({40}, graph, 10)
        self.assertEqual(roots, {20})
        self.assertEqual(descendants, {20, 30, 40, 50})
        self.assertNotIn(60, descendants)

    def test_recorded_launcher_bounds_walk_and_stale_launcher_falls_back(self):
        graph = {10: (1, 'pythonw.exe'), 20: (10, 'pythonw.exe'), 30: (20, 'python.exe'),
                 40: (30, 'mcp.exe')}
        self.assertEqual(process_tree.plan({30}, graph, 10, {30: 20})[0], {20})
        self.assertEqual(process_tree.plan({30}, graph, 10, {30: 999})[0], {30})
        with self.assertRaises(RuntimeError):
            process_tree.plan({10}, graph, 10)

    def test_failed_tree_kill_is_reported(self):
        with patch.object(process_tree, 'snapshot', return_value={12345: (1, 'python.exe')}), \
             patch.object(process_tree, '_pid_alive', return_value=True), \
             patch('engine.stop._kill_process_tree', return_value=False):
            with self.assertRaisesRegex(RuntimeError, '进程树失败'):
                process_tree.terminate({12345})

    @unittest.skipUnless(os.name == 'nt', 'Windows native process tree')
    def test_real_three_level_tree_and_unrelated_sibling(self):
        with tempfile.TemporaryDirectory(prefix='bot-tree-test-') as tmp:
            command = [sys.executable, str(Path(__file__).resolve()), '--child', tmp]
            options = {'creationflags': subprocess.CREATE_NO_WINDOW}
            root = subprocess.Popen(command + ['2'], **options)
            sibling = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], **options)
            try:
                deadline = time.monotonic() + 10
                while not Path(tmp, '0.pid').exists():
                    if time.monotonic() > deadline:
                        self.fail('synthetic grandchild did not start')
                    time.sleep(.05)
                pids = {i: int(Path(tmp, f'{i}.pid').read_text()) for i in range(3)}
                process_tree.terminate({pids[1]}, launchers={pids[1]: root.pid})
                root.wait(timeout=5)
                self.assertTrue(all(not process_tree._pid_alive(pid) for pid in pids.values()))
                self.assertIsNone(sibling.poll())
            finally:
                from engine.stop import _kill_process_tree
                if root.poll() is None:
                    _kill_process_tree(root.pid)
                    root.wait(timeout=5)
                sibling.terminate()
                sibling.wait(timeout=5)

    @unittest.skipUnless(os.name == 'nt', 'Windows native shutdown integration')
    def test_engine_timeout_kills_execution_then_closes_registered_harness(self):
        from engine import browser_resources, db, wake
        from engine.lifecycle import Shutdown
        from scripts.test_browser_resources import scope, launch_harness
        from scripts.unified_test_support import isolated_database
        with isolated_database():
            env, session, started = scope(active=True)
            harness, endpoint = launch_harness(self, env)
            proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                conn = db.get_connection()
                conn.execute("UPDATE wake_runs SET pid=?,runtime='codex' WHERE session=?", (proc.pid, session))
                conn.commit()
                conn.close()
                exited = Mock()
                controller = Shutdown(threading.Event(), exited, timeout=0)
                with patch.dict(wake._native_procs, {session: (proc, started)}, clear=True), \
                     patch.object(wake, '_running_wakes', {}):
                    controller.begin()
                    controller.worker.join(20)
                self.assertFalse(controller.worker.is_alive())
                self.assertEqual(controller.snapshot()['state'], 'stopping', controller.snapshot())
                exited.assert_called_once_with()
                proc.wait(timeout=5)
                self.assertEqual(harness.wait(timeout=5), 0)
                self.assertFalse(endpoint.exists())
                conn = db.get_connection()
                try:
                    self.assertEqual(conn.execute('SELECT count(*) FROM browser_resources').fetchone()[0], 0)
                    self.assertIsNotNone(conn.execute('SELECT ended_at FROM wake_runs WHERE session=?',
                                                     (session,)).fetchone()[0])
                finally:
                    conn.close()
            finally:
                wake._shutting_down.clear()
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)
                if harness.poll() is None:
                    import json
                    browser_resources._request(endpoint, json.loads(endpoint.read_text()), 'shutdown')
                    harness.wait(timeout=5)


if __name__ == '__main__':
    if '--child' in sys.argv:
        directory, depth = sys.argv[-2], int(sys.argv[-1])
        Path(directory, f'{depth}.pid').write_text(str(os.getpid()))
        if depth:
            child = subprocess.Popen([sys.executable, __file__, '--child', directory, str(depth - 1)],
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(60)
    else:
        unittest.main()
