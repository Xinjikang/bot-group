"""req#83 task#143：适配器骨架 + 会话绑定表——原生 new_session 拿真实 session_id 持久化。

契约（docs/design/SLASH-COMMAND-DESIGN-2026-09-27.md：会话绑定与 generation）：
- 绑定键 (project, profile, kind, thread_id)，work 的 thread_id 恒 0（统一空值约定）
- 值 = 真实 Hermes session_id + generation（并发校验/审计/使旧运行失效）
- resolve_or_create 三路径：绑定有效 → 返回（恢复）；绑定失效 → 抛 SessionBindingInvalid
  （恢复与首建分开，U1 红线，禁止静默重建）；无绑定 → 先按 title 回填旧会话（第 0 代
  兼容），无旧会话再原生 create_session 新建。
- spawn 传真实 session_id（-c 精确 ID 匹配优先 + compression tip 跟随），去掉 --create-if-missing。

运行（需 Hermes 源码的 .venv Python）：
    .venv/Scripts/python.exe scripts/test_session_binding.py

本脚本依赖 Hermes 源码路径（经 session_adapter 动态注入），不进 group-engine unittest
discovery（module 级零副作用、无 TestCase、仅 __main__ 时执行验证——与 test_hermes_*.py
同约定）。
"""
import os
import re
import sys
import tempfile
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_REAL_HERMES_SRC = r"E:\home\admin\hermes\hermes-agent"


def main() -> int:
    from engine import db, session_adapter

    iso = tempfile.mkdtemp(prefix="t143_")
    group_db = os.path.join(iso, "group.db")
    failures = []

    def check(label, cond, detail=""):
        print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            failures.append(label)

    # 隔离：db.db_path → 临时 group.db；_hermes_home → 临时 home；_hermes_src → 真实源码
    patchers = [
        patch.object(db, "db_path", return_value=group_db),
        patch.object(session_adapter, "_hermes_home", return_value=__import__("pathlib").Path(iso)),
        patch.object(session_adapter, "_hermes_src", return_value=_REAL_HERMES_SRC),
    ]
    for p in patchers:
        p.start()
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
                    "SELECT session_id, generation, title FROM session_bindings "
                    "WHERE project=? AND profile=? AND kind=? AND thread_id=?",
                    (project, profile, kind, thread_id)).fetchone()
            finally:
                c.close()

        # 1. 无绑定 → 原生 create_session 拿真实 session_id 并落库
        conn = db.get_connection()
        try:
            sid = session_adapter.resolve_or_create(
                conn, "bot-group", "be-dev-01", "work", None, "be-dev-01-bot-group-work")
        finally:
            conn.close()
        check("新会话 sid 形状 YYYYMMDD_HHMMSS_<6hex>", bool(re.match(r"^\d{8}_\d{6}_[0-9a-f]{6}$", sid)), sid)
        row = binding_row("bot-group", "be-dev-01", "work", 0)
        check("绑定落库 session_id 一致", row is not None and row["session_id"] == sid)
        check("generation 初始 0", row is not None and row["generation"] == 0)

        # 2. 绑定有效 → 返回同一 sid（断点续不新建）
        conn = db.get_connection()
        try:
            sid2 = session_adapter.resolve_or_create(
                conn, "bot-group", "be-dev-01", "work", None, "be-dev-01-bot-group-work")
        finally:
            conn.close()
        check("断点续返回同一 sid", sid2 == sid)

        # 3. 绑定失效 → 抛 SessionBindingInvalid
        conn = db.get_connection()
        try:
            conn.execute(
                "UPDATE session_bindings SET session_id='99999999_000000_dead00' "
                "WHERE project='bot-group' AND profile='be-dev-01' AND kind='work' AND thread_id=0")
            conn.commit()
        finally:
            conn.close()
        raised = False
        conn = db.get_connection()
        try:
            try:
                session_adapter.resolve_or_create(
                    conn, "bot-group", "be-dev-01", "work", None, "be-dev-01-bot-group-work")
            except session_adapter.SessionBindingInvalid:
                raised = True
        finally:
            conn.close()
        check("绑定失效抛 SessionBindingInvalid（禁止静默重建）", raised)

        # 4. work thread_id 恒 0；chat 用真实 thread_id 分开绑定
        conn = db.get_connection()
        try:
            conn.execute("DELETE FROM session_bindings")
            conn.commit()
            sid_work = session_adapter.resolve_or_create(
                conn, "bot-group", "be-dev-01", "work", None, "be-dev-01-bot-group-work")
            sid_chat = session_adapter.resolve_or_create(
                conn, "bot-group", "be-dev-01", "chat", 100, "be-dev-01-bot-group-chat-100")
        finally:
            conn.close()
        rw = binding_row("bot-group", "be-dev-01", "work", 0)
        rc = binding_row("bot-group", "be-dev-01", "chat", 100)
        check("work/chat 分开绑定且 sid 不同", rw is not None and rc is not None and rw["session_id"] != rc["session_id"])

        # 5. 第 0 代兼容：无绑定但 Hermes 存在同 title 旧会话 → 回填不新建
        #    用独立 profile（legacy-bot）+ 独立 title，避免与前四项的会话 title 冲突
        conn = db.get_connection()
        try:
            conn.execute("DELETE FROM session_bindings WHERE profile='legacy-bot'")
            conn.commit()
        finally:
            conn.close()
        sys.path.insert(0, _REAL_HERMES_SRC)
        from hermes_state_registry import acquire as _acquire
        from hermes_state_ids import new_session_id as _mint
        legacy_sid = _mint()
        os.makedirs(str(session_adapter.profile_state_db("legacy-bot").parent), exist_ok=True)
        hdb = _acquire(str(session_adapter.profile_state_db("legacy-bot")))
        try:
            hdb.create_session(legacy_sid, source="cli")
            hdb.set_session_title(legacy_sid, "legacy-bot-bot-group-work")
        finally:
            hdb.close()
        conn = db.get_connection()
        try:
            sid_backfill = session_adapter.resolve_or_create(
                conn, "bot-group", "legacy-bot", "work", None, "legacy-bot-bot-group-work")
        finally:
            conn.close()
        check("第 0 代兼容按 title 回填旧会话", sid_backfill == legacy_sid)
    finally:
        for p in patchers:
            p.stop()

    print("\n=== req#83 task#143 会话绑定验证 ===")
    if failures:
        print("SOME FAIL:", ", ".join(failures))
        return 1
    print("ALL PASS：绑定表 + resolve_or_create 三路径 + 第 0 代回填 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
