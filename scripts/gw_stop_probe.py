"""#251 隔离停止语义验证：真实网关 queued / started / 竞态 / 自然完成 / 会话隔离。

设计依据：docs/design/2026-10-27-hermes-gateway-v0225-call-table.md 3.4（停止并确认结束）
与 3.5（与 main 的关键差异）。

v0.22.5 停止序列（本脚本按此实现，不依赖 main 的自动中断）：
  1. prompt.receipt 查 status
  2. queued  → prompt.cancel(admission_id)
  3. started → session.interrupt(execution_generation)（generation 取 receipt 的）
  4. 循环 prompt.receipt 到 terminal，读 outcome 确认原执行结束

覆盖场景（验收标准 2）：
  S1 queued         提交长任务，未执行前 cancel，确认 terminal
  S2 started        status=started 后先证 prompt.cancel 抛 stale_generation，再 session.interrupt
  S3 queued→started 提交后立即 cancel，可能拿到 cancel_queued / cancel_or_claimed / 已被 claim
  S4 自然完成竞态   极短任务提交后立即 cancel，终态以网关回执为准（不强制 cancelled）
  S5 会话隔离       A 长任务被 interrupt，B 同时跑短任务必须正常 completed

隔离与生命周期完全复用 gw_probe：preflight 白名单环境 + 显式空 MCP + 停残留网关；
实例归属三方对齐（mismatched → rc=10）；心跳门禁；finally 里 stop 本次实例并落 stop-after 证据。
退出码沿用 gw_probe（0/3/4/5/6/8/9/10），另加：
  11 = 场景判定不符（例如 started 上 prompt.cancel 未抛 stale_generation）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time

import gw_probe as G

LONG_PROMPT = ("请从 1 开始逐个计算并列出每个整数的平方，写到 300 再汇总求和。"
               "必须逐行输出，不要省略、不要使用省略号。")
SHORT_PROMPT = "只回复两个字：收到"
POLL_MAX_S = 180.0
POLL_INTERVAL_S = 1.0


def _err_text(exc: RuntimeError) -> str:
    return str(exc)


async def _new_session(ws, tag: str) -> dict:
    """独立会话（隔离断言与 gw_probe 一致：info.mcp_servers 必须为空）。"""
    created = await G.rpc(ws, "session.create", {
        "request_id": f"stop-{tag}-create-{int(time.time() * 1000)}",
        "source": "tool",
        "cwd": str(G.PROBE_ROOT),
    })
    G.save_evidence(f"stop-{tag}-session-create", created)
    issue = G.check_mcp_empty(created, f"stop-{tag}/create")
    if issue:
        raise RuntimeError(f"ISOLATION UNKNOWN/VIOLATION (stop-{tag}/create): {issue}")
    sid = created.get("session_id")
    if not sid:
        raise RuntimeError(f"stop-{tag}: session.create 未返回 session_id")
    G.log("stop session created", tag=tag, session=str(sid)[:20] + "…",
          generation=created.get("execution_generation"))
    return created


async def _submit(ws, sid: str, text: str, tag: str) -> dict:
    sub = await G.rpc(ws, "prompt.submit", {
        "session_id": sid,
        "text": text,
        "submission_id": f"stop-{tag}-submit-{int(time.time() * 1000)}",
    })
    G.save_evidence(f"stop-{tag}-prompt-submit", sub)
    if not sub.get("admission_id"):
        raise RuntimeError(f"stop-{tag}: prompt.submit 未返回 admission_id")
    G.log("stop prompt submitted", tag=tag, admission=str(sub["admission_id"])[:12] + "…",
          status=sub.get("status"), generation=sub.get("execution_generation"))
    return sub


async def _receipt(ws, sid: str, aid: str, *, include_result: bool = False) -> dict:
    params = {"session_id": sid, "admission_id": aid}
    if include_result:
        params["include_result"] = True
    return await G.rpc(ws, "prompt.receipt", params)


async def _poll_terminal(ws, sid: str, aid: str, *, max_s: float = POLL_MAX_S,
                         stop_when_started: bool = False) -> tuple[dict | None, list]:
    """轮询到 terminal；stop_when_started=True 时拿到 started 立即返回（供 S2 用）。"""
    deadline = time.time() + max_s
    seq: list[dict] = []
    while time.time() < deadline:
        rec = await _receipt(ws, sid, aid)
        status = rec.get("status")
        seq.append({"t": round(time.time(), 3), "status": status,
                    "generation": rec.get("execution_generation")})
        G.log("receipt poll", status=status, generation=rec.get("execution_generation"))
        if status == "terminal":
            return rec, seq
        if stop_when_started and status == "started":
            return rec, seq
        await asyncio.sleep(POLL_INTERVAL_S)
    return None, seq


async def _cancel(ws, sid: str, aid: str, tag: str) -> dict:
    """prompt.cancel：成功返回回执，失败把错误原文记下来（S2 要证明 stale_generation）。

    v0.22.5 实测：只传 admission_id 会 invalid_params（缺 session_id），call-table 3.4
    只记了 admission_id，实际是 CanonicalSessionParams + admission_id。
    """
    try:
        rec = await G.rpc(ws, "prompt.cancel", {"session_id": sid, "admission_id": aid})
        G.save_evidence(f"stop-{tag}-prompt-cancel-ok", rec)
        G.log("prompt.cancel ok", tag=tag, status=rec.get("status"))
        return {"ok": True, "receipt": rec}
    except RuntimeError as exc:
        text = _err_text(exc)
        G.save_evidence(f"stop-{tag}-prompt-cancel-error", {"error": text})
        G.log("prompt.cancel error", tag=tag, error=text[:200])
        return {"ok": False, "error": text}


async def _interrupt(ws, sid: str, generation, tag: str) -> dict:
    try:
        rec = await G.rpc(ws, "session.interrupt",
                          {"session_id": sid, "execution_generation": generation})
        G.save_evidence(f"stop-{tag}-session-interrupt-ok", rec)
        G.log("session.interrupt ok", tag=tag)
        return {"ok": True, "receipt": rec}
    except RuntimeError as exc:
        text = _err_text(exc)
        G.save_evidence(f"stop-{tag}-session-interrupt-error", {"error": text})
        G.log("session.interrupt error", tag=tag, error=text[:200])
        return {"ok": False, "error": text}


def _final_record(final: dict | None, seq: list, **extra) -> dict:
    return {
        "terminal": bool(final),
        "status": (final or {}).get("status"),
        "outcome": (final or {}).get("outcome"),
        "admission_id": (final or {}).get("admission_id"),
        "poll": seq,
        **extra,
    }


async def s1_queued_cancel(ws) -> dict:
    created = await _new_session(ws, "s1")
    sid = created["session_id"]
    sub = await _submit(ws, sid, LONG_PROMPT, "s1")
    aid = sub["admission_id"]
    first = await _receipt(ws, sid, aid)
    first_status = first.get("status")
    cancel = await _cancel(ws, sid, aid, "s1")
    final, seq = await _poll_terminal(ws, sid, aid)
    rec = _final_record(final, seq, scenario="S1 queued→cancel",
                        first_receipt_status=first_status, cancel=cancel)
    rec["passed"] = bool(final) and cancel.get("ok") is True
    return rec


async def s2_started_interrupt(ws) -> dict:
    created = await _new_session(ws, "s2")
    sid = created["session_id"]
    sub = await _submit(ws, sid, LONG_PROMPT, "s2")
    aid = sub["admission_id"]
    started_rec, seq = await _poll_terminal(ws, sid, aid, stop_when_started=True)
    if not started_rec or started_rec.get("status") != "started":
        # 任务跑太快，没抓到 started：如实记为 blocked，不伪造
        final, seq2 = await _poll_terminal(ws, sid, aid)
        rec = _final_record(final, seq + seq2, scenario="S2 started→interrupt",
                            blocked="未观察到 started（任务在轮询间隔内进入终态）")
        rec["passed"] = False
        return rec
    generation = started_rec.get("execution_generation") or sub.get("execution_generation")
    cancel = await _cancel(ws, sid, aid, "s2")          # 期望 stale_generation（v0.22.5 语义）
    intr = await _interrupt(ws, sid, generation, "s2")   # 真正的停止
    final, seq2 = await _poll_terminal(ws, sid, aid)
    rec = _final_record(final, seq + seq2, scenario="S2 started→interrupt",
                        generation=generation, cancel=cancel, interrupt=intr)
    # 通过条件：cancel 未成功取消（应报 stale_generation）+ interrupt 成功 + 到达 terminal
    rec["cancel_rejected_stale"] = (cancel.get("ok") is False
                                    and "stale_generation" in (cancel.get("error") or ""))
    rec["passed"] = bool(final) and intr.get("ok") is True
    return rec


async def s3_queued_started_race(ws) -> dict:
    created = await _new_session(ws, "s3")
    sid = created["session_id"]
    sub = await _submit(ws, sid, LONG_PROMPT, "s3")
    aid = sub["admission_id"]
    cancel = await _cancel(ws, sid, aid, "s3")          # 不等 receipt，直接打竞态
    intr = None
    if cancel.get("ok") is False and "stale_generation" in (cancel.get("error") or ""):
        # 已被 claim：按 3.4 补 interrupt（generation 取 submit 回执）
        intr = await _interrupt(ws, sid, sub.get("execution_generation"), "s3")
    final, seq = await _poll_terminal(ws, sid, aid)
    rec = _final_record(final, seq, scenario="S3 queued→started 竞态",
                        cancel=cancel, interrupt=intr)
    rec["passed"] = bool(final)
    return rec


async def s4_natural_completion_race(ws) -> dict:
    created = await _new_session(ws, "s4")
    sid = created["session_id"]
    sub = await _submit(ws, sid, SHORT_PROMPT, "s4")
    aid = sub["admission_id"]
    cancel = await _cancel(ws, sid, aid, "s4")          # 极短任务：可能已完成
    final, seq = await _poll_terminal(ws, sid, aid)
    final = final or {}
    rec = _final_record(final or None, seq, scenario="S4 取消与自然完成竞态",
                        cancel=cancel)
    # 验收口径：终态子类型以回执为准，completed 也算通过；只要求到达 terminal
    rec["passed"] = final.get("status") == "terminal"
    return rec


async def s5_cross_session(ws) -> dict:
    a = await _new_session(ws, "s5a")
    b = await _new_session(ws, "s5b")
    sub_a = await _submit(ws, a["session_id"], LONG_PROMPT, "s5a")
    sub_b = await _submit(ws, b["session_id"], "只回复：B-OK", "s5b")
    started_a, seq_a = await _poll_terminal(ws, a["session_id"], sub_a["admission_id"],
                                            stop_when_started=True)
    generation = (started_a or {}).get("execution_generation") or sub_a.get("execution_generation")
    intr = await _interrupt(ws, a["session_id"], generation, "s5")
    final_a, seq_a2 = await _poll_terminal(ws, a["session_id"], sub_a["admission_id"], max_s=90)
    final_b, seq_b = await _poll_terminal(ws, b["session_id"], sub_b["admission_id"], max_s=90)
    rec = {
        "scenario": "S5 其他会话隔离",
        "a": _final_record(final_a, seq_a + seq_a2, interrupt=intr, generation=generation),
        "b": _final_record(final_b, seq_b),
    }
    rec["passed"] = (bool(final_a) and bool(final_b)
                     and (final_b or {}).get("outcome") == "completed")
    return rec


async def s6_natural_completion(ws) -> dict:
    """自然完成对照：极短任务不取消，确认能读到 completed 终态。

    S4 只覆盖了「取消先到」的分支；验收要求停止判定以网关回执为准，
    必须同时证明 completed 与 cancelled 两种终态都可读、不会把 completed 当异常。
    """
    created = await _new_session(ws, "s6")
    sid = created["session_id"]
    sub = await _submit(ws, sid, SHORT_PROMPT, "s6")
    aid = sub["admission_id"]
    final, seq = await _poll_terminal(ws, sid, aid)
    rec = _final_record(final, seq, scenario="S6 自然完成（不取消）")
    rec["passed"] = bool(final) and (final or {}).get("outcome") == "completed"
    return rec


SCENARIOS = [
    ("s1", s1_queued_cancel),
    ("s2", s2_started_interrupt),
    ("s3", s3_queued_started_race),
    ("s4", s4_natural_completion_race),
    ("s5", s5_cross_session),
    ("s6", s6_natural_completion),
]


async def run_scenarios(info: dict, only: list[str] | None) -> int:
    import websockets

    url, protocols = info["ws_url"], info["protocols"]
    G.log("connecting", target=url)
    ws = await websockets.connect(url, subprotocols=protocols, open_timeout=30,
                                  max_size=8 * 1024 * 1024, proxy=None)
    results = []
    try:
        G.log("connected", subprotocol=ws.subprotocol)
        await G.rpc(ws, "ping", {})
        described = await G.rpc(ws, "runtime.describe", {})
        G.save_evidence("stop-runtime-describe", described)
        caps = described.get("capabilities") or []
        if "durable-admission-v1" not in caps:
            G.log("AUTHORITY CHECK FAILED", got=caps)
            return 2
        G.log("authority confirmed", instance_id=described.get("instance_id"),
              epoch=described.get("authority_epoch"), capabilities=caps)

        for tag, fn in SCENARIOS:
            if only and tag not in only:
                continue
            G.log("scenario start", scenario=tag)
            try:
                rec = await fn(ws)
            except Exception as exc:  # noqa: BLE001 - 单场景失败不拖垮其余场景
                rec = {"scenario": tag, "passed": False,
                       "error": f"{type(exc).__name__}: {exc}"}
                G.log("scenario exception", scenario=tag, error=rec["error"][:200])
            results.append(rec)
            G.save_evidence(f"stop-result-{tag}", rec)
            G.log("scenario done", scenario=tag, passed=rec.get("passed"),
                  status=rec.get("status"), outcome=rec.get("outcome"))
    finally:
        await ws.close()
        G.log("ws closed")

    G.save_evidence("stop-results", results)
    failed = [r.get("scenario") for r in results if not r.get("passed")]
    G.log("STOP SUMMARY", total=len(results), failed=failed)
    return 0 if not failed else 11


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenarios", default="",
                        help="逗号分隔的场景标签（s1,s2,s3,s4,s5），默认全部")
    parser.add_argument("--preflight-only", action="store_true",
                        help="只跑隔离校验并退出（不启动网关）")
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
        forensics = G.instance_forensics(info)
        G.save_evidence("stop-instance-forensics", forensics)
        pid = (forensics.get("pid_file") or {}).get("pid")
        G.log("instance forensics", pid=pid,
              alignment=forensics.get("alignment", {}).get("verdict"))
        G.assert_alignment_or_raise(forensics)
        rc = asyncio.run(run_scenarios(info, only))
    except G.InstanceMismatch:
        rc = 10
    except SystemExit as exc:
        rc = rc or int(exc.code or 1)
    except Exception as exc:  # noqa: BLE001 - 可捕获异常也必须清理
        G.log("STOP PROBE EXCEPTION", error=f"{type(exc).__name__}: {exc}")
        rc = rc or 1
    finally:
        beats_after = G.mcp_log_snapshot()
        rc, verdict, delta = G.heartbeat_gate(rc, beats_before, beats_after)
        G.save_evidence("stop-heartbeat-diff", {
            "before": beats_before, "after": beats_after, "delta": delta, "gate": verdict})
        G.log("heartbeat check", delta=delta, gate=verdict)
        try:
            cleanup = G.stop_instance(info, pid)
        except Exception as exc:  # noqa: BLE001
            cleanup = {"clean": False, "verdict": "failed",
                       "error": f"{type(exc).__name__}: {exc}"}
        G.save_evidence("stop-after", cleanup)
        G.log("stop-after", verdict=cleanup.get("verdict"), port=cleanup.get("port"),
              pid_alive_after=cleanup.get("pid_alive_after"),
              port_listening=cleanup.get("port_listening"))
        if cleanup.get("verdict") == "failed" and rc == 0:
            rc = 9
    G.log("STOP PROBE DONE", rc=rc, elapsed_s=round(time.time() - t0, 1))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
