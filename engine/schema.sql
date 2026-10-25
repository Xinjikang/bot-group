-- group.db schema（引擎唯一库，方案B：任务池 + 群聊 + 项目 全并一个库）
-- 红线：messages 只 INSERT 永不 UPDATE/DELETE；WAL + busy_timeout=30s（连接层设置）

-- ============ 项目（根，方案B 新增）============
CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    owner TEXT NOT NULL,
    message_id INTEGER REFERENCES messages(id),
    position INTEGER NOT NULL DEFAULT 0,
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL UNIQUE,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_id);

CREATE TABLE IF NOT EXISTS projects (
    display_name TEXT NOT NULL DEFAULT '', -- 展示名；name 是稳定 ID
    directory_mode TEXT NOT NULL DEFAULT '', -- shared / worktree，由迁移补齐旧记录
    readiness_status TEXT NOT NULL DEFAULT 'ready' CHECK(readiness_status IN ('pending','initializing','ready','failed')),
    name        TEXT PRIMARY KEY,          -- 稳定项目 ID（兼容旧字段名）
    root        TEXT NOT NULL,             -- 项目根路径（主目录，agents.md 所在）
    worktree_root TEXT DEFAULT '',         -- worktree 根目录（bot 各自 worktree 的父目录，如 xjk-worktrees）
    description TEXT DEFAULT '',           -- 项目描述（2026-09-09 需求3：详情窗可改）
    archived    INTEGER NOT NULL DEFAULT 0, -- 归档：不在 UI 展示
    removed     INTEGER NOT NULL DEFAULT 0, -- 逻辑移除，保留历史并释放目录绑定
    removed_at  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

-- ============ Bot 身份层（req#86：宿主 Profile / Bot / 项目成员 三层对象）============
-- Bot = 引擎中的稳定身份，绑定宿主类型、配置根目录、profile 与接入模式，跨项目存在。
-- host_type: hermes / codex（内置 user 身份的 host 特殊标记由 req#86 task#149 定义，
--            此处不加 CHECK 收死，避免堵死内置身份落库）。
-- runtime: hermes / codex。display_name 全局统一（同一 Bot 跨项目同名，项目层不覆盖）。
-- UNIQUE(host_type, config_root, profile)：一个物理 profile 至多绑一个 Bot，
-- 同名 profile 双宿主歧义在 schema 层消解（设计稿 2.5 第 1 点）。
CREATE TABLE IF NOT EXISTS bots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    host_type    TEXT NOT NULL,
    profile      TEXT NOT NULL,
    config_root  TEXT NOT NULL,
    runtime      TEXT NOT NULL DEFAULT 'hermes',
    display_name TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT '', -- 旧版全局角色，仅保留用于存量迁移
    UNIQUE(host_type, config_root, profile)
);

-- ============ 成员（宿主 profile 的可发现资源花名册）============
-- req#27（ENGINEERING 28.1）：启停状态已迁至 project_members（按项目维度），此处不再有全局 active。
-- req#86 语义降级：members 从「身份」降为「宿主 profile 的可发现资源」（扫描宿主产出），
-- 身份主体上移 bots 表；@解析/唤醒/权限的运行时定位仍是 profile 字符串（设计稿 2.2
-- 影响面逐任务切换），profile 仍是各消费点的 JOIN 键。created_at/source 由 task#149 补。
-- Preview plans include staged content and hashes; project registration is a later transaction.
CREATE TABLE IF NOT EXISTS project_initializations (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL DEFAULT '',
    root TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'preview',
    status TEXT NOT NULL DEFAULT 'pending',
    plan TEXT NOT NULL,
    revision TEXT NOT NULL,
    completed TEXT NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT '{}',
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_setup_requests (
    id TEXT PRIMARY KEY, digest TEXT NOT NULL, status TEXT NOT NULL,
    payload TEXT NOT NULL, result TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_resources (
    project TEXT NOT NULL REFERENCES projects(name) ON DELETE CASCADE,
    path TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    config TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(project,path)
);

CREATE TABLE IF NOT EXISTS project_workspaces (
    project TEXT NOT NULL, profile TEXT NOT NULL, path TEXT NOT NULL,
    PRIMARY KEY(project, profile), UNIQUE(path)
);
CREATE TABLE IF NOT EXISTS project_workspace_modes (
    project TEXT NOT NULL, profile TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('shared','worktree')),
    PRIMARY KEY(project, profile)
);
CREATE TABLE IF NOT EXISTS project_permissions (
    project TEXT NOT NULL, profile TEXT NOT NULL, permission TEXT NOT NULL,
    PRIMARY KEY(project, profile, permission)
);
CREATE TABLE IF NOT EXISTS project_config_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT NOT NULL,
    actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
    profile      TEXT PRIMARY KEY,          -- 宿主 profile 名（@解析精确匹配此名）；req#86：内置 user 不落此表（设计稿 8.1，非扫描资源）
    display_name TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT '',   -- 旧版全局角色，仅用于存量迁移；现行角色取 project_members
    created_at   TEXT NOT NULL DEFAULT '',  -- req#86 8.2 可追溯：行创建时间（存量迁移行为 ''，注册 API 落 now_iso）
    source       TEXT NOT NULL DEFAULT 'legacy', -- 来源 legacy=存量迁移 / api=注册接口
    bot_id       INTEGER                    -- profile 是引擎路由标识，物理 profile 从 bots 读取
);

-- ============ 项目 × Bot 启停（req#27 按项目维度；req#86 关联键 profile → bot_id）============
-- 无记录表示未加入项目；active=1 为显式启用，active=0 为停用。
-- req#86 task#147：关联主体 bot_id（定位走 bots 稳定身份，PRIMARY KEY 由此承载）。
-- 前三列保持 req#27 的列序：运行时消费点（router/queue/wake/mentions/project_permissions
-- 等 11 处）与既有写入路径仍按 profile 定位（JOIN pm.profile 或具名列 INSERT），由
-- profile 兼容列承接——消费点随后续任务逐个切到 bot_id 后（模式同 req#27 的
-- _CONSUMERS_MIGRATED 门控），profile 兼容列与 bot_id 的可空性一并收紧退役。
-- bot_id 可空的唯一成因 = 兼容期裸 SQL 写入（生产路径：迁移回填 + api 写点均必填）。
CREATE TABLE IF NOT EXISTS project_members (
    project   TEXT NOT NULL,
    profile   TEXT NOT NULL,             -- 兼容列：旧消费点按 profile JOIN 仍在用，勿删
    active    INTEGER NOT NULL DEFAULT 1,
    bot_id    INTEGER,                   -- 新关联键（req#86）；兼容期可空，随消费点切换收紧
    role      TEXT NOT NULL DEFAULT '',  -- 本项目中的角色；职责与工作流程由 AGENTS.md 约定
    role_id   INTEGER,                   -- req#96：角色实体化，关联 project_roles.id；改名靠它稳定
    PRIMARY KEY (project, bot_id)
);

-- ============ 项目角色（req#96 task#163：角色从自由文本升级为可管理实体）============
-- 角色是项目级（跟 role/permission 一样按 project 分），每项目一套。
-- id 为稳定主键：改名后 project_members.role_id 关联不断。
-- UNIQUE(project, name) 拦截同项目同名角色；role 文本列仍保留在
-- project_members 作兼容列（旧消费点/前端仍按文本读），role_id 是权威关联。
CREATE TABLE IF NOT EXISTS project_roles (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project    TEXT NOT NULL,
    name       TEXT NOT NULL,            -- 角色名（显示名，可改名）
    created_at TEXT NOT NULL DEFAULT '',
    UNIQUE(project, name)
);

-- ============ 话题 ============
CREATE TABLE IF NOT EXISTS threads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project         TEXT NOT NULL DEFAULT 'xjk',  -- 项目维度（会话隔离/多项目群的根）
    title           TEXT NOT NULL,
    creator         TEXT NOT NULL,           -- 发起者 profile = 收口责任人
    task_id         INTEGER,                 -- 关联任务（可空=纯讨论）。修订代理硬校验边界
    members         TEXT NOT NULL,           -- JSON 数组（创建时定，creator 自动含）
    status          TEXT NOT NULL DEFAULT 'open',  -- open / closed
    hidden          INTEGER NOT NULL DEFAULT 0,     -- 隐藏：不显示，有新消息置回 0
    round           INTEGER NOT NULL DEFAULT 0,     -- 当前轮次号（开场白=0，Round N 发言=N）
    turn_profile    TEXT DEFAULT '',               -- 顺序唤醒：当前轮到谁（空=无人轮到）
    turn_wake_at    TEXT DEFAULT '',               -- 顺序唤醒：唤醒时间戳（超时跳过用）
    turn_parent_id  INTEGER,                       -- 当前 turn 的 parent_id（NULL=轮次独立发言，非空=子树接话）
    turn_skipped    TEXT DEFAULT '[]',             -- 弃权集合：发言位标识 JSON（"e:{msg_id}:{profile}"=@边弃权 / "r:{round}:{profile}"=轮次发言弃权）
    created_at      TEXT NOT NULL,
    last_active_at  TEXT NOT NULL,          -- 引擎计时唯一依据（静默30min→唤醒发起者评估收口）
    closed_at       TEXT,
    close_conclusion TEXT                    -- 收口结论（修订/仲裁指针）
);

-- 同一次创建请求的重试复用话题；作用域为项目和已解析的创建者。
CREATE TABLE IF NOT EXISTS thread_create_requests (
    project TEXT NOT NULL,
    creator TEXT NOT NULL,
    request_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    thread_id INTEGER NOT NULL REFERENCES threads(id),
    PRIMARY KEY (project, creator, request_id)
);

-- ============ 消息 ============
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL DEFAULT 'xjk',  -- 项目维度
    ts          TEXT NOT NULL,
    sender      TEXT NOT NULL,              -- profile 名 或 'user'
    sender_type TEXT NOT NULL,              -- bot / user / engine
    content     TEXT NOT NULL,
    mentions    TEXT NOT NULL DEFAULT '[]', -- JSON 数组（解析出的 profile 名）
    thread_id   INTEGER,                    -- NULL=点对点；非空=话题消息
    parent_id   INTEGER,                    -- 回应边：子树接话=@它的消息id；轮次独立发言/开场白=NULL
    round       INTEGER NOT NULL DEFAULT 0, -- 轮次号：开场白=0，Round N 的发言=N
    depth       INTEGER NOT NULL DEFAULT 0,  -- 树深度：开场白/轮次独立发言=0，子树接话=1..4
    retract_by  TEXT,                        -- req#41：撤回者 profile（NULL=未撤回）
    retract_at  TEXT,                        -- req#41：撤回时间（NULL=未撤回）
    need_reply  INTEGER DEFAULT 1            -- req#61：三态——NULL=无 @（无要求，窄闸门可静默标已读、不催办），0=声明无需回复（窄闸门豁免），1=必须回复
);
-- append-only 红线：本表只 INSERT；撤回只写 retract_by/retract_at 标记列，绝不 UPDATE content

-- ============ inbox ============
CREATE TABLE IF NOT EXISTS inbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL REFERENCES messages(id),
    profile     TEXT NOT NULL,              -- 待看者（跳过 sender 自己）
    status      TEXT NOT NULL DEFAULT 'pending', -- pending / seen / expired / failed（req#82 R2 整组陈旧显式失败终态）
    closed_by   INTEGER,                    -- 收口消息 id（expired 时记录溯源）
    seen_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_inbox_profile_pending ON inbox(profile, status);

-- ============ 唤醒队列 ============
CREATE TABLE IF NOT EXISTS wake_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    profile      TEXT NOT NULL,
    project      TEXT NOT NULL DEFAULT 'xjk',  -- 项目维度（scan_tasks 传参，多项目 bot 归属）
    kind         TEXT NOT NULL,             -- chat / work（进程分水岭：chat 受限工具集）
    trigger      TEXT NOT NULL,             -- @msg / thread-open / thread@msg / scan:group / scan:task / scan:thread-idle / scan:thread-all-responded / approved
    trigger_msg_id INTEGER,                 -- 触发消息 id（@ 触发时记录）
    thread_id    INTEGER,                   -- 话题 id（扫描器唤醒发起者时传，会话派生用）
    status       TEXT NOT NULL DEFAULT 'queued', -- queued / running / done / killed / failed
    exit_code    INTEGER,
    detail       TEXT,                      -- 结束原因摘要
    enqueued_at  TEXT NOT NULL,
    started_at   TEXT,
    ended_at     TEXT,
    chat_context_until TEXT,                -- chat 唤醒投递成功时写 now+600s：执行类工具运行时校验窗口
    last_wake_kind TEXT,                    -- 心跳归属（与防误开工拆开的独立字段）
    last_wake_thread_id INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wake_chat_active
    ON wake_jobs(profile, thread_id) WHERE kind='chat' AND status IN ('queued','running');
CREATE UNIQUE INDEX IF NOT EXISTS idx_wake_work_active
    ON wake_jobs(profile, project) WHERE kind='work' AND status IN ('queued','running');

-- ============ 任务池（统一 group.db，带 project 维度）============
CREATE TABLE IF NOT EXISTS requirements (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project      TEXT NOT NULL DEFAULT 'xjk',  -- 项目维度（方案B 新增，替代 project_name）
    title        TEXT NOT NULL,
    version      TEXT DEFAULT '',
    description  TEXT DEFAULT '',
    background_goal TEXT NOT NULL DEFAULT '',
    scope        TEXT NOT NULL DEFAULT '',
    approach_basis TEXT NOT NULL DEFAULT '',
    acceptance   TEXT NOT NULL DEFAULT '',
    undecided    TEXT NOT NULL DEFAULT '',
    source_ref   TEXT NOT NULL DEFAULT '',
    gate_reason  TEXT NOT NULL DEFAULT '',  -- req#111 task#192：门禁未过原因（scanner 写回，过门禁清空）
    decompose_state  TEXT NOT NULL DEFAULT 'pending',  -- req#117 task#198：pending | decomposing | done
    decompose_owner  TEXT NOT NULL DEFAULT '',  -- 当前拆解负责人 profile
    decompose_credential TEXT NOT NULL DEFAULT '',  -- 执行凭据（区分同一 bot 不同执行）
    decompose_expires_at TEXT NOT NULL DEFAULT '',  -- 占用有效期（30 分钟，写任务续期）
    decompose_stale_credentials TEXT NOT NULL DEFAULT '[]',  -- req#118 task#203：超时接管被覆盖的旧凭据（JSON 数组，完成后仍拒旧执行迟到写入）
    priority     INTEGER NOT NULL DEFAULT 0,   -- lower = more urgent
    status       TEXT NOT NULL DEFAULT 'open', -- open | done | blocked | cancelled
    project_root TEXT DEFAULT '',  -- 项目根路径（agents.md 所在）
    archived     INTEGER NOT NULL DEFAULT 0,   -- req#76：归档整个需求（仿 projects.archived），仅 done/cancelled 可归档
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- req#117 task#198：需求拆解幂等记录（idempotency_key → task_ids + 内容指纹），
-- 防跨调用重复创建；同一 key 不同内容报冲突。
CREATE TABLE IF NOT EXISTS decompose_requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project        TEXT NOT NULL DEFAULT 'xjk',
    requirement_id INTEGER NOT NULL REFERENCES requirements(id),
    idempotency_key TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    task_ids       TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL,
    UNIQUE(project, requirement_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS tasks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project        TEXT NOT NULL DEFAULT 'xjk',  -- 项目维度（方案B 新增）
    requirement_id INTEGER NOT NULL REFERENCES requirements(id),
    title          TEXT NOT NULL,
    priority       INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'pending', -- pending | in_progress | done | blocked
    next_bot       TEXT DEFAULT '',      -- 下一位接手人的引擎成员标识
    claimed_by     TEXT DEFAULT '',
    block_kind     TEXT DEFAULT '',  -- technical / decision
    created_at     TEXT NOT NULL,
    claimed_at     TEXT DEFAULT '',
    done_at        TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS nodes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL DEFAULT 'xjk',  -- 项目维度（方案B 新增）
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    actor   TEXT NOT NULL,
    type    TEXT NOT NULL,
    meta    TEXT DEFAULT '{}',
    result  TEXT DEFAULT '',
    comment TEXT DEFAULT '',
    ts      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project        TEXT NOT NULL DEFAULT 'xjk',  -- 项目维度（方案B 新增）
    requirement_id INTEGER,
    task_id        INTEGER,
    node_id        INTEGER,
    ts             TEXT NOT NULL,
    actor          TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    detail         TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT NOT NULL,
    project    TEXT NOT NULL DEFAULT 'xjk',  -- 项目维度
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (key, project)
);

CREATE INDEX IF NOT EXISTS idx_tasks_req ON tasks(requirement_id, priority);
CREATE INDEX IF NOT EXISTS idx_tasks_bot_status ON tasks(next_bot, status);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project, status);
CREATE INDEX IF NOT EXISTS idx_nodes_task ON nodes(task_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_req ON events(requirement_id, ts);

CREATE TABLE IF NOT EXISTS wake_runs (
    session TEXT NOT NULL,
    profile TEXT,
    thread_id INTEGER,
    runtime_session_id TEXT,           -- req#86：通用运行时会话 ID（原 hermes_session_id 改名）——hermes 存 session_id、codex 存 thread_id，按 Bot runtime 判读；codex 行 spawn 前为 NULL、执行后回填
    bot_id INTEGER,                   -- 归属 Bot 稳定身份
    runtime TEXT NOT NULL DEFAULT 'hermes',
    started_at REAL NOT NULL,
    ended_at REAL,
    exit_code INTEGER,
    pid INTEGER,                      -- req#39：spawn 的壳进程 pid（proc.pid），退出判定/审计溯源用
    project TEXT,                     -- req#72 中危：spawn 时直接落库 project，get_thinking 直读不再从 session 切段反推
    PRIMARY KEY(session, started_at)
);

-- Execution failures and their public notifications commit together.
CREATE TABLE IF NOT EXISTS execution_errors (
    project TEXT NOT NULL,
    profile TEXT NOT NULL,
    attempt TEXT NOT NULL,
    category TEXT NOT NULL,
    diagnostic TEXT NOT NULL,
    exit_code INTEGER,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    PRIMARY KEY(project, profile, attempt)
);

-- Browser harnesses inherit an engine-owned namespace for each execution.
CREATE TABLE IF NOT EXISTS browser_resources (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    profile TEXT NOT NULL,
    session TEXT NOT NULL,
    started_at REAL NOT NULL,
    thread_id INTEGER,
    task_id INTEGER,
    release_requested INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_browser_resources_task ON browser_resources(project, task_id);

CREATE TABLE IF NOT EXISTS message_executions (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id),
    session TEXT NOT NULL,
    wake_started_at REAL NOT NULL,
    sent_at REAL NOT NULL,
    bot_id INTEGER,                   -- req#86 task#149（2.5 第4点）：归属 Bot；NULL=待归属（反推二义），init_schema 回填
    FOREIGN KEY(session,wake_started_at) REFERENCES wake_runs(session,started_at)
);
CREATE INDEX IF NOT EXISTS idx_message_execution_run ON message_executions(session,wake_started_at);

CREATE TABLE IF NOT EXISTS codex_run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session TEXT NOT NULL,
    wake_started_at REAL NOT NULL,
    timestamp REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_codex_run_events ON codex_run_events(session,wake_started_at,id);

CREATE TABLE IF NOT EXISTS project_rule_copies (
    path TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    source TEXT NOT NULL,
    digest TEXT NOT NULL DEFAULT '',
    pending_digest TEXT NOT NULL DEFAULT ''
);

-- ============ 会话绑定（req#83：真实 Hermes session_id 持久化；req#86 主键改 bot_id）============
-- req#86 task#148：绑定主体从 profile 切到 Bot 稳定身份（设计稿 2.5 第 1 点）。
-- profile 为兼容列：wake.py/stop.py/steer_ledger.py 的按 profile 读取仍靠它承接
-- （同 req#86 task#147 的 pm 兼容模式），消费点切完后随收紧任务退役。
-- bot_id 兼容期可空：迁移回填 + session_adapter 写点必填；NULL 不参与 PK 去重。
CREATE TABLE IF NOT EXISTS session_bindings (
    project      TEXT NOT NULL,
    bot_id       INTEGER,                   -- Bot 稳定身份（req#86；兼容期可空，见上）
    profile      TEXT NOT NULL,             -- 兼容列：旧消费点按 profile 读取仍在用，勿删
    kind         TEXT NOT NULL,               -- work / chat
    thread_id    INTEGER NOT NULL DEFAULT 0,  -- chat 真实话题 id；work 恒 0（统一空值约定）
    session_id   TEXT NOT NULL,               -- 运行时会话 ID：hermes=真实 session_id（YYYYMMDD_HHMMSS_<6hex>）；codex=原生 thread_id（req#86 第4节分叉）
    runtime      TEXT NOT NULL DEFAULT 'hermes', -- 逐行 runtime 快照（req#88 task#153）：hermes/codex。读取方按此列判 session_id 语义，不靠 bots.runtime 推导
    generation   INTEGER NOT NULL DEFAULT 0,  -- /new 切换递增：并发校验 + 审计 + 使旧运行失效（codex 行不消费，恒 0）
    switching    INTEGER NOT NULL DEFAULT 0,  -- /new 切换中标记：新 wake 等待、崩溃恢复核对（codex 行恒 0，不读写）
    title        TEXT NOT NULL DEFAULT '',    -- 显示/迁移用途（= 旧会话名派生）
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (project, bot_id, kind, thread_id)
);

-- /new 切换操作记录（req#83：切换时序审计 + 待核实恢复）
CREATE TABLE IF NOT EXISTS session_switch_ops (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id   TEXT NOT NULL UNIQUE,      -- 命令操作 ID（UUID），回执/核对/恢复锚点
    project        TEXT NOT NULL,
    profile        TEXT NOT NULL,
    kind           TEXT NOT NULL,
    thread_id      INTEGER NOT NULL DEFAULT 0,
    old_session_id TEXT NOT NULL,             -- 切换前绑定 session_id（无绑定时 ''）
    old_generation INTEGER NOT NULL DEFAULT -1, -- 切换前 generation（无绑定时 -1）
    new_session_id TEXT,                      -- 新会话 id；切换成功=实际 id，待核实=候选 id
    status         TEXT NOT NULL DEFAULT 'switching', -- switching / committed / unknown
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- ============ 斜杠命令（req#83 task#146：命令分发留痕） ============
-- 设计文档第 7 条：拦截落到引擎 API 层、只对 sender=user 分发、命令落 commands 表留痕。
CREATE TABLE IF NOT EXISTS commands (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project     TEXT NOT NULL,
    sender      TEXT NOT NULL,                -- 发起者（命令只对 user 分发，这里恒 'user'，留审计）
    command     TEXT NOT NULL,                -- /new /steer /help（含斜杠）
    args        TEXT NOT NULL DEFAULT '',     -- 去除命令名与 @ 目标后的剩余参数
    targets     TEXT NOT NULL DEFAULT '[]',   -- @ 目标 profile 列表（JSON 数组）
    thread_id   INTEGER,                      -- 话题内命令的 thread_id（点对点为 NULL）
    status      TEXT NOT NULL DEFAULT 'accepted', -- accepted / rejected / error / ok
    result      TEXT NOT NULL DEFAULT '',     -- 执行结果摘要或错误
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_commands_project ON commands(project, created_at);

-- ============ /steer 纠偏账本（req#83 task#145：回执五层 + 悬空终态，路线 A 纯账本层） ============
-- 设计文档 S4 + 硬约束 U2/U3/T4：纠偏投递记录精确绑定 (project, profile, kind, thread_id,
-- session_id, generation, run_started_at)；回执分五层；悬空终态由操作 ID 消费证据 + leftover 判定。
-- 本表只记账本，传输通道由 adapter 落地后经 steer_ledger 注入（进程内 IPC 调 steer 不落地）。
CREATE TABLE IF NOT EXISTS steer_operations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id   TEXT NOT NULL UNIQUE,      -- 命令操作 ID（UUID），回执/核对/恢复锚点
    project        TEXT NOT NULL,
    profile        TEXT NOT NULL,             -- 目标 bot
    kind           TEXT NOT NULL,             -- work / chat
    thread_id      INTEGER NOT NULL DEFAULT 0,
    session_id     TEXT NOT NULL DEFAULT '',  -- 目标会话真实 session_id（投递时绑定）
    generation     INTEGER,                   -- 目标会话 generation（跨 /new 失效判定）
    run_started_at REAL,                      -- 目标 run 的 started_at（精确绑定运行，wake_runs 同键）
    text           TEXT NOT NULL,             -- 纠偏正文
    status         TEXT NOT NULL DEFAULT 'queued',
        -- queued(已入队待投递) / delivering(已投递等消费证据) / consumed(已插入上下文)
        -- / pending_verify(待核实：回执缺失证据不足) / undelivered(未送达：确定未消费)
        -- / target_ended(目标已结束) / generation_stale(跨 /new 旧运行失效) / expired(TTL 过期)
    deliver_layer  TEXT NOT NULL DEFAULT '',
        -- 回执五层口径（U3/T4）：queued / inserted(已插入上下文) / persisted(已持久化)
        -- / model_requested(已提交模型请求) / behavior_observed(行为响应)
        -- 落库只报「已写入会话」，进实际模型请求才报 model_requested，行为响应单独验收
    leftover       TEXT,                      -- 悬空 leftover（adapter 上报 finalizer 返回的 pending_steer）
    result         TEXT NOT NULL DEFAULT '',  -- 结果摘要 / 错误 / 待核实说明
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_steer_ops_run ON steer_operations(project, profile, kind, thread_id, run_started_at);
CREATE INDEX IF NOT EXISTS idx_steer_ops_status ON steer_operations(status, created_at);
