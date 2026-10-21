"""group.db 连接与建表。

铁律（ENGINEERING.md）：
- 所有连接统一 PRAGMA journal_mode=WAL + busy_timeout=30000（实测：多写者由库锁串行化，阻塞~2s 正常完成）
- messages 表只 INSERT，永不 UPDATE/DELETE
- 重启恢复：wake_jobs 的 running → queued 重新发信号；inbox 原样不动
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

def db_path() -> str:
    """group.db 路径（req#122 起走统一配置层：命令行 > 环境 > .env > 默认，GROUP_DB 覆盖优先）。"""
    from . import config
    return config.db_path()


def now_iso(seconds_offset: int = 0) -> str:
    if seconds_offset:
        return time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + seconds_offset)
        )
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def get_connection() -> sqlite3.Connection:
    """打开 group.db 连接，统一 WAL + busy_timeout=30s + foreign_keys。"""
    path = db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """建表（schema.sql）。幂等。"""
    schema_path = Path(__file__).resolve().parent / "schema.sql"
    # Rename before schema.sql creates the index referencing the new column.
    cols = {r['name'] for r in conn.execute('PRAGMA table_info(tasks)')}
    if 'next_role' in cols and 'next_bot' not in cols:
        conn.execute('ALTER TABLE tasks RENAME COLUMN next_role TO next_bot')
    had_project_roles = any(r['name'] == 'role' for r in conn.execute('PRAGMA table_info(project_members)'))
    conn.executescript(schema_path.read_text(encoding="utf-8"))
    # req#101: additive readiness contract; ALTER DEFAULT backfills existing rows.
    # Repeated initialization preserves states owned by future setup workflows.
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        _migrate_add_column(conn, 'projects', 'readiness_status',
            "TEXT NOT NULL DEFAULT 'ready' CHECK(readiness_status IN ('pending','initializing','ready','failed'))")
        _migrate_add_column(conn, 'projects', 'removed', 'INTEGER NOT NULL DEFAULT 0')
        _migrate_add_column(conn, 'projects', 'removed_at', "TEXT NOT NULL DEFAULT ''")
    # req#99: independent requirement fields; preserve historical description verbatim.
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        for field in ("background_goal", "scope", "approach_basis", "acceptance", "undecided", "source_ref"):
            _migrate_add_column(conn, "requirements", field, "TEXT NOT NULL DEFAULT ''")
    for field in ("description", "design_basis", "acceptance_criteria"):
        _migrate_add_column(conn, "tasks", field, "TEXT NOT NULL DEFAULT ''")
    _migrate_add_column(conn, "tasks", "prerequisites", "TEXT NOT NULL DEFAULT '[]'")
    _migrate_add_column(conn, "tasks", "briefing_status", "TEXT NOT NULL DEFAULT 'needs_details'")
    _migrate_add_column(conn, "attachments", "position", "INTEGER NOT NULL DEFAULT 0")
    # 迁移：已有库补列（SQLite ALTER ADD COLUMN 幂等判断）
    _migrate_add_column(conn, "wake_jobs", "chat_context_until", "TEXT")
    _migrate_add_column(conn, "wake_jobs", "project", "TEXT NOT NULL DEFAULT 'xjk'")
    _migrate_add_column(conn, "wake_jobs", "thread_id", "INTEGER")
    # 心跳归属字段独立于防误开工的上下文窗口。
    _migrate_add_column(conn, "wake_jobs", "last_wake_kind", "TEXT")
    _migrate_add_column(conn, "wake_jobs", "last_wake_thread_id", "INTEGER")
    _migrate_add_column(conn, "wake_runs", "pid", "INTEGER")
    _migrate_add_column(conn, "wake_runs", "project", "TEXT")
    _migrate_add_column(conn, "wake_runs", "profile", "TEXT")
    _migrate_add_column(conn, "wake_runs", "thread_id", "INTEGER")
    # req#86 task#148：列改名 hermes_session_id → runtime_session_id。老库补列兜底
    # 双向：极老库无源列时补新名；已改名库（有新名）不再补旧名。
    wr_cols = {r["name"] for r in conn.execute("PRAGMA table_info(wake_runs)")}
    if "runtime_session_id" not in wr_cols and "hermes_session_id" not in wr_cols:
        conn.execute("ALTER TABLE wake_runs ADD COLUMN runtime_session_id TEXT")
    _migrate_add_column(conn, "threads", "project", "TEXT NOT NULL DEFAULT 'xjk'")
    _migrate_add_column(conn, "threads", "hidden", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column(conn, "threads", "round", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column(conn, "threads", "turn_profile", "TEXT DEFAULT ''")
    _migrate_add_column(conn, "threads", "turn_wake_at", "TEXT DEFAULT ''")
    _migrate_add_column(conn, "threads", "turn_parent_id", "INTEGER")
    _migrate_add_column(conn, "threads", "turn_skipped", "TEXT DEFAULT '[]'")
    _migrate_add_column(conn, "messages", "project", "TEXT NOT NULL DEFAULT 'xjk'")
    _migrate_add_column(conn, "messages", "parent_id", "INTEGER")
    _migrate_add_column(conn, "messages", "round", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column(conn, "messages", "depth", "INTEGER NOT NULL DEFAULT 0")
    _migrate_add_column(conn, "messages", "retract_by", "TEXT")
    _migrate_add_column(conn, "messages", "retract_at", "TEXT")
    _migrate_add_column(conn, "messages", "need_reply", "INTEGER DEFAULT 1")
    # req#61：need_reply 三态（NULL/0/1）。老库该列仍是 NOT NULL（req#55 加的），
    # SQLite 不能 ALTER 去 NOT NULL，须表重建迁移（同引擎重启窗口做）。
    _migrate_messages_need_reply_nullable(conn)
    _migrate_add_column(conn, "projects", "description", "TEXT DEFAULT ''")
    _migrate_add_column(conn, "projects", "worktree_root", "TEXT DEFAULT ''")
    _migrate_add_column(conn, "requirements", "archived", "INTEGER NOT NULL DEFAULT 0")
    # req#111 task#192：scanner 落库门禁原因（未过门禁写回、过门禁清空）。
    _migrate_add_column(conn, "requirements", "gate_reason", "TEXT NOT NULL DEFAULT ''")
    # req#117 task#198：需求拆解状态机（原子占用/凭据/幂等/完成提交/存量迁移）。
    decompose_added = _migrate_add_column(conn, "requirements", "decompose_state", "TEXT NOT NULL DEFAULT 'pending'")
    _migrate_add_column(conn, "requirements", "decompose_owner", "TEXT NOT NULL DEFAULT ''")
    _migrate_add_column(conn, "requirements", "decompose_credential", "TEXT NOT NULL DEFAULT ''")
    _migrate_add_column(conn, "requirements", "decompose_expires_at", "TEXT NOT NULL DEFAULT ''")
    _migrate_add_column(conn, "requirements", "decompose_stale_credentials", "TEXT NOT NULL DEFAULT '[]'")
    if decompose_added:
        # 存量迁移（一次性）：已有任务的需求标「已拆完」避免重新全量拆解，保留任务/角色授权/历史；
        # 无任务需求保持 pending（默认）仍进兜底。
        conn.execute("UPDATE requirements SET decompose_state='done' "
                     "WHERE id IN (SELECT DISTINCT requirement_id FROM tasks)")
    # 索引迁移（req#38 串话题修复 task#75）：旧 idx_wake_active(profile, kind, project)
    # 改为两个按 kind 分家的 partial unique index（chat 按 thread_id、work 按 project），
    # 消除「同 project 多话题 chat 唤醒互相去重」的串话题 bug。见 _migrate_wake_active_index。
    _migrate_wake_active_index(conn)
    # req#27（ENGINEERING 28.1）：全局 active → project_members(project, profile, active)。
    # 幂等：先补数据（老 members.active=0 → 每个非归档项目插 active=0），再重建 members 表删列。
    _migrate_project_members(conn)
    _migrate_add_column(conn, 'members', 'bot_id', 'INTEGER')
    _migrate_member_bot_ids(conn)
    # req#86 task#147：members → bots 反推（「反推优先」口径）先行，后续 pm 迁移与
    # explicit 补数据都依赖 bots 行已存在。
    # req#87 task#150：先归一 #147 旧基线（LOCALAPPDATA）写的 config_root 到
    # 当前根，再反推——否则 NOT EXISTS 按新根查会把既有行当缺口再插一遍。
    # Registered roots are identity, not the calling worker's HERMES_HOME.
    # Root changes and historical duplicate repair require an explicit migration.
    _backfill_hermes_bots(conn)
    _migrate_explicit_membership(conn)
    # req#86 task#147：pm 关联键 profile → bot_id（老库表重建），含 profile 兼容列回填。
    _migrate_pm_bot_id(conn)
    _migrate_member_bot_ids(conn)
    _migrate_permission_membership(conn)
    pm_cols = {r['name'] for r in conn.execute('PRAGMA table_info(project_members)')}
    if 'role' not in pm_cols:
        conn.execute("ALTER TABLE project_members ADD COLUMN role TEXT NOT NULL DEFAULT ''")
    if not had_project_roles:
        conn.execute("UPDATE project_members SET role=COALESCE("
                     "(SELECT NULLIF(m.role,'') FROM members m WHERE m.profile=project_members.profile),"
                     "(SELECT b.role FROM bots b WHERE b.id=project_members.bot_id),'')")
    # req#96 task#163：项目角色实体化（project_roles 表 + 历史角色值初始化 +
    # 成员 role_id 回填）。须在 role 列确保存在之后跑（迁移读 pm.role 建角色）。
    _migrate_project_roles(conn)
    conn.execute('DROP INDEX IF EXISTS idx_tasks_role_status')
    _migrate_add_column(conn, "projects", "display_name", "TEXT NOT NULL DEFAULT ''")
    _migrate_add_column(conn, "projects", "directory_mode", "TEXT NOT NULL DEFAULT ''")
    # req#83：老库已有 session_bindings 表（#143 建的）缺 switching 列，补上。
    # session_switch_ops 表用 CREATE TABLE IF NOT EXISTS（schema.sql 已含），无需迁移。
    _migrate_add_column(conn, "session_bindings", "switching", "INTEGER NOT NULL DEFAULT 0")
    # req#88 task#153：session_bindings 补 runtime 列（逐行 runtime 快照）。必须先于
    # _migrate_session_bindings_bot_id 补——老形态表重建迁移 SELECT COALESCE(sb.runtime,...)
    # 读该列，不先补会踩空。
    _migrate_add_column(conn, "session_bindings", "runtime", "TEXT NOT NULL DEFAULT 'hermes'")
    # req#86 task#148：wake_runs.hermes_session_id → runtime_session_id（列改名）。
    _migrate_rename_wake_runs_session_column(conn)
    # req#86 task#148：session_bindings 主键 profile → bot_id（老库表重建 + 回填）。
    _migrate_session_bindings_bot_id(conn)
    # req#86 task#149：members 补 created_at/source（8.2 可追溯）。
    _migrate_add_column(conn, "members", "created_at", "TEXT NOT NULL DEFAULT ''")
    _migrate_add_column(conn, "members", "source", "TEXT NOT NULL DEFAULT 'legacy'")
    # req#86 task#149：内置 user 身份（8.1，host 特殊标记 host_type='builtin'，无条件 upsert）。
    _upsert_builtin_user(conn)
    # req#86 task#149：wake_runs/message_executions 存量归 bot_id（2.5 第4点：
    # 反推优先、二义标待归属=NULL 保留、不静默归边）。老库先补列（schema.sql 的
    # IF NOT EXISTS 不给老表加列）。
    _migrate_add_column(conn, "wake_runs", "bot_id", "INTEGER")
    runtime_added = 'runtime' not in {r['name'] for r in conn.execute('PRAGMA table_info(wake_runs)')}
    _migrate_add_column(conn, 'wake_runs', 'runtime', "TEXT NOT NULL DEFAULT 'hermes'")
    _migrate_add_column(conn, "message_executions", "bot_id", "INTEGER")
    _backfill_run_bot_ids(conn)
    if runtime_added:
        conn.execute("UPDATE wake_runs SET runtime='codex' WHERE bot_id IN (SELECT id FROM bots WHERE runtime='codex')")
    conn.execute("UPDATE projects SET display_name=name WHERE display_name=''")
    conn.execute("UPDATE projects SET directory_mode=CASE WHEN COALESCE(worktree_root,'')='' "
                 "THEN 'shared' ELSE 'worktree' END WHERE directory_mode=''")
    conn.commit()


def _migrate_permission_membership(conn: sqlite3.Connection) -> None:
    """Revocation survives every membership write path and later re-enrollment."""
    conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS revoke_permissions_on_disable
        AFTER UPDATE OF active ON project_members WHEN NEW.active<>1 OR OLD.active<>1
        BEGIN
          DELETE FROM project_permissions WHERE project=NEW.project AND profile=NEW.profile;
        END;
        CREATE TRIGGER IF NOT EXISTS revoke_permissions_on_leave
        AFTER DELETE ON project_members
        BEGIN
          DELETE FROM project_permissions WHERE project=OLD.project AND profile=OLD.profile;
        END;
        CREATE TRIGGER IF NOT EXISTS revoke_permissions_on_join
        AFTER INSERT ON project_members
        BEGIN
          DELETE FROM project_permissions WHERE project=NEW.project AND profile=NEW.profile;
        END;
        CREATE TRIGGER IF NOT EXISTS revoke_permissions_on_member_delete
        AFTER DELETE ON members
        BEGIN
          DELETE FROM project_permissions WHERE profile=OLD.profile;
        END;
    """)
    conn.execute("INSERT OR IGNORE INTO engine_migrations VALUES('active_member_permissions')")
    if conn.execute('SELECT changes()').fetchone()[0]:
        conn.execute("DELETE FROM project_permissions WHERE NOT EXISTS ("
                     "SELECT 1 FROM project_members pm JOIN members m ON m.profile=pm.profile "
                     "WHERE pm.project=project_permissions.project AND pm.profile=project_permissions.profile "
                     f"AND pm.active=1 AND m.profile<>'{BUILTIN_USER_PROFILE}')")


def _migrate_explicit_membership(conn: sqlite3.Connection) -> None:
    """Preserve existing membership once; future profiles/projects require opt-in."""
    conn.execute('CREATE TABLE IF NOT EXISTS engine_migrations (name TEXT PRIMARY KEY)')
    conn.execute("INSERT OR IGNORE INTO engine_migrations VALUES('explicit_project_membership')")
    if conn.execute('SELECT changes()').fetchone()[0]:
        # req#86 task#147：按 pm 当前形态分叉——老形态（无 bot_id 列）走原 SQL，
        # 行随后由 _migrate_pm_bot_id 重建迁移；新形态带 bot_id（bots 行由
        # _backfill_hermes_bots 先行反推）。两形态都保留 profile 列。
        pm_cols = {r["name"] for r in conn.execute("PRAGMA table_info(project_members)")}
        if "bot_id" in pm_cols:
            # NOT EXISTS 守卫 = 老形态 PK (project,profile) 的去重语义：兼容期 NULL
            # bot_id 行不参与 PK 去重，须显式按 profile 挡重复（active=0 语义优先）。
            conn.execute(
                "INSERT OR IGNORE INTO project_members(project,bot_id,profile,active) "
                "SELECT p.name, b.id, m.profile, 1 FROM projects p CROSS JOIN members m "
                "JOIN bots b ON b.host_type='hermes' AND b.config_root=? AND b.profile=m.profile "
                "WHERE m.profile<>? AND NOT EXISTS ("
                "  SELECT 1 FROM project_members pm "
                "  WHERE pm.project=p.name AND pm.profile=m.profile)",
                (_hermes_root(), BUILTIN_USER_PROFILE),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO project_members(project,profile,active) "
                "SELECT p.name,m.profile,1 FROM projects p CROSS JOIN members m WHERE m.profile<>?",
                (BUILTIN_USER_PROFILE,),
            )


def _migrate_add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    """已有库补列（幂等：列已存在则跳过）。返回是否新增了列（供一次性存量迁移判断）。"""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        return True
    return False


# messages 表完整列定义（req#61 重建迁移用）：与 schema.sql 保持一致。
# need_reply 在此为可空（INTEGER DEFAULT 1），去掉 NOT NULL 以支持落 NULL。
_MESSAGES_REBUILD_SQL = """
CREATE TABLE messages_new (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL DEFAULT 'xjk',
    ts          TEXT NOT NULL,
    sender      TEXT NOT NULL,
    sender_type TEXT NOT NULL,
    content     TEXT NOT NULL,
    mentions    TEXT NOT NULL DEFAULT '[]',
    thread_id   INTEGER,
    parent_id   INTEGER,
    round       INTEGER NOT NULL DEFAULT 0,
    depth       INTEGER NOT NULL DEFAULT 0,
    retract_by  TEXT,
    retract_at  TEXT,
    need_reply  INTEGER DEFAULT 1
)
"""

# 老库 messages 列顺序（迁移时 SELECT 列名，不依赖顺序）。
_MESSAGES_COLUMNS = [
    "id", "project", "ts", "sender", "sender_type", "content", "mentions",
    "thread_id", "parent_id", "round", "depth", "retract_by", "retract_at",
    "need_reply",
]


def _migrate_messages_need_reply_nullable(conn: sqlite3.Connection) -> None:
    """req#61：messages.need_reply 去 NOT NULL（三态 NULL/0/1），表重建迁移。

    SQLite 不能 ALTER 去 NOT NULL，只能重建表。步骤（官方推荐，已在沙箱实测验证）：
    1. PRAGMA foreign_keys=OFF（须在事务外执行）；
    2. BEGIN → 建 messages_new（need_reply 可空）→ 按列名 SELECT 拷贝全量数据
       → DROP TABLE messages → ALTER TABLE messages_new RENAME TO messages → COMMIT；
    3. PRAGMA foreign_keys=ON（须在事务外执行）。

    重建后子表（inbox/attachments）对 messages 的外键按表名自动恢复（沙箱实测
    PRAGMA foreign_key_list 引用名不变、外键仍拦截无效引用），id 序列由
    sqlite_sequence 保留（新行 id 不与旧行冲突）。

    幂等：仅当 messages.need_reply 列仍非空（NOT NULL）时才重建；已可空则跳过。
    调用前置条件：本函数在 init_schema 内、_migrate_add_column 之后，且 conn 尚未
    处于事务中（init_schema 末尾才 conn.commit），满足 foreign_keys=OFF 需在
    事务外执行的要求。
    """
    cols = conn.execute("PRAGMA table_info(messages)").fetchall()
    col_map = {c["name"]: c for c in cols}
    nr = col_map.get("need_reply")
    if nr is None:
        return  # 列都不存在（全新库走 schema.sql 直接可空），无需重建
    if nr["notnull"] == 0:
        return  # 已可空（重建过或全新库），幂等跳过

    # 列名核对：老库若缺某些列（极老的库），补齐到当前完整形态再拷贝。
    # 拷贝用「按列名 SELECT」，避免依赖顺序；缺失列用默认值。
    for name in _MESSAGES_COLUMNS:
        if name not in col_map:
            # 老库缺失的列，ALTER 补上（幂等）
            if name == "id":
                continue  # id 必有
            ddl = {
                "project": "TEXT NOT NULL DEFAULT 'xjk'",
                "ts": "TEXT NOT NULL",
                "sender": "TEXT NOT NULL",
                "sender_type": "TEXT NOT NULL",
                "content": "TEXT NOT NULL",
                "mentions": "TEXT NOT NULL DEFAULT '[]'",
                "thread_id": "INTEGER",
                "parent_id": "INTEGER",
                "round": "INTEGER NOT NULL DEFAULT 0",
                "depth": "INTEGER NOT NULL DEFAULT 0",
                "retract_by": "TEXT",
                "retract_at": "TEXT",
                "need_reply": "INTEGER NOT NULL DEFAULT 1",
            }[name]
            conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {ddl}")

    # 保留索引/触发器及历史自增高水位；失败时回滚后再恢复外键。
    objects = conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='messages' "
        "AND type IN ('index','trigger') AND sql IS NOT NULL"
    ).fetchall()
    sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='messages'").fetchone()
    if conn.in_transaction:
        raise RuntimeError("messages migration requires a connection outside a transaction")
    # 重建（foreign_keys=OFF 在事务外）
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        # 单条 CREATE TABLE 用 execute（executescript 会隐式 commit 破坏事务原子性）
        conn.execute(_MESSAGES_REBUILD_SQL)
        col_list = ", ".join(_MESSAGES_COLUMNS)
        conn.execute(
            f"INSERT INTO messages_new ({col_list}) SELECT {col_list} FROM messages"
        )
        conn.execute("DROP TABLE messages")
        conn.execute("ALTER TABLE messages_new RENAME TO messages")
        for obj in objects:
            conn.execute(obj["sql"])
        if sequence is not None:
            conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq, ?) WHERE name='messages'",
                         (sequence["seq"],))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")



def _migrate_project_members(conn: sqlite3.Connection) -> None:
    """req#27（ENGINEERING 28.1）：全局 active → project_members 按项目维度。

    幂等两步：
    1. 补数据——老库 members.active=0 的 profile，为每个非归档项目插
       project_members(project, profile, active=0)；active=1 的不插（默认启用）。
       project_members 表已在 schema.sql 的 CREATE TABLE IF NOT EXISTS 里建好。
    2. 重建 members 表去掉 active 列（SQLite 删列用重建方式，比 DROP COLUMN 稳）。

    判定「迁移是否已做」看 members 表里是否还有 active 列——重建后即无此列，
    再次进入时直接跳过（幂等）。老库若 active 列也不存在（全新库），同样跳过补数据。

    🔴 生效门槛（防断档）：members.active 的消费点（11 处，ENGINEERING 28.2）必须
    全部切到 project_members 之后，删列才能发生。在消费点切换完成前（代码里仍有
    `FROM members WHERE active` 的引用），本函数只做第 1 步补数据，**跳过删列**——
    否则删列后消费点 SQL 直接报 no such column，引擎瘫痪。切完消费点后删掉本
    函数里的 `_CONSUMERS_MIGRATED = False` 标志（置 True）即可放行删列。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(members)")}
    if "active" not in cols:
        return  # 已迁移过（或全新库），无事可做

    # 1) 把老的全局停用扩散到每个非归档项目（幂等：INSERT OR IGNORE）
    #    这一步无条件执行——project_members 表先有数据，消费点切换时立即可用。
    conn.execute(
        """
        INSERT OR IGNORE INTO project_members (project, profile, active)
        SELECT p.name, m.profile, 0
        FROM members m
        CROSS JOIN projects p
        WHERE m.active = 0 AND p.archived = 0
        """
    )
    conn.commit()

    # 2) 重建 members 表去掉 active 列——🔴仅当消费点全部切完（见 docstring）
    # task#57（a4bdfe2）已把 28.2 的 11 处消费点全切到 project_members，开闸。
    if not _CONSUMERS_MIGRATED:
        return
    conn.execute(
        "CREATE TABLE IF NOT EXISTS members_new ("
        "profile TEXT PRIMARY KEY, "
        "display_name TEXT NOT NULL, "
        "role TEXT NOT NULL DEFAULT '')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO members_new (profile, display_name, role) "
        "SELECT profile, display_name, role FROM members"
    )
    conn.execute("DROP TABLE members")
    conn.execute("ALTER TABLE members_new RENAME TO members")
    conn.commit()


# 🔴 task#57（a4bdfe2）已把 28.2 的 11 处消费点全切到 project_members，开闸置 True：
# init_schema 现在会真正删除 members.active 列。改回 False 等于回退迁移。
_CONSUMERS_MIGRATED = True


# project_members 重建迁移后的完整列定义（req#86 task#147）：与 schema.sql 保持一致。
_PM_REBUILD_SQL = """
CREATE TABLE project_members_new (
    project   TEXT NOT NULL,
    profile   TEXT NOT NULL,
    active    INTEGER NOT NULL DEFAULT 1,
    bot_id    INTEGER,
    role      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project, bot_id)
)
"""


def _backfill_hermes_bots(conn: sqlite3.Connection) -> None:
    """req#86 task#147：members → bots 反推（「反推优先」，设计稿 2.5 第 4 点）。

    为 members 表每个 profile 反推一个 hermes Bot（config_root 取 Hermes 根目录，
    display_name/role 沿用花名册行）。存量库的 Bot 此前全是 hermes 宿主，同名
    profile 双宿主二义不存在，反推不标待归属；后续宿主扫描发现 codex 时按
    UNIQUE(host_type, config_root, profile) 各自成行。

    一次性反推 + 数据状态幂等：不用 engine_migrations 标记（标记可能先于 members
    数据写入——库在 members 插入前跑过 init_schema 的路径会永久错过反推）。
    用 NOT EXISTS 过滤：members 里有而 bots 没有的 profile 才补——无缺口时
    SELECT 为空，不烧 AUTOINCREMENT 号（空 INSERT..SELECT 不推进 sequence），
    taskpool._conn 高频 init_schema 无持续成本。

    🔴 兼容异构 bots 表：生产库可能已被另一版实现建过 bots 表（多 created_at
    NOT NULL 列、idx_bots_profile 索引，2026-09-29 实测真库如此）。具名列 INSERT
    缺 created_at 会被 OR IGNORE 静默吞掉（rowcount=0 不报错）——按 bots 实际
    列集分叉写入，有 created_at 列的一并落 db.now_iso()。
    """
    bot_cols = {r["name"] for r in conn.execute("PRAGMA table_info(bots)")}
    if "created_at" in bot_cols:
        conn.execute(
            "INSERT OR IGNORE INTO bots (host_type, profile, config_root, runtime, display_name, role, created_at) "
            "SELECT 'hermes', m.profile, ?, 'hermes', m.display_name, m.role, ? FROM members m "
            # req#86 task#149：内置 user 不反推（8.1：builtin 标记由 _upsert_builtin_user
            # 维护，这里反推会与 builtin 行并存成双行）。
            "WHERE m.bot_id IS NULL AND m.profile<>? AND NOT EXISTS ("
            "  SELECT 1 FROM bots b WHERE b.host_type='hermes' AND b.config_root=? AND b.profile=m.profile)",
            (_hermes_root(), now_iso(), BUILTIN_USER_PROFILE, _hermes_root()),
        )
    else:
        conn.execute(
            "INSERT OR IGNORE INTO bots (host_type, profile, config_root, runtime, display_name, role) "
            "SELECT 'hermes', m.profile, ?, 'hermes', m.display_name, m.role FROM members m "
            "WHERE m.bot_id IS NULL AND m.profile<>? AND NOT EXISTS ("
            "  SELECT 1 FROM bots b WHERE b.host_type='hermes' AND b.config_root=? AND b.profile=m.profile)",
            (_hermes_root(), BUILTIN_USER_PROFILE, _hermes_root()),
        )
    # 独立提交：随后的 _migrate_pm_bot_id 表重建要求连接不处于事务中
    # （老库 bots 必有缺口、此 INSERT 必写库，隐式事务必须在此收口）。
    conn.commit()


def _migrate_member_bot_ids(conn: sqlite3.Connection) -> None:
    # Prefer explicit project associations over legacy Hermes inference. Repeated
    # initialization must never manufacture a Hermes Bot for a Codex handle.
    pm_cols = {r['name'] for r in conn.execute('PRAGMA table_info(project_members)')}
    if 'bot_id' in pm_cols:
        conn.execute('UPDATE members SET bot_id=(SELECT MIN(pm.bot_id) FROM project_members pm '
                     'WHERE pm.profile=members.profile) WHERE bot_id IS NULL AND '
                     '(SELECT COUNT(DISTINCT pm.bot_id) FROM project_members pm '
                     'WHERE pm.profile=members.profile)=1')
    conn.execute("UPDATE members SET bot_id=(SELECT b.id FROM bots b WHERE b.host_type='hermes' "
                 "AND b.config_root=? AND b.profile=members.profile) WHERE bot_id IS NULL",
                 (_hermes_root(),))
    conn.commit()


# 历史角色值（req#96 task#163）：存量库里 project_members.role 出现的 6 个非空值。
# 迁移把它们初始化成各项目的角色记录（按 project 一套）。新角色由 CRUD API 增删。
_HISTORICAL_ROLES = ("architect", "reviewer", "be-dev", "fe-dev", "tester", "orchestrator")


def _migrate_project_roles(conn: sqlite3.Connection) -> None:
    """req#96 task#163：角色实体化迁移（幂等）。

    1) project_members 补 role_id 列（老库 ALTER ADD）。
    2) 按「每个项目 × 该项目已出现的非空 role 文本」初始化 project_roles 记录
       （UNIQUE(project,name)；created_at 落 now_iso）。历史 6 个角色值成为
       角色记录（验收：改名后成员关联不断）。
       🔴 种子段由 engine_migrations('project_roles_seeded') 一次性门栏保护，只跑
       一次——否则删除角色后（role 文本按设计保留、role_id 置 NULL），任意一次
       init_schema 会按 pm.role<>'' 把已删角色同名补建（新 id）并回填 role_id，
       这会恢复已删除角色，并使后续同名 POST 返回 409。
    3) 成员按 role 文本回填 role_id，与种子一起只运行一次。

    后续角色关联由成员角色接口显式写入；删除后保留的历史 role 文本不参与
    自动分配，同名重建的角色须重新分配成员才能授予权限。
    """
    pm_cols = {r["name"] for r in conn.execute("PRAGMA table_info(project_members)")}
    if "role_id" not in pm_cols:
        conn.execute("ALTER TABLE project_members ADD COLUMN role_id INTEGER")
    # 一次性门槛（现行模式，见 _migrate_explicit_membership / _migrate_permission_membership）。
    conn.execute("CREATE TABLE IF NOT EXISTS engine_migrations (name TEXT PRIMARY KEY)")
    conn.execute("INSERT OR IGNORE INTO engine_migrations VALUES('project_roles_seeded')")
    if conn.execute('SELECT changes()').fetchone()[0]:
        conn.execute(
            "INSERT OR IGNORE INTO project_roles(project, name, created_at) "
            "SELECT pm.project, pm.role, ? FROM project_members pm "
            "WHERE pm.role<>'' GROUP BY pm.project, pm.role",
            (now_iso(),),
        )
        conn.execute(
            "UPDATE project_members SET role_id=(SELECT pr.id FROM project_roles pr "
            "WHERE pr.project=project_members.project AND pr.name=project_members.role) "
            "WHERE role_id IS NULL AND role<>'' "
            "AND EXISTS (SELECT 1 FROM project_roles pr2 WHERE pr2.project=project_members.project "
            "AND pr2.name=project_members.role)"
        )
    conn.commit()


def _migrate_pm_bot_id(conn: sqlite3.Connection) -> None:
    """req#86 task#147：project_members 关联键 profile → bot_id，表重建迁移。

    schema.sql 已把 project_members 主键改为 (project, bot_id)，bot_id 列新增（可空，
    兼容期语义见 schema.sql 注释）。老库（主键 (project, profile)、无 bot_id 列）需重建：

    1. bots 行由 _backfill_hermes_bots 先行反推，此处不重复。
    2. 重建 project_members：主键 (project, bot_id)，列序 (project, profile, active,
       bot_id) 保持既有前三列不动。bot_id 按反推的 hermes Bot 回填；JOIN 不上的
       孤儿行（members 里已删但 pm 残留）保留且 bot_id 置 NULL——不静默丢行，
       也不静默归边（设计稿 2.5 第 4 点口径；PRIMARY KEY 对 NULL 的去重不生效，
       逐行可存活）。profile 兼容列回填原值，运行时 11 处消费点（router/queue/
       wake/mentions/project_permissions 等）仍按 profile JOIN，靠该列不瘫痪
       （模式同 req#27 的 _CONSUMERS_MIGRATED）。
    3. 保留 sqlite_sequence 高水位（新行 id 不与旧行冲突）。

    幂等：仅当 project_members 主键仍是老形态 (project, profile)（含「schema.sql 刚建
    出新形态表但库里无既有行」之外的任意老库路径）时才重建；主键已是 (project, bot_id)
    直接跳过。老库判定用主键列集，不用 bot_id 列是否存在——init_schema 先 executescript
    schema.sql，老库的 pm 表因 IF NOT EXISTS 保留旧定义，但「老库无 pm 表」的路径会让
    schema.sql 直接建出新形态表，此时既有行仅可能来自 _migrate_project_members/
    _migrate_explicit_membership 的补数据（本函数前一步按当时形态写入），行数有限且
    profile 列完整，无需重建——主键列集判定天然覆盖这三种路径。
    新形态表（schema.sql 建出或已迁移）每次 init_schema 都执行 NULL bot_id 回填
    （幂等 UPDATE，无 NULL 行时无操作）：兼容期裸 SQL 写入（测试夹具/旧路径）可能
    落 NULL bot_id 行，且「老库无 pm 表」路径的 _migrate_project_members/
    _migrate_explicit_membership 补数据行先于 bots 反推落库。回填后再判主键形态。
    调用前置条件：本函数在 init_schema 内、conn 未处于事务中（PRAGMA foreign_keys
    须在事务外执行；init_schema 末尾才 conn.commit）。
    """
    pm_cols = {r["name"] for r in conn.execute("PRAGMA table_info(project_members)")}
    if "bot_id" not in pm_cols:
        # 老形态表（主键 (project,profile)、无 bot_id 列）→ 走下方重建迁移。
        # 前序步骤的写语句（如 INSERT OR IGNORE 即使冲突忽略）可能留下隐式事务，
        # 表重建要求连接事务外——先收口。
        conn.commit()
    else:
        # 新形态表：回填兼容期 NULL bot_id 行（bots 行由 _backfill_hermes_bots 先行保证）。
        # 外部工具直写库可能造成同
        # (project,profile) 双 NULL 行（PK 对 NULL 不去重），朴素回填会把两行都改成
        # 同一 bot_id 撞 UNIQUE → init_schema 抛异常引擎起不来。回填前按 profile
        # 判重：该 (project,profile) 组内已存在非 NULL bot_id 行、或组内多行无法
        # 定归属时**跳过回填**（行保留 NULL 不动，不静默丢、不静默归边，可人工
        # 清理），只回填组内唯一 NULL 行。
        conn.execute(
            "UPDATE project_members SET bot_id=("
            "  SELECT b.id FROM bots b WHERE b.host_type='hermes' AND b.config_root=? "
            "  AND b.profile=project_members.profile) "
            "WHERE bot_id IS NULL AND profile IN (SELECT profile FROM members) "
            "AND (SELECT COUNT(*) FROM project_members p2 "
            "     WHERE p2.project=project_members.project AND p2.profile=project_members.profile) = 1",
            (_hermes_root(),),
        )
        conn.commit()

    pk_cols = [r["name"] for r in conn.execute("PRAGMA table_info(project_members)") if r["pk"]]
    if pk_cols == ["project", "bot_id"]:
        return  # 已迁移过（或全新库），无事可做

    # 重建 project_members（外键关闭，同 messages 重建迁移的官方步骤）
    objects = conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='project_members' "
        "AND type IN ('index','trigger') AND sql IS NOT NULL"
    ).fetchall()
    sequence = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='project_members'"
    ).fetchone()
    if conn.in_transaction:
        raise RuntimeError("project_members migration requires a connection outside a transaction")
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(_PM_REBUILD_SQL)
        role_expr = 'pm.role' if 'role' in pm_cols else "''"
        conn.execute(
            f"""
            INSERT INTO project_members_new (project, profile, active, bot_id, role)
            SELECT pm.project, pm.profile, pm.active, b.id, {role_expr}
            FROM project_members pm
            LEFT JOIN bots b ON b.host_type='hermes' AND b.config_root=? AND b.profile=pm.profile
            """,
            (_hermes_root(),),
        )
        conn.execute("DROP TABLE project_members")
        conn.execute("ALTER TABLE project_members_new RENAME TO project_members")
        for obj in objects:
            conn.execute(obj["sql"])
        if sequence is not None:
            conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq, ?) WHERE name='project_members'",
                         (sequence["seq"],))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _hermes_root() -> str:
    """Hermes 根目录（req#87 task#150 起与 profile_config._root 同口径：HERMES_HOME 优先）。

    存量反推时 bots.config_root 统一记 Hermes 根目录——存量库的 Bot 全部是
    hermes 宿主，按「反推优先」口径归属，不做目录探测。

    🔴 口径修正（task#150 实测暴露）：#147 版只认 LOCALAPPDATA/hermes，但生产
    引擎由 start.sh export HERMES_HOME=<根>（E:/home/admin/hermes），两边根不
    一致 → 宿主发现/注册/复用按 config_root 精确匹配全 miss。统一为
    HERMES_HOME 优先、缺省回退 native。既有 LOCALAPPDATA 基线的 bots 行由
    _migrate_bot_config_roots 归一到当前根（保 id，pm/sb 引用不动）。
    """
    if sys.platform == "win32":
        native = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "hermes"
    else:
        native = Path.home() / ".hermes"
    configured = (os.environ.get("HERMES_HOME") or "").strip()
    root = Path(configured).resolve() if configured else native.resolve()
    return str(root)


def _migrate_bot_config_roots(conn: sqlite3.Connection) -> None:
    """req#87 task#150：既有 hermes Bot 的 config_root 归一到当前根（口径修正）。

    #147 反推基线只认 LOCALAPPDATA/hermes，引擎环境真根是 HERMES_HOME——
    库里 config_root 指向错误根的 hermes Bot 统一 UPDATE 到当前根（保 id，
    pm/sb/wr/me 的 bot_id 引用零漂移）。幂等：仅迁移「hermes 宿主且根不是
    当前根、且当前根下无同 profile 行」的行；当前根已有同 profile 行时保留
    旧行不动（不产生 UNIQUE 冲突，行待人工归并——不静默归边口径）。
    """
    current = _hermes_root()
    rows = conn.execute(
        "SELECT id, profile FROM bots WHERE host_type='hermes' AND config_root<>?",
        (current,),
    ).fetchall()
    for r in rows:
        clash = conn.execute(
            "SELECT 1 FROM bots WHERE host_type='hermes' AND config_root=? AND profile=?",
            (current, r["profile"]),
        ).fetchone()
        if clash is None:
            conn.execute("UPDATE bots SET config_root=? WHERE id=?", (current, r["id"]))
    conn.commit()


def _bot_id_of(conn: sqlite3.Connection, profile: str) -> int | None:
    """按 profile 反查反推基线（hermes 根）下的 Bot id，无行返回 None。

    兼容期统一出口：pm/session_bindings 的回填与写点定位 bot_id 都走这里，
    不散落 SQL。
    """
    row = conn.execute('SELECT bot_id FROM members WHERE profile=? AND bot_id IS NOT NULL',
                       (profile,)).fetchone()
    if row:
        return row['bot_id']
    row = conn.execute(
        "SELECT id FROM bots WHERE host_type='hermes' AND config_root=? AND profile=?",
        (_hermes_root(), profile),
    ).fetchone()
    return row["id"] if row is not None else None


# req#86 设计稿 8.1：内置 user 的 profile 名——魔法串唯一落点，代码不再散落 'user' 判等。
BUILTIN_USER_PROFILE = "user"


def is_builtin_user(profile: str | None) -> bool:
    """是否内置 user 身份（req#86 8.1：25 处 profile=='user' 硬编码的统一收敛出口）。

    判定口径 = host 特殊标记（bots.host_type='builtin'），不是字符串判等——
    但兼容期外部传入的只有 profile 字符串（sender/mention 解析产物），直接
    判名等价于查库标记（内置 user 的 profile 名恒为 BUILTIN_USER_PROFILE，
    由 _upsert_builtin_user 锁死）。不查库：25 处消费点多为热路径，逐次
    PRAGMA/SELECT 反而引入查询成本与库形态耦合。
    """
    return profile == BUILTIN_USER_PROFILE


def _upsert_builtin_user(conn: sqlite3.Connection) -> None:
    """无条件 upsert 内置 user 身份。

    - host 特殊标记 host_type='builtin'：不是扫描到的资源（不能等「扫描
      profile」步骤才建——冷启动六步任何一步引用它都不能踩空），也不是
      hermes/codex 宿主 Bot（不参与 pm/会话绑定等 Bot 层语义）。
    - runtime='builtin'：与 host_type 同口径，读取方判 Bot 类型时天然分流。
    - profile 名锁死 'user'：负责人回退/消息 sender/权限判定统一引用此身份；
      members 花名册不再落 user 行（8.1：非扫描资源，不进 members）。
    - 存量库归一：#147 的 _backfill_hermes_bots 曾把 members.user 行按 hermes
      反推成 hermes Bot（历史遗留）——先 UPDATE 转 builtin 标记（不换行、
      保 id 稳定，pm/sb 若曾引用其 bot_id 不漂移），再 INSERT OR IGNORE 兜底
      空库/被删场景。不并存双行（UNIQUE 只拦同 host_type，host 不同会漏拦）。

    🔴 兼容异构 bots 表：真库可能带 created_at NOT NULL（#147 实测），按
    实际列集分叉写入，缺列时 OR IGNORE 会静默吞行。

    🔴 双行归一（修复性，2026-09-29 实测真库出现 id10 builtin + id15 hermes
    双 user 行——持有旧代码的 MCP 桥接进程跑 init_schema 时旧 _backfill 无
    user 排除、又反推了一行 hermes-user）：UPDATE 转 builtin 也会撞 UNIQUE
    （两行同 config_root+profile 转成同 host_type 值）。处理：按「id 最小者
    为 canonical」保 id 最小行（转 builtin），多余同 profile 行是坏数据
    （UNIQUE 本应拦住的错误重复）删除——属迁移修复而非静默丢数据；id 最小
    保 pm/sb 若曾引用其 bot_id 不漂移。
    """
    dup_rows = conn.execute(
        "SELECT id FROM bots WHERE profile=? ORDER BY id", (BUILTIN_USER_PROFILE,)
    ).fetchall()
    if len(dup_rows) > 1:
        keep_id = dup_rows[0]["id"]
        conn.execute(
            "DELETE FROM bots WHERE profile=? AND id<>?", (BUILTIN_USER_PROFILE, keep_id)
        )
    # 1) 存量归一：既有 user 行（任意 host_type）统一转 builtin 标记。
    conn.execute(
        "UPDATE bots SET host_type='builtin', runtime='builtin' WHERE profile=? AND host_type<>'builtin'",
        (BUILTIN_USER_PROFILE,),
    )
    # 2) 空库/被删兜底：无行时按 builtin 建（含异构 created_at 列）。
    #    先 SELECT 判存在再 INSERT（不用裸 INSERT OR IGNORE——冲突忽略也推进
    #    sqlite_sequence，taskpool._conn 每次 init_schema 会持续烧号，iterdump
    #    类断言会漂移；与 _backfill_hermes_bots 的 NOT EXISTS 幂等同口径）。
    bot_cols = {r["name"] for r in conn.execute("PRAGMA table_info(bots)")}
    created = ", created_at" if "created_at" in bot_cols else ""
    legacy_user = conn.execute(
        "SELECT display_name FROM members WHERE profile=?", (BUILTIN_USER_PROFILE,)
    ).fetchone()
    initial_name = (legacy_user["display_name"] if legacy_user else "") or "用户"
    conn.execute(
        "INSERT INTO bots (host_type, profile, config_root, runtime, display_name, role"
        f"{created}) "
        "SELECT 'builtin', ?, ?, 'builtin', ?, ?"
        f"{', ?' if created else ''} "
        "WHERE NOT EXISTS (SELECT 1 FROM bots WHERE profile=?)",
        (BUILTIN_USER_PROFILE, _hermes_root(), initial_name, "",
         now_iso(), BUILTIN_USER_PROFILE) if created
        else (BUILTIN_USER_PROFILE, _hermes_root(), initial_name, "", BUILTIN_USER_PROFILE),
    )
    # members 花名册不落 user 行（8.1）；若存量库 members 里有 user 行（历史遗留），
    # 不删（不静默丢数据），仅靠 is_builtin_user 消费点自然跳过它。
    conn.commit()


def _backfill_run_bot_ids(conn: sqlite3.Connection) -> None:
    """wake_runs/message_executions 存量归 bot_id（req#86 2.5 第4点 + task#149）。

    口径「反推优先、二义标待归属、不静默归边」：
    - 按 profile 反推唯一 hermes Bot → 归属；反推不上（profile 空 / members
      已删 / 二义）→ 保留 NULL（待归属），不静默归边。
    - 数据状态幂等（NOT bot_id IS NULL 过滤），每次 init_schema 执行，新产生的
      待归属行下次自动补——与 pm/sb 回填同口径。
    - 判重防二义：同 profile 反推出多 Bot（理论上 UNIQUE 拦截、不可能）不处理，
      子查询取 MIN(id) 兜底不炸。
    """
    for table, profile_col in (("wake_runs", "profile"), ("message_executions", None)):
        if profile_col is None:
            # message_executions 无 profile 列——经 wake_runs JOIN 反推：
            # 同 (session,wake_started_at) 归属唯一 wake_runs 行的 bot_id；
            # wake_runs 行为 NULL（待归属）则执行记录同样保留 NULL。
            conn.execute(
                "UPDATE message_executions SET bot_id=("
                "  SELECT wr.bot_id FROM wake_runs wr "
                "  WHERE wr.session=message_executions.session "
                "  AND wr.started_at=message_executions.wake_started_at) "
                "WHERE bot_id IS NULL AND EXISTS ("
                "  SELECT 1 FROM wake_runs wr WHERE wr.session=message_executions.session "
                "  AND wr.started_at=message_executions.wake_started_at AND wr.bot_id IS NOT NULL)"
            )
        else:
            conn.execute(
                f"UPDATE {table} SET bot_id=("
                "  SELECT MIN(b.id) FROM bots b WHERE b.host_type='hermes' "
                f"  AND b.config_root=? AND b.profile={table}.{profile_col}) "
                "WHERE bot_id IS NULL AND profile IS NOT NULL AND profile<>''",
                (_hermes_root(),),
            )
    conn.commit()


def _ensure_hermes_bot(conn: sqlite3.Connection, profile: str,
                        display_name: str = "", role: str = "") -> int:
    """反推/复用该 profile 的 hermes Bot，返回 bot_id（req#86 兼容期写点兜底）。

    api._ensure_bot 与 session_adapter 共用的核心逻辑（HTTP 语义留在调用方）：
    ①已有行直接复用（不覆盖 display_name/role——Bot 身份全局统一，花名册
    行更新不冲击 Bot 层）；②无行按 hermes 宿主反推建 Bot；③并发撞 UNIQUE
    重查（另一连接先建了该 Bot）。

    🔴 兼容异构 bots 表（真库可能带 created_at NOT NULL 列，同 _backfill_
    hermes_bots 的分叉口径），按 bots 实际列集写入。
    """
    bot_id = _bot_id_of(conn, profile)
    if bot_id is not None:
        return bot_id
    bot_cols = {r["name"] for r in conn.execute("PRAGMA table_info(bots)")}
    created = ", created_at" if "created_at" in bot_cols else ""
    cur = conn.execute(
        "INSERT OR IGNORE INTO bots (host_type, profile, config_root, runtime, display_name, role"
        f"{created}) VALUES ('hermes', ?, ?, 'hermes', ?, ?{', ?' if created else ''})",
        (profile, _hermes_root(), display_name, role, now_iso()) if created
        else (profile, _hermes_root(), display_name, role),
    )
    if cur.rowcount == 1:
        return cur.lastrowid
    # 并发落库撞 UNIQUE → 重查（另一连接先建了该 Bot）
    bot_id = _bot_id_of(conn, profile)
    if bot_id is None:
        raise RuntimeError("Bot 身份落库失败")  # 调用方包装为各自的错误语义
    return bot_id


def _migrate_rename_wake_runs_session_column(conn: sqlite3.Connection) -> None:
    """req#86 task#148：wake_runs.hermes_session_id 改名 runtime_session_id。

    会话绑定按宿主区分：hermes 行存 Hermes session_id、
    codex 行存 codex thread_id，读取方按 Bot runtime 判读。列改名用 SQLite
    ALTER TABLE RENAME COLUMN（3.25+，引擎 Python 3.10/3.11 的 sqlite 均满足），
    幂等：目标列已存在（或源列不存在）时跳过。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(wake_runs)")}
    if "runtime_session_id" in cols:
        if "hermes_session_id" in cols:
            # 列改名迁移后清理残留的空列。
            # 旧值已由 RENAME 前的改名迁移搬到新列，残留列恒 NULL，删除零数据损失。
            conn.execute("ALTER TABLE wake_runs DROP COLUMN hermes_session_id")
        return  # 已改名（或全新库 schema.sql 直接建的），无事可做
    if "hermes_session_id" not in cols:
        return  # 极老库无此列（init_schema 的补列兜底先跑，不应出现）
    conn.execute("ALTER TABLE wake_runs RENAME COLUMN hermes_session_id TO runtime_session_id")


# session_bindings 重建迁移后的完整列定义（req#86 task#148）：与 schema.sql 保持一致。
_SB_REBUILD_SQL = """
CREATE TABLE session_bindings_new (
    project      TEXT NOT NULL,
    bot_id       INTEGER,
    profile      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    thread_id    INTEGER NOT NULL DEFAULT 0,
    session_id   TEXT NOT NULL,
    runtime      TEXT NOT NULL DEFAULT 'hermes',
    generation   INTEGER NOT NULL DEFAULT 0,
    switching    INTEGER NOT NULL DEFAULT 0,
    title        TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (project, bot_id, kind, thread_id)
)
"""


def _migrate_session_bindings_bot_id(conn: sqlite3.Connection) -> None:
    """req#86 task#148：session_bindings 主键 profile → bot_id，表重建迁移。

    schema.sql 已把主键改为 (project, bot_id, kind, thread_id)，profile 降为兼容列
    （wake/stop/steer_ledger 的按 profile 读取仍承接）。老库（主键
    (project,profile,kind,thread_id)、无 bot_id 列）重建：

    1. bot_id 按「反推优先」回填：profile → hermes 根下 Bot 反查（行由
       _backfill_hermes_bots 先行保证）。反查不上的孤儿行 bot_id 置 NULL、
       保留不丢（「不静默归边」口径；老库主键含 profile，孤儿行不可能与
       有效行同键，无去重冲突）。
    2. 列序 (project, profile, kind, ...) 保持兼容列在前。
    3. 保留 sqlite_sequence 高水位。

    兼容期 NULL bot_id 回填（每次 init_schema 执行，幂等）：表为新形态时按
    profile 反查回填 NULL 行——与 pm 同口径。老形态表跳过回填直接走重建。

    幂等：仅当主键仍是 (project, profile, ...) 时重建；已 (project, bot_id, ...)
    跳过。调用前置条件同 _migrate_pm_bot_id（conn 未处于事务中）。
    """
    sb_cols = {r["name"] for r in conn.execute("PRAGMA table_info(session_bindings)")}
    if "bot_id" not in sb_cols:
        # 老形态表（无 bot_id 列）→ 走下方重建迁移；先收口前序隐式事务
        conn.commit()
    else:
        # 新形态表：回填兼容期 NULL bot_id 行（幂等 UPDATE）。
        # 回填前需组内判重：外部直写库可能
        # 造成同绑定键 (project,profile,kind,thread_id) 双 NULL 行（PK 对 NULL 不
        # 去重），朴素回填会把两行改成同一 bot_id 撞 PK。同键多行时跳过回填
        # （行保留 NULL，不静默丢/归边，可人工清理），只回填组内唯一行。
        # 组键 = 绑定键四元组（不含 bot_id——profile 是兼容列，kind/thread_id
        # 参与主键，跨 kind/thread 的行不构成回填冲突）。
        conn.execute(
            "UPDATE session_bindings SET bot_id=("
            "  SELECT b.id FROM bots b WHERE b.host_type='hermes' AND b.config_root=? "
            "  AND b.profile=session_bindings.profile) "
            "WHERE bot_id IS NULL AND profile IN (SELECT profile FROM members) "
            "AND (SELECT COUNT(*) FROM session_bindings s2 "
            "     WHERE s2.project=session_bindings.project AND s2.profile=session_bindings.profile "
            "     AND s2.kind=session_bindings.kind AND s2.thread_id=session_bindings.thread_id) = 1",
            (_hermes_root(),),
        )
        conn.commit()

    pk_cols = [r["name"] for r in conn.execute("PRAGMA table_info(session_bindings)") if r["pk"]]
    if pk_cols == ["project", "bot_id", "kind", "thread_id"]:
        return  # 已迁移过（或全新库），无事可做

    objects = conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='session_bindings' "
        "AND type IN ('index','trigger') AND sql IS NOT NULL"
    ).fetchall()
    if conn.in_transaction:
        raise RuntimeError("session_bindings migration requires a connection outside a transaction")
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        conn.execute(_SB_REBUILD_SQL)
        conn.execute(
            """
            INSERT INTO session_bindings_new
                (project, bot_id, profile, kind, thread_id, session_id,
                 runtime, generation, switching, title, updated_at)
            SELECT sb.project, b.id, sb.profile, sb.kind, sb.thread_id, sb.session_id,
                   COALESCE(sb.runtime, 'hermes'), sb.generation, COALESCE(sb.switching, 0), sb.title, sb.updated_at
            FROM session_bindings sb
            LEFT JOIN bots b ON b.host_type='hermes' AND b.config_root=? AND b.profile=sb.profile
            """,
            (_hermes_root(),),
        )
        conn.execute("DROP TABLE session_bindings")
        conn.execute("ALTER TABLE session_bindings_new RENAME TO session_bindings")
        for obj in objects:
            conn.execute(obj["sql"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate_wake_active_index(conn: sqlite3.Connection) -> None:
    """幂等重建 wake 去重索引（req#38 串话题修复 task#75）。

    旧定义：idx_wake_active = (profile, kind, project) WHERE status IN ('queued','running')
    —— chat 与 work 共用一个索引，导致「同 profile 同 project 同 kind=chat」的
    不同话题（thread_id 不同）互相去重：同项目多话题并行唤醒同一 bot 被挡。

    新定义（两个 partial unique index，按 kind 分家）：
    - idx_wake_chat_active(profile, thread_id) WHERE kind='chat' AND status IN (...)
      同 bot 同话题只去重一次；不同话题（thread_id 不同）可并行并存。
    - idx_wake_work_active(profile, project) WHERE kind='work' AND status IN (...)
      work 保持 (profile, project) 粒度不变（点对点/任务池按项目分，不加 thread_id）。

    round 不进粒度（架构师定稿）：顺序唤醒模型下同 thread 同一时刻只有唯一
    turn（threads.turn_profile 单值），不存在「同 thread 不同 round 并发并存」。
    """
    # 1) 老索引还在就 DROP（不管定义新旧，统一重建为两索引形态）
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_wake_active'"
    ).fetchone()
    if row is not None:
        conn.execute("DROP INDEX idx_wake_active")
    # 2) 建两个新 partial unique index（幂等：IF NOT EXISTS）
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_wake_chat_active "
        "ON wake_jobs(profile, thread_id) WHERE kind='chat' AND status IN ('queued','running')"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_wake_work_active "
        "ON wake_jobs(profile, project) WHERE kind='work' AND status IN ('queued','running')"
    )


def recover_on_startup(conn: sqlite3.Connection) -> int:
    """恢复投递中断和当前话题的未完成执行，inbox 原样不动。

    req#58 孤儿行收尸：wake_runs 里 ended_at IS NULL 的行是「引擎重启前 watch 线程
    没来得及收尸」的孤儿。旧 watch 回调已丢失，进程存活由队列按会话重新核实。补标
    ended_at=本次启动时刻、exit_code=-1（可识别哨兵，与正常退出 returncode 区分，
    便于事后统计事故频率）。WHERE started_at < 本次启动时刻——别误伤启动后新 spawn
    的行（那些行 started_at > 启动时刻，watch 线程还在跑，会正常收尸）。

    返回恢复的 job 数（含已投递、执行中断的话题）。
    """
    from . import router, wake

    startup = time.time()
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute(
        "UPDATE wake_jobs SET status='queued', started_at=NULL, ended_at=NULL "
        "WHERE status='running'"
    )
    recovered = cur.rowcount
    # A done job only records successful delivery. Its bot may still have been
    # executing when the engine stopped. Bind recovery to the exact current slot.
    for thread in conn.execute(
        "SELECT t.* FROM threads t JOIN projects p ON p.name=t.project "
        "WHERE t.status='open' AND t.turn_profile!='' AND p.archived=0"
    ).fetchall():
        profile = thread["turn_profile"]
        if is_builtin_user(profile) or profile not in router._round_members(conn, thread["id"]):
            continue
        parent_id, round_no = thread["turn_parent_id"], thread["round"]
        slot = f"e:{parent_id}:{profile}" if parent_id is not None else f"r:{round_no}:{profile}"
        if slot in router._skipped(conn, thread["id"]):
            continue
        responded = (router._edge_responded(conn, parent_id, profile) if parent_id is not None
                     else router._round_spoken(conn, thread["id"], round_no, profile))
        if responded:
            continue
        session = wake.session_name(profile, "chat", thread["id"], thread["project"])
        run = conn.execute(
            "SELECT started_at,ended_at,project FROM wake_runs WHERE session=? "
            "ORDER BY started_at DESC LIMIT 1", (session,),
        ).fetchone()
        turn_started = router._ts_seconds(thread["turn_wake_at"])
        if (run is None or run["ended_at"] is not None or run["project"] != thread["project"]
                or not turn_started or not turn_started <= run["started_at"] < startup):
            continue
        trigger = f"recover:chat:{round_no}:{parent_id if parent_id is not None else 'root'}"
        # Keep the original run/job for audit. The unique active-job index also
        # covers a delivery interrupted before it could be marked done.
        inserted = conn.execute(
            "INSERT OR IGNORE INTO wake_jobs "
            "(profile,project,kind,trigger,thread_id,status,enqueued_at) "
            "VALUES(?,?,'chat',?,?,'queued',?)",
            (profile, thread["project"], trigger, thread["id"], now_iso()),
        )
        recovered += inserted.rowcount
        conn.execute(
            "UPDATE wake_jobs SET trigger=? WHERE profile=? AND project=? "
            "AND kind='chat' AND thread_id=? AND status='queued'",
            (trigger, profile, thread["project"], thread["id"]),
        )
        conn.execute("UPDATE threads SET turn_wake_at=? WHERE id=?", (now_iso(), thread["id"]))
    # req#58 孤儿行收尸：只补「启动时刻之前、还没收尸」的行（启动后新写的行不动）
    conn.execute(
        "UPDATE wake_runs SET ended_at=?, exit_code=-1 "
        "WHERE ended_at IS NULL AND started_at < ?",
        (startup, startup),
    )
    conn.commit()
    return recovered
