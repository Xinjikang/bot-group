"""req#83 task#145：/steer 纠偏账本层（路线 A 纯账本，留传输接口）。

契约（docs/design/SLASH-COMMAND-DESIGN-2026-09-27.md：纠偏状态）：
- 回执五层分层：queued / inserted / persisted / model_requested / behavior_observed。
- 悬空终态：确定未消费 → undelivered；回执缺失证据不足 → pending_verify（非终态可核对）。
- 精确绑定运行：operation 绑 (project, profile, kind, thread_id, session_id, generation,
  run_started_at)；跨 /new generation 不匹配 → generation_stale 失效。
- 状态机单向前进，终态后拒绝再写。
- 注入时机双路径（tool_batch / api_pre_drain）只记到达时点审计。

运行（纯 stdlib，系统 python 即可）：
    python scripts/test_steer_ledger.py
不进 unittest discovery（module 级零副作用、无 TestCase、仅 __main__ 执行）。
"""
import os
import sys
import tempfile
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def main() -> int:
    from engine import db, steer_ledger as sl

    iso = tempfile.mkdtemp(prefix="t145_")
    group_db = os.path.join(iso, "group.db")
    failures = []

    def check(label, cond, detail=""):
        print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            failures.append(label)

    patcher = patch.object(db, "db_path", return_value=group_db)
    patcher.start()
    try:
        conn = db.get_connection()
        try:
            db.init_schema(conn)
        finally:
            conn.close()

        # 1. 建账 queued + 精确绑定字段
        conn = db.get_connection()
        op = sl.new_operation(conn, "bot-group", "be-dev-01", "work", None, "请改用方案 B")
        conn.close()
        check("建账 queued 状态", op["status"] == "queued", op["status"])
        check("建账 operation_id 非空", bool(op["operation_id"]))
        check("建账 text 保存", op["text"] == "请改用方案 B")
        check("work thread_id 归一为 0", op["thread_id"] == 0)

        # 2. 空正文拒绝
        conn = db.get_connection()
        raised = False
        try:
            sl.new_operation(conn, "bot-group", "be-dev-01", "work", None, "  ")
        except sl.SteerError:
            raised = True
        conn.close()
        check("空正文抛 SteerError", raised)

        opid = op["operation_id"]

        # 3. 投递：绑定 session/generation/run + delivering
        conn = db.get_connection()
        op2 = sl.record_delivery(conn, opid, "20260928_160000_abc123", 1, 1727500000.0)
        conn.close()
        check("投递后 delivering 状态", op2["status"] == "delivering", op2["status"])
        check("投递绑定 session_id", op2["session_id"] == "20260928_160000_abc123")
        check("投递绑定 generation", op2["generation"] == 1)
        check("投递绑定 run_started_at", op2["run_started_at"] == 1727500000.0)

        # 4. 消费证据：inserted → consumed
        conn = db.get_connection()
        op3 = sl.report_consumed(conn, opid, "inserted")
        conn.close()
        check("消费 inserted → consumed", op3["status"] == "consumed" and op3["deliver_layer"] == "inserted",
              f"{op3['status']}/{op3['deliver_layer']}")

        # 5. 终态后拒绝再写
        conn = db.get_connection()
        raised = False
        try:
            sl.report_consumed(conn, opid, "persisted")
        except sl.SteerError:
            raised = True
        conn.close()
        check("终态后拒绝推进", raised)

        # 6. 五层口径：persisted / model_requested 合法
        op2id = sl.new_operation.__wrapped__ if hasattr(sl.new_operation, "__wrapped__") else None
        conn = db.get_connection()
        op_a = sl.new_operation(conn, "bot-group", "be-dev-01", "work", None, "纠偏A")
        sl.record_delivery(conn, op_a["operation_id"], "sid", 0, 1.0)
        op_a2 = sl.report_consumed(conn, op_a["operation_id"], "persisted")
        conn.close()
        check("回执层级 persisted 合法", op_a2["deliver_layer"] == "persisted")
        conn = db.get_connection()
        op_b = sl.new_operation(conn, "bot-group", "be-dev-01", "work", None, "纠偏B")
        sl.record_delivery(conn, op_b["operation_id"], "sid", 0, 1.0)
        op_b2 = sl.report_consumed(conn, op_b["operation_id"], "model_requested")
        conn.close()
        check("回执层级 model_requested 合法", op_b2["deliver_layer"] == "model_requested")

        # 7. 非法回执层级拒绝
        conn = db.get_connection()
        op_c = sl.new_operation(conn, "bot-group", "be-dev-01", "work", None, "纠偏C")
        raised = False
        try:
            sl.report_consumed(conn, op_c["operation_id"], "bogus")
        except sl.SteerError:
            raised = True
        conn.close()
        check("非法回执层级抛 SteerError", raised)

        # 8. leftover → undelivered 终态（确定未消费）
        conn = db.get_connection()
        op_d = sl.new_operation(conn, "bot-group", "be-dev-01", "work", None, "纠偏D")
        sl.record_delivery(conn, op_d["operation_id"], "sid", 0, 1.0)
        op_d2 = sl.report_leftover(conn, op_d["operation_id"], "这条纠偏本轮结束才到")
        conn.close()
        check("leftover → undelivered 终态", op_d2["status"] == "undelivered", op_d2["status"])
        check("leftover 正文保存", op_d2["leftover"] == "这条纠偏本轮结束才到")

        # 9. pending_verify（回执缺失证据不足，非终态）
        conn = db.get_connection()
        op_e = sl.new_operation(conn, "bot-group", "be-dev-01", "work", None, "纠偏E")
        sl.record_delivery(conn, op_e["operation_id"], "sid", 0, 1.0)
        op_e2 = sl.mark_pending_verify(conn, op_e["operation_id"])
        conn.close()
        check("pending_verify 非终态", op_e2["status"] == "pending_verify")
        conn = db.get_connection()
        op_e3 = sl.report_consumed(conn, op_e["operation_id"], "inserted")
        conn.close()
        check("pending_verify 可后续核对（非终态可推进）", op_e3["status"] == "consumed")

        # 10. generation_stale：跨 /new 旧运行失效
        conn = db.get_connection()
        op_f = sl.new_operation(conn, "bot-group", "be-dev-01", "work", None, "纠偏F",
                                generation=1)
        op_f2 = sl.record_delivery(conn, op_f["operation_id"], "new_sid", 2, 2.0)
        conn.close()
        check("跨 /new generation 不匹配 → generation_stale", op_f2["status"] == "generation_stale",
              op_f2["status"])

        # 11. target_ended 终态
        conn = db.get_connection()
        op_g = sl.new_operation(conn, "bot-group", "be-dev-01", "work", None, "纠偏G")
        op_g2 = sl.finalize_target_ended(conn, op_g["operation_id"], "目标已退出")
        conn.close()
        check("target_ended 终态", op_g2["status"] == "target_ended")

        # 12. TTL 过期清理：queued 超时 → expired
        conn = db.get_connection()
        op_h = sl.new_operation(conn, "bot-group", "be-dev-01", "work", None, "纠偏H")
        # 手工把 created_at 改成很久以前
        conn.execute("UPDATE steer_operations SET created_at='2000-01-01T00:00:00' WHERE operation_id=?",
                     (op_h["operation_id"],))
        conn.commit()
        n = sl.expire_stale(conn, ttl_seconds=600.0)
        conn.close()
        check("TTL 过期清理 expired 终态", n >= 1, f"cleaned={n}")
        conn = db.get_connection()
        op_h2 = sl.get_operation(conn, op_h["operation_id"])
        conn.close()
        check("过期操作状态=expired", op_h2["status"] == "expired")

        # 13. pending_operations 只列未终态
        conn = db.get_connection()
        pend = sl.pending_operations(conn, project="bot-group")
        conn.close()
        check("pending_operations 不含终态", all(p["status"] not in
              ("consumed", "undelivered", "target_ended", "generation_stale", "expired") for p in pend))

        # 14. 注入时机边界记录
        conn = db.get_connection()
        op_i = sl.new_operation(conn, "bot-group", "be-dev-01", "work", None, "纠偏I")
        op_i2 = sl.record_arrival(conn, op_i["operation_id"], "tool_batch")
        conn.close()
        check("记录注入边界 tool_batch", "tool_batch" in op_i2["result"], op_i2["result"])
        conn = db.get_connection()
        raised = False
        try:
            sl.record_arrival(conn, op_i["operation_id"], "bogus_boundary")
        except sl.SteerError:
            raised = True
        conn.close()
        check("非法注入边界抛 SteerError", raised)

    finally:
        patcher.stop()

    print("\n=== req#83 task#145 /steer 纠偏账本层验证 ===")
    if failures:
        print("SOME FAIL:", ", ".join(failures))
        return 1
    print("ALL PASS：建账/投递绑定/五层口径/leftover 悬空/pending_verify/generation_stale/TTL/边界记录 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
