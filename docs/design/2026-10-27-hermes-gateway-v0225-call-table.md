# Hermes v0.22.5 稳定版接入调用表（第 1 步交付）

## 1. 范围与版本基线

适配目标为 Hermes v0.22.5 稳定版。本表只记录该版本经源码核实的接口，main 的增量能力另列（见第 5 节），不承诺到稳定版。

| 项 | 值 |
| --- | --- |
| 适配目标 | v0.22.5 稳定版，commit `bf0203a794a07aac3ff764972ab9087baa4e4f4e`（tag `v0.22.5`） |
| 对照基线 | origin/main `085f562a`（`v0.22.5+canary.20261027T070237Z`），仅作差异参考 |
| 证据来源 | Hermes 源码只读核对（`git show v0.22.5:<path>`），未启动实验 |
| 协议 | `hermes-gateway-v1`（`gateway/session_contract.py:9` `CANONICAL_GATEWAY_PROTOCOL`） |
| 传输 | 认证的本地 WebSocket（`session_contract.py:7`「Authenticated local WebSocket family」） |

## 2. 连接方式

连接网关分三步：发现/启动进程 → 建立 WebSocket（`/api/ws`）→ 认证。

1. **发现/启动网关进程**：CLI 侧 `connect_gateway`（`hermes_cli/gateway_client.py:262`）先调 `ensure_gateway_runtime(home, idle_exit=True)` 确保网关进程运行（无客户端时网关自行退出，下一个客户端再拉起）；从 `GatewayEndpoint.api_origin`（`gateway_runtime.py:29`，网关进程发布的 HTTP origin）拿到端点。
2. **建立 WebSocket**：`gateway_ws_target`（`gateway_client.py:42`）把 `api_origin` 的 `http→ws` 并拼 `/api/ws`，得 `ws://<origin>/api/ws`。该端点由 serve（`web_server.py`）挂载——`web_routers/chat_ws.py:701` `@router.websocket("/api/ws")` → `tui_gateway.ws.handle_ws`，子协议 = `hermes-gateway-v1`。每条连接一个 `transport_id`（`session_controls.py:139`）。
3. **认证**：`_session_ticket`（`gateway_client.py:225`）本地生成 session ticket（含 `profile_id`、`instance_id`、`purpose`），随连接提交；`session_controls.py` `AuthorityConnection.__init__`（约 :120）按 identity 构造 `Principal(subject, profile_id, capabilities, transport_id)`。identity 需带 `user_id` 且 `instance_id` 匹配 `authority.instance_id`，否则 capabilities 置空（未认证，一切方法报 `permission_denied`）。

**legacy fallback 判别（实测必须验收到 authority 路径）**：`tui_gateway/ws.py` `_dispatch_request`（:370-388）在连接未挂 authority 时回落 `server.dispatch`（legacy dispatcher），会话动作返回 -32601（`ws_legacy_fallback`）。authority 从 `ws.scope['hermes.session_authority']` 或 `ws.app.state.session_authority` 取，为 None 时 `_dispatch_request` 走 legacy 分支。实测须确认会话动作走 authority（返回正常 admission/回执）而非 legacy（-32601），否则拿 legacy 回执当网关回执会误判。

认证能力集（`session_controls.py:125-135`）：

| 能力 | 授予条件 |
| --- | --- |
| `session:read` / `session:submit` / `session:control` / `session:approve` / `session:respond` | 默认（有有效 identity） |
| `session:create` | identity 存在 |
| `session:operator` | 仅由已验证 transport gate 授予（`operator=True`），不来自 identity 数据 |

能力探测用只读方法（已确认存在于 v0.22.5 `session_controls.py:173` handlers）：

| wire method | 参数 | 返回 | 源码 |
| --- | --- | --- | --- |
| `ping` | 无 | `{"pong": true}` | `session_controls.py:488` |
| `runtime.describe` | 无 | `{instance_id, profile_id, authority_epoch, capabilities:[...], session_create:{sources,parameters}, skin}` | `session_controls.py:495` |

`runtime.describe` 返回的 `capabilities`（`session_controls.py:499`）为 `['durable-admission-v1', 'event-replay-v1', 'local-cli-create-v1', 'acp-editor-policy-v1', 'acp-session-mcp-v1']`，是判定网关能力与协议版本的直接依据。

## 3. 五类动作调用表

通用约定：`ref = SessionRef(profile_id, session_id)`；`actor = Principal(subject, profile_id, capabilities, transport_id)`。所有方法经 `hermes-gateway-v1` JSON-RPC 分发（`session_controls.py` `handlers()` :170、`dispatch` :237）。

### 3.1 建会话 / 建立订阅

| wire method | 关键参数 | 认证 | 返回 | 源码 |
| --- | --- | --- | --- | --- |
| `session.create` | `title`（可选）、`request_id`、`cwd`、`source`、`policy`、`hidden`、`skills` 等 | `session:create` | `SubscriptionSnapshot`（内部会 `resume` 并 attach） | `session_controls.py:421` |
| `session.resume` | `{}` 或 `{title}` / `{latest}` / `{editor}` / `{api_key}` | `session:read`（`editor`/`api_key` 分支加 `session:control`） | `SubscriptionSnapshot` | `session_controls.py:562` |

`SubscriptionSnapshot`（`session_contract.py:69-77`）：`subscription_id`、`handle`（SessionHandle）、`replay_epoch`、`last_sequence`、`history`、`pending`（PendingAdmission 列表）、`prompts`。

`SessionHandle`（`session_contract.py:27-34`）：`ref`、`instance_id`、`authority_epoch`、`revision`、`execution_generation`、`execution_state ∈ {idle,running,waiting,unknown,terminal}`。

提交前必须先建立订阅：`prompt.submit` 的 `submission()`（`session_controls.py:751`）校验 `ref.session_id in self.subscriptions`，未订阅报 `permission_denied`。

### 3.2 提交并拿任务号

| wire method | 关键参数 | 认证 | 返回 | 源码 |
| --- | --- | --- | --- | --- |
| `prompt.submit` | `session_id`、`text`、`submission_id`（或 `input_id`，即 request_id，必填）、`queued`、`attachments`、`finite`、`unattended` 等 | `session:submit` | `AdmissionReceipt`（dict） | `session_controls.py:690` |

- `request_id` = `submission_id` 或 `input_id`（`session_controls.py:757`），非空字符串必填，否则 `invalid_params`。
- 返回 `AdmissionReceipt`（`session_contract.py:46-53`）：`admission_id`（uuid4 hex）、`ref`、`sequence`（事件序）、`status ∈ {queued,started,unknown,terminal}`、`outcome`、`authority_epoch`、`execution_generation`。
- 幂等（`hermes_state_runtime.py` `admit_session_input` :124）：同 `(principal_id, session_id, request_id)` + 同 payload digest → 返回既有 admission（不重复执行）；不同 payload → `admission_conflict`；队列满 → `queue_full`（`ADMISSION_QUEUE_MAX`）。

### 3.3 断线找回

| wire method | 关键参数 | 认证 | 返回 | 源码 |
| --- | --- | --- | --- | --- |
| `prompt.receipt` | `admission_id`、`include_result`（可选） | `session:read` | `AdmissionReceipt`；`include_result=true` 且 terminal 时附加 `result`、`usage` | `session_controls.py:795` |
| `session.resume` | `{}` | `session:read` | `SubscriptionSnapshot`（重连 + 快照） | `session_controls.py:562` |
| `prompt.resolve_unknown` | `admission_id`、`execution_generation` | `session:operator` | `AdmissionReceipt`（状态变更） | `session_controls.py:820` |

- 已拿到 `admission_id`：`prompt.receipt` 查 `status`/`outcome`（`get_session_admission`，`hermes_state_runtime.py:174`）。
- 未拿到 `admission_id`（提交回执丢失）：同 `request_id` + 同 payload 重提 `prompt.submit` 幂等命中拿回 `admission_id`（`admit_session_input` 幂等），再 `prompt.receipt` 查终态。此路径依赖 `session_admissions` 留存期内幂等命中，留存期未知时保持阻断并转人工。
- `find_session_admission`（按 request_id 查询，`hermes_state_runtime.py:181`）是网关内部函数，无公开 wire 直达。
- 重启恢复：`recover_session_inputs`（`hermes_state_runtime.py:447`）把旧 epoch 的 `started` → `unknown`，是网关侧恢复入口（内部）。
- `prompt.resolve_unknown` 是 operator 状态变更（确认 unknown→terminal，默认 interrupted），不是只读查询，不得用于自动对账。

### 3.4 停止并确认结束

| wire method | 关键参数 | 认证 | 返回 | 源码 |
| --- | --- | --- | --- | --- |
| `prompt.cancel` | `admission_id` | `session:control` | `AdmissionReceipt` | `session_controls.py:809` |
| `session.interrupt` | `execution_generation` | `session:control` | `SessionHandle` | `session_controls.py:826` |

**v0.22.5 稳定版的停止语义（关键，与 main 不同）**：

- `cancel_session_input`（`hermes_state_runtime.py:274`）：`queued` → 取消；`started` 或 steering → 抛 `stale_generation`；`unknown` → 抛 `unknown_execution`。
- `prompt.cancel`（`session_controls.py:809`）：
  - 读 receipt，若 `status != 'queued'` → `cancel_queued`（`session_authority.py:736`），对 `started` 会抛 `stale_generation`，**不中断运行中的执行**。
  - 若 `status == 'queued'` → `cancel_or_claimed`（`session_authority.py:748`）；若取消时发现已被 drain claim（`stale_generation`）则返回 started 行并 `interrupt(execution_generation)`。
- `session.interrupt`（`session_authority.py:865`）：校验 `execution_generation` 匹配（否则 `stale_generation`），`running` 时 `request_hard_interrupt(agent)` 硬中断 + `interrupt_for_session` 中断本会话后台 delegation。

因此 v0.22.5 上停止执行的标准序列：

1. `prompt.receipt(admission_id)` 查 `status`。
2. `queued` → `prompt.cancel(admission_id)`。
3. `started` → `session.interrupt(execution_generation)`（generation 取 receipt 的 `execution_generation`）。
4. 循环 `prompt.receipt` 直到 `status == 'terminal'`，读取 `outcome` 确认原执行确已结束，再释放占用。

### 3.5 更新工具凭据

| wire method | 关键参数 | 认证 | 返回 | 源码 |
| --- | --- | --- | --- | --- |
| `session.mutate` | `revision`、`operation`、`payload` | `session:control` | mutation 结果 | `session_controls.py:780`（dispatch :178） |
| `session.resume`（`editor` 分支） | `editor` | `session:control` | `SubscriptionSnapshot` | `session_controls.py:594` |

- 每执行工具凭据：`session.mutate` operation=`editor_mcp`，payload 精确 `{"mcp_servers": [...]}`（`gateway/session_local_mcp.py:28` `PRIVATE_KEY=('editor_mcp',)`、:85 `private_mutation_payload`）。
- 冻结时点：admission 起始 `refreeze_editor_mcp`（`session_local_mcp.py:11`），按 admission 冻结凭据。
- 恢复：`session.resume {editor: ...}` → `resume_editor_mcp`（`session_controls.py:596`）。
- 释放：`release_editor_mcp`（`session_local_mcp.py:219`）。
- 凭据：`gateway/session_policy_credentials.py`（frozen config credentials，`launch_credentials_unavailable`）。
- 每执行重绑定、旧凭据撤销、chat/work 工具权限隔离的闭环证据为**待运行验证**，不在本表源码证据范围内。

## 4. 隔离验证步骤（已授权，req#132 第一阶段）

用独立测试 profile、测试数据目录、可控测试任务。逐项保存请求、回执、实际执行记录。运行实验已授权（req#132），按任务链 #250→#251→#252→#253 分阶段执行。

1. **断线找回**：提交 `prompt.submit` 拿到 `admission_id` 后主动断开 WebSocket，重连 `session.resume`，用 `prompt.receipt(admission_id)` 确认拿到的是同一 admission 终态；再验「提交回执丢失」路径——同 `request_id` 同 payload 重提，确认幂等命中同一 `admission_id` 且未重复执行。
2. **停止确认结束**：`started` 执行上 `session.interrupt(execution_generation)`，随后 `prompt.receipt` 确认 `terminal`/`cancelled`，核对后端进程树已退出、无重复执行；`queued` 执行上 `prompt.cancel` 确认取消生效。
3. **凭据互不串用**：同网关连续两轮（第二轮新凭据生效、旧凭据失效）、两个并发会话，核对 `editor_mcp` 每执行凭据隔离、`chat`/`work` 工具权限准确。
4. **失败分支**：保留期满后 `prompt.receipt` 返回什么；epoch 变化后旧 `admission_id` 的 `receipt` 查询与 `interrupt` 边界；同 `request_id` 不同 payload 的重提返回（应 `admission_conflict`）。
5. **清理**：测试用独立 profile + 数据目录，不碰生产数据；结束释放测试进程与资源。

## 5. main 与 v0.22.5 的差异（另列，不承诺到稳定版）

对关键文件做 `git diff v0.22.5 origin/main`：

| 文件 | 差异 |
| --- | --- |
| `gateway/session_contract.py` | 无差异（数据模型与 SessionAuthority 协议一致） |
| `hermes_state_runtime.py` | 无差异（幂等/查询/取消/恢复语义一致） |
| `gateway/session_local_mcp.py` | 无差异（editor_mcp 一致） |
| `gateway/session_authority.py` | main 增量约 144 行 |
| `gateway/session_controls.py` | main 增量约 82 行 |
| `gateway/session_mutations.py` | main 增量约 19 行 |

**关键行为差异**：main 的 `prompt.cancel` 对 `started` 的 admission 会**自动调用 `interrupt(execution_generation)`**（`session_controls.py` 差异段，注释「Stopped in this RPC」）；v0.22.5 的 `prompt.cancel` 对 `started` 抛 `stale_generation`，必须由客户端单独调 `session.interrupt`。适配实现须按 v0.22.5 语义写（停止序列见 3.4），不可依赖 main 的自动中断行为。

## 6. 源码缺口（阻断生产准入，不阻断方案评审）

| 缺口 | 状态 |
| --- | --- |
| `request_id` 幂等保留期限（`session_admissions` 留存/清理策略） | 待源码核实 |
| 「按 request_id 只读查询」的公开 wire method（`find_session_admission` 为内部函数） | 保持缺口 |
| `session.resume` / `resolve` 权威重取语义 | 待源码核实 |
| `reset` / `rewind` 与 `/new`、尾部软删的等价映射 | 待源码核实 |
| `editor_mcp` 每执行重绑定、旧凭据撤销、chat/work 权限隔离闭环 | 待运行验证 |
| `receipt` 最终性强度（settle 后是否可修订） | 待运行验证 |
| epoch 失效后旧 admission 的 receipt 查询是否仍可用 | 待运行验证 |
| 0.22.3～0.22.5 各稳定版 wire 可用性（本表仅 v0.22.5） | 待运行验证 |
| 连接建立与认证的精确握手步骤（rendezvous record 读取、WebSocket 端点、identity 构造） | 待补（源码入口已确认，端到端握手待实测） |
