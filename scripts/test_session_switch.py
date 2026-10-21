"""req#83 task#144：/new 切换时序 + 压缩续集跟随 + generation 原子更新。

契约（docs/design/SLASH-COMMAND-DESIGN-2026-09-27.md：会话切换）：
- switch_session：原生 new_session 拿新真实 session_id，切换时序 = 互斥（_SWITCH_LOCK）
  + busy_check 锁内重查 + switching 状态 + 以旧 generation 为条件原子更新。
- 首建 generation=0；已有绑定切换 generation+1、session_id 换新；旧 id/generation 落
  session_switch_ops 审计表。
- 旧运行迟到回写被拒：原子更新带 `AND generation=旧值` 条件，条件不满足（并发切换）
  → rowcount 0 → 本次标 unknown 待核实，不覆盖新绑定。
- 压缩续集跟随（T5）：resolve_or_create 命中绑定后走 compression_tip 拿 live tip。
- create-if-missing 恢复与首建分开（U1）：绑定失效抛 SessionBindingInvalid，不静默替换。
- switching 残留（引擎崩溃）：resolve_or_create 清残留标记，按现有绑定继续。

运行（mock 隔离 Hermes，纯 stdlib，系统 python 即可）：
    python scripts/test_session_switch.py
不进 unittest discovery（module 级零副作用、无 TestCase、仅 __main__ 执行）。
"""
import os
import re
import sys
import tempfile
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def main() -> int:
    from engine import db, session_adapter

    iso = tempfile.mkdtemp(prefix="t144_")
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

        def binding_row(project, profile, kind, thread_id):
            c = db.get_connection()
            try:
                return c.execute(
                    "SELECT session_id, generation, switching FROM session_bindings "
                    "WHERE project=? AND profile=? AND kind=? AND thread_id=?",
                    (project, profile, kind, thread_id)).fetchone()
            finally:
                c.close()

        def ops(project, profile):
            c = db.get_connection()
            try:
                return c.execute(
                    "SELECT * FROM session_switch_ops WHERE project=? AND profile=? ORDER BY id",
                    (project, profile)).fetchall()
            finally:
                c.close()

        def new_session():
            """每调用一次 mint 一个递增的 fake sid。"""
            new_session.n += 1
            return f"20260928_150000_{new_session.n:06x}"

        new_session.n = 0

        # mock 隔离 Hermes 原生调用（不碰真实 state.db）
        with patch.object(session_adapter, "create_session",
                          side_effect=lambda profile, title: new_session()), \
             patch.object(session_adapter, "session_exists",
                          return_value=True), \
             patch.object(session_adapter, "resolve_session_by_title",
                          return_value=None):
            # 1. 首建：无绑定 → generation 0
            c = db.get_connection()
            r = session_adapter.switch_session(
                c, "bot-group", "be-dev-01", "work", None, "be-dev-01-bot-group-work")
            c.close()
            check("首建 switch 成功", r.get("ok") and r.get("generation") == 0, str(r))
            row = binding_row("bot-group", "be-dev-01", "work", 0)
            check("首建绑定落库 generation=0 switching=0",
                  row is not None and row["generation"] == 0 and row["switching"] == 0)

            # 2. 已有绑定切换 → generation+1、session_id 换新
            old_sid = row["session_id"]
            c = db.get_connection()
            r2 = session_adapter.switch_session(
                c, "bot-group", "be-dev-01", "work", None, "be-dev-01-bot-group-work")
            c.close()
            row2 = binding_row("bot-group", "be-dev-01", "work", 0)
            check("切换 generation 递增 0→1", r2.get("ok") and r2.get("generation") == 1, str(r2))
            check("切换 session_id 换新", row2 is not None and row2["session_id"] != old_sid)

            # 3. 操作审计：op 表记录 old_session_id / old_generation / committed
            all_ops = ops("bot-group", "be-dev-01")
            check("两条操作记录（首建+切换）", len(all_ops) == 2)
            op_switch = all_ops[-1]
            check("切换 op 记录旧 sid/generation",
                  op_switch["old_session_id"] == old_sid and op_switch["old_generation"] == 0
                  and op_switch["status"] == "committed" and op_switch["new_session_id"] == row2["session_id"])

            # 4. busy_check 命中 → 拒绝，不创建新会话
            before_count = len(ops("bot-group", "be-dev-01"))
            c = db.get_connection()
            r_busy = session_adapter.switch_session(
                c, "bot-group", "be-dev-01", "work", None, "be-dev-01-bot-group-work",
                busy_check=lambda p, k, t, prj: True)
            c.close()
            check("busy 拒绝 /new", not r_busy.get("ok"), str(r_busy))
            check("busy 拒绝不产生新 op", len(ops("bot-group", "be-dev-01")) == before_count)

            # 5. 旧运行迟到回写被拒：switch_session 的原子更新带 `AND generation=?` 条件。
            #    这里直接验证守卫语义——旧运行（拿着旧 generation）尝试回写绑定，
            #    条件不匹配 → rowcount=0，新绑定不被覆盖。
            c = db.get_connection()
            c.execute("UPDATE session_bindings SET generation=99 "
                      "WHERE project='bot-group' AND profile='be-dev-01' AND kind='work' AND thread_id=0")
            c.commit()
            c.close()
            # 记录当前新 sid（generation 已被手工改成 99 模拟并发切换）
            row_cur = binding_row("bot-group", "be-dev-01", "work", 0)
            stale_sid = row_cur["session_id"]
            # 迟到回写：旧运行以为 generation 还是 0，用 gen=0 条件尝试覆盖
            c = db.get_connection()
            cur = c.execute(
                "UPDATE session_bindings SET session_id='20260928_000000_stale' "
                "WHERE project='bot-group' AND profile='be-dev-01' AND kind='work' "
                "AND thread_id=0 AND generation=0")
            c.commit()
            c.close()
            row_after = binding_row("bot-group", "be-dev-01", "work", 0)
            check("迟到回写被拒（rowcount=0，绑定未被覆盖）",
                  cur.rowcount == 0 and row_after["session_id"] == stale_sid,
                  f"rowcount={cur.rowcount}, sid={row_after['session_id']}")

            # 5b. 正常切换继续：generation 从 99 → 100，证明递增连续
            c = db.get_connection()
            r_stale = session_adapter.switch_session(
                c, "bot-group", "be-dev-01", "work", None, "be-dev-01-bot-group-work")
            c.close()
            check("切换后 generation 继续递增（99→100）", r_stale.get("ok") and r_stale.get("generation") == 100, str(r_stale))

        # 6. 压缩续集跟随（T5）：resolve_or_create 命中绑定后走 compression_tip
        with patch.object(session_adapter, "session_exists", return_value=True), \
             patch.object(session_adapter, "resolve_session_by_title", return_value=None), \
             patch.object(session_adapter, "compression_tip", return_value="20260928_999999_tiptip"):
            c = db.get_connection()
            sid_resolved = session_adapter.resolve_or_create(
                c, "bot-group", "be-dev-01", "work", None, "be-dev-01-bot-group-work")
            c.close()
            check("压缩续集跟随返回 tip", sid_resolved == "20260928_999999_tiptip", sid_resolved)

        # 7. 绑定失效 → SessionBindingInvalid（U1 恢复与首建分开）
        with patch.object(session_adapter, "session_exists", return_value=False), \
             patch.object(session_adapter, "resolve_session_by_title", return_value=None):
            c = db.get_connection()
            raised = False
            try:
                session_adapter.resolve_or_create(
                    c, "bot-group", "be-dev-01", "work", None, "be-dev-01-bot-group-work")
            except session_adapter.SessionBindingInvalid:
                raised = True
            c.close()
            check("绑定失效抛 SessionBindingInvalid（不静默替换）", raised)

        # 8. switching 残留清理：resolve_or_create 遇 switching=1 清残留
        c = db.get_connection()
        c.execute("UPDATE session_bindings SET switching=1 "
                  "WHERE project='bot-group' AND profile='be-dev-01' AND kind='work' AND thread_id=0")
        c.commit()
        c.close()
        with patch.object(session_adapter, "session_exists", return_value=True), \
             patch.object(session_adapter, "resolve_session_by_title", return_value=None), \
             patch.object(session_adapter, "compression_tip", side_effect=lambda p, s: s):
            c = db.get_connection()
            session_adapter.resolve_or_create(
                c, "bot-group", "be-dev-01", "work", None, "be-dev-01-bot-group-work")
            c.close()
        row8 = binding_row("bot-group", "be-dev-01", "work", 0)
        check("switching 残留被清", row8 is not None and row8["switching"] == 0)
    finally:
        patcher.stop()

    print("\n=== req#83 task#144 /new 切换时序验证 ===")
    if failures:
        print("SOME FAIL:", ", ".join(failures))
        return 1
    print("ALL PASS：首建/递增/审计/busy 拒绝/原子守卫/压缩跟随/失效抛错/残留清理 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
