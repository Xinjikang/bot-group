# Hermes 网关会话级资源复用

## 范围

本方案只改 Hermes 网关 WS 执行路径（`engine/hermes_gateway*.py`、`engine/hermes_connections.py`），解决同一会话内 agent 每轮被重建的问题。子进程执行路径（`engine/wake.py` 的 subprocess 通道）不涉及本方案，保持原样。

## 背景与根因

网关 WS 路径下，同一会话的 agent 本可跨轮复用（Hermes 原生 per-session AIAgent 缓存，见 `gateway/run_agent_cache.py`），但当前引擎在每次唤醒的收尾与下次接入时，都会触发 Hermes 清掉缓存的 agent，导致每轮重建。

已确认的失效链：

1. `engine/hermes_gateway_sessions.py` 的 `configure()` 每次 admission 前先 `session.mutate editor_mcp` 提交空列表、再提交本次 task_queue 配置；`clear_mcp()`（`hermes_gateway_sessions.py:100`）在轮次收尾把 editor_mcp 改为 `[]`。
2. Hermes 将 editor_mcp 归入策略层（`gateway/session_mutations.py`），变更时 `publish_local_policy(..., evict=True)`，进一步调用 `_evict_cached_agent`（`gateway/session_local.py`、`gateway/run_agent_cache.py`），把该会话的 agent 从缓存移除并释放其 LLM 客户端池。
3. 下一轮 admission 时缓存未命中（`gateway/session_execution.py:208`），走 `_build_fresh_agent` 重建。

订阅侧同样每轮断开：`engine/hermes_connections.py` 的 `Lease.close()` 在最后一个租约释放时调用 `session.detach`，Hermes 侧随即 `release_editor_mcp` 并在无执行借用时断开连接。

结论：每轮「换凭据 → 重配 editor_mcp → detach」是 agent 每轮重建的根因，不是安全硬需求。

## 方案

把 MCP 配置、订阅与授权的清理时机，从「每轮完成」提升到「会话生命周期」。每轮仍保留独立的执行记录（调度、取消、记账的粒度），同一会话跨轮复用 agent、MCP 连接与订阅。

### 会话生命周期

1. **会话首次接入**：建立 gateway session、配置 MCP、绑定会话授权并保留订阅。
2. **后续唤醒**：复用已有会话，只提交新消息；沿用现有 busy 检查（`hermes_gateway.py:123-133`）与同会话唯一非终态执行约束（`hermes_gateway_ledger.py` 唯一索引）保证串行，B 在 A 终态后才提交。
3. **执行终态收尾**：只更新执行账本、消息处理进度并调度后续消息；不 `clear_mcp`、不撤销会话授权、不 detach。
4. **会话关闭 / 撤销权限 / 资源回收**：走独立生命周期，此时才清理 MCP 配置、释放订阅与连接、回收 agent。

### 具体改动点

| 位置 | 当前行为 | 目标行为 |
| --- | --- | --- |
| `engine/hermes_gateway.py` `spawn`/`_finish` | 收尾调 `clear_mcp`，接入时 `configure_tracked` 重配 | 收尾不 `clear_mcp`；接入时仅在配置实际变化时更新 |
| `engine/hermes_gateway_sessions.py` `configure`/`clear_mcp` | 每轮清空并重装 editor_mcp | editor_mcp 会话内稳定，model/reasoning 等仅在值变化时 mutate |
| `engine/hermes_connections.py` `Lease.close` | 每轮释放租约 → `session.detach` | 保留订阅至会话关闭或空闲回收点 |

MCP 服务（`mcp/server.py`）配套改为识别稳定的会话身份，不再依赖每轮更换的启动环境凭据。会话授权、MCP 配置、订阅三者随会话建立、随会话结束，与单次执行解耦。

## 兼容边界

本方案涉及部分共用模块，不能只改上述三个 gateway 文件。三条边界必须同时满足：

1. **分流点**（`engine/wake.py:912-916`）：仅 `runtime=hermes` 且 `hermes_gateway.enabled()` 时进入 WS 路径；子进程仍走既有 spawn、逐执行 env 与进程退出清理。WS 的稳定会话配置走独立构造分支，保留共用 `_mcp_server_config` 的原调用行为，不改其签名与子进程侧语义。
2. **共用鉴权模块**（`engine/auth.py`、`engine/access.py`、`mcp/server.py`）：新增「网关会话凭据/上下文」分支，与子进程的 `execution_token`、`wake_started_at` 校验、终态规则并存。子进程侧原逻辑保持原样；网关会话授权按会话记录独立校验。
3. **MCP 执行上下文**（`mcp/server.py` 的 `_execution_context`、`group_send`、`_task_call`）：当前强制要求 `wake_started_at` 并逐执行传递。网关分支改用稳定会话上下文，不再每轮携带唤醒时间；单次执行的消息记账与任务关联，由引擎串行调度记录（执行账本）承载，设计需明确这一关联方式，避免常驻 MCP 一直携带首次唤醒时间。

### 保留不动的部分

- 每轮执行记录（`hermes_executions`）——调度、取消、记账的粒度，继续每轮新建。
- 同会话串行约束（busy 检查 + 唯一索引）——B 只能在 A 终态后提交。
- 消息队列与重启恢复（`hermes_gateway_ledger.py` 账本、`recover()`）——消息持久、执行跟踪跨重启接回。
- 子进程执行路径——每轮 spawn 新进程，agent 随进程生命周期新建，无复用语义，不涉及 editor_mcp 与订阅。

## 回归验收

**网关 WS 路径**：

1. 同一会话连续两轮，agent 构建次数为 1（复用），第二轮不重建。
2. MCP 进程启动次数不随轮次增加（连接复用）。
3. 停止/取消：`session.interrupt`/`prompt.cancel` 打断当前回答，会话授权保留、下一轮可继续。
4. 会话关闭、移除成员、撤销权限后，MCP 连接释放、agent 回收。
5. 引擎重启：旧凭据会话完成交接后退役，订阅与执行跟踪按账本/回执恢复。
6. 串行：A 执行中 B 留在队列，A 终态后才提交 B。

**子进程路径（维持原样）**：

1. 启动配置（spawn 命令、逐执行 env 注入）不变。
2. 鉴权（`execution_token`、`wake_started_at` 校验）不变。
3. 消息/任务调用（`group_send`、`_task_call` 的逐执行上下文）不变。
4. 退出行为（进程退出清理）不变。

## 依据

- `docs/engineering/HERMES-GATEWAY.md`
- `docs/design/2026-10-30-hermes-gateway-connections.md`
- `docs/design/SERVE-RESIDENT-SOLUTION-2026-10-08.md`
