"""req#159 晨间发送卡顿只读探针（首选档：零触碰系统级关联证据采集）。

目标：在限定窗口内采集现场证据，为清晨首条消息取连接卡顿（现场 19.3s）建立
系统级关联线索。设计依据 docs/design/2026-11-12-morning-probe-design.md。

零触碰约束（首选档）：
- 不读环境变量、配置文件、.env、group.db；不调用 find_bot_processes（会查库）；
- 盘符从注入的项目主目录推导；引擎进程用 engine/process_query.py 的 psutil 列表
  匹配定位（名字 python/pythonw + `-m engine.main`，多候选时选有 LISTENING 连接、
  CPU 累计最大者，全部候选落盘）；
- 只写自有输出文件（probe- 前缀），容量轮转只管理这些文件。

采样项（周期默认值，均可参数化覆盖）：
- 引擎 PID 轻量指标 1s：Win32_Process UserModeTime/KernelModeTime 增量（psutil
  cpu_times 即 GetProcessTimes）+ RSS；PID 变化/退出时重新定位并落事件行；
- 磁盘计数器 10s：typeperf 采集 LogicalDisk(<盘符>) Current Disk Queue Length 与
  Avg. Disk sec/Transfer（单次 1 秒采样），失败按错误码落盘并降级；
- 全进程快照默认 30s：psutil 全进程 CPU 增量与 IO 字节率增量；检测到更新事件
  （RestartManager 10000 或 AppX 603）升频 1s，该类事件停止出现 5 分钟后恢复 30s；
- 事件短轮询 5s 四通道：RM 10000/10001/10002 与 WER=Application、AppX
  603/400/613=Microsoft-Windows-AppXDeploymentServer/Operational、Defender=
  Microsoft-Windows-Windows Defender/Operational；每条记事件发生时间与收到时间，
  三态（读到 / 读不到记错误码 / 查询成功无事件）分开落盘；
- 真实侧：尾随只读 <项目主目录>/logs/engine.log 的 message.send 行（connect_ms
  超阈值记异常）；窗口内无异常 connect_ms 记「本次未复现」正常退出，不延长窗口。

退出与清理：窗口结束正常退出；输出目录不可创建/无写权限明确报错退出；单文件
10MB 轮转、每日最多 3 个、目录总上限 100MB 达限保留证据停止采集；启动写运行
标记与启动时刻；正常/异常均落 exit 日志；typeperf/wevtutil 均为同步短生命周期
调用（超时强杀并回收），退出后无残留进程与临时文件。

范围外：不做定时任务、不改引擎业务代码、不重启生产引擎。

运行（示例，输出目录与项目主目录必须显式注入）：
PYTHONPATH= .venv/Scripts/python.exe -X utf8 -B scripts/morning_send_probe.py \
  --project-root E:\\Code\\bot-group --output-dir <输出目录> --window 600
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

# 引擎进程定位复用进程查询模块的 psutil 列表匹配（不查库、不读配置）。
# 该模块只依赖 psutil 与进程命令行，不触碰 .env/group.db。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psutil  # noqa: E402
from engine import process_query  # noqa: E402

log = logging.getLogger('morning-send-probe')

# 四事件通道查询定义：(通道名, 事件日志通道, XPath 谓词)
_CHANNELS = [
    ('rm', 'Application',
     "(Provider[@Name='Microsoft-Windows-RestartManager'] and "
     "(EventID=10000 or EventID=10001 or EventID=10002))"),
    ('wer', 'Application', "(Provider[@Name='Windows Error Reporting'])"),
    ('appx', 'Microsoft-Windows-AppXDeploymentServer/Operational',
     '(EventID=603 or EventID=400 or EventID=613)'),
    ('defender', 'Microsoft-Windows-Windows Defender/Operational', '(EventID>0)'),
]
_FAST_EVENTS = {('rm', 10000), ('appx', 603)}  # 触发全进程快照升频的事件
_SEND_RE = re.compile(
    r'message\.send message_id=(\S+) .*connect_ms=([\d.]+) route_ms=([\d.]+) '
    r'publish_ms=([\d.]+) total_ms=([\d.]+)')
_LOG_TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+')


class OutputSink:
    """probe- 前缀输出管理：单文件 10MB 轮转、每日最多 3 个、总上限 100MB。

    只轮转/核算自有前缀文件，不删除目录内其他报告；达限时抛 CapacityReached 由
    主循环安全停止（保留已采集证据）。
    """

    def __init__(self, directory: Path, *, max_file_bytes=10 * 1024 * 1024,
                 max_daily_files=3, max_total_bytes=100 * 1024 * 1024):
        self.directory = directory
        self.max_file_bytes = max_file_bytes
        self.max_daily_files = max_daily_files
        self.max_total_bytes = max_total_bytes
        self._handles: dict[str, object] = {}
        self._index: dict[str, int] = {}

    def _paths(self) -> list[Path]:
        return sorted(p for p in self.directory.glob('probe-*') if p.is_file())

    def _total_bytes(self) -> int:
        return sum(p.stat().st_size for p in self._paths())

    def _daily_count(self, day: str, stream: str) -> int:
        return sum(1 for p in self._paths()
                   if p.name.startswith(f'probe-{day}-{stream}-'))

    def _target(self, stream: str) -> Path:
        day = datetime.now().strftime('%Y%m%d')
        if self._daily_count(day, stream) >= self.max_daily_files:
            raise CapacityReached(
                f'daily file limit {self.max_daily_files} reached for {stream}')
        if self._total_bytes() >= self.max_total_bytes:
            raise CapacityReached(f'total size limit {self.max_total_bytes} reached')
        index = self._index.get(stream, 0)
        for _ in range(self.max_daily_files):
            path = self.directory / f'probe-{day}-{stream}-{index}.jsonl'
            if not path.exists() or path.stat().st_size < self.max_file_bytes:
                return path
            index += 1
        self._index[stream] = index
        raise CapacityReached(f'rotation exhausted for {stream}')

    def write(self, stream: str, record: dict) -> None:
        """追加一行 JSON；容量上限抛出 CapacityReached（调用方安全停止）。"""
        path = self._target(stream)
        handle = self._handles.get(stream)
        if handle is None or getattr(handle, 'name', None) != str(path):
            if handle is not None:
                handle.close()
            handle = open(path, 'a', encoding='utf-8')
            self._handles[stream] = handle
        line = json.dumps(record, ensure_ascii=False, default=str) + '\n'
        if path.stat().st_size + len(line.encode('utf-8')) > self.max_file_bytes:
            self._index[stream] = self._index.get(stream, 0) + 1
            self.write(stream, record)  # 轮转后重写
            return
        handle.write(line)
        handle.flush()

    def close(self) -> None:
        for handle in self._handles.values():
            handle.close()
        self._handles.clear()


class CapacityReached(Exception):
    """容量上限：保留已采集证据，安全停止采集。"""


def derive_drive(project_root: Path) -> str:
    """盘符从注入的项目主目录推导；无盘符（相对路径/UNC）明确报错。"""
    drive = project_root.drive
    if not drive or len(drive) != 2 or drive[1] != ':':
        raise ValueError(f'无法从项目主目录推导盘符: {project_root}')
    return drive


def locate_engine(project_root: Path) -> tuple[list[tuple[int, list[str]]], str | None]:
    """psutil 列表匹配定位引擎进程：python/pythonw + `-m engine.main`。

    多候选时按（有 LISTENING 连接、CPU 累计）排序，全部候选由调用方落盘。
    返回 (候选列表, 错误说明)；候选列表按优先级排序。
    """
    candidates = process_query.list_processes({'python', 'pythonw'}, scope='morning-probe')
    if candidates is None:
        return [], 'enumerate_failed'
    matches = []
    for pid, argv in candidates:
        if '-m' in argv and 'engine.main' in ' '.join(argv):
            matches.append((pid, argv))
    if not matches:
        return [], 'not_found'
    scored = []
    for pid, argv in matches:
        try:
            process = psutil.Process(pid)
            listening = any(conn.status == 'LISTEN' for conn in
                            process.net_connections(kind='tcp'))
            cpu = process.cpu_times()
            total_cpu = cpu.user + cpu.system
        except (psutil.Error, OSError):
            listening, total_cpu = False, 0.0
        scored.append((listening, total_cpu, pid, argv))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [(pid, argv) for _, _, pid, argv in scored], None


def sample_engine_cpu(process: psutil.Process, previous: dict) -> dict | None:
    """Win32_Process UserModeTime/KernelModeTime 增量（psutil cpu_times）。"""
    now = time.time()
    times = process.cpu_times()
    delta_user = times.user - previous.get('user', times.user)
    delta_system = times.system - previous.get('system', times.system)
    previous['user'] = times.user
    previous['system'] = times.system
    return {'ts': now, 'pid': process.pid, 'user_ms': round(delta_user * 1000, 3),
            'system_ms': round(delta_system * 1000, 3),
            'rss_mb': round(process.memory_info().rss / 1048576, 1)}


def run_typeperf(drive: str) -> tuple[dict | None, str | None]:
    """typeperf 单次 1 秒采样 LogicalDisk 队列长度与平均传输秒。

    GBK 输出陷阱：子进程输出按 utf-8/errors=replace 解码，只取数值列。
    """
    counters = [f'\\LogicalDisk({drive})\\Current Disk Queue Length',
                f'\\LogicalDisk({drive})\\Avg. Disk sec/Transfer']
    command = ['typeperf', '-si', '1', '-sc', '1', *counters]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=25,
                                   encoding='utf-8', errors='replace')
    except FileNotFoundError:
        return None, 'launch_failed'
    except subprocess.TimeoutExpired:
        return None, 'timeout'
    if completed.returncode != 0:
        return None, f'exit_{completed.returncode}'
    # 输出含 header 行、数值行与本地化提示行（GBK 解码为替换符）：只取可解析数值行
    value_line = None
    for line in completed.stdout.splitlines():
        fields = [field.strip() for field in line.replace('"', '').split(',')]
        if len(fields) >= 3:
            try:
                float(fields[-1])
                float(fields[-2])
                value_line = fields
            except ValueError:
                continue
    if value_line is None:
        return None, 'unexpected_output'
    return {'ts': time.time(), 'drive': drive,
            'queue_length': float(value_line[-2]),
            'avg_sec_transfer': float(value_line[-1])}, None


def run_wevtutil(channel: str, predicate: str, after_record_id: int,
                 limit: int = 30) -> tuple[list[dict], str | None]:
    """wevtutil 短轮询单通道事件；返回 (新事件列表, 错误说明)。"""
    query = f"*[System[{predicate} and EventRecordID>{after_record_id}]]"
    command = ['wevtutil', 'qe', channel, '/q:' + query,
               '/c:%d' % limit, '/rd:true', '/f:xml']
    try:
        completed = subprocess.run(command, capture_output=True, timeout=20,
                                   encoding='utf-8', errors='replace')
    except FileNotFoundError:
        return [], 'launch_failed'
    except subprocess.TimeoutExpired:
        return [], 'timeout'
    if completed.returncode != 0:
        return [], f'exit_{completed.returncode}'
    if not completed.stdout.strip():
        return [], None
    try:
        # wevtutil /f:xml 输出多个平铺 <Event>（无根元素），包一层再解析
        root = ET.fromstring('<root>' + completed.stdout + '</root>')
    except ET.ParseError:
        return [], 'parse_failed'
    events = []
    ns = {'ns': 'http://schemas.microsoft.com/win/2004/08/events/event'}
    for event in root.findall('ns:Event', ns):
        system = event.find('ns:System', ns)
        if system is None:
            continue
        record_id = system.findtext('ns:EventRecordID', default=None, namespaces=ns)
        event_id = system.findtext('ns:EventID', default=None, namespaces=ns)
        provider = system.find('ns:Provider', ns)
        time_created = system.find('ns:TimeCreated', ns)
        events.append({'record_id': int(record_id) if record_id else None,
                       'event_id': int(event_id) if event_id else None,
                       'provider': provider.get('Name') if provider is not None else None,
                       'time_created': (time_created.get('SystemTime')
                                        if time_created is not None else None),
                       'received_ts': time.time()})
    events.sort(key=lambda item: item['record_id'] or 0)
    return events, None


def read_engine_sends(log_path: Path, offset: int) -> tuple[list[dict], int]:
    """尾随只读 engine.log，解析 message.send 行（offset 起始；轮转后重读）。

    只读打开；首轮从调用方给定的 offset（窗口起始处文件尾）开始。
    """
    try:
        with open(log_path, 'rb') as handle:
            handle.seek(0, 2)
            size = handle.tell()
            if size < offset:  # RotatingFileHandler 轮转：从头重读新文件
                offset = 0
            handle.seek(offset)
            chunk = handle.read()
            offset = handle.tell()
    except FileNotFoundError:
        return [], offset
    except OSError as exc:
        log.warning('engine log read failed: %s', exc)
        return [], offset
    sends = []
    for raw in chunk.decode('utf-8', errors='replace').splitlines():
        match = _SEND_RE.search(raw)
        if not match:
            continue
        ts_match = _LOG_TS_RE.match(raw)
        sends.append({'log_ts': ts_match.group(1) if ts_match else None,
                      'received_ts': time.time(),
                      'message_id': match.group(1),
                      'connect_ms': float(match.group(2)),
                      'route_ms': float(match.group(3)),
                      'publish_ms': float(match.group(4)),
                      'total_ms': float(match.group(5))})
    return sends, offset


def snapshot_all(previous: dict) -> dict:
    """全进程快照：CPU 增量（%）与 IO 字节率（B/s），权限不足实例跳过并计数。"""
    now = time.time()
    procs = []
    skipped = 0
    for process in psutil.process_iter(['pid', 'name']):
        try:
            pid = process.info['pid']
            name = process.info['name'] or ''
            times = process.cpu_times()
            counters = process.io_counters()
        except (psutil.Error, OSError):
            skipped += 1
            continue
        base = previous.get(pid)
        elapsed = now - base['ts'] if base else None
        cpu_pct = None
        io_r_bps = io_w_bps = None
        if base and elapsed and elapsed > 0:
            cpu_pct = round((times.user + times.system - base['cpu']) / elapsed * 100, 2)
            io_r_bps = round((counters.read_bytes - base['read']) / elapsed, 1)
            io_w_bps = round((counters.write_bytes - base['write']) / elapsed, 1)
        previous[pid] = {'ts': now, 'cpu': times.user + times.system,
                         'read': counters.read_bytes, 'write': counters.write_bytes}
        procs.append([pid, name, cpu_pct, io_r_bps, io_w_bps])
    return {'ts': now, 'procs': procs, 'skipped': skipped}


class Scheduler:
    """固定周期调度：各采样器独立周期，按单调钟对齐，记录每次耗时。"""

    def __init__(self):
        self._due: list[tuple[str, float, float, object]] = []  # (name, period, next_due, fn)

    def add(self, name: str, period: float, function) -> None:
        self._due.append([name, period, time.monotonic() + period, function])

    def set_period(self, name: str, period: float) -> None:
        for entry in self._due:
            if entry[0] == name:
                entry[1] = period
                entry[2] = time.monotonic() + period

    def due(self) -> list[tuple[str, float, object]]:
        now = time.monotonic()
        ready = []
        for entry in self._due:
            if now >= entry[2]:
                ready.append((entry[0], entry[1], entry[3]))
                entry[2] = now + entry[1]  # 下一帧从现在起算（慢采样滞后跳帧）
        return ready

    def next_delay(self) -> float:
        now = time.monotonic()
        delays = [entry[2] - now for entry in self._due]
        return max(0.0, min(delays)) if delays else 0.1


def run_probe(args) -> int:
    root = Path(args.project_root)
    output_dir = Path(args.output_dir)
    drive = derive_drive(root)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f'输出目录不可创建: {output_dir} ({exc})', file=sys.stderr)
        return 2
    if not output_dir.is_dir():
        print(f'输出目录不可用: {output_dir}', file=sys.stderr)
        return 2
    sink = OutputSink(output_dir, max_file_bytes=args.max_file_mb * 1024 * 1024,
                      max_daily_files=args.max_daily_files,
                      max_total_bytes=args.max_total_mb * 1024 * 1024)
    start = time.time()
    started_at = datetime.now().isoformat(timespec='seconds')
    # 引擎定位（psutil 列表匹配；全部候选落盘，注入 PID 优先）
    candidates, locate_error = locate_engine(root)
    if args.engine_pid:
        candidates = [(args.engine_pid, ['<injected>'])] + candidates
    marker = {'started_at': started_at, 'pid': os.getpid(),
              'project_root': str(root), 'drive': drive,
              'output_dir': str(output_dir), 'window': args.window,
              'connect_threshold_ms': args.connect_threshold,
              'engine_candidates': [{'pid': pid, 'argv': argv} for pid, argv in candidates],
              'locate_error': locate_error}
    sink.write('run-marker', marker)
    engine_previous: dict = {}
    engine_state: dict = {'pid': candidates[0][0] if candidates else None,
                          'process': None, 'last_locate': time.time()}
    snapshot_previous: dict = {}
    # 事件基线：启动时静默查询各通道当前最新 EventRecordID（不落历史事件，
    # 避免把窗口前的更新事件误判为当前更新窗口触发升频）
    after_record_id: dict[str, int] = {}
    for name, channel, predicate in _CHANNELS:
        events, _ = run_wevtutil(channel, predicate, 0)
        after_record_id[name] = max((event['record_id'] or 0 for event in events), default=0)
    event_state: dict = {'after_record_id': after_record_id,
                         'last_fast_event': 0.0}
    snapshot_period: dict = {'value': args.snapshot_interval}
    log_offset = 0
    try:
        with open(root / 'logs' / 'engine.log', 'rb') as handle:
            handle.seek(0, 2)
            log_offset = handle.tell()
    except (FileNotFoundError, OSError):
        log_offset = 0  # 无日志文件：首轮从 0 开始，后续按文件出现跟踪
    scheduler = Scheduler()
    scheduler.add('engine', args.engine_interval, lambda: sample_engine())
    scheduler.add('disk', args.disk_interval, lambda: sample_disk())
    scheduler.add('snapshot', snapshot_period['value'], lambda: sample_snapshot())
    scheduler.add('events', args.event_interval, lambda: sample_events())
    scheduler.add('sends', args.log_interval, lambda: sample_sends())
    scheduler.add('self', 30.0, lambda: sample_self())
    self_previous = {'ts': time.monotonic(), 'cpu': 0.0}
    sample_counts: dict[str, int] = {}
    anomaly_count = 0
    send_count = 0

    def sample_engine() -> None:
        nonlocal engine_state, engine_previous
        t0 = time.monotonic()
        if engine_state['process'] is None or time.time() - engine_state['last_locate'] > 60:
            engine_state['process'] = None
            if engine_state['pid'] is not None:
                try:
                    process = psutil.Process(engine_state['pid'])
                    if not process.is_running():
                        process = None
                except psutil.NoSuchProcess:
                    process = None
                except psutil.Error:
                    process = None
                if process is not None:
                    engine_state['process'] = process
                    engine_state['last_locate'] = time.time()
        process = engine_state['process']
        if process is None:
            candidates, locate_error = locate_engine(root)
            engine_state['last_locate'] = time.time()
            if candidates:
                engine_state['pid'] = candidates[0][0]
                engine_state['process'] = psutil.Process(candidates[0][0])
                sink.write('engine-events',
                           {'ts': time.time(), 'event': 'relocated',
                            'pid': engine_state['pid'], 'locate_error': locate_error})
            else:
                sink.write('engine-events',
                           {'ts': time.time(), 'event': 'engine_missing',
                            'locate_error': locate_error})
                return
        try:
            record = sample_engine_cpu(engine_state['process'], engine_previous)
        except psutil.NoSuchProcess:
            engine_state['process'] = None
            sink.write('engine-events',
                       {'ts': time.time(), 'event': 'engine_exited', 'pid': engine_state['pid']})
            return
        except psutil.Error as exc:
            sink.write('engine-events',
                       {'ts': time.time(), 'event': 'engine_read_failed', 'error': str(exc)})
            return
        if record is not None:
            sink.write('engine', record)
        sample_counts['engine'] = sample_counts.get('engine', 0) + 1
        _record_cost('engine', time.monotonic() - t0)

    def sample_disk() -> None:
        t0 = time.monotonic()
        record, error = run_typeperf(drive)
        if error:
            sink.write('disk-errors', {'ts': time.time(), 'error': error, 'drive': drive})
        else:
            sink.write('disk', record)
        sample_counts['disk'] = sample_counts.get('disk', 0) + 1
        _record_cost('disk', time.monotonic() - t0)

    def sample_snapshot() -> None:
        t0 = time.monotonic()
        now = time.time()
        frame = snapshot_all(snapshot_previous)
        frame['interval'] = snapshot_period['value']
        # 清理退出进程的基线（超过 2 分钟未见的 PID）
        stale = [pid for pid, base in snapshot_previous.items() if now - base['ts'] > 120]
        for pid in stale:
            snapshot_previous.pop(pid, None)
        sink.write('snapshot', frame)
        sample_counts['snapshot'] = sample_counts.get('snapshot', 0) + 1
        _record_cost('snapshot', time.monotonic() - t0)

    def sample_events() -> None:
        t0 = time.monotonic()
        for name, channel, predicate in _CHANNELS:
            events, error = run_wevtutil(channel, predicate,
                                         event_state['after_record_id'][name])
            if error:
                sink.write('events-read-errors',
                           {'ts': time.time(), 'channel': name, 'error': error})
                continue
            if not events:
                sink.write('events-none',
                           {'ts': time.time(), 'channel': name,
                            'after_record_id': event_state['after_record_id'][name]})
                continue
            for event in events:
                event['channel'] = name
                sink.write('events', event)
                if event['record_id']:
                    event_state['after_record_id'][name] = \
                        max(event_state['after_record_id'][name], event['record_id'])
                if (name, event['event_id']) in _FAST_EVENTS:
                    event_state['last_fast_event'] = time.time()
                    scheduler.set_period('snapshot', args.snapshot_fast)
                    snapshot_period['value'] = args.snapshot_fast
        sample_counts['events'] = sample_counts.get('events', 0) + 1
        _record_cost('events', time.monotonic() - t0)

    def sample_sends() -> None:
        nonlocal log_offset, anomaly_count, send_count
        t0 = time.monotonic()
        sends, log_offset = read_engine_sends(root / 'logs' / 'engine.log', log_offset)
        for record in sends:
            sink.write('engine-sends', record)
            send_count += 1
            if record['connect_ms'] >= args.connect_threshold:
                anomaly_count += 1
                sink.write('anomalies', {**record, 'threshold_ms': args.connect_threshold})
        sample_counts['sends'] = sample_counts.get('sends', 0) + 1
        _record_cost('sends', time.monotonic() - t0)

    def sample_self() -> None:
        t0 = time.monotonic()
        process = psutil.Process()
        times = process.cpu_times()
        elapsed = t0 - self_previous['ts']
        cpu_pct = None
        if elapsed > 0:
            cpu_pct = round((times.user + times.system - self_previous['cpu']) / elapsed * 100, 2)
        self_previous['ts'] = t0
        self_previous['cpu'] = times.user + times.system
        sink.write('self', {'ts': time.time(), 'cpu_pct': cpu_pct,
                            'rss_mb': round(process.memory_info().rss / 1048576, 1)})
        sample_counts['self'] = sample_counts.get('self', 0) + 1

    def _record_cost(name: str, cost: float) -> None:
        sink.write('self-cost', {'ts': time.time(), 'sampler': name, 'cost_ms': round(cost * 1000, 1)})

    stop_reason = None
    exit_code = 0
    try:
        while time.time() - start < args.window:
            delay = scheduler.next_delay()
            if delay > 0:
                time.sleep(min(delay, 0.5))
            for name, period, function in scheduler.due():
                function()
            # 升频恢复：更新事件停止出现 5 分钟后回到默认快照周期
            if (event_state['last_fast_event'] and
                    time.time() - event_state['last_fast_event'] > 300 and
                    snapshot_period['value'] != args.snapshot_interval):
                scheduler.set_period('snapshot', args.snapshot_interval)
                snapshot_period['value'] = args.snapshot_interval
                sink.write('events', {'ts': time.time(), 'channel': 'update_window',
                                      'event_id': 'fast_snapshot_end'})
                event_state['last_fast_event'] = 0.0
        stop_reason = 'window_ended'
    except CapacityReached as exc:
        stop_reason = f'capacity_reached: {exc}'
    except KeyboardInterrupt:
        stop_reason = 'interrupted'
    except Exception as exc:
        stop_reason = f'error: {exc}'
        traceback.print_exc()
        try:
            sink.write('error-log', {'ts': time.time(), 'error': repr(exc),
                                     'traceback': traceback.format_exc()})
        except CapacityReached:
            pass
        exit_code = 1
    finally:
        if not anomaly_count:
            verdict = '本次未复现'
        else:
            verdict = f'复现 {anomaly_count} 条异常 connect_ms（阈值 {args.connect_threshold}ms）'
        summary = {'stop_reason': stop_reason, 'verdict': verdict,
                   'started_at': started_at, 'ended_at': datetime.now().isoformat(timespec='seconds'),
                   'window_s': round(time.time() - start, 1),
                   'sample_counts': sample_counts, 'send_count': send_count,
                   'anomaly_count': anomaly_count,
                   'engine_final_pid': engine_state['pid'],
                   'snapshot_permissions_skipped': '见快照帧 skipped 计数'}
        sink.close()
        # exit 日志绕过容量上限直接追加（达限也要留下停止原因）
        day = datetime.now().strftime('%Y%m%d')
        with open(output_dir / f'probe-{day}-exit-log-0.jsonl', 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(summary, ensure_ascii=False, default=str) + '\n')
    print(json.dumps(summary, ensure_ascii=False))
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description='晨间发送卡顿只读探针（零触碰）')
    parser.add_argument('--project-root', required=True,
                        help='项目主目录（推导盘符与 engine.log 路径）')
    parser.add_argument('--output-dir', required=True,
                        help='输出目录（必填注入；不可创建/无写权限时报错退出）')
    parser.add_argument('--window', type=float, default=1800.0, help='采样窗口秒数')
    parser.add_argument('--engine-pid', type=int, default=None, help='引擎 PID 显式注入（可选）')
    parser.add_argument('--connect-threshold', type=float, default=5000.0,
                        help='connect_ms 异常阈值（毫秒）')
    parser.add_argument('--engine-interval', type=float, default=1.0)
    parser.add_argument('--disk-interval', type=float, default=10.0)
    parser.add_argument('--snapshot-interval', type=float, default=30.0)
    parser.add_argument('--snapshot-fast', type=float, default=1.0)
    parser.add_argument('--event-interval', type=float, default=5.0)
    parser.add_argument('--log-interval', type=float, default=5.0)
    parser.add_argument('--max-file-mb', type=float, default=10.0)
    parser.add_argument('--max-daily-files', type=int, default=3)
    parser.add_argument('--max-total-mb', type=float, default=100.0)
    return run_probe(parser.parse_args())


if __name__ == '__main__':
    sys.exit(main())
