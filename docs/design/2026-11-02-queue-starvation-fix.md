# 队列饥饿修复与自发执行状态展示方案

## 状态

- 方案状态：定稿，需求 #142 已立项，按任务流程进入开发与验收。
- 关联：`2026-10-27-hermes-gateway-migration.md`（网关适配，引入缺口）、`../engineering/HERMES-GATEWAY.md`（执行适配契约）、[需求文档](../requirements/2026-11-02-queue-starvation-fix.md)。
- 历史归因：`a057284`（初版统一队列表按 ID 取队首）、`1b1b1d6`（按会话跳过 busy + busy 退回保留原 ID）、`6d3f3b1`（网关适配，预检查未接入原生忙闲）。按会话保序、跨会话独立推进是既有正确设计，本方案修复其数据源缺口，不改变调度语义。

## 1. 调度预检查修复

### 1.1 缺口位置

- 预检查链路：`queue.process_one` 逐候选调 `wake.session_busy`（`wake.py:111-116`）→ 内存 `_running_sessions` + `find_bot_processes`。
- `find_bot_processes`（`wake.py:141`）已按 runtime 分流：Codex/DSH 查 `wake_runs`（`_find_codex_processes`，`wake.py:198-212`）；Hermes 托管会话查 `hermes_gateway.process_state`（`hermes_gateway.py:85-102`）；其余回退本地进程匹配。
- `process_state` 当前判定：`hermes_executions` 最新记录与 `wake_runs` 最新 `started_at` 比对（93-95 行，返回 False 交回进程匹配），最近执行 terminal 且清理完成返回 `[]` 判空闲（96 行）。Hermes 自发续跑不落引擎表，两条路径都可能漏判；具体现场走哪条仅按证据归因，修复须一并核对。
- 投递阶段经绑定实时 attach 网关原生会话，running/pending 判定准确；预检查与投递两个数据源不一致即缺口本身。

### 1.2 修复要求

- 按 runtime 与 `session_bindings` 接入网关原生执行状态：引擎执行记录（`wake_runs`/`hermes_executions` 已有清理语义）与原生 running/pending 任一表明占用，均按忙碌处理，预检查跳过该候选并继续检查下一个独立会话。
- 判定顺序：内存已知占用（`_running_sessions` 等零成本路径）先判忙；本地判空闲的候选再查原生状态。保留 CLI/Codex/DSH 现有查询语义，不改变其判定路径。
- 异常边界：原生查询超时、断连、未知 → 暂缓该会话（保守不投递），继续调度其他独立会话；查询恢复后该会话自动回到正常判定。查询必须有超时上界，避免单个网关查询拖住调度循环。
- 性能边界：如采用短 TTL 缓存降低查询频率，缓存按「原生会话 + 绑定代次」隔离，`/new` 或绑定切换立即失效；缓存空闲窗口内新执行启动的竞态作为回归场景覆盖。
- 保留不动：`queue.py` 按会话跳过 busy 逻辑、第 143 行退回逻辑、任务原 ID 与原顺序；同话题按轮次、同工作会话按请求顺序。退回分支只在预检查后才变忙的竞态窗口触发，不再形成死循环。

## 2. /thinking 自发执行状态展示

### 2.1 现状

`GET /thinking`（`api.py:1345`）读 `heartbeat.active_entries`，再按 pid 关联 `wake_runs` 补 `session`/`wake_started_at`/`spawned_at`/`project`；缺少运行身份的心跳只提供基本活跃信息，不挂载气泡。自发续跑无 `wake_runs` 记录，因此不显示；执行结束也无撤下事件。

### 2.2 修复要求

- `/thinking` 以原生执行观察补充引擎记录：`hermes_executions`/ledger 中 running/pending 且无引擎派发记录的执行，经 `session_bindings` 还原成员、项目、work/chat 与话题归属，输出结构与既有条目兼容（`profile`/`thread_id`/`session`/`wake_started_at`/`spawned_at`/`project`）。
- 观察记录与引擎主动派发记录分开：仅在有证据（回执、绑定、执行记录）时关联任务与消息，不猜测关联。
- 执行结束（terminal 回执落库）撤下对应活动状态，并发布 `thinking.changed` 事件供前端即时更新。
- 心跳既有路径、`wake.spawned`/`wake.finished` 事件语义不变。

## 3. 前端配合

- 前端思考气泡渲染由 `/thinking` 数据与 SSE 事件驱动，气泡按话题归属过滤、黄点动画、回复折叠、结束撤下的交互约定不变（见 `ui-spec/UI-SPEC.md` 与既有 think-bubbles 行为）。
- 后端新增条目以既有输出结构呈现，前端按需做最小配合（如无 `wake.spawned` 事件的条目挂载）；不新增独立渲染机制，改动范围仅限展示配合，并用合成 API 浏览器回归验证显示、归属与撤下。

## 4. 隔离真机验收

- 触发方式：真实异步子任务完成回调驱动 Hermes 自发续跑（与 2026-11-02 现场同款时序）。真机场景只能由真实网关回执产生，模拟层不得顶替。
- 验收内容：原生会话/回执、引擎执行记录、队列调度（其他独立会话继续推进、原 ID 保序、解除 busy 后恢复投递）、`/thinking` 显示与撤下，逐项取证。
- 隔离要求：使用引擎分配的隔离端口、监听地址、数据目录与专用测试账号；不污染生产数据；结束释放本次启动的进程与资源。
- 证据分层：模拟层（20 轮复现与三 runtime 混合调度用例，mocked gateway/内存 SQLite）证队列逻辑，落 `scripts/`；真机层证据落项目主目录 `reports/tests/`；两者分别记录，标注各 runtime 验证层级与未覆盖项。

## 5. 回归与测试

- 现有 20 轮复现（`process_one` + 隔离数据库）整理为 `scripts/` 可重复用例并同步 `scripts/TEST_INDEX.md`。
- 新增明确标注 Hermes/Codex/DSH 的混合调度回归；运行相关既有队列用例（索引见 `scripts/TEST_INDEX.md`）。
- 测试范围与改动风险匹配；修复缺陷补充对应回归验证。所有测试遵守索引中的运行命令、环境与前置条件约定。
