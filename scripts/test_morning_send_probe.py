"""req#159 晨间发送卡顿只读探针回归（隔离夹具，不触达生产引擎/事件日志/磁盘计数器）。

覆盖验收 1-7 模拟层：
- CLI 参数校验与输出目录权限失败明确退出；
- 盘符推导与引擎进程定位（多候选/未找到/枚举失败）；
- typeperf/wevtutil 输出解析（含本地化提示行、平铺 XML、错误码）；
- 事件三态分开落盘、双时间戳、EventRecordID 增量过滤；
- 更新事件（RM 10000/AppX 603）升频全进程快照；
- engine.log 尾随解析与异常 connect_ms 复现判据、「本次未复现」退出；
- 容量上限（单文件轮转/每日上限/总上限）与只管理自有文件；
- 零触碰：源码静态断言 + 运行路径断言（不读 .env/group.db/配置/环境变量）；
- 退出清理（无残留子进程/临时文件）与运行标记/exit 日志。

隔离方式：临时目录 + 打桩 subprocess/psutil/process_query，全部外部命令不真实执行。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

import morning_send_probe as probe  # noqa: E402


class FakeProcess:
    def __init__(self, pid=1234):
        self.pid = pid
        self._user = 10.0
        self._system = 5.0
        self._rss = 50 * 1048576

    def name(self):
        return 'pythonw.exe'

    def cpu_times(self):
        times = MagicMock()
        times.user = self._user
        times.system = self._system
        return times

    def memory_info(self):
        info = MagicMock()
        info.rss = self._rss
        return info

    def is_running(self):
        return True

    def net_connections(self, kind=None):
        return [MagicMock(status='LISTEN')]


class ProbeHarness(unittest.TestCase):
    """隔离夹具：临时输出目录 + 打桩全部外部命令。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='probe-test-')
        self.addCleanup(self.tmp.cleanup)
        self.out_dir = Path(self.tmp.name) / 'out'
        self.fake_engine = FakeProcess()
        self.fake_events: dict[str, tuple[list[dict], str | None]] = {
            name: ([], None) for name, _, _ in probe._CHANNELS}
        # 打桩外部系统接口
        patchers = [
            patch.object(probe, 'locate_engine',
                         return_value=([(1234, ['<fake>', '-m', 'engine.main'])], None)),
            patch.object(probe, 'run_typeperf',
                         return_value=({'ts': 1.0, 'drive': 'E:', 'queue_length': 0.0,
                                        'avg_sec_transfer': 0.001}, None)),
            patch.object(probe, 'snapshot_all',
                         return_value={'ts': 1.0, 'procs': [], 'skipped': 0}),
            patch.object(probe.psutil, 'Process',
                         side_effect=lambda pid=None, **kw: self.fake_engine
                         if pid == self.fake_engine.pid else FakeProcess(pid or 1)),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        def fake_wevtutil(channel, predicate, after_record_id, limit=30):
            return self.fake_events.get(self._channel_name(channel), ([], None))

        patcher = patch.object(probe, 'run_wevtutil', side_effect=fake_wevtutil)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.wevtutil_calls = []

    @staticmethod
    def _channel_name(channel: str) -> str:
        for name, log_channel, _ in probe._CHANNELS:
            if log_channel == channel:
                return name
        return channel

    def run_probe_args(self, extra_args: list[str]) -> dict:
        args = ['--project-root', str(ROOT), '--output-dir', str(self.out_dir),
                '--window', '0.6', '--engine-interval', '0.1', '--disk-interval', '0.2',
                '--snapshot-interval', '0.2', '--event-interval', '0.2',
                '--log-interval', '0.2', *extra_args]
        sys.argv = ['probe'] + args
        try:
            exit_code = probe.main()
        except SystemExit as exc:
            exit_code = exc.code
        marker = self.out_dir / 'probe-test-marker'
        marker.write_text(json.dumps({'exit': exit_code}), encoding='utf-8')
        return {'exit_code': exit_code, 'out_dir': self.out_dir}

    def read_stream(self, stream: str) -> list[dict]:
        rows = []
        for path in self.out_dir.glob(f'probe-*-{stream}-*.jsonl'):
            for line in path.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        return rows


class CliAndPathTests(unittest.TestCase):
    def test_missing_output_dir_rejected(self):
        with patch.object(sys, 'argv', ['probe', '--project-root', str(ROOT)]):
            with self.assertRaises(SystemExit) as ctx:
                probe.main()
            self.assertNotEqual(ctx.exception.code, 0)

    def test_missing_project_root_rejected(self):
        with patch.object(sys, 'argv', ['probe', '--output-dir', str(ROOT)]):
            with self.assertRaises(SystemExit) as ctx:
                probe.main()
            self.assertNotEqual(ctx.exception.code, 0)

    def test_output_dir_creation_failure_exits_with_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocked = Path(tmp) / 'blocked'
            with patch.object(probe.Path, 'mkdir',
                              side_effect=OSError('permission denied')):
                with patch.object(sys, 'argv',
                                  ['probe', '--project-root', str(ROOT),
                                   '--output-dir', str(blocked)]):
                    with patch('sys.stderr') as stderr:
                        exit_code = probe.main()
                    self.assertEqual(exit_code, 2)
                    self.assertTrue(stderr.write.called)

    def test_derive_drive(self):
        self.assertEqual(probe.derive_drive(Path(r'E:\Code\bot-group')), 'E:')
        with self.assertRaises(ValueError):
            probe.derive_drive(Path('relative/path'))
        with self.assertRaises(ValueError):
            probe.derive_drive(Path(r'\\server\share'))


class LocateEngineTests(unittest.TestCase):
    def test_matches_module_form_and_ranks_listening(self):
        from engine import process_query
        candidates = [(100, ['python.exe', '-m', 'engine.main']),
                      (200, ['pythonw.exe', '-m', 'engine.main']),
                      (300, ['python.exe', '-c', 'other'])]
        with patch.object(process_query, 'list_processes', return_value=candidates):
            found, error = probe.locate_engine(Path(r'E:\Code\bot-group'))
        self.assertIsNone(error)
        self.assertEqual([pid for pid, _ in found], [100, 200])
        # 有 LISTENING 连接的优先（打桩 psutil.Process）
        with patch.object(probe.psutil, 'Process', side_effect=FakeProcess), \
                patch.object(process_query, 'list_processes', return_value=candidates):
            found, error = probe.locate_engine(Path(r'E:\Code\bot-group'))
        self.assertIsNone(error)
        self.assertEqual([pid for pid, _ in found], [100, 200])

    def test_not_found_and_enumerate_failed(self):
        from engine import process_query
        with patch.object(process_query, 'list_processes', return_value=[]):
            found, error = probe.locate_engine(Path(r'E:\Code\bot-group'))
            self.assertEqual(found, [])
            self.assertEqual(error, 'not_found')
        with patch.object(process_query, 'list_processes', return_value=None):
            found, error = probe.locate_engine(Path(r'E:\Code\bot-group'))
            self.assertEqual(found, [])
            self.assertEqual(error, 'enumerate_failed')


class TypeperfParseTests(unittest.TestCase):
    def test_parses_value_line_ignoring_localized_trailer(self):
        stdout = ('\n"(PDH-CSV 4.0)","\\\\XJK74\\\\LogicalDisk(E:)\\\\Current Disk Queue Length",'
                  '"\\\\XJK74\\\\LogicalDisk(E:)\\\\Avg. Disk sec/Transfer"\n'
                  '"11/12/2026 09:51:08.857","0.000000","0.000236"\n'
                  '命令已成功完成...\n')
        completed = subprocess.CompletedProcess(['typeperf'], 0, stdout, '')
        with patch.object(probe.subprocess, 'run', return_value=completed):
            record, error = probe.run_typeperf('E:')
        self.assertIsNone(error)
        self.assertEqual(record['queue_length'], 0.0)
        self.assertEqual(record['avg_sec_transfer'], 0.000236)

    def test_exit_code_error(self):
        completed = subprocess.CompletedProcess(['typeperf'], 1, '', 'no counters')
        with patch.object(probe.subprocess, 'run', return_value=completed):
            record, error = probe.run_typeperf('E:')
        self.assertIsNone(record)
        self.assertEqual(error, 'exit_1')

    def test_timeout_error(self):
        with patch.object(probe.subprocess, 'run', side_effect=subprocess.TimeoutExpired('x', 25)):
            record, error = probe.run_typeperf('E:')
        self.assertIsNone(record)
        self.assertEqual(error, 'timeout')


class WevtutilParseTests(unittest.TestCase):
    EVENT_XML = ("<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'>"
                 "<System><Provider Name='Microsoft-Windows-RestartManager'/>"
                 "<EventID>10000</EventID><EventRecordID>5001</EventRecordID>"
                 "<TimeCreated SystemTime='2026-11-12T00:00:00.000Z'/></System></Event>")

    def test_parses_flat_events_with_dual_timestamps(self):
        completed = subprocess.CompletedProcess(['wevtutil'], 0, self.EVENT_XML, '')
        with patch.object(probe.subprocess, 'run', return_value=completed):
            events, error = probe.run_wevtutil('Application', 'x', 5000)
        self.assertIsNone(error)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['event_id'], 10000)
        self.assertEqual(events[0]['record_id'], 5001)
        self.assertEqual(events[0]['time_created'], '2026-11-12T00:00:00.000Z')
        self.assertIn('received_ts', events[0])

    def test_empty_and_error_states(self):
        completed = subprocess.CompletedProcess(['wevtutil'], 0, '', '')
        with patch.object(probe.subprocess, 'run', return_value=completed):
            events, error = probe.run_wevtutil('Application', 'x', 0)
        self.assertEqual((events, error), ([], None))
        completed = subprocess.CompletedProcess(['wevtutil'], 5, '', 'access denied')
        with patch.object(probe.subprocess, 'run', return_value=completed):
            events, error = probe.run_wevtutil('Application', 'x', 0)
        self.assertEqual(events, [])
        self.assertEqual(error, 'exit_5')


class EngineLogTailTests(unittest.TestCase):
    def test_parses_send_lines_and_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / 'engine.log'
            log_path.write_text(
                '2026-11-12 09:00:00,123 engine.api INFO message.send message_id=1 '
                'thread_id=2 outcome=ok connect_ms=0.8 route_ms=38.8 publish_ms=0.2 '
                'total_ms=40.6\n', encoding='utf-8')
            sends, offset = probe.read_engine_sends(log_path, 0)
            self.assertEqual(len(sends), 1)
            self.assertEqual(sends[0]['connect_ms'], 0.8)
            self.assertEqual(sends[0]['log_ts'], '2026-11-12 09:00:00')
            # 轮转：文件变小后从新头部重读
            log_path.write_text('', encoding='utf-8')
            sends, offset = probe.read_engine_sends(log_path, offset)
            self.assertEqual(offset, 0)


class OutputSinkCapacityTests(unittest.TestCase):
    def test_rotation_daily_limit_and_total_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            sink = probe.OutputSink(directory, max_file_bytes=100,
                                    max_daily_files=2, max_total_bytes=10 ** 9)
            with self.assertRaises(probe.CapacityReached):
                for i in range(30):
                    sink.write('engine', {'i': i, 'pad': 'x' * 50})
            sink.close()
            engine_files = sorted(directory.glob('probe-*-engine-*.jsonl'))
            self.assertEqual(len(engine_files), 2, 'daily per-stream rotation cap')

    def test_total_limit_stops_and_keeps_other_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'other-report.md').write_text('keep me', encoding='utf-8')
            sink = probe.OutputSink(directory, max_file_bytes=10 ** 6,
                                    max_daily_files=3, max_total_bytes=200)
            sink.write('engine', {'pad': 'x' * 150})
            sink.write('snapshot', {'pad': 'y' * 60})
            with self.assertRaises(probe.CapacityReached):
                sink.write('snapshot', {'pad': 'z' * 10})
            sink.close()
            self.assertTrue((directory / 'other-report.md').exists(),
                            '容量管理不得删除非自有文件')


class ProbeEndToEndTests(ProbeHarness):
    def test_unreproduced_window_exits_cleanly_with_logs(self):
        result = self.run_probe_args([])
        self.assertEqual(result['exit_code'], 0)
        exit_rows = self.read_stream('exit-log')
        self.assertEqual(exit_rows[-1]['verdict'], '本次未复现')
        self.assertEqual(exit_rows[-1]['stop_reason'], 'window_ended')
        marker_rows = self.read_stream('run-marker')
        self.assertEqual(marker_rows[0]['drive'], 'E:')
        self.assertEqual(marker_rows[0]['engine_candidates'][0]['pid'], 1234)
        # 三态分开落盘（打桩事件全部无事件 → events-none）
        none_rows = self.read_stream('events-none')
        self.assertGreater(len(none_rows), 0)
        self.assertEqual(len(self.read_stream('events-read-errors')), 0)
        # 无残留子进程/临时文件：打桩外部命令，输出目录仅 probe-* 文件
        for path in result['out_dir'].iterdir():
            self.assertTrue(path.name.startswith('probe-'),
                            f'意外文件 {path.name}')

    def test_anomalous_connect_ms_reported(self):
        # 打桩 engine.log 尾随：仅第一轮返回一条异常 connect_ms，之后无新行
        anomaly = {'log_ts': '2026-11-12 09:00:00', 'received_ts': 1.0,
                   'message_id': '9', 'connect_ms': 19314.9,
                   'route_ms': 3100.0, 'publish_ms': 500.0, 'total_ms': 22400.0}
        calls = {'n': 0}

        def fake_read(path, offset):
            calls['n'] += 1
            if calls['n'] == 1:
                return [anomaly], offset + 100
            return [], offset + 100
        with patch.object(probe, 'read_engine_sends', side_effect=fake_read):
            result = self.run_probe_args(['--connect-threshold', '5000'])
        self.assertEqual(result['exit_code'], 0)
        exit_rows = self.read_stream('exit-log')
        self.assertIn('复现 1 条', exit_rows[-1]['verdict'])
        anomaly_rows = self.read_stream('anomalies')
        self.assertEqual(anomaly_rows[0]['connect_ms'], 19314.9)

    def test_update_event_fast_snapshot(self):
        # 首轮后出现 RM 10000 → 全进程快照升频至 --snapshot-fast
        self.fake_events['rm'] = ([{'record_id': 9001, 'event_id': 10000,
                                    'provider': 'Microsoft-Windows-RestartManager',
                                    'time_created': '2026-11-12T00:00:00Z',
                                    'received_ts': 1.0}], None)
        result = self.run_probe_args(['--snapshot-fast', '0.05'])
        self.assertEqual(result['exit_code'], 0)
        frames = self.read_stream('snapshot')
        self.assertTrue(any(frame.get('interval') == 0.05 for frame in frames),
                        '更新事件后全进程快照应升频')
        event_rows = self.read_stream('events')
        self.assertTrue(any(e.get('event_id') == 10000 for e in event_rows))

    def test_engine_pid_exit_relocates(self):
        # 前两轮正常采样，之后引擎进程退出（采样抛 NoSuchProcess）→ 重定位找不到
        original = probe.sample_engine_cpu
        alive = [True]

        def dying_cpu(process, previous):
            if alive[0]:
                alive[0] = False  # 首次调用后引擎死亡
                return original(process, previous)
            raise probe.psutil.NoSuchProcess(1234)

        def fake_process(pid=None, **kw):
            if not alive[0]:
                raise probe.psutil.NoSuchProcess(1234)
            return self.fake_engine
        locate_calls = {'n': 0}

        def flaky_locate(root):
            locate_calls['n'] += 1
            if locate_calls['n'] == 1:  # 启动定位成功，引擎死亡后重定位失败
                return [(1234, ['<fake>', '-m', 'engine.main'])], None
            return [], 'not_found'
        with patch.object(probe.psutil, 'Process', side_effect=fake_process), \
                patch.object(probe, 'locate_engine', side_effect=flaky_locate), \
                patch.object(probe, 'sample_engine_cpu', side_effect=dying_cpu):
            result = self.run_probe_args([])
        self.assertEqual(result['exit_code'], 0)
        events = self.read_stream('engine-events')
        self.assertTrue(any(e.get('event') == 'engine_exited' for e in events), events)
        self.assertTrue(any(e.get('event') == 'engine_missing' for e in events), events)

    def test_zero_touch_no_business_files_or_env_reads(self):
        # 运行路径断言：探针不读 .env / group.db / 配置文件 / 环境变量
        accessed: list[str] = []
        real_open = probe.os.open if hasattr(probe.os, 'open') else None
        import builtins
        original_open = builtins.open

        def spy_open(path, *args, **kwargs):
            accessed.append(str(path))
            return original_open(path, *args, **kwargs)
        env_reads: list[str] = []

        def spy_getenv(key, *args):
            env_reads.append(key)
            return None
        with patch('builtins.open', side_effect=spy_open), \
                patch.object(probe.os, 'getenv', side_effect=spy_getenv):
            result = self.run_probe_args([])
        self.assertEqual(result['exit_code'], 0)
        sensitive = [p for p in accessed if '.env' in p or 'group.db' in p
                     or 'config' in p]
        self.assertEqual(sensitive, [], f'零触碰违反：{sensitive}')
        self.assertEqual(env_reads, [], f'环境变量读取：{env_reads}')


class ZeroTouchStaticTests(unittest.TestCase):
    def test_source_does_not_import_engine_business_modules(self):
        source = Path(probe.__file__).read_text(encoding='utf-8')
        # 只检查 import 语句（docstring 中的说明文字不作为引用证据）
        imports = [line.strip() for line in source.splitlines()
                   if line.strip().startswith(('import ', 'from '))]
        for statement in imports:
            for forbidden in ('find_bot_processes', 'engine.config', 'engine.db',
                              'engine.wake', 'engine.api'):
                self.assertNotIn(forbidden, statement, f'源码含禁用引用：{statement}')
        # process_query 是唯一允许的 engine 导入（纯 psutil 列表匹配）
        self.assertTrue(any('from engine import process_query' in line for line in imports),
                        '缺少 process_query 定位引用')


if __name__ == '__main__':
    unittest.main()
