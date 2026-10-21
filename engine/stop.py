"""叫停（stop）——硬杀正在干活的 bot，追回发错的指令。

停止行为约定：
1. 粒度：单 bot（全停是附加，暂不做）
2. 语义：硬杀（taskkill /T /F 杀进程树，连 MCP 子进程一起停）
3. 会话：同会话保留（不换名，保住审批链断点续机制）
4. 软删半截尾巴：杀透后，把该 bot 的 -c 会话从「最后一条 user 消息」起
   UPDATE messages SET active=0（hermes 原生 rewind 同款，零 DELETE、零 FTS 触发、
   零计数漂移），下次醒来不读半截 tool_call 残留
5. 追回范围：将当前 work 唤醒 job 的 trigger_msg_id 对应 inbox 行置 seen，
   其余 pending 消息保留
6. 取消 queued/running wake_jobs（堵队列 worker 路径，pending 置 seen 只堵扫描器路径）
7. 任务池 in_progress 退回 pending（防 bot 锁死领不了新活）

停止时序：taskkill → 等 reaper 确认进程退出 → 软删除会话消息。
软删不能跑在进程死透之前，否则 bot 可能软删后又 flush 一条 active=1 的悬空行。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from . import db, wake

# 🔴 2026-09-10 后台化黑框 bug：taskkill 也是控制台程序，pythonw 父进程 spawn 会弹窗。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _hermes_home() -> Path:
    """hermes 根目录（state.db 所在）。引擎环境 HERMES_HOME 已设。"""
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env)
    # 兜底：引擎进程跑在 hermes-agent venv 里，根目录是上两级
    return Path(__file__).resolve().parent.parent.parent


def _profile_state_db(profile: str) -> Path:
    """该 profile 的 state.db 路径。default → <home>/state.db，其他 → <home>/profiles/<名>/state.db。"""
    home = _hermes_home()
    from . import bot_identity
    return bot_identity.hermes_home(profile, home) / 'state.db'


def _kill_process_tree(pid: int) -> bool:
    """Windows 硬杀进程树（/T 连子进程 MCP 一起，/F 强制）。"""
    try:
        r = subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True, timeout=15, creationflags=_NO_WINDOW,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _wait_dead(profile: str, session: str, timeout: float = 8.0) -> bool:
    """等该 profile 的指定会话进程全部死透（命令行实时匹配，
    active_pids 是壳 pid 秒死恒空——用它判断恒真=假死透。）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        _pids = wake.find_bot_processes(profile, session=session)
        if _pids == []:            # 明确无进程=死透（None=查询失败继续等）
            return True
        time.sleep(0.2)
    # 超时兜底：最后查询失败(None)时保守返回 False（不软删——宁可这轮不清
    # 不撞悬空行坑），只有明确 [] 才算死透
    return wake.find_bot_processes(profile, session=session) == []


def _soft_delete_tail(profile: str, session_name: str) -> int:
    """软删该 profile 的 -c 会话半截尾巴：从最后一条 user 消息起置 active=0。

    返回置 active=0 的行数。会话不存在或已无 user 消息时返回 0（幂等）。
    """
    from . import session_adapter
    group_conn = db.get_connection()
    try:
        binding = group_conn.execute('SELECT session_id FROM session_bindings WHERE profile=? AND title=?',
                                     (profile, session_name)).fetchone()
    finally:
        group_conn.close()
    bound_sid = session_adapter.compression_tip(profile, binding['session_id']) if binding else None
    db_path = _profile_state_db(profile)
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        # -c <name> 存的是 sessions.title（hermes_cli/main.py _resolve_continue_arg 实证）
        sess = conn.execute('SELECT id FROM sessions WHERE id=?', (bound_sid,)).fetchone() if bound_sid else conn.execute(
            "SELECT id FROM sessions WHERE title=? ORDER BY started_at DESC LIMIT 1",
            (session_name,),
        ).fetchone()
        if sess is None:
            return 0
        sid = sess["id"]
        # 最后一条 active 的 user 消息（唤醒信号 -q 即 user 消息）
        last_user = conn.execute(
            "SELECT id FROM messages WHERE session_id=? AND role='user' AND active=1 "
            "ORDER BY id DESC LIMIT 1",
            (sid,),
        ).fetchone()
        if last_user is None:
            return 0
        cur = conn.execute(
            "UPDATE messages SET active=0 WHERE session_id=? AND id>=? AND active=1",
            (sid, last_user["id"]),
        )
        # 审计一致：rewind_count+1（hermes 原生 rewind 同款）
        conn.execute(
            "UPDATE sessions SET rewind_count = COALESCE(rewind_count,0)+1 WHERE id=?",
            (sid,),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def _mark_seen_current(conn: sqlite3.Connection, profile: str, project: str,
                       kind: str, thread_id: int | None) -> int:
    """追回指定项目会话最近一条唤醒 job 的 trigger_msg_id。

    仅将当前执行对应的消息置 seen，其余 pending 消息保留。
    """
    job = conn.execute(
        "SELECT trigger_msg_id FROM wake_jobs "
        "WHERE profile=? AND project=? AND kind=? AND thread_id IS ? AND trigger_msg_id IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (profile, project, kind, thread_id),
    ).fetchone()
    if job is None or job["trigger_msg_id"] is None:
        return 0
    cur = conn.execute(
        "UPDATE inbox SET status='seen', seen_at=? "
        "WHERE profile=? AND message_id=? AND status='pending'",
        (db.now_iso(), profile, job["trigger_msg_id"]),
    )
    conn.commit()
    return cur.rowcount


def _cancel_jobs(conn: sqlite3.Connection, profile: str, project: str,
                 kind: str, thread_id: int | None) -> int:
    """取消指定项目会话的 queued/running wake_jobs。"""
    cur = conn.execute(
        "UPDATE wake_jobs SET status='cancelled', ended_at=? "
        "WHERE profile=? AND project=? AND kind=? AND thread_id IS ? AND status IN ('queued','running')",
        (db.now_iso(), profile, project, kind, thread_id),
    )
    conn.commit()
    return cur.rowcount


def _release_tasks(profile: str, project: str) -> int:
    """任务池 in_progress 退回 pending（按 role 定位，防 bot 锁死）。方案B：单库 group.db。"""
    # profile → role（members 表）
    conn = db.get_connection()
    try:
        db.init_schema(conn)
        role = profile
        cur = conn.execute(
            "UPDATE tasks SET status='pending', claimed_by='', claimed_at='' "
            "WHERE claimed_by=? AND status='in_progress' AND project=?",
            (role, project),
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def stop_profile(profile: str, project: str, thread_id: int | None = None) -> dict:
    """Stop one project work session, or one explicitly selected topic session."""
    if not project or not project.strip():
        raise ValueError('project is required for stop')
    if not profile or db.is_builtin_user(profile):
        raise ValueError('a bot profile is required for stop')
    if thread_id is not None and (type(thread_id) is not int or thread_id <= 0):
        raise ValueError('thread_id must be a positive integer')
    from .session_adapter import _SWITCH_LOCK
    # Serializes stop with new-session switches and spawn. Never hold a DB write
    # transaction while waiting for this mutex.
    with _SWITCH_LOCK:
        if thread_id is not None:
            conn = db.get_connection()
            try:
                if not conn.execute('SELECT 1 FROM threads WHERE id=? AND project=?',
                                    (thread_id, project)).fetchone():
                    raise ValueError('thread does not belong to project')
            finally:
                conn.close()
        return _stop_session(profile, project, thread_id)


def _stop_session(profile: str, project: str, thread_id: int | None) -> dict:
    """叫停一个 bot。返回 {stopped, killed, soft_deleted, seen, cancelled, released, pending_cancelled}。"""
    kind = 'chat' if thread_id is not None else 'work'
    session = wake.session_name(profile, kind, thread_id, project)
    wake.mark_intentional_stop(profile, session)
    result = {
        "profile": profile,
        "project": project,
        "thread_id": thread_id,
        "killed": 0,
        "soft_deleted": 0,
        "seen": 0,
        "cancelled": 0,
        "released": 0,
        "pending_cancelled": 0,
    }

    # 1. 硬杀进程树（🔴 2026-09-09 修：壳 pid 全废——_active_procs 恒空、心跳 pid 是
    # 真 agent pid 但只在首调 group_read_inbox 后才有。改命令行实时匹配（照
    # stop_engine.py 模式），找到真 agent 进程后 taskkill /T 连树杀（venv 中间层+MCP）。
    _found = wake.find_bot_processes(profile, session=session)
    pids = set(_found) if _found else set()   # None(查询失败)=按空处理：靠心跳pid兜底
    # Heartbeats lack project identity: only the exact session matcher is safe.
    for pid in pids:
        if _kill_process_tree(pid):
            result["killed"] += 1

    # 2. 等 reaper 确认进程退出，这是软删除的前置条件。
    #    确认失败时跳过软删除；8s 超时说明进程
    #    卡在不可中断态或 reaper 阻塞，此时软删会撞「软删后 bot 再 flush 悬空行」
    #    的坑。宁可这轮不清（下次叫停再清），不可硬上。
    dead = _wait_dead(profile, session)
    result["stopped"] = dead
    if not dead:
        result['detail'] = '进程仍存活或无法确认退出，保留任务与队列占用'
        result['soft_deleted'] = -1
        return result

    # 3. 软删半截尾巴（同会话保留，只清最后一条 user 消息起的 active 行）
    #    会话名按最近一次唤醒的 kind/thread_id 派生
    conn = db.get_connection()
    try:
        db.init_schema(conn)
        # 软删除定位限定 kind='work'——叫停的是「发错的
        # work 指令」，软删就该针对 work 会话。此前取「最近任意 job」：叫停前一刻
        # 若有 chat job 入队（话题里有人@它），会误删 chat 会话的讨论思考历史。
        # 与 _mark_seen_current 的 kind='work' 限定对齐。chat 会话的叫停语义
        # （话题讨论中断）是另一个需求，不做猜测处理。
        job = conn.execute(
            "SELECT kind, thread_id, project FROM wake_jobs "
            "WHERE profile=? AND project=? AND kind=? AND thread_id IS ? ORDER BY id DESC LIMIT 1",
            (profile, project, kind, thread_id),
        ).fetchone()
        from . import bot_identity
        bot = bot_identity.resolve(conn, project, profile)
        if kind == 'work' and job is not None and dead and (not bot or bot['runtime'] == 'hermes'):
            _tid = job["thread_id"] if "thread_id" in job.keys() else None
            _proj = job["project"] if "project" in job.keys() else "xjk"
            session = wake.session_name(profile, "work", _tid, _proj)
            result["soft_deleted"] = _soft_delete_tail(profile, session)
        elif kind == 'work' and job is not None and not dead:
            result["soft_deleted"] = -1  # 死透确认失败，软删跳过（下轮叫停再清）

        # 4. 追回当前指令（trigger_msg_id 置 seen）
        result["seen"] = _mark_seen_current(conn, profile, project, kind, thread_id)

        # 5. 取消 queued/running job
        result["cancelled"] = _cancel_jobs(conn, profile, project, kind, thread_id)
    finally:
        conn.close()

    # Stop releases interrupted work for later scheduling. Pending work survives.
    if kind == 'work':
        result["released"] = _release_tasks(profile, project)

    return result
