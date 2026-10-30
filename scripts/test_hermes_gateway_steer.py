"""req#136 task#261：/steer 到网关 session.steer 传输通道回执映射验证。

契约（docs/design/2026-10-30-steer-transport.md §4.3，定稿 375254d）：
- 定位：project+profile+kind+COALESCE(thread_id,0) 唯一 running 执行；
  无 → target_ended；多 → pending_verify（身份歧义，不取最大 generation）。
- queued → 保持 delivering（通道接受，非 inserted）。
- rejected+delivered:False → undelivered（确定未投递，state 只补原因）。
- rejected 缺 delivered → pending_verify（语义不完整）。
- stale_generation → target_ended；unsupported_control/execution_not_ready → undelivered（能力边界）。
- permission_denied/invalid_params → pending_verify；GatewayTransportError/超时 → pending_verify。

运行（纯 stdlib + mock 隔离网关，系统 python 即可）：
    python scripts/test_hermes_gateway_steer.py
不进 unittest discovery（module 级零副作用、无 TestCase、仅 __main__ 执行）。
"""
import os
import sys
import tempfile
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


class _FakeClient:
    """可配置 rpc 结果的假 GatewayClient。"""

    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def rpc(self, method, **params):
        if self._error is not None:
            raise self._error
        return self._result

    def close(self):
        pass


def _running_row(conn, execution_id="exec0", profile="be-dev-01", kind="work",
                 thread_id=None, generation=1, state="running", started_at=12345.0):
    """插一条 hermes_executions 行（running 执行，供 _locate_running 定位）。"""
    conn.execute(
        "INSERT INTO hermes_executions "
        "(execution_id,session,started_at,project,profile,kind,thread_id,source,python,home,"
        "workdir,session_id,profile_id,submission_id,generation,state,submit_deadline,reconcile_deadline) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (execution_id, "sess-" + execution_id, started_at, "bot-group", profile, kind, thread_id,
         "", "", "/home/" + profile, "", "gw-" + execution_id, "pid", "sub-" + execution_id,
         generation, state, started_at + 30, started_at + 150),
    )
    conn.commit()


def main() -> int:
    from engine import db, steer_ledger, hermes_gateway_steer
    from engine.hermes_gateway_client import GatewayRefusal, GatewayTransportError

    iso = tempfile.mkdtemp(prefix="t261_")
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

        def run_scenario(label, expected_status, expected_ledger, *, result=None,
                         error=None, rows=1):
            """插 rows 条 running 执行，mock connection 后投递，断言返回 status 与账本终态。"""
            conn = db.get_connection()
            try:
                conn.execute("DELETE FROM hermes_executions")
                conn.execute("DELETE FROM steer_operations")
                conn.commit()
                for i in range(rows):
                    _running_row(conn, execution_id=f"exec{i}", generation=i + 1,
                                 started_at=12345.0 + i)
                op = steer_ledger.new_operation(conn, "bot-group", "be-dev-01", "work", None, "纠偏文本")
                fake = _FakeClient(result=result, error=error)
                with patch("engine.hermes_gateway.connection", return_value=(fake, "/home", None)):
                    outcome = hermes_gateway_steer.deliver_steer(
                        conn, "bot-group", "be-dev-01", "work", None, "纠偏文本", op["operation_id"])
                check(f"{label} status", outcome["status"] == expected_status, outcome["status"])
                op2 = steer_ledger.get_operation(conn, op["operation_id"])
                if expected_ledger:
                    check(f"{label} 账本", op2["status"] == expected_ledger, op2["status"])
            finally:
                conn.close()

        # 1. 无运行中执行 → target_ended
        run_scenario("无运行中", "target_ended", "target_ended", rows=0)
        # 2. 多运行中执行（身份歧义）→ pending_verify，不取最大 generation
        run_scenario("身份歧义", "pending_verify", "pending_verify", rows=2)
        # 3. queued → 保持 delivering（通道接受，非 inserted）
        run_scenario("queued", "queued", "delivering", result={"status": "queued"})
        # 4. rejected + delivered:False（state running）→ undelivered
        run_scenario("rejected deliveredFalse", "undelivered", "undelivered",
                     result={"status": "rejected", "delivered": False})
        # 5. rejected 缺 delivered 字段 → pending_verify
        run_scenario("rejected 缺delivered", "pending_verify", "pending_verify",
                     result={"status": "rejected"})
        # 6. stale_generation → target_ended
        run_scenario("stale_generation", "target_ended", "target_ended",
                     error=GatewayRefusal("stale_generation"))
        # 7. unsupported_control → undelivered（能力边界）
        run_scenario("unsupported_control", "undelivered", "undelivered",
                     error=GatewayRefusal("unsupported_control"))
        # 8. permission_denied → pending_verify
        run_scenario("permission_denied", "pending_verify", "pending_verify",
                     error=GatewayRefusal("permission_denied"))
        # 9. GatewayTransportError → pending_verify
        run_scenario("transport error", "pending_verify", "pending_verify",
                     error=GatewayTransportError("boom"))

        # 10. _state_reason 回查 state：running 与 terminal 分别补原因
        conn = db.get_connection()
        try:
            conn.execute("DELETE FROM hermes_executions")
            conn.commit()
            _running_row(conn, execution_id="execR", state="running")
            check("state_reason running", "目标运行中" in hermes_gateway_steer._state_reason(conn, "execR"),
                  hermes_gateway_steer._state_reason(conn, "execR"))
            conn.execute("UPDATE hermes_executions SET state='terminal' WHERE execution_id='execR'")
            conn.commit()
            check("state_reason terminal", "目标已结束" in hermes_gateway_steer._state_reason(conn, "execR"),
                  hermes_gateway_steer._state_reason(conn, "execR"))
        finally:
            conn.close()

        # 11. 定位 SQL 带 project + COALESCE(thread_id,0)：不同 project/thread_id 不串会话
        conn = db.get_connection()
        try:
            conn.execute("DELETE FROM hermes_executions")
            conn.commit()
            _running_row(conn, execution_id="execA", profile="be-dev-01", thread_id=7)
            located = hermes_gateway_steer._locate_running(conn, "bot-group", "be-dev-01", "work", None)
            check("work 定位不匹配 thread_id=7 的执行（COALESCE 空值统一）", located is None, str(located))
            _running_row(conn, execution_id="execB", profile="be-dev-01", kind="work", thread_id=None)
            located = hermes_gateway_steer._locate_running(conn, "bot-group", "be-dev-01", "work", None)
            check("work 定位到 thread_id NULL 的 running 执行", located is not None and located["execution_id"] == "execB",
                  str(located))
        finally:
            conn.close()

    finally:
        patcher.stop()

    print("\n=== req#136 task#261 /steer 传输通道回执映射验证 ===")
    if failures:
        print("SOME FAIL:", ", ".join(failures))
        return 1
    print("ALL PASS：定位（project/空值/歧义）+ 回执映射（queued/rejected/refusal/transport）+ state 原因 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
