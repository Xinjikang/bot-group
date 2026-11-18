# 远程 Hermes 接入（ECS）：gateway 插件三平面方案

日期：2026-11-18
状态：方案定稿（讨论收敛于话题 #10046 系列消息，等待需求立项）
范围：bot-group 引擎 + ECS 上 Hermes gateway 插件

## 1. 背景与目标

主人诉求：让 ECS 上的 Hermes 接入 bot-group。核心用法：在群里 @ 它 → 它在 ECS 上干活 → 结果回到原话题。首版只接 Hermes，一名成员固定一台机器。

主人明确过的前提（话题内逐条拍板）：

1. ECS 上的 Hermes gateway 本来就常驻运行，唤醒 = 唤醒会话，不是启动 gateway。
2. 「Hermes 连 bot-group」指 WS 连接方向（ECS 主动出站），连接建立后双向都可发消息。
3. 会话由 bot-group 引擎创建并指定，不是网关自己找会话。
4. MCP 集成进插件：用户装一次插件、填一次配置，不单独配 MCP、不复制成员 token。
5. 本地也能用同一套组件，长期可让引擎退役 spawn 路径（退役评估留到直连覆盖本机+远程后）。

## 2. 方案形态

ECS 上 Hermes gateway 常驻（systemd 托管），bot-group 插件随 gateway 加载。插件作为 WS 客户端主动连 bot-group（出站 WSS），bot-group 是服务端（引擎新增带认证的 WS 入口）。一个成员一条连接，引擎按成员认证派发。

插件在 gateway 进程内，职责收窄为「传令」：不解析会话语义、不复制引擎逻辑（越笨越稳）。

## 3. 三平面结构

### 3.1 控制面：relay（建会话/配置/停止）

引擎经插件转发标准 gateway RPC 帧到 ECS 本机 gateway `/api/ws`，只做三件事：`session.new`、`session.configure`（含 editor_mcp 注入）、必要时 stop。

- 复用现有 `GatewayClient` 取票链：本地控制通道 `identify` → `session-ticket` → 三项对齐（`profile_id`/`instance_id`/`runtime_protocol` 相等校验；`api_origin` 在端点发现时检查；`authority_epoch` 做正整数/语义检查并用于回执 fencing，取票时不比对——命名 profile 可与启动 profile 不同 epoch，强求五项相等会误拒）。
- 票据单次短时效，每次建本地连接重取，不缓存。
- 插件与 gateway 同进程，回连本机 `/api/ws` 必须独立线程/事件循环（取票是带超时的阻塞 pipe/socket 调用，RPC 等待同样阻塞），放 gateway 事件循环会卡住所有会话。
- relay 帧带 home/stream 标识，插件按 home 维护本地连接；不同 home 的 JSON-RPC id 各自从 1 开始，压进一条流会串；事件帧无 id，靠流标识归属。

### 3.2 数据面：adapter（消息投递与回复）

引擎把「会话 key + 消息」交给插件，插件构造为平台入站消息（MessageEvent）投进指定会话；回复沿原连接回原话题。复用 Hermes 平台 adapter 契约（Mattermost/飞书先例：插件常驻、WS 收消息、交网关起会话）。

- session key 由 `build_session_key` 从 source（chat_id + profile）纯函数派生。adapter 把引擎下发的会话标识稳定编码进 chat_id，即可每次命中同一会话。
- `handle_message` 支持 `metadata.gateway_session_key` 显式路由，派生不一致直接丢弃——**命中不到就丢弃，绝不回退自动建会话**（防止野会话、破坏「引擎建会话」约定）。
- gateway_session_key 双路径已源码坐实（2026-11-18）：平台 adapter 路径（`gateway/platforms/base.py` handle_message）里它是**校验值、不绕过派生**——事件必须仍从 source 派生出目标 key，不一致即 drop，所以 chat_id 稳定编码会话是必须的；另一条 automation 路径（`gateway/session_automation.py:384`）里 `metadata['gateway_session_key']` 优先于 source 派生（存在绕过实例）。首版走平台 adapter 标准路径（chat_id 编码+metadata 校验），实测时一并确认两条路径差异后再决定是否可用 automation 路径。
- 投递失败回执：adapter 投递前做本地会话存在性检查（`session.list` 公开 RPC 面）+ start 回调超时兜底；失败用 `sender_type='engine'` 消息发进原话题（不吃发言位），用户直接看到「未送达」。
- 引擎 thread_id → chat_id 的稳定映射是 adapter 核心契约，与 `session_bindings` 对账，设计实现时定死编码规则。

### 3.3 状态面：dispatch 表 + outbox

- dispatch 表：派发编号、范围（project/member/chat-work）、状态。引擎侧 `dispatch_id` 唯一约束，结果写入与状态更新同事务；确认丢失重发返回已有记录不重复插入。
- outbox 三态：`pending`（已落盘未确认）/ `delivered`（引擎已确认）/ `unknown`（已发但回执丢失）。重连时 pending 带编号重发、unknown 先按编号查引擎再决定、delivered 不动。
- 处理回调：`on_processing_start` / `on_processing_complete` 上报进度与占用。

## 4. 关键契约

### 4.1 时序

`configure` 回执必须先于数据面投递（引擎串行等回执再投，写成硬约束），否则 agent 用默认 MCP 跑。

### 4.2 凭据（两段式）

- 连接凭据：常驻 ECS、绑定成员、可单独吊销。
- 派发凭据：短时效、每次下发、绑定 project/member/chat-work 范围。服务端在写操作上按凭据范围校验——**凭据范围才是执行边界，工具隐藏只是 UX 层**。
- `HERMES_CHAT_MODE` 只负责工具不注册（客户端层）；服务端按派发凭据范围拒绝话题身份的工作写操作。

### 4.3 送达状态机

- 「未送达」仅限可证明未交付：插件未交出（预检失败）、公开接口明确拒收。
- 其余一律「送达待确认」：消息交出去后无 start 回调，Hermes 可能已开工只是回执晚，按原派发编号核对，绝不重投。
- `_gateway_accepted` 是下划线内部字段、无公开契约，只进日志/诊断，不作状态判据。

### 4.4 幂等（at-most-once）

- 同一派发最多交给 Hermes 一次。投递前把派发编号可靠落盘。
- 插件在能识别原派发时，编号+最终正文一起持久保存（outbox），写成功才向 Hermes 确认接手；之后按编号重发已保存结果。
- 收到无编号的恢复回调（`_redeliver_claimed_obligations` 只传 chat_id/正文/thread_id，不传原事件编号），单独标「待核对」；自动归属只凭编号，正文只给人看。
- 同文可能以「正常结果+待核对」两条出现，由人消解——不改 Hermes 源码的必然代价，验收标准写明。

### 4.5 结果认领与轮次

- 出站信封 `{dispatch_id, thread_id, text}`，最终回复用明确的 final 标记（`send_final_ledgered`）认领结果；一次派发只发一条最终消息，其余输出走状态通道。
- 进度不得作为新消息入话题（硬约束+用例断言）：树模型「发言=表态」，进度消息会吃发言位，把最终结论挤成迟到结果。
- 迟到结果按原派发编号挂回原点名展示、不触发新轮次；话题已收口/关闭只更新原派发记录。
- 断线期间：发言位照常超时推进、不挂起 turn（挂起=整个话题卡住等离线节点）；弃权原因标记「离线」（skip 槽位加原因，skip 那一刻用当时节点状态写标记——skip 有超时弃权与主动 pass 两个触发源，只有超时弃权需区分离线）。
- 断线链路完整防御：节点离线无心跳 → 超时弃权 → queued job 派发时 turn_profile 已变 → `_discard_queued('superseded topic turn')` 置 done → `settle_thread` 把该话题 pending 全置 expired = 离线期间的 @ 静默消失。防御=节点离线对发言位超时逻辑可见 + 重连后按原话题状态补投/对账。

### 4.6 调度与容量

- 派发闸门按成员连接状态：离线暂停派新活、保留在途占用；容量按 dispatch 非终态计数，终态释放。
- fencing：连接 generation 随派发下发，adapter 拒旧代帧；重连后只让当前连接接新消息。
- 节点（机器）级并发上限首版不承诺（成员级可判忙闲）。

### 4.7 MCP 随包交付

- 插件随包带 `mcp/server.py` + 依赖，安装时配一次本地 python 路径/目录；引擎按节点配置渲染 MCP 启动命令与 env，插件原样转发。
- 配置指纹对随包 MCP 入口的**内容摘要**（内容 hash/包级 digest）计算，握手时上报，引擎按版本拒绝不兼容节点；configure 前复核一次。升级验证分两种：正常升级（重启 gateway/插件，WS 断开重来）与「连接不断、磁盘上 MCP 包被原地替换」（后者才暴露缓存 bug）。

## 5. 首版范围

- 只支持 hermes runtime；一名成员固定一台机器（一条连接）；不跨节点迁移。
- 依赖引擎主机直接读写文件的本地管理功能首版明确提示不支持。
- 旧本机 spawn/网关链路保留不动；退役评估留到直连覆盖本机+远程后。
- 引擎侧改动面：WS 入口+成员级认证、控制面 transport 抽取、dispatch 表、派发闸门按成员、断线补投与 skip 原因标记。
- 首版不做：messages.kind 全局改造（进度不进话题流已消解）、节点表（按成员连接已消解）。

## 6. 验证清单

1. **A→B 会话切换**（决定性）：同话题绑会话 A，引擎切到 B，下一次 @ 进 B；B 不可用时引擎收到失败回执；无野会话。通过标准（bot-36/bot-43 2026-11-18 定）：从会话记录取证——投递后 B 的历史里有这条消息、A 的历史没有、会话总数不变。bot-36 本机模拟会话表路由分支已验：绑 A 时指定 B→拒收、已绑 B→选 B、无绑定→拒收，三次均无野会话。测试标记放 metadata，验证端从 B 的记录读回。
2. **handle_message 路由语义**（已源码坐实 2026-11-18）：无返回值、丢弃分支 return None；`_gateway_accepted` 仅诊断。投递前预检 + start 回调兜底。
3. **on_processing_start 入参关联**（已源码坐实 2026-11-18）：回调签名为 `on_processing_start(self, event)`，入参是完整 MessageEvent——adapter 构造 event 并自持 event→dispatch 映射即可关联派发编号。**注意（bot-36 实测发现）**：start 回调在真正消息处理函数之前触发，收到「开始」不能证明目标会话实际处理过——路由判定必须从会话记录取证（见第 1 条通过标准），start 回调只用于「已开工」信号与超时兜底。
4. **断线重连**：回执前断线 → 重连按编号核对 → 同一派发最多执行一次；补送保留编号；两次同文本派发各自归回原点名、最多展示一次、歧义标待核对。
5. **插件独立线程连本机 /api/ws**：取票+阻塞 RPC 不卡 gateway 事件循环；identify 身份与 ticket 请求一致。
6. **MCP 注入**：控制面 editor_mcp（`session.mutate(editor_mcp)` 路径已确认存在）；数据面按会话 MCP env；验证从零手工配置启动 gateway → @ → 会话自动获得正确成员/话题权限并回复原话题。
7. **进度不进话题流**：A 发进度、B 接位时，话题流不出现 A 的进度消息。
8. **插件升级重核摘要**：连接不断、原地替换 MCP 文件，系统重核摘要再配会话。

## 7. 源码依据（已核）

- 引擎侧：`engine/hermes_gateway_client.py`（取票链 identify/session-ticket/对齐）、`engine/hermes_gateway_ledger.py`（state!='terminal' 计占用、apply_receipt 终态）、`engine/queue.py`（process_one 判定链、_discard_queued 置 done）、`engine/task_access.py`（WORK_ONLY 依赖 execution.thread_id 非空）、`engine/router.py`（settle_thread 置 pending expired、advance_timed_out_turns、_next_speaker）。
- Hermes 侧（本机安装 v0.22.5 系）：`plugins/platforms/mattermost/adapter.py`（connect + 后台 WS 循环）、`plugins/platforms/feishu`（websocket 连接模式 + 重连配置）、`gateway/platforms/base.py`（handle_message、_event_session_key、_start_session_processing、gateway_session_key 显式路由）、`gateway/session_mutations.py`（_POLICY 含 editor_mcp）、`gateway/session_local_mcp.py`（bind/install/prepare_editor_mcp）。

## 8. 明确不采用（红线与边界）

- **改 Hermes 源码**（主人明令禁止）：`_redeliver_claimed_obligations` 透传原事件编号的方案直接排除，不在候选/兜底清单内。
- 读 Hermes 内部送达记录反查原编号：内部接口、版本耦合，不作设计依赖。
- 独立 daemon 进程形态：被 gateway 插件形态取代（主人拍板）。
- 全程 relay（含 prompt.submit 转发）：仅作「控制面+数据面组合验证失败」时的兜底选型。

## 9. 遗留决策点

- 旧本机执行链退役时机：等直连覆盖本机+远程、能力对等后另行评估。
- 「结果待核对」UI 展示形态（dispatch 面板 vs 原点名挂载）。
