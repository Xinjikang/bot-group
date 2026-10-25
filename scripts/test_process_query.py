"""Process-query races, diagnostics, argument identity and native Windows probes."""
import ast
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from engine import process_query as query, wake


def process(pid=123, name='python.exe', argv=None):
    result = Mock(pid=pid)
    result.name.return_value = name
    result.cmdline.return_value = argv if argv is not None else ['python.exe', 'hermes', '-p', 'dev', 'chat']
    result.is_running.return_value = True
    return result


@contextmanager
def snapshot(*processes):
    by_pid = {p.pid: p for p in processes}
    with patch.object(query, '_candidate_pids', return_value=list(by_pid)), \
         patch.object(query.psutil, 'Process', side_effect=by_pid.__getitem__):
        yield


class ProcessQueryTests(unittest.TestCase):
    def setUp(self):
        query._failures.clear()
        self.addCleanup(query._failures.clear)

    def test_reads_unicode_arguments_without_shell(self):
        argv = ['C:\\程序 文件\\python.exe', 'hermes', '-p', '开发 者', 'chat', '-c', '会话 1']
        p = process(argv=argv)
        with patch.object(query.psutil, 'Process', return_value=p), \
             patch('subprocess.run', side_effect=AssertionError('shell query')):
            self.assertEqual(query.command_line(123, scope='dev'), argv)

    def test_exited_pid_is_absent(self):
        with patch.object(query.psutil, 'Process', side_effect=query.psutil.NoSuchProcess(123)):
            self.assertEqual(query.command_line(123, scope='dev'), [])

    def test_candidate_exiting_during_read_is_skipped(self):
        p = process()
        p.cmdline.side_effect = query.psutil.NoSuchProcess(123)
        with snapshot(p):
            self.assertEqual(query.list_processes({'python.exe'}, scope='dev'), [])

    def test_reused_pid_during_read_is_absent(self):
        p = process()
        p.is_running.return_value = False
        with patch.object(query.psutil, 'Process', return_value=p):
            self.assertEqual(query.command_line(123, scope='dev'), [])

    def test_denied_candidate_makes_partial_snapshot_unknown(self):
        denied = process(pid=456)
        denied.cmdline.side_effect = query.psutil.AccessDenied(456)
        with snapshot(process(), denied):
            self.assertIsNone(query.list_processes({'python.exe'}, scope='dev'))

    def test_unrelated_process_arguments_are_never_read(self):
        unrelated = process(name='chrome.exe')
        unrelated.cmdline.side_effect = query.psutil.AccessDenied(123)
        with snapshot(unrelated):
            self.assertEqual(query.list_processes({'python.exe'}, scope='dev'), [])
        unrelated.cmdline.assert_not_called()

    def test_enumeration_failure_is_unknown(self):
        with patch.object(query, '_candidate_pids', side_effect=OSError(5, 'private text')):
            self.assertIsNone(query.list_processes({'python.exe'}, scope='dev'))

    def test_pid_reused_between_scans_reads_new_process_name(self):
        old = process(name='chrome.exe')
        new = process()
        with patch.object(query, '_candidate_pids', return_value=[123]), \
             patch.object(query.psutil, 'Process', side_effect=[old, new]):
            self.assertEqual(query.list_processes({'python.exe'}, scope='dev'), [])
            self.assertEqual(query.list_processes({'python.exe'}, scope='dev'), [(123, new.cmdline.return_value)])

    def test_pid_exits_before_process_object_is_constructed(self):
        with patch.object(query, '_candidate_pids', return_value=[123]), \
             patch.object(query.psutil, 'Process', side_effect=query.psutil.NoSuchProcess(123)):
            self.assertEqual(query.list_processes({'python.exe'}, scope='dev'), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows Toolhelp snapshot')
    def test_native_names_filter_before_opening_processes(self):
        with patch('engine.process_tree.snapshot', return_value={4: (0, 'system'), 123: (1, 'python.exe')}), \
             patch.object(query.psutil, 'Process', return_value=process()) as opened:
            self.assertEqual(len(query.list_processes({'python.exe'}, scope='dev')), 1)
            opened.assert_called_once_with(123)

    def test_empty_live_command_is_unknown(self):
        with patch.object(query.psutil, 'Process', return_value=process(argv=[])):
            self.assertIsNone(query.command_line(123, scope='dev'))

    def test_failure_logs_are_limited_and_recovery_is_recorded(self):
        with patch.object(query.time, 'monotonic', side_effect=[0, 1, 61]), \
             self.assertLogs(query._log, level='INFO') as logs:
            for _ in range(3):
                query.diagnostic('dev', 'enumerate', OSError(5, 'secret-token'))
            query.diagnostic('dev', 'enumerate')
            query.diagnostic('dev', 'enumerate')
        self.assertEqual(len(logs.output), 3)
        self.assertIn('recovered', logs.output[-1])
        self.assertNotIn('secret-token', '\n'.join(logs.output))
        self.assertIn('errno=5', logs.output[0])

    def test_other_pid_success_does_not_reset_failure_limit(self):
        denied = process(pid=456)
        denied.cmdline.side_effect = query.psutil.AccessDenied(456)
        with snapshot(process(), denied), self.assertLogs(query._log, level='WARNING') as logs:
            for _ in range(2):
                self.assertIsNone(query.command_line(456, scope='dev'))
                self.assertTrue(query.command_line(123, scope='dev'))
        self.assertEqual(len(logs.output), 1)

    def test_exact_profile_session_and_prompt_boundary(self):
        records = [
            (1, ['python.exe', 'hermes', '-p', '开发 者', 'chat', '-c', '会话 1']),
            (2, ['python.exe', 'hermes', '-p', '开发 者2', 'chat', '-c', '会话 1']),
            (3, ['python.exe', 'hermes', '-p', 'other', 'chat', '-q', '-p', '开发 者']),
        ]
        with patch.object(wake, '_find_codex_processes', return_value=False), \
             patch.object(query, 'list_processes', return_value=records):
            self.assertEqual(wake.find_bot_processes('开发 者', '会话 1'), [1])

    def test_unknown_query_keeps_session_busy(self):
        with patch.object(wake, '_find_codex_processes', return_value=False), \
             patch.object(query, 'list_processes', return_value=None):
            self.assertTrue(wake.session_busy('isolated-probe', 'work', None, 'test'))
        with patch.object(wake, '_find_codex_processes', return_value=False), \
             patch.object(query, 'list_processes', return_value=[]):
            self.assertFalse(wake.session_busy('isolated-probe', 'work', None, 'test'))

    def test_long_option_equals_form(self):
        argv = ['hermes', '--profile=开发 者', 'chat', '--continue=会话 1']
        self.assertEqual(query.option(argv, '-p', '--profile'), '开发 者')
        self.assertEqual(query.option(argv, '-c', '--continue'), '会话 1')

    def test_mcp_parent_identity_uses_shared_query(self):
        source = (Path(__file__).resolve().parents[1] / 'mcp/server.py').read_text(encoding='utf-8')
        node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == '_parent_cmdline')
        scope = dict(os=os, subprocess=subprocess, process_query=query, _profile=lambda: 'dev')
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<parent-query>', 'exec'), scope)
        with patch.object(query, 'command_line', return_value=['hermes', '-p', '开发 者', 'chat', '-c', 'dev-p-work']) as read:
            self.assertEqual(scope['_parent_cmdline'](), 'hermes -p "开发 者" chat -c dev-p-work')
        read.assert_called_once_with(os.getppid(), scope='mcp:dev')

    @unittest.skipUnless(os.name == 'nt', 'Windows native process query')
    def test_native_child_is_found_without_wmic_and_disappears_after_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'hermes.py'
            script.write_text("print('ready', flush=True)\ninput()\n", encoding='utf-8')
            argv = [sys.executable, str(script), '-p', 'probe-中文 空格', 'chat', '-c', 'probe-p-work']
            with subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True, env={**os.environ, 'PATH': ''},
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as child:
                try:
                    self.assertEqual(child.stdout.readline().strip(), 'ready')
                    with patch('subprocess.run', side_effect=AssertionError('WMIC unavailable')), \
                         patch.object(wake, '_find_codex_processes', return_value=False):
                        self.assertEqual(query.command_line(child.pid, scope='native-probe'), argv)
                        self.assertIn(child.pid, wake.find_bot_processes('probe-中文 空格', 'probe-p-work'))
                    child.communicate('\n', timeout=5)
                    self.assertEqual(query.command_line(child.pid, scope='native-probe'), [])
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
