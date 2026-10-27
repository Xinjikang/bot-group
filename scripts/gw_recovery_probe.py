"""#252 断线恢复 / 幂等 / epoch / 冲突回执验证（真实网关）。

设计依据：docs/design/2026-10-27-hermes-gateway-migration.md 4.2（request_id 幂等与提交恢复）
与 5（状态机约束：终态吸收、写前日志、裁决源、自动对账可达性）。

场景：
  R1 幂等命中     同 session + 同 submission_id + 同 payload 重提 → 同一 admission_id
  R2 冲突         同 submission_id + 异 payload → admission_conflict（不得静默新建执行）
  R3 回执丢失     提交后断开连接（不取回执）→ 重连 + session.resume → 同 submission_id 重提
                  → 命中同一 admission → receipt 对账到 terminal
  R4 断线恢复     断开前已 started，重连 resume 后仍能取到同一 admission 的终态
  R5 重启/epoch   停掉网关再拉起（authority_epoch 变化）→ 旧 admission receipt 查询与 cancel 边界
  R6 迟到回执     R5 中旧 epoch 的 receipt 记录为迟到回执，确认不会把 unknown 当终态

安全边界（写进结论，不是断言通过）：
  - request_id 幂等依赖 session_admissions 留存期，留存期未知时自动重提必须阻断转人工；
    R3 只在「同进程内短间隔」重提，用于观察幂等是否命中，不构成留存期结论。
  - 旧 epoch 的 started 可能被 recover_session_inputs 改成 unknown；unknown 不是终态证据，
    不得据此自动重提或释放资源，必须走人工/operator 路径。

隔离与清理完全复用 gw_probe：preflight 白名单环境 + 显式空 MCP + 停残留网关；
会话创建后断言 info.mcp_servers 为空；心跳门禁；finally 停止本次实例并落 stop-after。
退出码沿用 gw_probe（0/3/4/5/6/8/9/10），另加 11 = 场景判定不符。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time

import gw_probe as G
import gw_stop_probe as S

LONG_PROMPT = ("请从 1 开始逐个计算并列出每个整数的平方，写到 200 再汇总求和。"
               "必须逐行输出，不要省略。")

STATE: dict = {}


def _err(exc: RuntimeError) -> str:
    return str(exc)


def _is_open(ws) -> bool:
    """websockets 各版本判断连接活性的字段不同（新版是 state，旧版是 closed）。"""
    if ws is None:
        return False
    state = getattr(ws, "state", None)
    if state is not None:
        return getattr(state, "name", str(state)).upper() == "OPEN"
    return not getattr(ws, "closed", False)


async def _open(info: dict):
    import websockets
    return await websockets.connect(info["ws_url"], subprotocols=info["protocols"],
                                     open_timeout=30, max_size=8 * 1024 * 1024, proxy=None)


async def _reconnect():
    """断线重连必须重新取 ticket。

    实测：用同一 ws_url/subprotocol 重连会被网关拒绝（`InvalidMessage: did not receive
    a valid HTTP response` 后 `InvalidStatus: HTTP 403`），ticket 随连接一次性使用。
    因此重连前重新走 gateway_ws_target（已运行的实例会复用，不会重启）。
    """
    fresh = G.connect_gateway_info()
    STATE["info"] = fresh
    return await _open(fresh)


async def _session(ws, tag: str) -> str:
    created = await S._new_session(ws, tag)
    return created["session_id"]


async def _submit(ws, sid: str, text: str, rid: str, tag: str) -> dict | str:
    """submit 指定 submission_id；冲突/错误时返回错误文本而不是抛异常。"""
    try:
        sub = await G.rpc(ws, "prompt.submit",
                          {"session_id": sid, "text": text, "submission_id": rid})
    except RuntimeError as exc:
        G.save_evidence(f"rec-{tag}-submit-error", {"submission_id": rid, "error": _err(exc)})
        return _err(exc)
    G.save_evidence(f"rec-{tag}-submit", {"submission_id": rid, "receipt": sub})
    return sub


async def _resume(ws, sid: str, tag: str) -> dict | str:
    try:
        rec = await G.rpc(ws, "session.resume", {"session_id": sid})
    except RuntimeError as exc:
        G.save_evidence(f"rec-{tag}-resume-error", {"error": _err(exc)})
        return _err(exc)
    G.save_evidence(f"rec-{tag}-resume", rec)
    return rec


async def r1_r2_idempotent(ws, info) -> dict:
    """R1 同 payload 幂等 / R2 异 payload 冲突。"""
    sid = await _session(ws, "r1")
    rid = f"rec-r1-{int(time.time() * 1000)}"
    first = await _submit(ws, sid, LONG_PROMPT, rid, "r1-first")
    if isinstance(first, str):
        return {"scenario": "R1/R2", "passed": False, "error": first}
    again = await _submit(ws, sid, LONG_PROMPT, rid, "r1-again")
    conflict = await _submit(ws, sid, "完全不同的另一段文本", rid, "r2-conflict")
    rec = {
        "scenario": "R1 幂等 / R2 冲突",
        "admission_first": first.get("admission_id"),
        "admission_again": again.get("admission_id") if isinstance(again, dict) else None,
        "conflict_error": conflict if isinstance(conflict, str) else None,
    }
    rec["idempotent_hit"] = (isinstance(again, dict)
                             and again.get("admission_id") == first.get("admission_id"))
    rec["conflict_detected"] = (isinstance(conflict, str)
                                and "admission_conflict" in conflict)
    rec["passed"] = bool(rec["idempotent_hit"] and rec["conflict_detected"])
    # 收尾：停掉这个长任务，避免一直跑
    try:
        await G.rpc(ws, "session.interrupt",
                    {"session_id": sid, "execution_generation": first.get("execution_generation")})
    except RuntimeError:
        pass
    final, _ = await S._poll_terminal(ws, sid, first["admission_id"], max_s=90)
    rec["cleanup_terminal"] = bool(final)
    return rec


async def r3_lost_receipt(ws, info) -> dict:
    """回执丢失：提交后断链，重连 resume 后用同 submission_id 重提，观察是否命中同一 admission。"""
    sid = await _session(ws, "r3")
    rid = f"rec-r3-{int(time.time() * 1000)}"
    first = await _submit(ws, sid, LONG_PROMPT, rid, "r3-first")
    if isinstance(first, str):
        return {"scenario": "R3", "passed": False, "error": first}
    admission = first["admission_id"]
    G.log("r3 drop connection", admission=str(admission)[:12] + "…")
    await ws.close()

    ws2 = await _reconnect()
    try:
        resumed = await _resume(ws2, sid, "r3")
        if isinstance(resumed, str):
            return {"scenario": "R3", "passed": False, "resume_error": resumed}
        again = await _submit(ws2, sid, LONG_PROMPT, rid, "r3-again")
        rec = {
            "scenario": "R3 回执丢失→重连重提",
            "admission_first": admission,
            "admission_after_reconnect": again.get("admission_id") if isinstance(again, dict) else None,
            "resume_session_id": resumed.get("session_id"),
        }
        rec["resume_matched"] = resumed.get("session_id") == sid
        rec["idempotent_hit"] = (isinstance(again, dict)
                                 and again.get("admission_id") == admission)
        rec["passed"] = bool(rec["resume_matched"] and rec["idempotent_hit"])
        # 终态对账
        try:
            await G.rpc(ws2, "session.interrupt",
                        {"session_id": sid, "execution_generation": first.get("execution_generation")})
        except RuntimeError:
            pass
        final, seq = await S._poll_terminal(ws2, sid, admission, max_s=120)
        rec["reconciled_terminal"] = bool(final)
        rec["outcome"] = (final or {}).get("outcome")
        rec["poll"] = [p["status"] for p in seq]
        return rec
    finally:
        await ws2.close()


async def r4_reconnect_started(ws, info) -> dict:
    """断线时执行已 started；重连 resume 后仍能读到同一 admission 的终态。"""
    sid = await _session(ws, "r4")
    rid = f"rec-r4-{int(time.time() * 1000)}"
    first = await _submit(ws, sid, LONG_PROMPT, rid, "r4-first")
    if isinstance(first, str):
        return {"scenario": "R4", "passed": False, "error": first}
    admission = first["admission_id"]
    started, _ = await S._poll_terminal(ws, sid, admission, stop_when_started=True)
    G.log("r4 drop after", status=(started or {}).get("status"))
    await ws.close()

    ws2 = await _reconnect()
    try:
        resumed = await _resume(ws2, sid, "r4")
        rec = {
            "scenario": "R4 断线前已 started→重连恢复",
            "status_before_drop": (started or {}).get("status"),
            "admission_id": admission,
            "resume_ok": not isinstance(resumed, str),
        }
        if isinstance(resumed, str):
            rec["passed"] = False
            rec["resume_error"] = resumed
            return rec
        generation = (started or {}).get("execution_generation")
        try:
            await G.rpc(ws2, "session.interrupt",
                        {"session_id": sid, "execution_generation": generation})
        except RuntimeError as exc:
            rec["interrupt_error"] = _err(exc)
        final, seq = await S._poll_terminal(ws2, sid, admission, max_s=120)
        rec["terminal_after_reconnect"] = bool(final)
        rec["outcome"] = (final or {}).get("outcome")
        rec["poll"] = [p["status"] for p in seq]
        rec["passed"] = bool(final)
        return rec
    finally:
        await ws2.close()


async def r5_epoch_change(ws, info) -> dict:
    """重启网关（epoch 变化）后，旧 admission 的 receipt/cancel 边界。"""
    sid = await _session(ws, "r5")
    rid = f"rec-r5-{int(time.time() * 1000)}"
    first = await _submit(ws, sid, LONG_PROMPT, rid, "r5-first")
    if isinstance(first, str):
        return {"scenario": "R5", "passed": False, "error": first}
    admission = first["admission_id"]
    started, _ = await S._poll_terminal(ws, sid, admission, stop_when_started=True)
    epoch_before = STATE.get("epoch_before")
    rec = {
        "scenario": "R5 重启/epoch 变化",
        "admission_id": admission,
        "status_before_restart": (started or {}).get("status"),
    }
    await ws.close()

    # 停掉当前实例，再重新拉起（新 authority_epoch）
    forensics = STATE.get("forensics") or {}
    pid = (forensics.get("pid_file") or {}).get("pid")
    old = G.stop_instance(info, pid, force=True)
    rec["stop_before_restart"] = {"verdict": old.get("verdict"), "port": old.get("port")}
    new_info = G.connect_gateway_info()
    STATE["info"] = new_info
    forensics2 = G.instance_forensics(new_info)
    STATE["forensics"] = forensics2
    new_pid = (forensics2.get("pid_file") or {}).get("pid")
    STATE["pid"] = new_pid
    rec["new_instance"] = {
        "pid": new_pid,
        "port": G._origin_port(new_info.get("ws_url") or ""),
        "epoch": None,
    }
    ws2 = await _open(new_info)
    try:
        described = await G.rpc(ws2, "runtime.describe", {})
        rec["epoch_after"] = described.get("authority_epoch")
        rec["epoch_before"] = epoch_before if epoch_before else None
        rec["epoch_changed"] = bool(rec["epoch_before"] and
                                    described.get("authority_epoch") != rec["epoch_before"])
        resumed = await _resume(ws2, sid, "r5")
        rec["resume_after_restart"] = (not isinstance(resumed, str))
        if isinstance(resumed, str):
            rec["resume_error"] = resumed
        old_receipt = None
        try:
            old_receipt = await G.rpc(ws2, "prompt.receipt",
                                      {"session_id": sid, "admission_id": admission})
        except RuntimeError as exc:
            old_receipt = {"error": _err(exc)}
        rec["old_admission_receipt"] = old_receipt
        cancel_try = None
        try:
            cancel_try = await G.rpc(ws2, "prompt.cancel",
                                     {"session_id": sid, "admission_id": admission})
        except RuntimeError as exc:
            cancel_try = {"error": _err(exc)}
        rec["old_admission_cancel"] = cancel_try
        # 判定：只要求拿到明确响应并记录下来；unknown 属预期（不是终态证据）
        status = (old_receipt or {}).get("status") if isinstance(old_receipt, dict) else None
        rec["old_receipt_status"] = status
        rec["old_receipt_outcome"] = (old_receipt or {}).get("outcome")
        # 设计文档 4.4 表述为 recover_session_inputs 把旧 epoch 的 started→unknown；
        # 实测（epoch 29→30）旧 admission 直接返回 terminal/interrupted，即恢复路径写了
        # interrupted 终态。两者都不能证明「原执行确已结束」，安全边界一致：
        # 不得据此自动重提，须人工确认（5.4 裁决源）。
        rec["recovery_semantics"] = (
            f"{status}/{(old_receipt or {}).get('outcome')}" if status else "no-status")
        rec["passed"] = isinstance(old_receipt, dict) and status in ("terminal", "unknown")
        return rec
    finally:
        await ws2.close()


SCENARIOS = [
    ("r1", r1_r2_idempotent),
    ("r3", r3_lost_receipt),
    ("r4", r4_reconnect_started),
    ("r5", r5_epoch_change),
]


async def run_all(info: dict, only: list[str] | None) -> int:
    ws = await _open(info)
    results = []
    try:
        await G.rpc(ws, "ping", {})
        described = await G.rpc(ws, "runtime.describe", {})
        G.save_evidence("rec-runtime-describe", described)
        if "durable-admission-v1" not in (described.get("capabilities") or []):
            G.log("AUTHORITY CHECK FAILED", got=described.get("capabilities"))
            return 2
        STATE["epoch_before"] = described.get("authority_epoch")
        G.log("authority confirmed", epoch=described.get("authority_epoch"),
              instance=described.get("instance_id"))

        for tag, fn in SCENARIOS:
            if only and tag not in only:
                continue
            # 前一个场景可能主动断链（R3/R4 模拟回执丢失）或重启网关（R5），按需重连
            if not _is_open(ws):
                ws = await _reconnect()
            G.log("scenario start", scenario=tag)
            try:
                rec = await fn(ws, STATE.get("info") or info)
            except Exception as exc:  # noqa: BLE001
                rec = {"scenario": tag, "passed": False,
                       "error": f"{type(exc).__name__}: {exc}"}
                G.log("scenario exception", scenario=tag, error=rec["error"][:200])
            results.append(rec)
            G.save_evidence(f"rec-result-{tag}", rec)
            G.log("scenario done", scenario=tag, passed=rec.get("passed"))
        return 0 if all(r.get("passed") for r in results) else 11
    finally:
        try:
            if _is_open(ws):
                await ws.close()
        except Exception:  # noqa: BLE001
            pass
        G.save_evidence("rec-results", results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenarios", default="", help="r1,r3,r4,r5；默认全部")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    only = [s.strip() for s in args.scenarios.split(",") if s.strip()] or None

    t0 = time.time()
    pre = G.preflight()
    if args.preflight_only:
        G.log("PREFLIGHT ONLY: all static gates passed")
        return 0
    beats_before = pre["heartbeat_baseline"]
    info: dict = {}
    pid = None
    rc = 0
    try:
        info = G.connect_gateway_info()
        STATE["info"] = info
        forensics = G.instance_forensics(info)
        STATE["forensics"] = forensics
        G.save_evidence("rec-instance-forensics", forensics)
        pid = (forensics.get("pid_file") or {}).get("pid")
        G.assert_alignment_or_raise(forensics)
        rc = asyncio.run(run_all(info, only))
    except G.InstanceMismatch:
        rc = 10
    except SystemExit as exc:
        rc = rc or int(exc.code or 1)
    except Exception as exc:  # noqa: BLE001
        G.log("REC PROBE EXCEPTION", error=f"{type(exc).__name__}: {exc}")
        rc = rc or 1
    finally:
        beats_after = G.mcp_log_snapshot()
        rc, verdict, delta = G.heartbeat_gate(rc, beats_before, beats_after)
        G.save_evidence("rec-heartbeat-diff", {"before": beats_before, "after": beats_after,
                                               "delta": delta, "gate": verdict})
        G.log("heartbeat check", delta=delta, gate=verdict)
        try:
            # 用最后一轮真实 info 清理（R5 可能已重启过实例，pid 也随之更新）
            cleanup = G.stop_instance(STATE.get("info") or info,
                                      STATE.get("pid") or pid, force=True)
        except Exception as exc:  # noqa: BLE001
            cleanup = {"clean": False, "verdict": "failed",
                       "error": f"{type(exc).__name__}: {exc}"}
        G.save_evidence("rec-stop-after", cleanup)
        G.log("stop-after", verdict=cleanup.get("verdict"), port=cleanup.get("port"),
              pid_alive_after=cleanup.get("pid_alive_after"),
              port_listening=cleanup.get("port_listening"))
        if cleanup.get("verdict") == "failed" and rc == 0:
            rc = 9
    G.log("REC PROBE DONE", rc=rc, elapsed_s=round(time.time() - t0, 1))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
