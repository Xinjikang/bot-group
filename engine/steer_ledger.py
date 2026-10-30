"""req#83 task#145：/steer 纠偏账本层（路线 A：纯账本，留传输接口待 adapter 落地）。

账本层与传输通道解耦。agent.steer 是进程内方法，subprocess 通道缺少对应 IPC，
ACP adapter 也未提供 steer 接口。本模块负责引擎侧账本：

- 回执五层分层（U3/T4 硬约束）：queued / inserted / persisted / model_requested /
  behavior_observed。落库只报「已写入会话」，进实际模型请求才报 model_requested，
  行为响应单独验收。
- 悬空终态（U2 硬约束）：终态由精确 operation_id 的消费证据 + leftover 判定；确定未消费
  报 undelivered，回执缺失证据不足报 pending_verify，避免误判重投。
- 精确绑定运行（S4）：operation 绑定 (project, profile, kind, thread_id, session_id,
  generation, run_started_at)；跨 /new 后 generation 不匹配 → generation_stale 失效。
- 传输接口：record_delivery() / report_consumed() / report_leftover() / finalize_target_ended()
  供 adapter 落地后调用；本层不主动传输。

状态机：
    queued → delivering → consumed →（行为响应单独验收）
             ↓              ↓
          target_ended   pending_verify（回执缺失证据不足）
             ↓              ↓
        generation_stale  undelivered（确定未消费）
        expired（TTL 过期）
"""

from __future__ import annotations

import sqlite3
import uuid

from . import db

# 回执五层（U3/T4 硬约束，升序：越靠后证据越强）
DELIVER_LAYERS = ("queued", "inserted", "persisted", "model_requested", "behavior_observed")

# 终态 status 集合（不再流转）
TERMINAL_STATUSES = ("consumed", "undelivered", "target_ended", "generation_stale", "expired")


class SteerError(ValueError):
    """账本操作参数/状态非法。"""


def new_operation(conn: sqlite3.Connection, project: str, profile: str, kind: str,
                  thread_id: int | None, text: str, session_id: str = "",
                  generation: int | None = None, run_started_at: float | None = None) -> dict:
    """落一条纠偏操作（queued 待投递），返回 operation 记录。

    精确绑定运行（S4）：session_id / generation / run_started_at 在投递时绑定；
    账本建账时先落 queued，绑定字段可留空（投递时补）。
    """
    text = (text or "").strip()
    if not text:
        raise SteerError("纠偏正文不能为空")
    op_id = uuid.uuid4().hex
    tid = thread_id if (kind == "chat" and thread_id is not None) else 0
    now = db.now_iso()
    conn.execute(
        "INSERT INTO steer_operations "
        "(operation_id, project, profile, kind, thread_id, session_id, generation, "
        " run_started_at, text, status, deliver_layer, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (op_id, project, profile, kind, tid, session_id, generation, run_started_at,
         text, "queued", "", now, now),
    )
    conn.commit()
    return get_operation(conn, op_id)


def get_operation(conn: sqlite3.Connection, operation_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM steer_operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def _advance(conn: sqlite3.Connection, operation_id: str, status: str,
             deliver_layer: str = "", result: str = "", leftover: str | None = None) -> dict:
    """账本状态推进（状态机单向前进，终态后拒绝再写）。"""
    row = conn.execute(
        "SELECT * FROM steer_operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if row is None:
        raise SteerError(f"操作不存在：{operation_id}")
    if row["status"] in TERMINAL_STATUSES:
        raise SteerError(f"操作已终态（{row['status']}），不能再推进")
    sets = ["status=?", "updated_at=?"]
    params = [status, db.now_iso()]
    if deliver_layer:
        sets.append("deliver_layer=?")
        params.append(deliver_layer)
    if result:
        sets.append("result=?")
        params.append(result)
    if leftover is not None:
        sets.append("leftover=?")
        params.append(leftover)
    params.append(operation_id)
    conn.execute(
        f"UPDATE steer_operations SET {', '.join(sets)} WHERE operation_id=?", params
    )
    conn.commit()
    return get_operation(conn, operation_id)


def record_delivery(conn: sqlite3.Connection, operation_id: str, session_id: str,
                    generation: int, run_started_at: float) -> dict:
    """投递动作已发起（delivering，等消费证据）——传输通道落地的入口。

    传输通道（hermes_gateway_steer）投递时调用：绑定目标会话 / 网关 execution_generation /
    运行，标记已投递等消费证据。generation 是网关 execution_generation
    （hermes_executions.generation，设计 §4.8），不是 session_bindings.generation。
    五层口径：这一层仍属「已入队/已投递」，未到 inserted。
    """
    row = conn.execute(
        "SELECT * FROM steer_operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if row is None:
        raise SteerError(f"操作不存在：{operation_id}")
    # 重复投递防护：已绑定 generation 且与本次不同 → generation_stale（不盲目重投）
    if row["generation"] is not None and row["status"] in ("queued",) and row["generation"] != generation:
        return _advance(conn, operation_id, "generation_stale",
                        result=f"已绑定不同执行代际（generation {row['generation']}→{generation}），拒绝重复投递")
    conn.execute(
        "UPDATE steer_operations SET session_id=?, generation=?, run_started_at=?, updated_at=? "
        "WHERE operation_id=?",
        (session_id, generation, run_started_at, db.now_iso(), operation_id),
    )
    conn.commit()
    return _advance(conn, operation_id, "delivering", "queued",
                    result="已投递，等待消费证据")


def report_consumed(conn: sqlite3.Connection, operation_id: str,
                    deliver_layer: str = "inserted") -> dict:
    """上报消费证据（consumed，五层推进到 inserted/persisted/model_requested）。

    adapter 落地后调用：messages 出现带 OUT-OF-BAND marker 的 user 行 = inserted；
    落 SessionDB = persisted；进实际模型请求 = model_requested。
    """
    if deliver_layer not in DELIVER_LAYERS:
        raise SteerError(f"非法回执层级 {deliver_layer}")
    return _advance(conn, operation_id, "consumed", deliver_layer,
                     result=f"纠偏已消费（回执层级={deliver_layer}）")


def report_leftover(conn: sqlite3.Connection, operation_id: str, leftover: str, *, result: str = '') -> dict:
    """上报退出 leftover（悬空证据）：finalizer 返回的 pending_steer 交还引擎。

    确定未消费 → undelivered（U2：本 run 结束即关闭本次投递，不重投）；
    回执缺失证据不足 → 由调用方决定 pending_verify。
    """
    row = conn.execute(
        "SELECT * FROM steer_operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if row is None:
        raise SteerError(f"操作不存在：{operation_id}")
    # 有 leftover = 目标退出时纠偏仍 pending → 确定未消费
    return _advance(conn, operation_id, "undelivered", row["deliver_layer"] or "",
                    result=result or "目标运行结束，纠偏未消费（leftover 交还）", leftover=leftover)


def mark_pending_verify(conn: sqlite3.Connection, operation_id: str,
                        result: str = "") -> dict:
    """回执缺失且证据不足 → 待核实（pending_verify，非终态，可后续核对）。"""
    return _advance(conn, operation_id, "pending_verify", result=result or "回执缺失，证据不足，待核实")


def finalize_target_ended(conn: sqlite3.Connection, operation_id: str,
                          result: str = "") -> dict:
    """目标已结束（进程退出且纠偏未投递/未消费）→ target_ended 终态。"""
    return _advance(conn, operation_id, "target_ended",
                    result=result or "目标运行已结束，纠偏未能投递")


def expire_stale(conn: sqlite3.Connection, ttl_seconds: float = 600.0,
                 now: float | None = None) -> int:
    """TTL 过期清理：queued/delivering/pending_verify 超过 ttl 的 → expired 终态。

    返回处理的条数。由扫描器/心跳周期调用（有界重投语义，不静默丢）。
    """
    import time
    now = now if now is not None else time.time()
    # created_at 是 ISO 串，用 unix 时间比对需要转换；这里用 SQL 时间串近似：
    # Compare the same local ISO format used by db.now_iso().
    from datetime import datetime, timedelta
    cutoff_dt = datetime.fromtimestamp(now) - timedelta(seconds=ttl_seconds)
    cutoff_iso = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S")
    cur = conn.execute(
        "UPDATE steer_operations SET status='expired', updated_at=?, "
        " result='TTL 过期，纠偏未消费' "
        "WHERE status IN ('queued','delivering','pending_verify') AND created_at < ?",
        (db.now_iso(), cutoff_iso),
    )
    conn.commit()
    return cur.rowcount


def reconcile_targets(conn) -> int:
    """Close queued intents when their exact target is invalidated.

    Delivery with missing consumption evidence stays pending_verify until TTL.
    Legacy unbound queued intents cannot safely be assigned to a later run.
    steer_operations.generation 现绑定网关 execution_generation（设计 §4.8），
    不在此处与 session_bindings.generation 比对——两套代际分开对账，防代际混用误判；
    会话级失效由目标 run 生命周期（run_started_at → wake_runs ended_at）判定。
    """
    changed = 0
    for row in pending_operations(conn):
        status = reason = None
        if row['run_started_at'] is None and row['status'] == 'queued':
            status, reason = 'undelivered', '缺少精确运行绑定，未投递，禁止转移到后续运行'
        elif row['run_started_at'] is not None:
            run = conn.execute(
                'SELECT ended_at FROM wake_runs WHERE profile=? AND project=? AND started_at=? '
                'AND COALESCE(thread_id,0)=?',
                (row['profile'], row['project'], row['run_started_at'], row['thread_id'])).fetchone()
            if run and run['ended_at'] is not None:
                status = 'target_ended' if row['status'] == 'queued' else 'pending_verify'
                reason = '目标运行结束；未投递' if status == 'target_ended' else '目标运行结束；消费回执缺失，待核实'
        if status and status != row['status']:
            cur = conn.execute('UPDATE steer_operations SET status=?,result=?,updated_at=? WHERE operation_id=? AND status=?',
                               (status, reason, db.now_iso(), row['operation_id'], row['status']))
            changed += cur.rowcount
    conn.commit()
    return changed


def pending_operations(conn: sqlite3.Connection, project: str = "",
                        profile: str = "") -> list[dict]:
    """列出未终态操作（供扫描器核对/adapter 恢复）。"""
    sql = ("SELECT * FROM steer_operations WHERE status NOT IN "
           "('consumed','undelivered','target_ended','generation_stale','expired')")
    params: list = []
    if project:
        sql += " AND project=?"
        params.append(project)
    if profile:
        sql += " AND profile=?"
        params.append(profile)
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# ---------------------------------------------------------------------------
# 注入时机双路径状态机（T3/U2 硬约束）：纠偏在「工具批边界」或「API 前 drain」双路径取先到。
# 账本层不驱动注入（那是传输通道的事），只提供「到达时点」记录，供 adapter 消费。
# ---------------------------------------------------------------------------

def record_arrival(conn: sqlite3.Connection, operation_id: str, boundary: str) -> dict:
    """记录纠偏到达时点（boundary ∈ {'tool_batch','api_pre_drain'}），供注入时机判定审计。"""
    if boundary not in ("tool_batch", "api_pre_drain"):
        raise SteerError(f"非法注入边界 {boundary}")
    row = conn.execute(
        "SELECT * FROM steer_operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if row is None:
        raise SteerError(f"操作不存在：{operation_id}")
    conn.execute(
        "UPDATE steer_operations SET result=? WHERE operation_id=?",
        (f"到达时点={boundary}（{row['result']}）" if row["result"] else f"到达时点={boundary}",
         operation_id),
    )
    conn.commit()
    return get_operation(conn, operation_id)
