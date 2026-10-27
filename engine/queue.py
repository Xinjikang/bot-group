"""串行队列 worker + 看门狗。

- 队列 worker：轮询 wake_jobs 表 queued 状态，逐个 deliver（发唤醒信号），标记 done/failed。
- 串行语义：同 profile 同 kind 在 queued/running 时不重复入队（partial unique index 保证），
  所以同一 profile 同 kind 同时只有一个 job 在跑。
- 看门狗：running 超过 3600s 的 job 标记 killed（防 deliver 卡死；kill 不碰 inbox）。

唤醒是 fire-and-forget（API server / subprocess 都是发完即返回），bot 进程由 gateway
管理，引擎不持有进程句柄。bot 是否真正干完活，由 scanner 的「挂死重投」兜底。
"""

from __future__ import annotations

import json
import logging
import time

from . import db, wake

logger = logging.getLogger(__name__)

WATCHDOG_TIMEOUT_SECONDS = 3600  # 量纲=排空时长，可配置
CHAT_CONTEXT_SECONDS = 600  # chat 执行上下文窗口（防误开工运行时校验事实源）。
# 此窗口约等于一次话题讨论的执行时长（读取话题、思考、回复，分钟级），
# 不是看门狗的排空时长。3600 会把 work/chat 并行变成互斥一小时（进过话题的 bot 一小时内
# 领不了活），阻碍 work/chat 并行执行。600s 为保守上限：多数讨论几分钟内
# 完成，真有长讨论（超10分钟仍在跑）时宁可漏挡一次误开工（还有工具集/信号层兜底），
# 不可误锁一小时的活。


def _mark(conn, job_id: int, status: str, detail: str = "", exit_code=None) -> None:
    conn.execute(
        "UPDATE wake_jobs SET status=?, detail=?, exit_code=?, ended_at=? WHERE id=? AND status='running'",
        (status, detail, exit_code, db.now_iso(), job_id),
    )
    conn.commit()


def _profile_disabled(conn, project: str, profile: str) -> bool:
    """未加入、停用或删除的成员均不能投递。"""
    from .router import _profile_active
    return not _profile_active(conn, project, profile)


def _discard_queued(conn, job_id: int, detail: str) -> None:
    conn.execute(
        "UPDATE wake_jobs SET status='done',detail=?,ended_at=? WHERE id=? AND status='queued'",
        (detail, db.now_iso(), job_id),
    )
    conn.commit()


def process_one(conn) -> bool:
    """取一个 queued job 并 deliver。返回是否处理了 job。"""
    from . import project_readiness
    job = None
    for candidate in conn.execute("SELECT * FROM wake_jobs WHERE status='queued' ORDER BY id").fetchall():
        # 停用者不投递（规格 27.3 第 6 点 + req#27 按项目维度）：存量 job 也不许
        # 把已停用的 bot 叫醒。丢弃不投递、留审计（不改任何业务数据）。
        if _profile_disabled(conn, candidate["project"], candidate["profile"]):
            _discard_queued(conn, candidate["id"], "profile disabled")
            continue
        if not project_readiness.is_ready(conn, candidate['project']):
            continue
        if candidate["kind"] == "chat" and candidate["thread_id"] is not None:
            thread = conn.execute("SELECT status,turn_profile,project,members,round,turn_parent_id FROM threads WHERE id=?", (candidate["thread_id"],)).fetchone()
            if thread is None or thread["status"] != "open" or (
                thread["turn_profile"] != candidate["profile"] or
                thread["project"] != candidate["project"] or
                candidate["profile"] not in json.loads(thread["members"] or '[]')
            ):
                _discard_queued(conn, candidate["id"], "superseded topic turn")
                continue
            if candidate["trigger"].startswith("recover:chat:"):
                parent = thread["turn_parent_id"]
                expected = f"recover:chat:{thread['round']}:{parent if parent is not None else 'root'}"
                if candidate["trigger"] != expected:
                    _discard_queued(conn, candidate["id"], "superseded topic recovery")
                    continue
        if wake.session_busy(candidate["profile"], candidate["kind"], candidate["thread_id"], candidate["project"]):
            continue
        if candidate['kind'] == 'chat' and wake.topic_busy(conn, candidate['thread_id'], candidate['project']):
            continue
        job = candidate
        break
    if job is None:
        return False

    # Recheck topic eligibility inside the atomic claim; session_busy may have
    # yielded while another connection advanced the turn or removed a member.
    cur = conn.execute(
        "UPDATE wake_jobs SET status='running', started_at=? WHERE id=? AND status='queued' "
        "AND EXISTS (SELECT 1 FROM projects p WHERE p.name=wake_jobs.project AND p.archived=0 AND p.readiness_status='ready') "
        f"AND (profile='{db.BUILTIN_USER_PROFILE}' OR EXISTS (SELECT 1 FROM project_members pm JOIN members m ON m.profile=pm.profile "
        "WHERE pm.project=wake_jobs.project AND pm.profile=wake_jobs.profile AND pm.active=1)) "
        "AND (kind!='chat' OR thread_id IS NULL OR EXISTS ("
        "SELECT 1 FROM threads t WHERE t.id=wake_jobs.thread_id AND t.status='open' "
        "AND t.turn_profile=wake_jobs.profile AND t.project=wake_jobs.project "
        "AND EXISTS (SELECT 1 FROM json_each(t.members) m WHERE m.value=wake_jobs.profile) "
        "AND (wake_jobs.trigger NOT LIKE 'recover:chat:%' OR wake_jobs.trigger="
        "'recover:chat:' || t.round || ':' || COALESCE(t.turn_parent_id,'root'))))",
        (db.now_iso(), job["id"]),
    )
    if cur.rowcount == 0:
        if not project_readiness.is_ready(conn, job['project']):
            conn.commit()
            return False
        # The UPDATE holds the write transaction until stale queued work is
        # retired. Already claimed/cancelled rows retain their existing state.
        _discard_queued(conn, job["id"], "superseded topic turn")
        return False
    if job["trigger"].startswith("recover:chat:"):
        # Waiting for the old process (or an uncertain process query) must not
        # consume the recovered bot's response window.
        conn.execute("UPDATE threads SET turn_wake_at=? WHERE id=?", (db.now_iso(), job["thread_id"]))
    conn.commit()

    try:
        # thread_id/project 列（schema 迁移同批）——列未存在时 .keys 容错
        _keys = job.keys()
        _tid = job["thread_id"] if "thread_id" in _keys else None
        _proj = job["project"] if "project" in _keys else "xjk"
        # req#82：need_reply 不再经信号全局后缀透传（已废弃），trigger_msg_id 只做
        # 注入正文里的触发消息标注；回复要求由注入正文逐条按 effective_need_reply 承载。
        _trigger_msg_id = job["trigger_msg_id"]
        from . import task_retry
        task_id = int(job['trigger'].split(':')[-1]) if job['trigger'].startswith('retry:task:') else None
        bound_task = task_retry.select_task(conn, job['profile'], _proj, task_id) if job['kind'] == 'work' else None
        if job['kind'] == 'work' and (job['trigger'] == 'scan:task' or job['trigger'].startswith('retry:task:')) and bound_task is None:
            _mark(conn, job['id'], 'cancelled', 'No published complete task remains eligible')
            return True
        if job['kind'] == 'work' and job['trigger'] == 'scan:req' and bound_task is None:
            requirement_signal = wake._requirement_signal(conn, job['profile'], 'work', _proj)
            pending = wake._snapshot_pending(conn, job['profile'], 'work', _tid, _proj)
            if not requirement_signal and not pending:
                _mark(conn, job['id'], 'cancelled', 'No actionable requirement, unread message or eligible task remains')
                return True
        result = wake.deliver(job["profile"], job["kind"], thread_id=_tid, project=_proj,
                              trigger_msg_id=_trigger_msg_id, job_id=job['id'], task_id=task_id or bound_task)
        current = conn.execute('SELECT status FROM wake_jobs WHERE id=?', (job['id'],)).fetchone()
        if current is None or current['status'] == 'cancelled':
            return True
        if result.get("busy"):
            conn.execute("UPDATE wake_jobs SET status='queued',started_at=NULL WHERE id=?", (job["id"],))
            conn.commit()
        elif result.get("ok"):
            # chat 唤醒投递成功：记执行上下文窗口（防误开工运行时校验的事实源——
            # running 只是投递窗口，bot 真正执行在 job done 之后，须用宽限窗口覆盖）
            if job["kind"] == "chat":
                until = db.now_iso(seconds_offset=CHAT_CONTEXT_SECONDS)
                conn.execute(
                    "UPDATE wake_jobs SET chat_context_until=? WHERE id=?",
                    (until, job["id"]),
                )
            # req#58：心跳归属不再写 last_wake_kind/last_wake_thread_id——判活事实源
            # 已改为心跳槽的 thread_id（MCP beat 时按会话报，heartbeat.active_entries
            # 按槽粒度输出），DB last_wake 关联是 profile 级单一归属、同 profile 双话题
            # 并行时会把 A 话题心跳替 B 话题气泡背书，已废弃删除。last_wake 两列保留在
            # schema 里（删列需重建表、风险大于收益），但不再读写。
            _mark(conn, job["id"], "done", result.get("detail", ""))
        else:
            _mark(conn, job["id"], "failed", result.get("detail", ""), exit_code=1)
            if not result.get('failure_handled') and not wake._shutting_down.is_set():
                task_retry.finish(job['profile'], _proj, bound_task, f"queue:{job['id']}",
                                  result.get('detail', 'delivery failed'), job_id=job['id'])
    except Exception as exc:  # noqa: BLE001
        logger.exception("deliver job %s failed", job["id"])
        _mark(conn, job["id"], "failed", str(exc), exit_code=1)
        if 'bound_task' in locals() and not wake._shutting_down.is_set():
            task_retry.finish(job['profile'], _proj, bound_task, f"queue:{job['id']}", str(exc), job_id=job['id'])
    return True


def watchdog(conn) -> int:
    """把 running 超过 WATCHDOG_TIMEOUT_SECONDS 的 job 标记 killed。返回 kill 数。"""
    cutoff = db.now_iso()
    # 用 started_at 字符串比较不可靠，改用时间戳：直接查 running 且 started_at 早于阈值
    rows = conn.execute(
        "SELECT id, started_at FROM wake_jobs WHERE status='running'"
    ).fetchall()
    killed = 0
    now = time.time()
    for r in rows:
        started = r["started_at"]
        if not started:
            continue
        try:
            t = time.mktime(time.strptime(started, "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            continue
        if now - t > WATCHDOG_TIMEOUT_SECONDS:
            _mark(conn, r["id"], "killed", "hang watchdog 3600s")
            killed += 1
    return killed


def run_forever(stop_event) -> None:
    """队列 worker 主循环（后台线程）。"""
    while not stop_event.is_set():
        if wake._shutting_down.is_set():
            stop_event.wait(0.1)
            continue
        conn = db.get_connection()
        try:
            db.init_schema(conn)
            processed = process_one(conn)
            watchdog(conn)
        except Exception:  # noqa: BLE001
            logger.exception("queue worker tick error")
        finally:
            conn.close()
        # 有活就快转，没活就慢等
        stop_event.wait(0.5 if processed else 2.0)
