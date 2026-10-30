"""req#136 task#261：/steer 到网关 session.steer 的运行中纠偏传输通道。

设计：docs/design/2026-10-30-steer-transport.md（定稿 375254d）。

本模块只做传输：定位运行中执行 → session.steer 投递 → 回执映射回账本。
账本动作全部走 steer_ledger，本模块不直接写 steer_operations。
投递结果与目标状态是两个独立维度（设计 §4.3）：
- delivered:False = 确定未投递 → undelivered（state 只补原因）；
- pending_verify 只留给送达结果不确定（超时/断链/回执缺失/rejected 缺 delivered 字段）。
"""

from __future__ import annotations

import sqlite3

from . import steer_ledger
from .hermes_gateway_client import GatewayRefusal, GatewayTransportError


def _locate_running(conn: sqlite3.Connection, project: str, profile: str,
                    kind: str, thread_id: int | None) -> dict | None:
    """按 project+profile+kind+thread_id 定位唯一运行中执行（设计 §4.1）。

    返回 {execution_id, session_id, generation, started_at, home} 或 None（无运行中）；
    多匹配（身份歧义）抛 SteerError，由调用方转 pending_verify，不取最大 generation。
    """
    tid = thread_id if (kind == "chat" and thread_id is not None) else 0
    rows = conn.execute(
        "SELECT execution_id, session_id, generation, started_at, home "
        "FROM hermes_executions "
        "WHERE project=? AND profile=? AND kind=? AND COALESCE(thread_id,0)=? "
        "AND state='running' AND generation IS NOT NULL",
        (project, profile, kind, tid),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise steer_ledger.SteerError("目标身份歧义：同一目标出现多个运行中执行，拒绝投递")
    return dict(rows[0])


def _state_reason(conn: sqlite3.Connection, execution_id: str) -> str:
    """回查 hermes_executions.state 补充 undelivered 的原因（§4.3 投递结果/目标状态独立）。"""
    row = conn.execute(
        "SELECT state FROM hermes_executions WHERE execution_id=?",
        (execution_id,),
    ).fetchone()
    state = row["state"] if row is not None else "terminal"
    if state == "running":
        return "目标运行中但当前无法接收纠偏（rejected delivered=False，目标继续运行）"
    return "目标已结束（rejected delivered=False）"


def _map_refusal(conn: sqlite3.Connection, operation_id: str, text: str, exc: GatewayRefusal) -> dict:
    reason = exc.reason
    if reason == "stale_generation":
        steer_ledger.finalize_target_ended(conn, operation_id, "目标运行已结束（stale_generation）")
        return {"status": "target_ended", "detail": "目标运行已结束，纠偏未投递"}
    if reason in ("unsupported_control", "execution_not_ready"):
        steer_ledger.report_leftover(conn, operation_id, text,
                                     result=f"目标后端不支持运行中纠偏（{reason}）")
        return {"status": "undelivered", "detail": f"目标后端不支持纠偏（{reason}）"}
    # permission_denied / invalid_params / 其他拒绝：已 record_delivery，留准确账本结果
    steer_ledger.mark_pending_verify(conn, operation_id, f"投递被拒（{reason}）")
    return {"status": "pending_verify", "detail": f"投递被拒（{reason}）"}


def deliver_steer(conn: sqlite3.Connection, project: str, profile: str, kind: str,
                  thread_id: int | None, text: str, operation_id: str) -> dict:
    """定位运行中执行并投递一条 /steer，返回 {status, detail}。

    status ∈ {'queued', 'target_ended', 'undelivered', 'pending_verify'}。
    账本动作（record_delivery / report_leftover / finalize_target_ended /
    mark_pending_verify）全部在此完成，命令层只读返回的 status/detail 组装响应。
    """
    # 1. 定位运行中执行
    try:
        located = _locate_running(conn, project, profile, kind, thread_id)
    except steer_ledger.SteerError as exc:
        steer_ledger.mark_pending_verify(conn, operation_id, str(exc))
        return {"status": "pending_verify", "detail": str(exc)}

    if located is None:
        steer_ledger.finalize_target_ended(conn, operation_id, "目标当前无运行中 turn，纠偏未投递")
        return {"status": "target_ended", "detail": "目标当前无运行中 turn，纠偏未投递"}

    execution_id = located["execution_id"]
    session_id = located["session_id"]
    generation = located["generation"]
    started_at = located["started_at"]
    home = located["home"]

    # 2. 发起前绑定（delivering，通道接受层；generation=网关 execution_generation）
    steer_ledger.record_delivery(conn, operation_id, session_id, generation, started_at)

    # 3. 传输调用（只 discover，不 ensure——目标已有 running 执行，网关必在运行）
    from .hermes_gateway import connection
    client = None
    try:
        client, _, _ = connection(profile, home=home, read_only=True)
        result = client.rpc("session.steer", session_id=session_id, text=text,
                            execution_generation=generation)
    except GatewayRefusal as exc:
        return _map_refusal(conn, operation_id, text, exc)
    except GatewayTransportError:
        steer_ledger.mark_pending_verify(conn, operation_id, "送达结果未知（传输失败/超时），待核实")
        return {"status": "pending_verify", "detail": "送达结果未知，待核实"}
    finally:
        if client is not None:
            client.close()

    # 4. 回执映射（§4.3）
    status = result.get("status") if isinstance(result, dict) else None
    if status == "queued":
        # 通道接受，保持 delivering，等观察 OUT-OF-BAND marker 后 report_consumed(inserted)
        return {"status": "queued", "detail": "纠偏已送达运行中执行，等待消费证据"}
    if status == "rejected":
        if result.get("delivered") is False:
            reason = _state_reason(conn, execution_id)
            steer_ledger.report_leftover(conn, operation_id, text, result=reason)
            return {"status": "undelivered", "detail": reason}
        # rejected 缺 delivered 字段 → 语义不完整，保守待核实
        steer_ledger.mark_pending_verify(conn, operation_id, "回执 rejected 但缺 delivered 字段，语义不完整")
        return {"status": "pending_verify", "detail": "回执语义不完整，待核实"}
    # 未知回执形状
    steer_ledger.mark_pending_verify(conn, operation_id, f"未知回执状态 {status!r}，待核实")
    return {"status": "pending_verify", "detail": "回执未知，待核实"}


def observe_session(conn: sqlite3.Connection, client, session_id: str, started_at: float) -> int:
    """观察指定 session 的消费证据：delivering 操作匹配 OUT-OF-BAND marker 后 report_consumed(inserted)。

    精确绑定：session_id + run_started_at（timestamp >= started_at）+ 正文匹配，
    防重复正文/旧历史误归因；观察失败不改变账本（下次轮询再试）。
    由 hermes_gateway._watch 在运行中执行轮询时调用（复用已有 client）。
    """
    ops = conn.execute(
        "SELECT operation_id, text FROM steer_operations "
        "WHERE session_id=? AND run_started_at=? AND status='delivering'",
        (session_id, started_at),
    ).fetchall()
    if not ops:
        return 0
    try:
        snapshot = client.rpc("session.resume", session_id=session_id, timeout=5)
    except (GatewayRefusal, GatewayTransportError):
        return 0
    if not isinstance(snapshot, dict) or snapshot.get("session_id") != session_id:
        return 0
    changed = 0
    for op in ops:
        text = op["text"]
        for message in snapshot.get("messages") or []:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            ts = message.get("timestamp")
            if not isinstance(ts, (int, float)) or ts < started_at:
                continue  # 旧历史（本次 run 之前），不误归因
            content = message.get("content") or ""
            if "OUT-OF-BAND USER MESSAGE" not in content:
                continue
            if text not in content:
                continue  # 正文不匹配，不误归因
            steer_ledger.report_consumed(conn, op["operation_id"], "inserted")
            changed += 1
            break
    return changed
