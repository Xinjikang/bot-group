"""req#142 成果 2：/thinking Hermes 原生执行观察（自发执行状态展示）。

Hermes 异步子任务回调驱动的自发续跑没有引擎心跳/运行记录，/thinking 只按心跳与
活跃运行显示，思考气泡持续占位。本模块维护观察批次注册表：

- refresh(conn)：对每个 Hermes 托管绑定（session_bindings runtime='hermes'），
  引擎当前没有该会话的活动派发记录（wake_runs 未结束行或内存活跃运行）时，
  复用成果 1 的原生状态缓存查询原生忙闲：
  原生忙 → 记录/续租观察批次（首次观察到 busy 的时刻为 wake_started_at，批次内
  保持稳定，供前端气泡按 (session, wake_started_at) 判活与折叠）；
  原生空闲 → 结束当前批次并记录 ended_at。
- ended_for(session, wake_started_at)：已结束批次的结束时刻（/reasoning 用）。

批次按 (session, wake_started_at) 不可变寻址：同一绑定结束后再次 busy 开启**新批次**
（新的 wake_started_at），已结束批次各自保留 END_TTL——新批次启动后旧批次的前端气泡
仍能按自己的 /reasoning 折叠（迟到查询不丢终态），两批终态并存、各自到期清除
（node1715 返修：原实现 epoch.update 覆写旧批次终态，旧 /reasoning process_ended_at
由 1005 变 null）。

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
_END_TTL = 120.0  # 每个结束批次终态保留秒数（/reasoning 折叠与晚到轮询的窗口）
# (session_id, generation) -> 批次列表；列表尾部为该绑定的最新批次。
# 批次字段：started_at/ended_at/profile/project/kind/thread_id/session（不可变记录，
# 结束后不再改写；新批次追加新记录，历史终态保留至各自到期）
_batches: dict[tuple[str, int], list[dict]] = {}


def _engine_active(conn, engine_session):
    """引擎主动派发的活动记录：wake_runs 未结束行或内存活跃运行（有则不再观察）。"""
    row = conn.execute('SELECT 1 FROM wake_runs WHERE session=? AND ended_at IS NULL LIMIT 1',
                       (engine_session,)).fetchone()
    if row is not None:
        return True
    with wake._session_lock:
        return engine_session in wake._running_wakes


def _end_current(batches, now):
    """结束该绑定当前活跃批次并返回（无活跃批次返回 None）。"""
    if not batches:
        return None
    current = batches[-1]
    if current['ended_at'] is None:
        current['ended_at'] = now
        return current
    return None


def _entry(batch, state):
    """观察条目：与引擎 runs 契约兼容（profile/session/wake_started_at/thread_id/state），
    observed 标记区分观察记录；不携带任务/消息关联（无证据）。"""
    return {'profile': batch['profile'], 'session': batch['session'],
            'wake_started_at': batch['started_at'], 'thread_id': batch['thread_id'],
            'project': batch['project'], 'kind': batch['kind'],
            'state': state, 'observed': True,
            'spawned_at': datetime.fromtimestamp(batch['started_at']).isoformat()}


def refresh(conn):
    """/thinking 观察刷新：返回当前原生忙的观察条目；批次起止发布 thinking.changed。

    conn 为调用方持有的引擎连接（只读使用）。
    """
    from . import events, hermes_gateway
    now = time.time()
    with _lock:
        # 收尾：每个结束批次按自己的 ended_at 到期清除（多批终态各自保留期）
        for key, batches in list(_batches.items()):
            kept = [b for b in batches
                    if b['ended_at'] is None or now - b['ended_at'] <= _END_TTL]
            if kept:
                _batches[key] = kept
            else:
                _batches.pop(key)
    rows = conn.execute("SELECT * FROM session_bindings WHERE runtime='hermes'").fetchall()
    seen = set()
    changed = []
    out = []
    for row in rows:
        key = (row['session_id'], row['generation'])
        seen.add(key)
        if row['switching']:
            # /new 切换中：原生会话即将更换，结束当前批次（不观察切换态）
            with _lock:
                batch = _end_current(_batches.get(key, []), now)
            if batch is not None:
                changed.append(batch)
            continue
        engine_session = wake.session_name(row['profile'], row['kind'],
                                           row['thread_id'] or None, row['project'])
        if _engine_active(conn, engine_session):
            # 引擎主动派发接手：心跳/运行记录已有展示，结束当前观察批次（不重复观察）
            with _lock:
                batch = _end_current(_batches.get(key, []), now)
            if batch is not None:
                changed.append(batch)
            continue
        state = hermes_gateway._native_observation_state(conn, row['profile'], engine_session)
        if state == 'native_running':
            with _lock:
                batches = _batches.setdefault(key, [])
                current = batches[-1] if batches else None
                if current is None or current['ended_at'] is not None:
                    # 新批次：不动已结束批次的历史终态（各自保留期折叠旧气泡）
                    current = {'started_at': now, 'ended_at': None,
                               'profile': row['profile'], 'project': row['project'],
                               'kind': row['kind'], 'thread_id': row['thread_id'] or None,
                               'session': engine_session}
                    batches.append(current)
                    changed.append(current)
            out.append(_entry(current, state))
        elif state == 'idle':
            with _lock:
                batch = _end_current(_batches.get(key, []), now)
            if batch is not None:
                changed.append(batch)
    with _lock:
        # 绑定消失（解绑/删除）的当前批次一并结束
        for key in list(_batches):
            if key not in seen:
                batch = _end_current(_batches[key], now)
                if batch is not None:
                    changed.append(batch)
    for batch in changed:
        events.bus.publish('thinking.changed', {'profile': batch['profile']})
    return out


def ended_for(session, wake_started_at):
    """/reasoning 用：已结束观察批次的结束时刻（按 session+wake_started_at 精确寻址，
    仅保留期内；同绑定多批次并存时旧批次终态不受新批次影响）。"""
    with _lock:
        for batches in _batches.values():
            for batch in batches:
                if batch['session'] == session and batch['started_at'] == wake_started_at \
                        and batch['ended_at'] is not None:
                    return batch['ended_at']
    return None
