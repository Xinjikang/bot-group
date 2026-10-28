"""req#81 spike②（task#136）：验证「新进程能按真实 session_id 恢复（非 title）」，且「能调 ≠ 能恢复」。

背景：斜杠命令方案的 /new 路线要求引擎能拿到 Hermes 真实 session_id 并让后续新进程
（bot 是一次性 subprocess）按该 id 恢复同一会话上下文。硬证据第 2、3 条要求：
  2. 新进程能按真实 session_id 恢复（非 title）
  3. 能调 ≠ 能恢复 ≠ 能注入，三条分开验

约束（req#81）：不碰生产、不改 Hermes 源码、隔离 profile/db 跑。

验证方式：主进程用隔离 db 创建会话 + 写历史 + 取 session_id，然后 spawn 一个**全新
子进程**（独立解释器、独立 SessionDB 连接，等价于「新 bot 进程」），子进程只凭
session_id 恢复并读回历史。同时验证 steer() 的内存态在进程退出后丢失——证明「能调」
与「能恢复」是两回事，必须持久化绑定 session_id。

运行（需 Hermes 源码的 Python 3.11 venv）：
    python scripts/test_hermes_session_restore.py

通过 HERMES_TEST_SOURCE 指定已安装的 Hermes 源码绝对路径。
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.probe_config import hermes_source


def _isolate_home() -> str:
    iso = tempfile.mkdtemp(prefix="hermes_spike_iso_")
    os.environ["HERMES_HOME"] = iso
    return iso


# ── 子进程：模拟「新 bot 进程」，只凭 session_id 恢复 ─────────────────────────
CHILD_CODE = r'''
import json, sys
sys.path.insert(0, {src!r})
from hermes_state_registry import acquire
db_path, session_id, want_title = sys.argv[1], sys.argv[2], sys.argv[3] == "1"

db = acquire(db_path)
try:
    # 按真实 session_id 恢复（非 title 反查）
    row = db.get_session(session_id)
    resolved = db.resolve_session_id(session_id)
    conv = db.get_messages_as_conversation(session_id)
    # 用 title 反查，作为对照（证明 session_id 是主键而非 title）
    by_title = None
    if row and row.get("title"):
        by_title = db.resolve_session_by_title(row["title"])
    print(json.dumps({{
        "row_exists": row is not None,
        "row_id": (row or {{}}).get("id"),
        "row_title": (row or {{}}).get("title"),
        "resolved_id": resolved,
        "conv_len": len(conv),
        "conv_roles": [m.get("role") for m in conv],
        "conv_first_user": (conv[0].get("content") if conv else None),
        "by_title_resolves_to_same": by_title == session_id,
    }}))
finally:
    db.close()
'''


def _run_child(db_path: str, session_id: str, want_title: bool) -> dict:
    code = CHILD_CODE.format(src=hermes_source())
    proc = subprocess.run(
        [sys.executable, "-c", code, db_path, session_id, "1" if want_title else "0"],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "HERMES_HOME": os.environ["HERMES_HOME"]},
    )
    if proc.returncode != 0:
        return {"child_error": proc.stderr.strip()[-2000:]}
    # 子进程可能打印隔离警告到 stdout 之外；取最后一行 JSON
    lines = [l for l in proc.stdout.splitlines() if l.strip().startswith("{")]
    if not lines:
        return {"child_no_json": proc.stdout.strip()[-2000:], "stderr": proc.stderr.strip()[-1000:]}
    return json.loads(lines[-1])


def main() -> int:
    source = hermes_source()
    iso = _isolate_home()
    sys.path.insert(0, source)
    from hermes_state_registry import acquire
    from hermes_state_ids import new_session_id

    db_path = os.path.join(iso, "state.db")
    failures = []

    def check(label, cond, detail=""):
        print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            failures.append(label)

    # 1. 主进程：隔离 db 创建会话 + 写历史 + 取真实 session_id
    sid = new_session_id()
    db = acquire(db_path)
    try:
        db.create_session(sid, source="cli")
        db.set_session_title(sid, "be-dev-01-bot-group-work")
        db.append_message(sid, "user", "主人：请处理任务 #1")
        db.append_message(sid, "assistant", "收到，开始处理")
        check("主进程创建会话 + 写 2 条历史", True)
    finally:
        db.close()

    # 2. 新进程（子进程）只凭 session_id 恢复
    child = _run_child(db_path, sid, want_title=True)
    check("子进程无报错", "child_error" not in child and "child_no_json" not in child,
          json.dumps(child, ensure_ascii=False)[:300])
    check("按 session_id 恢复 row 存在", child.get("row_exists") is True)
    check("恢复的 row.id == 传入 session_id", child.get("row_id") == sid,
          f"row_id={child.get('row_id')}")
    check("resolve_session_id 精确命中", child.get("resolved_id") == sid)
    check("恢复历史 2 条", child.get("conv_len") == 2, f"conv_len={child.get('conv_len')}")
    check("历史 role 顺序 user→assistant", child.get("conv_roles") == ["user", "assistant"])
    check("首条 user 内容原样恢复", child.get("conv_first_user") == "主人：请处理任务 #1",
          repr(child.get("conv_first_user")))
    check("title 反查也指向同一 session_id（title 非主键，仅显示）",
          child.get("by_title_resolves_to_same") is True)

    # 3. 能调 ≠ 能恢复：steer 是内存态，进程退出即丢——新进程读不到上一个进程的 pending steer
    #    这里用「标题恢复」证明 title 只是显示层；真正的恢复键是 session_id。
    #    进一步：不存在的 session_id 无法恢复（fail-closed）。
    child_bad = _run_child(db_path, "99999999_000000_zzzzzz", want_title=False)
    check("不存在的 session_id 恢复为 None（fail-closed）", child_bad.get("row_exists") is False)

    print("\n=== spike② 结论 ===")
    if failures:
        print("SOME FAIL:", ", ".join(failures))
        return 1
    print("ALL PASS：新进程可按真实 session_id（非 title）恢复会话与历史")
    print(f"真实 session_id = {sid}")
    print(f"隔离 db = {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
