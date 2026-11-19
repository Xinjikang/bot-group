"""Durable, ordered scheduling after a topic message has been acknowledged."""

import logging
import threading
import time
from contextlib import closing

from . import db, events, wake

logger = logging.getLogger(__name__)
changed = threading.Event()

# req#169 缺陷③：跨话题重试阻塞——一个持续失败话题不能阻塞其他话题。
# 异常按话题退避（进程内内存态，重启清零即恢复处理，job 永不丢弃）：
# 同一话题在退避期内的全部待办跳过，其他话题继续；退避到期后从该话题
# 最早待办重试。成功处理即清除退避计数。job 本身不删不丢——重启续办与
# 幂等语义（原 ID 保留、不重复投递）不变。
_BACKOFF_STEPS = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)  # 秒，封顶 30
_retry_after: dict[int, float] = {}  # thread_id -> monotonic 下次可处理时间
_retry_count: dict[int, int] = {}    # thread_id -> 连续失败次数
_retry_lock = threading.Lock()


def _mark_backoff(thread_id: int) -> None:
    """处理异常：该话题退避（指数递增），其他话题不受影响。"""
    with _retry_lock:
        count = _retry_count.get(thread_id, 0)
        _retry_count[thread_id] = count + 1
        step = _BACKOFF_STEPS[min(count, len(_BACKOFF_STEPS) - 1)]
        _retry_after[thread_id] = time.monotonic() + step
    logger.warning("topic.dispatch thread_id=%s failed; backing off %.1fs", thread_id, step)


def _clear_backoff(thread_id: int) -> None:
    """处理成功：清除该话题退避态。"""
    with _retry_lock:
        _retry_count.pop(thread_id, None)
        _retry_after.pop(thread_id, None)


def _backed_off(thread_id: int) -> bool:
    with _retry_lock:
        return _retry_after.get(thread_id, 0.0) > time.monotonic()


def _pick_job(conn) -> dict | None:
    """取全局最早待办，跳过退避期话题（每话题只处理其最早待办）。"""
    rows = conn.execute(
        "SELECT d.* FROM topic_dispatch d JOIN threads t ON t.id=d.thread_id "
        "JOIN projects p ON p.name=t.project WHERE p.archived=0 AND p.readiness_status='ready' "
        "ORDER BY d.id"
    ).fetchall()
    for job in rows:
        if not _backed_off(job["thread_id"]):
            return job
    return None


def schedule(conn, thread_id, message_id, *, consumes_turn=False):
    # A silent pass has no message of its own. Bound its read to the last
    # visible message exactly as for a normal send.
    through_id = message_id
    if through_id is None:
        through_id = conn.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
    conn.execute("INSERT INTO topic_dispatch(thread_id,through_id,trigger_msg_id,consumes_turn) VALUES(?,?,?,?)",
                 (thread_id, through_id, message_id, consumes_turn))


def _state(conn, tid):
    row = conn.execute(
        "SELECT t.status,t.project,t.round,t.turn_profile,t.turn_parent_id,t.turn_skipped,t.members,"
        "t.creator,t.turn_wake_at,p.archived,p.readiness_status FROM threads t "
        "JOIN projects p ON p.name=t.project WHERE t.id=?", (tid,),
    ).fetchone()
    # 成员停用等变化不在此快照——提交前由 _candidate_wakeable 按实时活跃
    # 成员复核并当轮重判（req#169 缺陷②：成员变化时推进到有效发言位）。
    return row


def _turn_consumed(conn, job):
    # The current speaker may already have replied while older interjections
    # were waiting. Its queued response will evaluate their @ edges in order.
    return conn.execute("SELECT 1 FROM topic_dispatch WHERE thread_id=? AND id>? AND consumes_turn=1 LIMIT 1",
                        (job["thread_id"], job["id"])).fetchone() is not None


def _candidate_wakeable(conn, nxt, tid, project) -> bool:
    """req#169 缺陷②：提交前复核候选可唤醒——profile 仍 active 且在成员列表。

    选人快照后成员可能被停用（发送后/选人中停用），落 turn 前必须复核；
    不可唤醒则重判到有效发言位（活跃成员接棒）。
    """
    from . import router
    profile = nxt["profile"]
    members = router._round_members(conn, tid)
    return profile in members and router._profile_active(conn, project, profile)


def process_one(conn):
    """Consume the oldest pending item. Both phases require a dedicated connection.

    锁外选人（快照读，可含任意等待）→ 锁内复核（状态/候选可唤醒未变才写）。
    req#169 缺陷①：收口选人不在写锁内——选人/等待/网络 I/O 不持锁。
    req#169 缺陷③：任何阶段异常都按话题退避（job 保留，其他话题继续）。
    """
    from . import router

    tid = None
    try:
        with conn:
            conn.execute("BEGIN")
            job = _pick_job(conn)
            if job is None:
                return False
            tid = job["thread_id"]
            state = _state(conn, tid)
            topic = state
            started = time.perf_counter()
            nxt = (router._next_speaker(conn, tid, job["through_id"])
                   if topic and topic["status"] == "open" and not _turn_consumed(conn, job) else None)
            select_ms = (time.perf_counter() - started) * 1000

        locked = 0.0
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            locked = time.perf_counter()
            if not conn.execute("SELECT 1 FROM topic_dispatch WHERE id=?", (job["id"],)).fetchone():
                return True
            if _state(conn, tid) != state:
                return True
            # req#169 缺陷②：锁内复核候选可唤醒——选人快照后成员停用时重判，
            # 推进到有效发言位（活跃成员接棒），绝不把 turn 落在停用成员身上。
            if nxt and topic and topic["status"] == "open" and not _candidate_wakeable(
                    conn, nxt, tid, topic["project"]):
                nxt = router._next_speaker(conn, tid, job["through_id"])
            # Consuming the durable head and writing the turn and wake are atomic.
            conn.execute("DELETE FROM topic_dispatch WHERE id=?", (job["id"],))
            if not topic or topic["status"] != "open" or _turn_consumed(conn, job):
                return True
            if nxt:
                router._set_turn(conn, tid, nxt["profile"], nxt["parent_id"], nxt["round"])
                wake.enqueue(conn, nxt["profile"], "chat", "thread-open", job["trigger_msg_id"],
                             thread_id=tid, project=topic["project"], commit=False)
                next_round = nxt["round"]
            else:
                router.settle_thread(conn, tid, through_id=job["through_id"])
                next_round = topic["round"]
                if conn.execute("SELECT 1 FROM topic_dispatch WHERE thread_id=? LIMIT 1", (tid,)).fetchone():
                    # A user interjection was already saved while this last pass
                    # was being evaluated. It resumes the following round.
                    next_round += 1
                    conn.execute("UPDATE threads SET status='open',round=? WHERE id=?", (next_round, tid))
            if next_round != topic["round"]:
                # Interjections accepted during the snapshot belong to the new
                # round. Only still-pending messages have provisional routing data.
                conn.execute("UPDATE messages SET round=? WHERE sender='user' AND id IN "
                             "(SELECT trigger_msg_id FROM topic_dispatch WHERE thread_id=?)",
                             (next_round, tid))
    except Exception:
        # req#169 缺陷③：按话题退避，不因一个话题持续失败阻塞其他话题。
        # job 保留（with conn 回滚），退避到期后重试同一条——不丢不重复。
        if tid is not None:
            _mark_backoff(tid)
        logger.exception("topic.dispatch failed for thread_id=%s; pending work retained", tid)
        return True
    finished = time.perf_counter()
    _clear_backoff(tid)
    logger.debug("topic.dispatch thread_id=%s message_id=%s select_and_wait_ms=%.1f write_ms=%.1f",
                 tid, job["trigger_msg_id"], select_ms, (finished - locked) * 1000)
    events.bus.publish("thread.updated", {"thread_id": tid})
    return True


def run_forever(stop):
    while not stop.is_set():
        try:
            with closing(db.get_connection()) as conn:
                while not stop.is_set():
                    if wake._shutting_down.is_set():
                        stop.wait(0.1)
                        continue
                    changed.clear()
                    if not process_one(conn):
                        # MCP leases may release a savepoint before their outer
                        # transaction commits. Polling also covers other processes.
                        changed.wait(1)
        except Exception:
            logger.exception("Topic dispatch failed; pending work retained")
            stop.wait(1)
