"""Durable, ordered scheduling after a topic message has been acknowledged."""

import logging
import threading
import time
from contextlib import closing

from . import db, events, wake

logger = logging.getLogger(__name__)
changed = threading.Event()


def schedule(conn, thread_id, message_id, *, consumes_turn=False):
    # A silent pass has no message of its own. Bound its read to the last
    # visible message exactly as for a normal send.
    through_id = message_id
    if through_id is None:
        through_id = conn.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]
    conn.execute("INSERT INTO topic_dispatch(thread_id,through_id,trigger_msg_id,consumes_turn) VALUES(?,?,?,?)",
                 (thread_id, through_id, message_id, consumes_turn))


def _state(conn, tid):
    from . import router
    row = conn.execute(
        "SELECT t.status,t.project,t.round,t.turn_profile,t.turn_parent_id,t.turn_skipped,t.members,"
        "t.creator,t.turn_wake_at,p.archived,p.readiness_status FROM threads t "
        "JOIN projects p ON p.name=t.project WHERE t.id=?", (tid,),
    ).fetchone()
    return row, router._round_members(conn, tid) if row else []


def _turn_consumed(conn, job):
    # The current speaker may already have replied while older interjections
    # were waiting. Its queued response will evaluate their @ edges in order.
    return conn.execute("SELECT 1 FROM topic_dispatch WHERE thread_id=? AND id>? AND consumes_turn=1 LIMIT 1",
                        (job["thread_id"], job["id"])).fetchone() is not None


def process_one(conn):
    """Consume the oldest pending item. Both phases require a dedicated connection."""
    from . import router

    with conn:
        conn.execute("BEGIN")
        job = conn.execute(
            "SELECT d.* FROM topic_dispatch d JOIN threads t ON t.id=d.thread_id "
            "JOIN projects p ON p.name=t.project WHERE p.archived=0 AND p.readiness_status='ready' "
            "ORDER BY d.id LIMIT 1"
        ).fetchone()
        if job is None:
            return False
        tid = job["thread_id"]
        state = _state(conn, tid)
        topic = state[0]
        started = time.perf_counter()
        nxt = (router._next_speaker(conn, tid, job["through_id"])
               if topic and topic["status"] == "open" and not _turn_consumed(conn, job) else None)

    with conn:
        conn.execute("BEGIN IMMEDIATE")
        locked = time.perf_counter()
        if not conn.execute("SELECT 1 FROM topic_dispatch WHERE id=?", (job["id"],)).fetchone():
            return True
        if _state(conn, tid) != state:
            return True
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
    finished = time.perf_counter()
    logger.debug("topic.dispatch thread_id=%s message_id=%s select_and_wait_ms=%.1f write_ms=%.1f",
                 tid, job["trigger_msg_id"], (locked - started) * 1000, (finished - locked) * 1000)
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
