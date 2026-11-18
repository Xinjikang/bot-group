"""慢发送分步计时诊断（req#167）：发送路径步骤计时与写事务起止记录。

开关：环境变量 XDG_SLOW_SEND_DIAG=1 启用（XDG_ 前缀可穿过 Hermes gateway
spawn 子进程的 _build_safe_env 过滤，跨进程透传）。未设置=零开销零日志：
所有公共入口第一行判 enabled() 直接返回。

输出：logs/slow-send-diag.jsonl（引擎日志目录），字段固定、机器可解析。
按天轮转 + 单文件容量上限（20MB），目录保留最近 3 个轮转文件与当日主文件
（容量上限后保留最近，不无界增长）。

行类型：
- step：发送分步计时，每组 wall_ms + thread_cpu_ms 成对值（墙钟长而线程
  CPU 短=等待型；反之为计算型）。组：C1-C4（db.get_connection，thread_id
  关联发送线程）、S1-S7（api.py / router.py route_message，conn_id+thread_id
  可串联到同一发送）。见设计 docs/design/2026-11-18-slow-send-instrumentation.md 3.1。
- txn：写事务事件，三态区分锁申请/实际取得/释放：
  - is_request=1：BEGIN IMMEDIATE 执行前（锁申请发起，尚未取得）；
  - is_begin=1：BEGIN IMMEDIATE 成功后（实际取得，持有区间起点）；
  - is_end=1 + end_kind：commit=提交释放 / rollback=回滚释放 /
    failed=申请失败未取得（busy 超时等，绝不出现持有记录）。
  持锁时长 = 同 conn_id 的 is_begin→is_end 的 wall_ts 差值，区间排除
  锁申请等待与诊断日志 I/O。

失败隔离（业务零影响）：诊断写入/轮转任何异常只打一次告警日志并降级
跳过（绝不冒泡到发送路径，已提交消息不受影响）；事务内事件先进内存
缓冲，事务结束（锁外）统一落盘——锁内零同步磁盘输出，诊断自身不延长
持锁区间。

单点开销目标 <0.5ms（enabled 时 time.monotonic + time.thread_time 各一次
调用 + 一行内存缓冲；未启用仅一次 os.environ 查询）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date
from pathlib import Path

ENV_NAME = "XDG_SLOW_SEND_DIAG"

# 轮转边界：单文件字节上限 / 目录保留轮转文件数 / 当日主文件后缀。
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ROTATED = 3
MAIN_NAME = "slow-send-diag.jsonl"
ROTATED_PREFIX = "slow-send-diag-"

_logger = logging.getLogger(__name__)

# 行写入锁：多线程并发发送时保持 jsonl 行完整（引擎 FastAPI 线程池）。
_lock = threading.Lock()

# 进程级缓存当日文件句柄与大小（每行 open/close 会引入毫秒级开销，
# 缓存保持单点微秒级；跨天/容量轮转时关闭重开）。
_stream: dict = {"date": None, "file": None, "size": 0}

# 事务内事件缓冲：route_message 持锁期间事件只进内存（锁内零磁盘输出），
# 事务结束（锁外）统一落盘。线程局部：每个发送线程各自缓冲。
_buffer_local = threading.local()

# 诊断写入失败告警只打一次（避免磁盘故障时刷屏）。
_warned_once = False


def enabled() -> bool:
    """开关读取（每处埋点只调一次，未启用零后续开销）。"""
    return os.environ.get(ENV_NAME) == "1"


def _warn_once(message: str) -> None:
    global _warned_once
    if not _warned_once:
        _warned_once = True
        _logger.warning(message)


def defer_on() -> None:
    """进入事务段：后续事件先进线程本地缓冲，defer_flush 时锁外落盘。"""
    _buffer_local.items = []


def defer_flush() -> None:
    """事务结束（锁外）统一落盘；空缓冲/未开启 no-op（可重复调用）。"""
    items = getattr(_buffer_local, "items", None)
    if items is None:
        return
    _buffer_local.items = None
    for row in items:
        _write_row(row)


def _defer_push(row: dict) -> None:
    """事务内事件进缓冲（缓冲存在时）；否则立即写（事务外路径如 C1-C4/S1-S2）。"""
    items = getattr(_buffer_local, "items", None)
    if items is not None:
        items.append(row)
    else:
        _write_row(row)


def _rotate(path: Path, today: str) -> None:
    """主文件改名进轮转池（同名按序号避让），清理超量旧轮转文件。"""
    idx = 0
    while True:
        suffix = today if idx == 0 else f"{today}-{idx}"
        target = path.with_name(ROTATED_PREFIX + suffix + ".jsonl")
        if not target.exists():
            break
        idx += 1
    path.rename(target)
    rotated = sorted(
        path.parent.glob(ROTATED_PREFIX + "*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in rotated[MAX_ROTATED:]:
        old.unlink(missing_ok=True)


def _ensure_stream(path: Path, today: str) -> None:
    """进程级文件句柄：跨天切换/容量上限触发轮转时关闭重开。"""
    if _stream["file"] is not None and _stream["date"] != today:
        _stream["file"].close()
        _stream["file"] = None
    if _stream["file"] is None:
        if path.exists() and _stream["date"] != today:
            _rotate(path, today)
        _stream["file"] = path.open("a", encoding="utf-8")
        _stream["date"] = today
        _stream["size"] = path.stat().st_size if path.exists() else 0
    if _stream["size"] >= MAX_FILE_BYTES:
        _stream["file"].close()
        _stream["file"] = None
        _rotate(path, today)
        _stream["file"] = path.open("a", encoding="utf-8")
        _stream["size"] = 0


def close_stream() -> None:
    """关闭进程级句柄（测试收尾/进程退出前调用，避免 Windows 文件占用）。"""
    with _lock:
        if _stream["file"] is not None:
            _stream["file"].close()
            _stream["file"] = None


def _write_row(row: dict) -> None:
    """写一行到诊断文件；任何失败只告警一次并静默跳过（诊断绝不冒泡）。"""
    try:
        from . import config  # 延迟导入：引擎启动早期避免环形依赖
        today = date.today().isoformat()
        path = config.logs_dir() / MAIN_NAME
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            _ensure_stream(path, today)
            # 不逐行 flush：OS 缓冲写保持单点微秒级（诊断行在进程级停顿前
            # 可能滞留缓冲是可接受损失，避免诊断自身延长发送路径）。
            _stream["file"].write(line)
            _stream["size"] += len(line.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 诊断失败降级，不改变业务
        _warn_once(f"slow_send_diag 写入失败（诊断降级，不影响发送）：{exc!r}")


def record(kind: str, fields: dict) -> None:
    """记一行诊断事件（调用方已确认 enabled，不再查开关）。"""
    if kind not in ("step", "txn"):
        raise ValueError(f"unknown diag kind: {kind}")
    _defer_push({"kind": kind, "wall_ts": time.time(), **fields})


def step(name: str, wall_ms: float, thread_cpu_ms: float, **fields) -> None:
    """发送分步计时行（成对值：wall_ms + thread_cpu_ms）。"""
    record("step", {"name": name, "wall_ms": round(wall_ms, 3),
                    "thread_cpu_ms": round(thread_cpu_ms, 3), **fields})


def txn_request(conn, thread_id: int | None = None) -> None:
    """锁申请行（BEGIN IMMEDIATE 执行前：申请发起，尚未取得）。"""
    record("txn", {"conn_id": id(conn),
                   "thread_id": thread_id if thread_id is not None else threading.get_ident(),
                   "is_request": 1})


def txn_begin(conn, thread_id: int | None = None) -> None:
    """锁取得行（BEGIN IMMEDIATE 成功后：实际取得，持有区间起点）。"""
    record("txn", {"conn_id": id(conn),
                   "thread_id": thread_id if thread_id is not None else threading.get_ident(),
                   "is_begin": 1})


def txn_end(conn, end_kind: str, thread_id: int | None = None) -> None:
    """锁释放/失败行。end_kind=commit|rollback|failed（failed=申请失败未取得）。"""
    record("txn", {"conn_id": id(conn),
                   "thread_id": thread_id if thread_id is not None else threading.get_ident(),
                   "is_end": 1, "end_kind": end_kind})


def now_pair() -> tuple[float, float]:
    """单点计时取样：wall（单调钟）+ 同线程 CPU（同线程 CPU 时钟）。"""
    return time.monotonic(), time.thread_time()


def diff_ms(pair_start: tuple[float, float], pair_end: tuple[float, float]) -> tuple[float, float]:
    """两取样点差 → (wall_ms, thread_cpu_ms)。"""
    return (pair_end[0] - pair_start[0]) * 1000.0, (pair_end[1] - pair_start[1]) * 1000.0
