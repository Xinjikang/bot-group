# Hermes 网关执行迁移方案（第一阶段）

## 状态与依据

- 方案状态：第一阶段已立项（需求 #132《Hermes 网关适配第一阶段：/api/ws 隔离接入开发与验证》），进入隔离验证开发。
- 证据分层（本阶段强制）：**源码已确认**（v0.22.5 bf0203a7 逐行核对的接口位置、调用链、签名）与**运行待验证**（真实网关的启动认证、取消恢复、临时配置生效）分列表述；禁止将源码可调用表述成运行已通过，禁止以 mock 回执替代真实网关集成 PASS。
- 依据提交：Hermes `origin/main` 085f562a 与 `v0.22.5` bf0203a7（源码已核对）；本地运行安装仍为 v0.21.3 afc3b7c6f3（未触碰）。v0.22.5 稳定版调用表见 `docs/design/2026-10-27-hermes-gateway-v0225-call-table.md`。
- 关联：`docs/design/2026-10-27-hermes-compatibility.md`（v0.21.x 会话兼容，属旧范围）。
- 范围外（本阶段不做，后续按验证结果立项）：生产引擎执行路径迁移、生产部署/升级/切换、修改 Hermes 源码、自定义 Hermes 插件、ACP 接入、Codex 重构、profile 保留名。

## 1. 目标与范围

bot-group 引擎要在「直接运行版」与「网关版」两种 Hermes 下，正确完成提交、取消、对账、释放与重试。

核心变更：废弃「杀 CLI PID = 停止执行」的进程生命周期模型，改为统一执行契约——「提交取消/查询回执 → 确认终态 → 释放资源」。

首要验收项：**执行终态可确认**——停止或超时后取得原执行已结束的回执，再释放占用、清理凭据、安排重试；断线后先查询原执行状态。

## 2. 版本边界与协议选型

| Hermes 版本 | 执行方式 | 核对状态 |
| --- | --- | --- |
| 0.21.7 / 0.22.0 / 0.22.2 | 直接运行（CLI 进程即执行主体） | 版本边界已核对 |
| 0.22.1 | 网关（错误提交，0.22.2 纠正） | 版本边界已核对 |
| 0.22.3～0.22.5 | 网关 | 版本边界已核对（wire 可用性待实测，见 8） |
| main 085f562a | 网关 | 源码已核对（wire 方法/签名证据来源） |

- 未来版本按实际能力识别（运行时能力探测），不以版本号一刀切。
- **main 085f562a 的 wire 证据与 0.22.5 稳定版的可用性分列**：main 的 wire 方法/签名已从源码核对，但不等于 0.22.3～0.22.5 稳定版已确认同等可用；稳定版可用性属运行准入实测项。

直接运行模式的保留范围与适配边界：

- 0.21.7/0.22.0/0.22.2 继续沿用现有进程生命周期适配路径，但「杀 PID = 停止执行」收紧为「**核实对应执行进程树已退出后确认终止**」（仅发 kill 不算终态确认）。
- 两种模式共享上层约束：终态确认、重试决策、占用释放；各自提供不同形式的终止证据（直接运行=进程树退出；网关=网关终态回执）。

协议选型（入口关系与顺序）：

先澄清入口的真实关系（v0.22.5 源码核对，纠正此前「serve 是 UI」的错误结论）：

| 入口 | 是什么 | 启动/配置 | 是否 bot-group 执行接入面 |
| --- | --- | --- | --- |
| 网关进程（gateway runner）的 native `/api/ws` | hermes-gateway-v1 协议，`run_api.py start_gateway_api` → `GatewayDashboardMount._bind_state` 挂 session_authority（:70） | CLI/桌面端经 `ensure_gateway_runtime` 拉起 | 是（完整 SessionAuthority，含 editor_mcp） |
| 独立 serve 的 `/api/ws`（`web_server.start_server`） | 同 `/api/ws` 路由（`web_routers/chat_ws.py:701` → `handle_ws`），但 web_server 不挂 session_authority → legacy fallback（-32601） | `hermes serve` 启动（`cmd_dashboard` → `start_server`） | 是（会话/任务控制，editor_mcp 不可用） |
| serve 的 `/v1/*`（API server，HTTP） | OpenAI 兼容 HTTP，:8642 | 需配置 `API_SERVER_KEY` 才启用 | 是（/v1/runs 提交/状态/停止） |
| serve 的 `/api/mcp/servers`（REST） | 持久 MCP 配置管理（写 config.yaml） | serve 运行即暴露（dashboard auth 门控） | 是（固定 MCP 配置基线） |
| dashboard（浏览器 UI） | 另一个子命令（`build_dashboard_parser`），browser TUI | `hermes dashboard` | 否（UI，非程序化执行接口） |

关键澄清（authority 挂载差异）：`/api/ws` 有两条启动路径、能力不同——**网关进程（gateway runner）的 native /api/ws 挂了 session_authority**（`run_api.py:70` `_bind_state`），editor_mcp 临时注入可用；**独立 `hermes serve` 的 /api/ws 不挂 session_authority**（`web_server.start_server` 路径无此挂载，`chat_ws.py:717` 读到 None → `tui_gateway/ws.py` legacy fallback → -32601），editor_mcp 不可用。二者都叫 /api/ws，但 authority 挂载不同，不能等同。

MCP 配置的两种作用域（主人定调：固定配置为可接受基线，动态注入为加分项）：

| 作用域 | 机制 | 能力 | 源码 |
| --- | --- | --- | --- |
| 常驻配置（固定基线） | `/api/mcp/servers` REST（GET/POST/PUT/DELETE）写 config.yaml，持久生效；或 profile 配置 | 设置持久 MCP 配置，必要时重启/重连生效 | `web_routers/mcp.py:94/110/160` → `mcp_config._replace_mcp_servers:282` |
| 按执行注入（加分项） | `session.mutate editor_mcp`（WS 协议，依赖 session_authority） | 会话级临时配置，执行期间冻结，重启后需重新提供；仅 gateway runner 的 /api/ws 可用，独立 serve 无 authority 不可用 | `gateway/session_local_mcp.py` |

两套完整接法的对比（各自覆盖必要功能，不预设谁免写客户端）：

| 维度 | A 套：HTTP/Runs 接法 | B 套：`/api/ws` 接法 |
| --- | --- | --- |
| 启动形态 | gateway runner（官方启动，经 `ensure_gateway_runtime`） | 同左（gateway runner） |
| 任务提交 | `POST /v1/runs` → run_id | `prompt.submit` → admission_id |
| 状态查询 | `GET /v1/runs/{run_id}` | `prompt.receipt(admission_id)` |
| 停止 | `POST /v1/runs/{run_id}/stop`（硬中断） | `prompt.cancel`（queued）+ `session.interrupt`（started，分步） |
| 断线找回 | run_id 状态查询（幂等语义待核实） | 同 request_id 幂等重提 + receipt |
| MCP 固定配置 | `/api/mcp/servers` REST（HTTP） | 无需（editor_mcp 按执行覆盖）；若需固定基线也可经 REST |
| MCP 临时注入 | 无（/v1/runs 无请求级 mcp 参数） | `session.mutate editor_mcp`（会话级，依赖 session_authority） |
| 认证 | `API_SERVER_KEY`（需配置） | session ticket（本地生成） |
| 客户端 | HTTP 客户端（标准库） | WebSocket 客户端（websockets 库） |
| 恢复逻辑 | run 状态轮询 + 幂等 | 连接重连 + 订阅恢复 + event replay + 幂等重提 |
| 版本差异 | api_server 契约稳定性待核实 | session_contract 标 Public 为源码自我声明；authority/controls/mutations 有增量（82-144 行） |

B 套（`/api/ws`）部署证据（补齐「可独立覆盖」为可执行结论）：

1. **启动命令**：gateway runner 由 CLI 侧 `ensure_gateway_runtime(home, idle_exit=True)` 拉起（`gateway_client.py:262`），无客户端自行退出、下个客户端再拉起——官方启动形态，非独立 `hermes serve`（后者无 authority）。
2. **发现**：`GatewayEndpoint.api_origin`（`gateway_runtime.py:29`）为网关进程发布的 HTTP origin；WebSocket 端点 = `gateway_ws_target` 把 http→ws 拼 `/api/ws`（`gateway_client.py:42`）。
3. **认证**：`_session_ticket`（`gateway_client.py:225`）本地生成 session ticket（含 profile_id、instance_id、purpose），随连接提交；`AuthorityConnection` 按 identity 构造 Principal，instance_id 匹配才授予 capabilities。

详见调用表第 2 节连接方式。

- **两套都要写 bot-group 接入代码**：HTTP 客户端和 WebSocket 客户端都要写，客户端数量与传输类型不决定维护成本；WebSocket 库只负责连接收发，业务状态与协议适配由 bot-group 承担。
- **B 套（/api/ws）可独立覆盖必要功能**：`prompt.submit`/`receipt`/`cancel` + `session.interrupt` + `session.mutate editor_mcp` 覆盖提交/查询/停止/临时 MCP，无需额外搭配 HTTP 客户端；HTTP 仅列 WS 缺失的必要补充项（当前未发现硬性缺失）。
- **启动形态与功能覆盖分开**：serve 是启动命令，`/api/ws`、`/v1`、`/api/mcp/servers` 是接口路径；同名 `/api/ws` 在独立 serve（无 authority）与 gateway runner（有 authority）下能力不同，接入须用官方 gateway runner 启动形态。
- **维护成本不下结论**：两套的调用、恢复逻辑、部署步骤、版本差异已分列，谁更省待据引擎现有业务场景核对后定，不预设 HTTP 更简单或 WS 更贵。
- **ACP 不采用**：双协议带来两套会话标识与凭据管理方，与「会话写入单一归属」约束冲突。

## 3. 实体关联

各实体分别记录生成方、作用域与更新边界；不做字段等同。

| 实体 | 生成方 | 作用域 | 更新边界 |
| --- | --- | --- | --- |
| execution_id（本地执行身份） | bot-group | 一次执行唯一 | 提交前生成 |
| SessionRef=(profile_id, session_id)（会话身份） | 上游 | 会话唯一；一个会话可关联多次执行 | 会话创建时确定 |
| request_id（提交幂等键） | bot-group | (principal_id, session_id, request_id) + payload_digest | 同一次提交的传输重发复用原键+原 payload；业务重试换新键 |
| admission_id（上游执行身份） | 上游 admit_session_input | 一次 admission 唯一 | 提交时生成 |
| attempt（业务重试代次） | bot-group | 一次执行内递增 | 每次业务重试 +1，并生成新 request_id、关联新 admission |
| sequence（事件序） | 上游 | 事件顺序与去重 | 仅排序，不映射 attempt |
| generation（本地绑定代次） | bot-group | session_bindings 一行 | /new（switch_session）递增，使旧运行迟到回写被拒 |
| execution_generation（上游代次） | 上游 claim/settle | 绑定 admission 的 owner_epoch | claim/settle 递增，与本地 generation 分别保存关联 |
| authority_epoch（网关实例代次） | 上游 begin_runtime_epoch | 网关实例级 | 网关每次启动递增 |
| revision（会话修订号） | 上游 SessionHandle | 会话级 | 每次 mutate 递增 |

关键区分：

- **传输重发 ≠ 业务重试**。传输重发（回执丢失/网络抖动）复用原 request_id + 原 payload，幂等命中既有 admission；业务重试（一次执行终态后决定再跑）产生新 attempt + 新 request_id + 新 admission。混淆两者会把业务重试去重成旧执行。
- 本地 generation 是 session_bindings 的绑定代次（`/new` 开新会话时递增，旧运行迟到回写被拒），与上游 execution_generation / authority_epoch 分别保存关联，不做同步或等同。

## 4. 接口证据

三类状态：**已确认接口**（源码可见，签名已核对）、**待源码核实**（需进一步读符号/语义）、**待运行验证**（源码无法替代，须实测闭环）。

### 4.1 公开 RPC（wire method，hermes-gateway-v1 JSON-RPC）

从 RPC 分发入口 `gateway/session_controls.py` 的 `handlers()` dispatch map 追到实现：

| wire method | 会话契约方法 | 关键参数 | 认证能力 | 返回 | 状态 |
| --- | --- | --- | --- | --- | --- |
| prompt.submit | submit → admit_session_input | submission_id/input_id（即 request_id）、text、session_id | session:submit | AdmissionReceipt(admission_id, status, execution_generation) | 已确认 |
| prompt.receipt | receipt → get_session_admission | admission_id | session:read | AdmissionReceipt（可带 result） | 已确认 |
| prompt.cancel | cancel（内部路由 cancel_queued / interrupt） | admission_id | session:control | AdmissionReceipt | 已确认 |
| prompt.resolve_unknown | resolve_unknown → resolve_unknown_session_input | admission_id、execution_generation | session:operator | AdmissionReceipt | 已确认（状态变更） |
| session.interrupt | interrupt | execution_generation | session:control | SessionHandle | 已确认 |
| session.mutate | mutate → mutate_runtime_session | revision、operation、payload | session:control | result | 已确认 |
| session.resume | resume / resolve | — | session:create / resume | SessionHandle | 待核实（权威重取语义） |

认证能力集（`gateway/session_controls.py` AuthorityConnection）：`session:read` / `session:submit` / `session:control` / `session:approve` / `session:respond` / `session:create` / `session:operator`（operator 仅由已验证 transport gate 授予，不来自 identity 数据）。

### 4.2 request_id 幂等与提交恢复（关键）

- 调用链：`prompt.submit` → `authority.submit` → `admit_session_input(request_id, payload)`。
- 幂等语义（`hermes_state_runtime.py` admit_session_input）：同 (principal_id, session_id, request_id) + 同 payload_digest → 返回既有 admission（不重复执行）；不同 payload_digest → `admission_conflict`。
- 传输重发恢复：同 request_id + 同 payload 重提 `prompt.submit` → 返回同一 admission_id → 再 `prompt.receipt` 查终态。**此路径依赖留存期内幂等命中**（与 4.1 缺口、5.9 约束同源）；留存期未知时该路径同样阻断，转人工。
- 安全重提窗口受幂等留存约束：重提命中既有 admission 的前提是 admission 行仍在 `session_admissions`；留存期外重提会 INSERT 新 admission（创建新执行）——危险。
- `session_admissions` 留存/清理策略：**待源码核实**。**保留期未知时保持自动重提阻断，转人工。**

### 4.3 会话级 MCP（editor_mcp）

- 入口：`session.mutate` operation=`editor_mcp`，payload 精确 `{'mcp_servers': [...]}`（`gateway/session_local_mcp.py` private_mutation_payload）。
- 存储：servers 仅存 fingerprint，实际 servers 留在 owner 内存；scope 按 session 隔离（session_scope，不同 session/fork/profile 不共享）。
- 冻结时点：admission 起始 `refreeze_editor_mcp`（按 admission 冻结）。
- 凭据：`gateway/session_policy_credentials.py`（frozen config credentials，`launch_credentials_unavailable`）。
- 待运行验证：每执行凭据更新、旧凭据撤销、chat/work 工具权限隔离闭环。

### 4.4 内部运行时函数（`hermes_state_runtime.py`）

| 函数 | 语义 | 状态 |
| --- | --- | --- |
| begin_runtime_epoch(db, instance_id) | 网关启动递增 authority_epoch | 已确认 |
| admit_session_input(...) | 提交，幂等（见 4.2） | 已确认 |
| get_session_admission(db, admission_id) | 按 admission_id 只读查询 | 已确认（内部函数） |
| find_session_admission(db, principal_id, session_id, request_id) | 按 request_id 只读查询 | 已确认（内部函数） |
| list_session_admissions(db, session_id, pending_only) | 列 pending 只读查询 | 已确认（内部函数） |
| claim_session_input(...) | 认领，带 head 前置校验 | 已确认 |
| settle_session_input(...) | 写终态 outcome | 已确认 |
| cancel_session_input(db, epoch, admission_id) | 取消队列 admission | 已确认 |
| resolve_unknown_session_input(...) | **状态变更**（operator 确认 unknown→terminal，默认 interrupted） | 已确认 |
| recover_session_inputs(db, epoch) | 重启恢复：旧 epoch 的 started→unknown | 已确认 |
| mutate_runtime_session(...) | 会话修改，mutation_receipt 幂等重试 | 已确认 |

### 4.5 session.mutate 支持的 operation 与引擎现有行为对照

`gateway/session_mutations.py` 已确认 operation：`rename` / `archive` / `sidebar`（元数据）、`delete`（会话删除）、`model` / `reasoning`（策略）、`compress` / `reset` / `rewind`（上下文）、`import`、`branch`（fork）、`editor_mcp`（MCP 注入）。

引擎现有会话行为的目标归类：

| 引擎行为 | 语义 | 目标能力 |
| --- | --- | --- |
| compression_tip | 查找已压缩会话的续集 ID（解析 live tip），非主动修改 | 查询/解析能力（保留） |
| resolve_session_by_title | 按 title 反查现存会话 sid 并回填绑定（第 0 代兼容） | 查询/解析能力（保留） |
| create_session / switch_session | /new 创建新真实 session_id + 更新绑定 + generation 递增 | session.create（保留此行为） |
| _soft_delete_tail | 停止时软删消息尾部 | session.mutate（rewind/reset 等价性待核实） |
| 标题迁移 | 设会话标题 | session.mutate rename（operation 已确认） |
| 压缩续接 | 跟随压缩续集 | 查询/解析压缩续集（compression_tip 续集 ID 查询，非主动 compress） |

- `reset`/`rewind` 与「/new 建新 session_id」「尾部软删」的等价性：**待源码核实**。

## 5. 状态机与约束

状态：`idle / submitting / running / cancelling / unknown / manual / terminal`。

约束：

1. **终态吸收态、状态单调**：终态事实不被非终态覆盖；**过时事件**（身份/事件顺序检查不匹配）只入审计不回滚；**有效非终态回执推进状态**（submitting 收到匹配 started → running；unknown 收到有效状态恢复跟踪，取消意图与人工阻断继续生效）。unknown/manual 收到归属明确终态可推进，但推进与重试链决策解耦。
2. **写前日志**：提交/取消均先事务落盘本地意图、关联标识、绝对截止时间，再发请求。终态事实与后续处理待办同事务持久化，再执行释放/清理/调度；崩溃后重放未完成待办，内部动作靠唯一键与条件更新幂等。
3. **绝对截止时间（三个超时事件分开）**：提交确认截止（30s）、取消确认截止（30s）、对账截止（首次确认截止 + 120s）三个事件分开；全部使用已持久化的绝对时间戳，恢复时已过期立即升级、未过期按剩余时间，重复事件不延长等待。对账截止锚定首次确认截止时间。初值均为待验证参数。
4. **裁决源**：自动裁决只用公开只读接口（`prompt.receipt`，session:read 能力）且其一致性/最终性契约已核实；证据不足则保留未知并转人工。`resolve_unknown_session_input` 是 operator 状态变更，不在自动裁决链。operator 修复单列：授权（session:operator）、执行已停止的证据、操作审计、后续复核；「写成 terminal」不视为原执行确已结束的证据。
5. **自动对账可达性**：引擎通过公开协议实际可调的只读接口是 `prompt.receipt(admission_id)`。`get/find/list_session_admissions` 是网关内部函数，无公开 wire method 直达。「未拿到 admission_id 时按 request_id 只读查询」的公开路径当前不存在（唯一可达路径是同 request_id 幂等重提 `prompt.submit`）；该能力保持缺口状态，待核实上游是否暴露 request_id 查询。
6. **取消三态区分（v0.22.5 分步语义）**：本地取消意图与截止时间先落盘（写前日志）再发请求；远端受理只更新受理记录，不等同终态；原执行终态以网关回执为准。停止为客户端分步——先 `prompt.receipt` 判状态：`queued` 调 `prompt.cancel`（内部 `cancel_queued`，被 claim 竞态走 `cancel_or_claimed` 再 interrupt）；`started` 单独调 `session.interrupt(execution_generation)`；`prompt.cancel` 对 started 抛 `stale_generation`、不中断运行中。提交回执尚未取得时收到停止请求，也须落盘停止意图、阻断后续自动提交、对账原执行，取得 admission_id 后再按身份取消。main 的 `prompt.cancel` 会自动中断 started，属 main 增量、不承诺到稳定版（见调用表第 5 节差异）。
7. **客户端 unknown 与上游 unknown 区分**：上游报 unknown 是可查询对账信号；客户端 unknown 是未收到回执。分别记录。
8. **authority_epoch 失效**：epoch 变化（网关重启）后，恢复流程以 `session.resume`/`resolve` 权威重取语义核实成立为前提，再核查旧 admission 的查询与取消边界；重取语义未核实前该恢复路径保持待核实状态，原执行归属确认前保持自动重试阻断。
9. **幂等对账与安全重提**：回执丢失后同 request_id 同 payload 重提命中既有 admission（传输重发）；安全窗口受幂等留存约束，保留期未知时阻断自动重提。业务重试用新 request_id。禁止以「会话最新一条执行」猜测归属。
10. **释放幂等**：释放恰好一次，重复回执只消费一次；矛盾终态保留两份证据重查上游权威状态。
11. **重试链决策按触发原因拆开**：用户主动停止 → 终止本次重试链；可重试故障/超时 → 结合终态、重试预算、副作用对账决定；自然成功 → 结束该执行重试链。
12. **人工标记持续阻断自动重提**：已转人工的执行，迟到回执可更新终态事实、可走幂等清理资源，但自动重提保持阻断。
13. **撤权与停止分离**：撤销旧执行访问 bot-group 权限，与确认 Hermes 已停止执行分别记录。撤权前已受理的在途操作按服务端受理记录追踪；迟到结果不得更新新执行状态或触发重复释放。
14. **释放后才收到冲突**：标记会话恢复待处理、阻断后续自动派发，核查后继执行是否已启动及其副作用；禁止仅凭迟到冲突回执直接终止后继。

### 5.1 事件 × 状态转换矩阵

行=事件，列=当前状态；格内=目标状态 + 动作。终态后续动作统一引用 5.11/5.12 重试决策；terminal 列保留事件处理（幂等消费/过时审计/冲突对账）。

| 事件 \ 状态 | submitting | running | cancelling | unknown | manual | terminal |
| --- | --- | --- | --- | --- | --- | --- |
| 提交确认回执（匹配 started） | → running：推进执行中 | 过时→审计 | 过时→审计 | → running：恢复跟踪，取消意图/人工阻断继续生效 | 过时→审计（阻断不解除） | 过时→审计 |
| 有效非终态回执（身份匹配） | 维持（仅 started 推进） | 维持：审计跟踪 | 维持：审计跟踪 | → running：恢复可观测性 | 审计（不解除人工阻断） | 审计 |
| 终态回执 | → terminal：后续引用 5.11/5.12 | → terminal：后续引用 5.11/5.12 | → terminal：子类型以网关回执为准，后续引用 5.11/5.12 | → terminal：归属校验通过后推进，重试决策解耦 | → terminal：更新终态事实、幂等清理；自动重提仍阻断 | 幂等消费：重复终态只消费一次 |
| 过时事件（身份/顺序不匹配） | 只入审计 | 只入审计 | 只入审计 | 只入审计 | 只入审计 | 只入审计 |
| 本地取消请求（停止意图） | 落盘停止意图+截止，阻断后续自动提交，对账原执行 | → cancelling：先落盘意图+截止，再分步取消（receipt 判态：started→session.interrupt；queued→prompt.cancel） | 保持：已记录 | 落盘停止意图，对账原执行 | 落盘停止意图 | 落盘停止意图 |
| 远端取消受理 | — | — | 更新受理记录（不等同终态） | — | — | — |
| 提交确认截止（30s） | → unknown：保留占用，查对账 | — | — | — | — | — |
| 取消确认截止（30s） | — | — | → unknown：保留占用，查对账 | — | — | — |
| 对账截止（首次确认+120s） | — | — | — | → manual：转人工，阻断自动重试 | — | — |
| 冲突回执（矛盾终态） | — | → 冲突对账：保留占用，重查权威 | → 冲突对账：同左 | → 冲突对账：同左 | → 冲突对账：阻断自动重提 | 见 5.14：释放前保留占用 / 释放后阻断新派发并核查后继 |
| epoch 变化 | → unknown：引用 5.8，恢复待核实，保留占用 | → unknown：recover 把 started→unknown | → unknown：保留占用，核查旧 admission | 保持 unknown：引用 5.8，权威重取语义核实成立后再 resolve 重取 | 保持 manual | 保持 terminal：终态事实不变，旧 admission 查询边界待核实 |

## 6. 入口对账表

### 6.1 最初 10 项逐项对账

| # | 现入口 | 具体函数（调用方） | 去向 | 失败清理路径 |
| --- | --- | --- | --- | --- |
| 1 | wake 子进程 spawn | engine/wake.py | 改为 prompt.submit | 回执丢失→同 request_id 同 payload 传输重发（安全窗口内）→receipt 对账 |
| 2 | SessionDB 直读写 | engine/session_adapter.py `create_session`(:74)、`resolve_session_by_title`(:148) | 改为 session.create；查询/解析能力保留 | 创建失败→错误分类诊断 |
| 3 | taskkill 停 | engine/stop.py | 改为 prompt.cancel | 取消确认超时→unknown→对账→转人工 |
| 4 | proc.wait 判完成 | engine/wake.py `_watch_session`(:1112) | 改为 prompt.receipt 终态回执 | 断线→receipt 查询原执行 |
| 5 | PID 心跳 | engine/heartbeat.py | 废弃；改 authority_epoch + session.resume（条件成立后） | epoch 失效→重取 handle（待核实） |
| 6 | env 注入 MCP | engine/mcp_runtime.py `hermes_overlay`(:25)（清理 `cleanup_overlay`(:16)） | 改为 session.mutate editor_mcp | 凭据撤销→launch_credentials_unavailable 诊断 |
| 7 | 静态兼容检测 | engine/hermes_runtime.py `inspect_source`(:118)、`prepare_source`(:167)、`new_session_id`(:194) | 运行时能力探测（见 8）+ 版本边界表 | 探测失败→明确不兼容状态 |
| 8 | 会话尾部修改 | engine/stop.py `_soft_delete_tail`(:76) | 改为 session.mutate（rewind/reset 等价性待核实） | mutation_receipt 精确重试 |
| 9 | 压缩续接 / 会话绑定 | engine/session_adapter.py `compression_tip`(:127)、`_bind_key`(:122)、`switch_session`(:287) | 查询/解析保留；Hermes 会话写归网关，bot-group session_bindings 仍由引擎管理 | 读失效→回退 resolve |
| 10 | 执行账本 | engine/steer_ledger.py | 保留本地，关联上游 admission/generation/epoch | 账本与上游对账 |

### 6.2 新增项（另列）

| # | 项 | 具体函数 | 去向 | 说明 |
| --- | --- | --- | --- | --- |
| 11 | 重试链收尾 | engine/task_retry.py `finish`(:50) | 结合终态/重试预算/副作用对账 | 按触发原因拆开（见 5.11） |
| 12 | /new 命令 | engine/commands.py（调 `switch_session`） | session.create（建新真实 session_id + 更新绑定） | reset/rewind 等价性待证据 |
| 13 | 标题迁移 | engine/session_adapter.py 标题解析 | session.mutate rename | operation 已确认存在 |
| 14 | 失败清理 | stop.py `_kill_process_tree`(:50)、`_wait_dead`(:62)、`_soft_delete_tail`(:76)、`_release_tasks`(:163)、`_cancel_jobs`(:151)；mcp_runtime.py `cleanup_overlay`(:16) | 核实进程树退出后释放、软删尾部、释放中断 work、撤销临时 MCP 配置 | 处理条件：终态确认→释放占用；超时/对账无果→保留占用转人工；崩溃→重放待办幂等清理 |

`_bind_key` 仅计算 work/chat 的绑定键（work 的 thread_id 恒 0，chat 用真实 thread_id），不涉及会话写入；`switch_session` 才写绑定（新 session_id + generation 递增）。第 6.1 第 1 项的「重提 request_id」仅指传输重发，须满足安全窗口条件（幂等留存期内、同 payload），留存期未知时阻断自动重提。

## 7. MCP 注入门槛与降级预案

- 门槛：hermes-gateway-v1 的会话级 MCP 配置入口（editor_mcp）、每执行凭据更新、旧凭据撤销、chat/work 工具权限隔离，须实测闭环证据。
- 降级预案（按条件分层）：
  1. 原生协议缺所需 MCP 隔离能力 → 默认暂停该模式准入，阻断原因记录。
  2. 「每执行独立网关实例」仅列候选研究，需评估网关实例隔离、会话存储单一所有权、凭据继承、取消恢复、启动成本与资源回收，满足必要契约后才采纳。
  3. 支持范围受限 → 明确允许的版本/模式与阻断原因。

## 8. 能力探测与实测设计

### 8.1 能力探测（待补技术契约）

探测分层：

- **L1 静态识别**：源码符号（SessionAuthority、`gateway/session_contract.py`）仅作识别线索，**不兜底放行**。
- **L2 运行时握手**：连接网关，用无副作用只读方法探测可用性与协议版本。`ping` / `runtime.describe` 已确认存在于 v0.22.5（`session_controls.py:488/495`）；`session.list` 待核实。准确请求/响应字段/超时值/失败分类待以真实 wire 证据补齐。**判别 legacy fallback**：/api/ws 连接未挂 authority 时回落 legacy dispatcher（会话动作返回 -32601，`ws.py:370-388`），实测须确认会话动作走 authority 路径而非 legacy，否则拿 legacy 回执当网关回执误判。
- **L3 能力判定**：所需能力是否可用——`prompt.receipt`（session:read）、`prompt.cancel`/`session.interrupt`/`session.mutate`（session:control）、`editor_mcp`、认证能力集。判定以真实握手响应为准。

准入契约：静态符号存在与具体方法名推测**均不能替代准入契约**——运行准入以 L2 握手 + L3 能力判定的真实响应为准，静态通过不兜底放行。

待定稿项（需写出准确命令/字段/响应样例/判定条件/失败处理）：

- 启动/连接方式：连接已有网关 vs 启动独立网关。
- 探测的准确方法名、请求/响应字段、超时值（`ping`/`runtime.describe` 已核实存在于 v0.22.5，`session.list` 待核实；均须以真实 wire 证据替换）。
- 失败分类：连接失败 / 认证失败 / 能力缺失 / 版本不符 / 超时。
- 判定条件：看到什么特征判定为网关模式、什么判定为直接运行。

副作用边界：

- `prompt.submit` / `prompt.cancel` 带执行副作用，其调用验证归入隔离实验，生产自动探测不触发。
- 生产自动探测只用无副作用只读方法；静态通过不兜底放行，运行准入以 L2/L3 实测为准。

### 8.2 实测设计

隔离资源方案（第一阶段 #132，测试专用不碰生产）：

- 独立测试 profile 与独立 HERMES_HOME/数据目录：测试配置、会话、日志、MCP 子进程落独立目录，不与生产 profile 混用。
- 独立监听地址/端口：gateway runner 使用独立端口，避免与生产网关冲突。
- 可执行启动/认证步骤见第 2 节「B 套（/api/ws）部署证据」（启动 `ensure_gateway_runtime`、发现 `api_origin`+`gateway_ws_target`、认证 `_session_ticket`）；这些为源码调用链，实际部署操作与完整运行结果待本阶段实测。
- 清理步骤：测试后释放进程、凭据、临时配置与数据目录，资源使用与释放可核验（验收第 6 条）。

验证矩阵按版本表展开，运行准入核验实际能力，记录 Hermes 版本、提交、执行入口。

失败分支（必测）：

- 保留期满后 receipt 返回什么。
- epoch 失效后旧 handle / 旧 admission 的查询与取消边界。
- 相同 request_id 不同请求体的重提返回（应 `admission_conflict`）。

竞态用例：

- queued→running 窗口的取消（prompt.cancel 的 cancel_or_claimed 分支）。
- 取消与自然完成并发（终态子类型以网关回执为准）。
- epoch 变化（网关重启）下的恢复（resolve → recover → 对账）。

验收按触发原因拆开：

- 正常成功：提交→终态回执（completed）→释放→结束重试链。
- 主动停止：提交→prompt.cancel→确认终态→释放→终止重试链。**主动停止与自然完成竞态时可能收到 completed（非 cancelled），验收依据是终态正确、资源已释放、重试链已终止，不强制终态必须 cancelled。**
- 可重试故障/超时：提交→超时→对账→按终态与重试预算决定是否重试。
- 补齐：同网关连续两轮（新凭据生效、旧凭据失效）、并发会话互不影响、旧会话接管、网关重启状态恢复。

验收边界（隔离粒度与凭据传递，验证设计细化）：

- **隔离粒度不只 profile 层**：不同 profile 隔离只是其中一层。必须覆盖同一 profile 下的话题 A、话题 B、work 并发及连续执行，验证 project / thread / chat-work / 本次执行凭据各自准确；成员级配置隔离尚不能证明这些执行级归属。同 profile 并发需检查配置归属：A/B/work 共享一份可变配置时，即使每 run 重读也可能读到另一执行刚写入的值，故「连续两轮刷新成功」与「并发身份隔离」分别验收。
- **凭据传递链需实测（已收窄）**：HTTP 路线源码已证配置读取是每 run 粒度（`api_server.py:2315` 每请求 `_load_gateway_config()` + `:2316` `_get_platform_tools`；`:2354` 每 run 新建 `AIAgent`），非网关启动快照，配置快照层已排除。保留待实测的是「agent 重建后 MCP 子进程重连与凭据刷新」这一段（MCP server 子进程若常驻复用，改配置后新建 agent 是否重连子进程、旧连接是否沿用旧凭据）。XDG_ 变量穿过滤层仅证明变量获准传递，常驻网关下 env 传递路径未证，不放宽。
- **部署验证按路线列明**：`API_SERVER_KEY` 核对 HTTP 启用与认证路径；session ticket、authority/legacy fallback 判别核对 WS；网关存活与 idle_exit 分别核对两种连接行为。不把单一路径的要求写成共有前提。

证据分级：真实网关证据为集成验收门槛；单元测试可验证局部逻辑，但 mock 网关回执的单测不能替代真实生命周期链。

## 9. 能力缺口

| 缺口 | 状态 |
| --- | --- |
| request_id 幂等保留期限（session_admissions 留存/清理策略） | 待源码核实 |
| 「按 request_id 只读查询」的公开 wire method（find_session_admission 是内部函数，无公开路径） | 保持缺口状态 |
| `session.resume` / `resolve` 的权威重取语义 | 待源码核实 |
| `_soft_delete_tail` / `reset` / `rewind` 的等价映射 | 待源码核实 |
| editor_mcp 按执行注入（加分项）：每执行重绑定、旧凭据撤销、chat/work 权限隔离闭环 | 待运行验证 |
| receipt 最终性强度（settle 后是否可修订） | 待运行验证 |
| epoch 失效后旧 admission 的 receipt 查询是否仍可用 | 待运行验证 |
| 0.22.3～0.22.5 稳定版的 wire 可用性 | 待运行验证 |
| 能力探测的准确命令/字段/判定条件 | 待定稿补齐 |

## 10. 待决项

- 30s（确认）/ 120s（对账窗口）为待验证初值，具体值待与上游契约核对后定稿。
- 降级预案选型：默认暂停准入 vs 每执行独立网关实例（后者仅候选研究）。
- 协议单选项（hermes-gateway-v1）确认。
- 能力探测的准确命令/字段/判定条件（第 8.1 待补齐项）。
