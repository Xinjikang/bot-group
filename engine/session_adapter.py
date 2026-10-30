"""req#83 会话绑定适配器：引擎侧调 Hermes 原生 SessionDB 拿真实 session_id。

背景（docs/design/SLASH-COMMAND-DESIGN-2026-09-27.md S1/U1/T5）：
- /new 与断点续的正路 = 引擎持久化绑定真实 Hermes session_id，spawn 传真实
  session_id（走 `-c`，Hermes CLI `_resolve_session_by_name_or_id` 精确 ID 匹配
  优先 + 自动 `get_compression_tip` 跟随压缩续集）。
- 会话 title 只是显示层/迁移用途，不是恢复主键（spike② 实证）。
- 压缩轮转 session_id→续集 id 由 get_compression_tip 接住（T5），generation 职责
  改为并发校验 + 审计 + 使旧运行失效（/new 开新 generation 后旧运行迟到回写被拒）。

本模块跑在引擎进程（.venv，Python 3.11）内，引擎 venv 无 hermes 包，需动态
注入 hermes-agent 源码路径再 import（hermes_state_ids 纯 stdlib 可安全 import）。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import threading
import uuid
from pathlib import Path

from . import db, hermes_runtime

# 引擎进程 HERMES_HOME 语义 = hermes 根目录（start.sh export 的 E:\home\admin\hermes），
# 与 bot 进程（hermes -p <profile>）的 HERMES_HOME=profiles/<name> 不同。state.db 定位
# 与 engine/reasoning.py::_profile_state_db、engine/stop.py::_profile_state_db 同规则。
_DEFAULT_HOME_FALLBACK = Path(__file__).resolve().parent.parent.parent


def _hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env)
    return _DEFAULT_HOME_FALLBACK


def _hermes_src() -> Path:
    """Locate the source belonging to the selected Hermes executable."""
    return hermes_runtime.source_directory(_hermes_home())


def available() -> bool:
    """Select binding for a source-root home or an explicitly configured entry.

    Capability validation runs before accessing SessionDB. A profile-scoped home
    retains CLI-managed sessions and never becomes the parent of other profiles.
    """
    if _hermes_home().parent.name == 'profiles':
        return False
    return ((_hermes_home() / "hermes-agent").is_dir()
            or bool(os.environ.get('HERMES_HOME', '').strip()
                    and os.environ.get('HERMES_BIN', '').strip()))


def profile_state_db(profile: str) -> Path:
    """该 profile 的 state.db（default → <home>/state.db，其他 → <home>/profiles/<名>/state.db）。"""
    home = _hermes_home()
    from . import bot_identity
    return bot_identity.hermes_home(profile, home) / 'state.db'


def _ensure_src_on_path() -> None:
    hermes_runtime.prepare_source(_hermes_src())


def _ensure_profile_dir(profile: str) -> None:
    """named profile 需显式建目录（Hermes assert_named_profile_home_live 强制）。"""
    dbp = profile_state_db(profile)
    if dbp.parent != dbp:  # 非根 state.db
        dbp.parent.mkdir(parents=True, exist_ok=True)


def create_session(profile: str, title: str, *, project=None, kind='work') -> str:
    """调 Hermes 原生 new_session 拿真实 session_id 并落库（含 title）。

    等价于 hermes_cli/main.py::_create_titled_session，但显式定位 per-profile state.db。
    返回真实 session_id（YYYYMMDD_HHMMSS_<6hex>）。
    """
    from . import hermes_gateway
    if hermes_gateway.enabled():
        from . import wake, hermes_prompt_state
        workdir = wake._workdir_for(profile, project) if project is not None else str(Path.cwd())
        prompt = wake.wake_system_prompt(kind, project, workdir) if project is not None else ''
        client, _, _ = hermes_gateway.connection(profile)
        with client:
            sid = hermes_gateway.sessions.new(client, title=title, workdir=workdir,
                                              system_prompt=prompt, model_args=[])['session_id']
            if project is not None:
                from . import db as group_db
                conn = group_db.get_connection()
                try:
                    hermes_prompt_state.created(conn, client.endpoint['profile_id'], sid, prompt)
                    conn.commit()
                finally:
                    conn.close()
            return sid
    _ensure_src_on_path()
    _ensure_profile_dir(profile)
    from hermes_state_registry import acquire

    sid = hermes_runtime.new_session_id(_hermes_src())
    db = acquire(str(profile_state_db(profile)))
    try:
        db.create_session(sid, source="cli")
        try:
            db.set_session_title(sid, title)
        except Exception:
            db.delete_session_if_empty(sid)
            raise
        return sid
    finally:
        db.close()


def session_exists(profile: str, session_id: str) -> bool:
    """校验该 profile 下 session_id 是否仍有效（恢复前置，防失效绑定静默替换）。"""
    from . import hermes_gateway
    if hermes_gateway.enabled():
        client, _, _ = hermes_gateway.connection(profile)
        with client:
            try:
                hermes_gateway.sessions.attach(client, session_id)
                return True
            except hermes_gateway.GatewayRefusal as exc:
                if exc.reason == 'not_found':
                    return False
                raise
    _ensure_src_on_path()
    _ensure_profile_dir(profile)
    from hermes_state_registry import acquire

    db = acquire(str(profile_state_db(profile)))
    try:
        return db.get_session(session_id) is not None
    finally:
        db.close()


class SessionBindingInvalid(Exception):
    """绑定存在但 Hermes 会话已失效——恢复应明确失败，禁止静默重建（U1）。"""


class SessionBusy(Exception):
    """目标会话正在执行或切换中，/new 拒绝（S3 空闲互斥）。"""


_SWITCH_LOCK = threading.RLock()


def _bind_key(kind: str, thread_id: int | None) -> int:
    """work 的 thread_id 恒 0；chat 用真实话题 id（统一空值约定）。"""
    return thread_id if (kind == "chat" and thread_id is not None) else 0


def compression_tip(profile: str, session_id: str) -> str:
    """压缩续集跟随（T5）：绑定 id 可能已压缩轮转，解析到 live tip。

    沿用 Hermes `_resolve_session_by_name_or_id` 的 get_compression_tip 语义——
    绑定旧 id 若落回已压缩父会话，这里接住续集 id；同 generation 内解析，
    适配器回传实际 session_id，引擎只接受匹配当前 generation 的更新。
    """
    from . import hermes_gateway
    if hermes_gateway.enabled():
        client, _, _ = hermes_gateway.connection(profile)
        with client:
            return hermes_gateway.sessions.attach(client, session_id)['session_id']
    _ensure_src_on_path()
    _ensure_profile_dir(profile)
    from hermes_state_registry import acquire

    db = acquire(str(profile_state_db(profile)))
    try:
        try:
            return db.get_compression_tip(session_id) or session_id
        except Exception:
            return session_id
    finally:
        db.close()


def resolve_session_by_title(profile: str, title: str, *, read_only=False) -> str | None:
    """按 title 反查 Hermes 现存会话 sid（第 0 代兼容：旧会话无绑定记录时回填）。"""
    from . import hermes_gateway
    from websockets.exceptions import WebSocketException
    try:
        if hermes_gateway.enabled():
            client, _, _ = hermes_gateway.connection(profile, read_only=read_only)
            with client:
                rows = client.rpc('session.list', title=title)['sessions']
                return rows[0]['session_id'] if rows else None
    except (hermes_gateway.GatewayTransportError, hermes_gateway.GatewayRefusal,
            hermes_runtime.HermesConfigurationError, subprocess.TimeoutExpired,
            OSError, WebSocketException):
        if read_only:
            return None
        raise
    if read_only:
        return _read_legacy_session_by_title(profile, title)
    _ensure_src_on_path()
    _ensure_profile_dir(profile)
    from hermes_state_registry import acquire

    db = acquire(str(profile_state_db(profile)))
    try:
        return db.resolve_session_by_title(title)
    finally:
        db.close()


def resolve_or_create(conn, project: str, profile: str, kind: str,
                      thread_id: int | None, title: str) -> str:
    """查/建会话绑定，返回真实 Hermes session_id。

    绑定键 (project, bot_id, kind, thread_id)（req#86 task#148：主体从 profile
    切到 Bot 稳定身份）；work 的 thread_id 恒 0（统一空值约定）。profile 保留为
    兼容列（wake/stop/steer_ledger 的按 profile 读取仍在用）。对外签名不变：
    仍收 profile，内部解析 bot_id。
    - 绑定存在且 Hermes 会话仍有效 → 压缩续集跟随（get_compression_tip）后返回实际 id
    - 绑定存在但会话已失效 → 抛 SessionBindingInvalid（恢复与首建分开，U1 红线）
    - 绑定不存在 → 先按 title 回填 Hermes 现存旧会话（第 0 代兼容，决策点 4），
      无旧会话再调原生 create_session 新建真实 session_id，UPSERT 绑定后返回（首建）
    """
    tid = _bind_key(kind, thread_id)
    with _SWITCH_LOCK:
        return _resolve_or_create_locked(conn, project, profile, kind, tid, title)


def _resolve_or_create_locked(conn, project: str, profile: str, kind: str,
                              tid: int, title: str) -> str:
    """resolve_or_create 的锁内实现（与 switch_session 共用 _SWITCH_LOCK 互斥）。"""
    bot_id = db._ensure_hermes_bot(conn, profile)
    _repair_shifted_binding(conn, project, profile, bot_id, kind, tid)
    row = conn.execute(
        "SELECT session_id, switching FROM session_bindings "
        "WHERE project=? AND bot_id=? AND kind=? AND thread_id=?",
        (project, bot_id, kind, tid),
    ).fetchone()
    if row is not None and row["switching"]:
        # switching=1 残留（引擎崩溃或并发切换未清）：恢复时先核对，这里保守清掉
        # 残留标记、按现有绑定继续（不静默替换，恢复与首建分开）。
        conn.execute(
            "UPDATE session_bindings SET switching=0, updated_at=? "
            "WHERE project=? AND bot_id=? AND kind=? AND thread_id=?",
            (db.now_iso(), project, bot_id, kind, tid),
        )
        conn.commit()
    if row is not None:
        sid = row["session_id"]
        if session_exists(profile, sid):
            return compression_tip(profile, sid)
        raise SessionBindingInvalid(
            f"{profile} {kind} 会话绑定失效（session_id={sid}），需 /new 或人工处理"
        )
    # 第 0 代兼容：旧会话（无绑定记录）按 title 回填，避免断点续断裂
    existing = resolve_session_by_title(profile, title)
    if existing is not None:
        conn.execute(
            "INSERT INTO session_bindings "
            "(project, bot_id, profile, kind, thread_id, session_id, runtime, generation, title, updated_at) "
            "VALUES(?,?,?,?,?,?, 'hermes', 0,?,?) "
            "ON CONFLICT(project, bot_id, kind, thread_id) DO UPDATE SET "
            "session_id=excluded.session_id, title=excluded.title, updated_at=excluded.updated_at",
            (project, bot_id, profile, kind, tid, existing, title, db.now_iso()),
        )
        conn.commit()
        return existing
    # 首建：调原生 new_session 拿真实 session_id
    sid = create_session(profile, title)
    conn.execute(
        "INSERT INTO session_bindings "
        "(project, bot_id, profile, kind, thread_id, session_id, runtime, generation, title, updated_at) "
        "VALUES(?,?,?,?,?,?, 'hermes', 0,?,?) "
        "ON CONFLICT(project, bot_id, kind, thread_id) DO UPDATE SET "
        "session_id=excluded.session_id, title=excluded.title, updated_at=excluded.updated_at",
        (project, bot_id, profile, kind, tid, sid, title, db.now_iso()),
    )
    conn.commit()
    return sid


def _repair_shifted_binding(conn, project, profile, bot_id, kind, tid):
    """Recover the exact #148 column-shift defect using a verified existing session."""
    row = conn.execute(
        "SELECT session_id,generation FROM session_bindings "
        "WHERE project=? AND bot_id=? AND kind=? AND thread_id=?",
        (project, bot_id, kind, tid),
    ).fetchone()
    if row is None or row['session_id'] != '0' or not isinstance(row['generation'], str):
        return
    sid = row['generation']
    if not session_exists(profile, sid):
        raise SessionBindingInvalid(f'{profile} 错位绑定中的会话不存在，需人工核对')
    conn.execute(
        "UPDATE session_bindings SET session_id=?,generation=0,switching=0,updated_at=? "
        "WHERE project=? AND bot_id=? AND kind=? AND thread_id=? "
        "AND session_id='0' AND generation=?",
        (sid, db.now_iso(), project, bot_id, kind, tid, sid),
    )
    conn.commit()


def existing_session_id(conn, project: str, profile: str, kind: str,
                        thread_id: int | None, title: str) -> str | None:
    """Read existing binding or legacy title without creating a profile/session.

    读不建行：按 bot_id 反查绑定（req#86 task#148；Bot 行没有时视为无绑定，
    不做反推建 Bot——这是只读状态查询）。兼容期老形态行（bot_id IS NULL 且
    profile 匹配）不会被 bot_id 键读到——绑定写入方（resolve_or_create/
    switch_session）都先建 Bot 再写，正常运行不会出现该形态；老形态行只可能
    来自迁移期手工写入，走下方 legacy title 兜底路径。
    """
    bot_id = db._bot_id_of(conn, profile)
    if bot_id is not None:
        bot = conn.execute('SELECT runtime FROM bots WHERE id=?', (bot_id,)).fetchone()
        if bot and bot['runtime'] == 'codex':
            # Codex has no Hermes legacy title/state.db fallback. Empty reset
            # tombstones and bindings from another runtime are absent sessions.
            return codex_thread_id(conn, project, profile, kind, thread_id, bot_id=bot_id)
        row = conn.execute('SELECT session_id FROM session_bindings '
                           'WHERE project=? AND bot_id=? AND kind=? AND thread_id=?',
                           (project, bot_id, kind, _bind_key(kind, thread_id))).fetchone()
        if row:
            return row['session_id']
    return resolve_session_by_title(profile, title, read_only=True)


def _read_legacy_session_by_title(profile: str, title: str) -> str | None:
    """Read a legacy title through SQLite's read-only connection mode."""
    path = profile_state_db(profile)
    if not path.is_file():
        return None
    legacy = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        row = legacy.execute('SELECT id FROM sessions WHERE title=? ORDER BY started_at DESC LIMIT 1',
                             (title,)).fetchone()
        return row[0] if row else None
    finally:
        legacy.close()


def switch_session(conn, project: str, profile: str, kind: str,
                   thread_id: int | None, title: str, busy_check=None, existing_only=False) -> dict:
    """/new 切换：原生 new_session 拿新真实 session_id，原子更新绑定 + generation 递增。

    S3 切换时序（设计文档第三节第 2 条）：
    1. 用与 wake 共用的目标作用域互斥（_SWITCH_LOCK）取得切换权；锁内重查忙碌
       （busy_check 回调：目标会话执行所有权 + 正在投递 wake + 真实存活进程）。
    2. 写命令操作记录（operation_id + 旧 id/generation + switching 状态），再跑原生适配器。
    3. 新会话持久化后，以旧 generation 为条件原子更新绑定（generation+1、session_id 换新、
       switching 清零），记录结果，解除切换。
    4. 原生调用可能已生效但回执失败 → status='unknown'（待核实），保留候选新 id，
       恢复时先核对实际会话，避免重复创建。

    返回 {ok, session_id, generation, operation_id, status}。
    """
    tid = _bind_key(kind, thread_id)
    with _SWITCH_LOCK:
        bot_id = db._ensure_hermes_bot(conn, profile)
        _repair_shifted_binding(conn, project, profile, bot_id, kind, tid)
        existing = existing_session_id(conn, project, profile, kind, thread_id, title) if existing_only else None
        if existing_only and not existing:
            return {"ok": False, "detail": "本项目暂无工作会话"}
        # 锁内重查忙碌（空闲判定覆盖执行所有权 + 投递中 wake + 真实存活进程）
        if busy_check is not None and busy_check(profile, kind, tid, project):
            return {"ok": False, "detail": "目标 bot 忙碌，请等空闲后再 /new"}
        row = conn.execute(
            "SELECT session_id, generation FROM session_bindings "
            "WHERE project=? AND bot_id=? AND kind=? AND thread_id=?",
            (project, bot_id, kind, tid),
        ).fetchone()
        old_sid = row["session_id"] if row is not None else (existing or "")
        old_gen = row["generation"] if row is not None else -1
        op_id = uuid.uuid4().hex
        # 写操作记录（switching 状态），审计锚点
        conn.execute(
            "INSERT INTO session_switch_ops "
            "(operation_id, project, profile, kind, thread_id, old_session_id, "
            " old_generation, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?, 'switching', ?, ?)",
            (op_id, project, profile, kind, tid, old_sid, old_gen, db.now_iso(), db.now_iso()),
        )
        if row is not None:
            conn.execute(
                "UPDATE session_bindings SET switching=1, updated_at=? "
                "WHERE project=? AND bot_id=? AND kind=? AND thread_id=?",
                (db.now_iso(), project, bot_id, kind, tid),
            )
        conn.commit()
        # 原生 new_session 拿新真实 session_id（首建路径复用，与恢复分开）
        try:
            # Hermes titles are unique; logical title remains in the binding.
            new_sid = create_session(profile, f'{title}#{op_id}', project=project, kind=kind)
        except Exception as exc:
            conn.execute(
                "UPDATE session_switch_ops SET status='unknown', updated_at=? "
                "WHERE operation_id=?",
                (db.now_iso(), op_id),
            )
            conn.execute(
                "UPDATE session_bindings SET switching=0, updated_at=? "
                "WHERE project=? AND bot_id=? AND kind=? AND thread_id=?",
                (db.now_iso(), project, bot_id, kind, tid),
            )
            conn.commit()
            return {"ok": False, "detail": f"原生 new_session 失败：{exc}", "operation_id": op_id}
        new_gen = old_gen + 1 if row is not None else 0
        # 原子更新：以旧 generation 为条件（防并发旧运行迟到回写覆盖新绑定）
        cur = conn.execute(
            "UPDATE session_bindings SET session_id=?, generation=?, switching=0, "
            " title=?, updated_at=? "
            "WHERE project=? AND bot_id=? AND kind=? AND thread_id=? AND generation=?",
            (new_sid, new_gen, title, db.now_iso(), project, bot_id, kind, tid, old_gen),
        )
        if cur.rowcount == 0 and row is not None:
            # 旧 generation 已被别处改动（并发切换），回滚本次，标记 unknown 待核实
            conn.execute(
                "UPDATE session_switch_ops SET status='unknown', new_session_id=?, updated_at=? "
                "WHERE operation_id=?",
                (new_sid, db.now_iso(), op_id),
            )
            conn.commit()
            return {"ok": False, "detail": "会话绑定已被并发改动，本次 /new 待核实",
                    "operation_id": op_id, "new_session_id": new_sid}
        if row is None:
            conn.execute(
                "INSERT INTO session_bindings "
                "(project, bot_id, profile, kind, thread_id, session_id, runtime, generation, title, updated_at) "
                "VALUES(?,?,?,?,?,?, 'hermes', ?,?,?)",
                (project, bot_id, profile, kind, tid, new_sid, new_gen, title, db.now_iso()),
            )
        conn.execute(
            "UPDATE session_switch_ops SET status='committed', new_session_id=?, updated_at=? "
            "WHERE operation_id=?",
            (new_sid, db.now_iso(), op_id),
        )
        conn.commit()
        from . import steer_ledger
        steer_ledger.reconcile_targets(conn)
        return {"ok": True, "session_id": new_sid, "generation": new_gen,
                "operation_id": op_id, "status": "committed"}


# ---------------------------------------------------------------------------
# req#88 task#153：codex 会话映射（设计稿第四节第 1-6 条 + Codex 批注技术收尾）
# ---------------------------------------------------------------------------
# codex 无「会话创建」动作（第四节第 5 条）：绑定是「执行后回填」（thread.started 拿
# thread_id），不是「执行前创建」。session_id 列语义分叉：codex 行存原生 thread_id。
# runtime 列逐行快照（第四节第 1 条）：读取方按行 runtime 判 session_id 语义，不靠
# bots.runtime 推导——Bot runtime 可切换后旧行残留异种 ID 会静默撞车。
# codex 行 switching 恒 0（第四节第 2 条）：codex 无会话创建链，无 switching 状态可清。

def _codex_bot_id(conn, project: str, profile: str) -> int | None:
    """codex Bot 的稳定身份 id（pm.bot_id → bots.runtime='codex' 正路）。

    codex Bot 由 #150/#151 注册接口产生，注册即写 bot_id，故 codex 场景 bot_id
    恒非空。runtime 判等兜底：同 profile 若被改成 hermes，则不命中（返回 None），
    调用方据此不走 codex 映射。
    """
    from . import bot_identity
    bot = bot_identity.resolve(conn, project, profile)
    return bot['id'] if bot and bot['runtime'] == 'codex' else None


def codex_thread_id(conn, project: str, profile: str, kind: str,
                    thread_id: int | None, bot_id: int | None = None) -> str | None:
    """读 codex 绑定行的原生 thread_id（runtime 列逐行校验，第四节第 1/3 条）。

    返回 None 表示「无有效绑定」→ 首建（codex exec 无 resume）。session_id 空串
    （/new 清空后）同样视为无绑定。非 codex 行（runtime != 'codex'）不返回——
    防 hermes 行残留 session_id 被误当 codex thread_id 去 resume。
    """
    if bot_id is None:
        bot_id = _codex_bot_id(conn, project, profile)
    if bot_id is None:
        return None
    row = conn.execute(
        "SELECT session_id, runtime FROM session_bindings "
        "WHERE project=? AND bot_id=? AND kind=? AND thread_id=?",
        (project, bot_id, kind, _bind_key(kind, thread_id)),
    ).fetchone()
    if row is None or row["runtime"] != "codex":
        return None
    return row["session_id"] or None


def codex_save_thread(conn, project: str, profile: str, kind: str,
                      thread_id: int | None, native_thread_id: str,
                      bot_id: int | None = None, spawn_gen: int | None = None) -> bool:
    """thread.started 回填：把 codex 原生 thread_id 写进绑定行（runtime='codex'）。

    第四节第 4 条「收到 thread.started 时及时保存绑定」+ 第 6 条「旧执行回写保护」：
    - 无绑定行且 spawn_gen is None（本次为首建）→ INSERT（generation=0）。
    - 有绑定行且 generation == spawn_gen → 原子 UPDATE（条件含 generation，防
      /new 递增后旧运行迟到回写覆盖新绑定）。
    - generation 不匹配（/new 已弃旧 thread）或行态与 spawn 时不一致 → 拒绝回填，
      返回 False（旧运行迟到回写被拒，不覆盖）。
    返回 True 表示绑定已落库。
    """
    if bot_id is None:
        bot_id = _codex_bot_id(conn, project, profile)
    if bot_id is None:
        return False
    tid = _bind_key(kind, thread_id)
    with _SWITCH_LOCK:
        row = conn.execute(
            "SELECT generation FROM session_bindings "
            "WHERE project=? AND bot_id=? AND kind=? AND thread_id=?",
            (project, bot_id, kind, tid),
        ).fetchone()
        if row is None:
            if spawn_gen is not None:
                # spawn 时有行、回填时已无行（/new 清空/并发）→ 拒，不复活已弃绑定
                return False
            conn.execute(
                "INSERT INTO session_bindings "
                "(project, bot_id, profile, kind, thread_id, session_id, runtime, "
                " generation, switching, title, updated_at) "
                "VALUES(?,?,?,?,?,?, 'codex', 0, 0, '', ?)",
                (project, bot_id, profile, kind, tid, native_thread_id, db.now_iso()),
            )
            conn.commit()
            return True
        if spawn_gen is None or row["generation"] != spawn_gen:
            # 首建途中 /new 抢先建行，或 generation 已递增（旧运行迟到回写）→ 拒
            return False
        cur = conn.execute(
            "UPDATE session_bindings SET session_id=?, runtime='codex', switching=0, "
            " updated_at=? "
            "WHERE project=? AND bot_id=? AND kind=? AND thread_id=? AND generation=?",
            (native_thread_id, db.now_iso(), project, bot_id, kind, tid, spawn_gen),
        )
        conn.commit()
        return cur.rowcount == 1


def codex_new(conn, project: str, profile: str, kind: str,
              thread_id: int | None, title: str, bot_id: int | None = None,
              busy_check=None, existing_only=False) -> dict:
    """/new 清空 Codex Bot 的旧线程绑定，下一次唤醒创建新线程。

    语义 = 「放弃旧 thread、清空绑定、下次唤醒重新 codex exec 首建」。codex 无
    会话创建链，不存在 switching 状态可清，故 /new 对 codex 行不插 switching=1，
    直接清空绑定（session_id 置 ''，generation+1 防旧运行迟到回写）+ 落
    session_switch_ops 审计（status='committed'，old/new session_id 存 thread_id；
    清空后无新 thread_id，new_session_id 置 ''）。
    """
    if bot_id is None:
        bot_id = _codex_bot_id(conn, project, profile)
    if bot_id is None:
        return {"ok": False, "detail": "目标不是 codex Bot，无法按 codex 会话映射重置"}
    tid = _bind_key(kind, thread_id)
    with _SWITCH_LOCK:
        if busy_check and busy_check(profile, kind, thread_id, project):
            return {'ok': False, 'detail': '目标会话正在执行，请空闲后再输入 /new'}
        row = conn.execute(
            "SELECT session_id, generation FROM session_bindings "
            "WHERE project=? AND bot_id=? AND kind=? AND thread_id=?",
            (project, bot_id, kind, tid),
        ).fetchone()
        if existing_only and (row is None or not row['session_id']):
            return {'ok': False, 'detail': '当前项目尚无可重置的会话'}
        old_sid = row["session_id"] if row is not None else ""
        old_gen = row["generation"] if row is not None else -1
        op_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO session_switch_ops "
            "(operation_id, project, profile, kind, thread_id, old_session_id, "
            " old_generation, new_session_id, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?, '', 'committed', ?, ?)",
            (op_id, project, profile, kind, tid, old_sid, old_gen, db.now_iso(), db.now_iso()),
        )
        if row is not None:
            conn.execute(
                "UPDATE session_bindings SET session_id='', runtime='codex', generation=generation+1, "
                " switching=0, updated_at=? "
                "WHERE project=? AND bot_id=? AND kind=? AND thread_id=?",
                (db.now_iso(), project, bot_id, kind, tid),
            )
        else:
            # A tombstone invalidates a first spawn that has not emitted
            # thread.started yet. Its spawn_gen=None can no longer insert.
            conn.execute("INSERT INTO session_bindings(project,bot_id,profile,kind,thread_id,"
                         "session_id,runtime,generation,switching,title,updated_at) "
                         "VALUES(?,?,?,?,?,'','codex',0,0,?,?)",
                         (project, bot_id, profile, kind, tid, title, db.now_iso()))
        conn.commit()
        return {"ok": True, "operation_id": op_id, "status": "committed",
                "old_session_id": old_sid, "new_session_id": ""}
