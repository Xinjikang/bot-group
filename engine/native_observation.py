"""req#142 成果 2：/thinking Hermes 原生执行观察（自发执行状态展示）。

Hermes 异步子任务回调驱动的自发续跑没有引擎心跳/运行记录，/thinking 只按心跳与
活跃运行显示，思考气泡持续占位。本模块维护观察批次注册表：

- refresh(conn)：对每个 Hermes 托管绑定（session_bindings runtime='hermes'），
  引擎当前没有该会话的活动派发记录（wake_runs 未结束行或内存活跃运行）时，
  复用成果 1 的原生状态缓存查询原生忙闲：
  原生忙 → 记录/续租观察批次（首次观察到 busy 的时刻为 wake_started_at，批次内
  保持稳定，供前端气泡按 (session, wake_started_at) 判活与折叠）；
  原生空闲 → 结束批次并记录 ended_at（保留 END_TTL 供 /reasoning 返回
  process_ended_at 折叠气泡，之后清除）。
- ended_for(session, wake_started_at)：已结束批次的结束时刻（/reasoning 用）。

观察记录与引擎主动派发记录区分：观察条目带 observed=true 且无任务/消息关联字段
（观察不产生引擎执行记录，无证据不关联）；引擎活动派发的会话不重复观察。
批次起止通过 events.bus 发布 thinking.changed（复用前端即时刷新通道）。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

from . import wake

_lock = threading.Lock()
_END_TTL = 120.0  # 结束后批次终态保留秒数（/reasoning 折叠与晚到轮询的窗口）
_epochs: dict[tuple[str, int], dict] = {}  # (session_id, generation) -> 批次


def _engine_active(conn, engine_session):
    """引擎主动派发的活动记录：wake_runs 未结束行或内存活跃运行（有则不再观察）。"""
    row = conn.execute('SELECT 1 FROM wake_runs WHERE session=? AND ended_at IS NULL LIMIT 1',
                       (engine_session,)).fetchone()
    if row is not None:
        return True
    with wake._session_lock:
        return engine_session in wake._running_wakes


def _end(key, now):
    """结束未结束批次并返回（无批次或已结束返回 None）。"""
    epoch = _epochs.get(key)
    if epoch is not None and epoch['ended_at'] is None:
        epoch['ended_at'] = now
        return epoch
    return None


def _entry(epoch, state):
    """观察条目：与引擎 runs 契约兼容（profile/session/wake_started_at/thread_id/state），
    observed 标记区分观察记录；不携带任务/消息关联（无证据）。"""
    return {'profile': epoch['profile'], 'session': epoch['session'],
            'wake_started_at': epoch['started_at'], 'thread_id': epoch['thread_id'],
            'project': epoch['project'], 'kind': epoch['kind'],
            'state': state, 'observed': True,
            'spawned_at': datetime.fromtimestamp(epoch['started_at']).isoformat()}


def refresh(conn):
    """/thinking 观察刷新：返回当前原生忙的观察条目；批次起止发布 thinking.changed。

    conn 为调用方持有的引擎连接（只读使用）。
    """
    from . import events, hermes_gateway
    now = time.time()
    with _lock:
        # 收尾：结束批次保留期过后清除
        stale = [k for k, e in _epochs.items()
                 if e['ended_at'] is not None and now - e['ended_at'] > _END_TTL]
        for k in stale:
            _epochs.pop(k, None)
    rows = conn.execute("SELECT * FROM session_bindings WHERE runtime='hermes'").fetchall()
    seen = set()
    changed = []
    out = []
    for row in rows:
        key = (row['session_id'], row['generation'])
        seen.add(key)
        if row['switching']:
            # /new 切换中：原生会话即将更换，结束旧批次（不观察切换态）
            with _lock:
                epoch = _end(key, now)
            if epoch is not None:
                changed.append(epoch)
            continue
        engine_session = wake.session_name(row['profile'], row['kind'],
                                           row['thread_id'] or None, row['project'])
        if _engine_active(conn, engine_session):
            # 引擎主动派发接手：心跳/运行记录已有展示，结束观察批次（不重复观察）
            with _lock:
                epoch = _end(key, now)
            if epoch is not None:
                changed.append(epoch)
            continue
        state = hermes_gateway._native_observation_state(conn, row['profile'], engine_session)
        if state == 'native_running':
            with _lock:
                epoch = _epochs.get(key)
                if epoch is None:
                    epoch = {'started_at': now, 'profile': row['profile'],
                             'project': row['project'], 'kind': row['kind'],
                             'thread_id': row['thread_id'] or None, 'session': engine_session,
                             'ended_at': None}
                    _epochs[key] = epoch
                    changed.append(epoch)
                elif epoch['ended_at'] is not None:
                    # 结束后又观察到忙：新批次（新 wake_started_at，前端新气泡）
                    epoch.update(started_at=now, ended_at=None)
                    changed.append(epoch)
            out.append(_entry(epoch, state))
        elif state == 'idle':
            with _lock:
                epoch = _end(key, now)
            if epoch is not None:
                changed.append(epoch)
    with _lock:
        # 绑定消失（解绑/删除）的批次一并结束
        for key in list(_epochs):
            if key not in seen:
                epoch = _end(key, now)
                if epoch is not None:
                    changed.append(epoch)
    for epoch in changed:
        events.bus.publish('thinking.changed', {'profile': epoch['profile']})
    return out


def ended_for(session, wake_started_at):
    """/reasoning 用：观察批次结束时刻（仅保留期内），供前端按 process_ended_at 折叠。"""
    with _lock:
        for epoch in _epochs.values():
            if epoch['session'] == session and epoch['started_at'] == wake_started_at \
                    and epoch['ended_at'] is not None:
                return epoch['ended_at']
    return None
