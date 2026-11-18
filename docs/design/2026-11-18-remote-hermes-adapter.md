# 远程 Hermes 接入（ECS）：gateway 插件 relay 方案

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

## 3. 全程 relay 架构

引擎沿用现有 gateway 会话执行全链路，把 GatewayClient 的本地 socket 抽成 transport：本机 bot 走本机直连，远程 bot 走「引擎↔插件 WSS + 插件↔本机 gateway」帧转发。传输内容仍是现有 JSON-RPC 帧（session.create / session.configure / prompt.submit / stop 等），ledger、调度、UI 不动。

- **引擎侧**：transport 抽取（本地 websockets / 远程 relay 帧）+ WS 服务端入口（成员级认证）+ 节点连接状态 + 派发闸门 + 断线核对。会话由引擎创建并指定（`session.create`），`session_bindings`（project+bot+kind+thread→session_id）原样复用；`editor_mcp` 注入沿用现有 `session.mutate(editor_mcp)` 路径；执行回执沿用 `hermes_executions` / admission / `submission_id` 全链。
- **插件侧**：持一条出站 WSS（节点/连接凭据握手 + generation fencing）；本地控制通道取票（`identify` → `session-ticket` → 三项对齐：`profile_id`/`instance_id`/`runtime_protocol` 相等校验；`api_origin` 在端点发现时检查；`authority_epoch` 做正整数/语义检查并用于回执 fencing，取票时不比对）；转发帧给本机 gateway `/api/ws`；上报代码版本与 MCP 包内容摘要。插件与 gateway 同进程，连本机 `/api/ws` 必须独立线程/事件循环（取票是带超时的阻塞 pipe/socket 调用、RPC 等待同样阻塞，放 gateway 事件循环会卡住所有会话）。票据单次短时效，每次建本地连接重取、不缓存。relay 帧带 home/stream 标识，按 home 维护本地连接；不同 home 的 JSON-RPC id 各自从 1 开始，压进一条流会串；事件帧无 id，靠流标识归属。
- **MCP 随包交付**：插件随包带 `mcp/server.py` + 依赖，安装时配一次本地 python 路径/目录；引擎按节点配置渲染 MCP 启动命令与 env，经 relay 帧下发，插件原样转发。配置指纹对随包 MCP 入口的**内容摘要**（内容 hash/包级 digest）计算，握手时上报，引擎按版本拒绝不兼容节点；configure 前复核一次。升级验证分两种：正常升级（重启 gateway/插件，WS 断开重来）与「连接不断、磁盘上 MCP 包被原地替换」（后者才暴露缓存 bug）。

### 3.1 为什么不做 adapter 数据面（选型证据链）

首版评估过「控制面 relay + 数据面 adapter（平台入站消息投递）」的组合方案，被绑定接口证据否定（2026-11-18 bot-36 源码核查）：

- 引擎经 `session.create` 建的会话登记为 `Platform.LOCAL`（session_local.py:370-375）；平台 adapter 的 `build_session_key` 把平台名编进路由键（session.py:728 布局 `<ns>:<platform>:<chat_type>...`）——插件平台入站事件派生的路由与引擎建会话的路由不同名，chat_id 相同也命中不了。
- `metadata.gateway_session_key` 只核对派生值，不建立绑定、不保证严格投递。
- `/resume` 校验目标会话归属，普通成员仅凭会话 ID 无法把引擎建的 LOCAL 会话接到平台话题。
- `handoff.request` 只收 session_id/profile/platform，目标固定为该 profile 在该平台的默认频道，无法指定本次点名的 chat_id/thread_id。

在「引擎建会话 + 只用公开接口 + 不改 Hermes 源码」三条约束下，没有可证明的绑定入口，adapter 数据面不进基线。若后续发现别的公开绑定入口（如配置级 channel→session 映射），再回头评估直连；拿到证据前不写进设计基线。

## 4. 关键契约

- **断线语义**：节点离线 = 暂停向它派新执行，在途执行保留「待核对」（ledger 已有 `unknown`/`terminal` 分离，终态只由 `apply_receipt` 产生）；重连后先 `session.resume`/对回执，再决定是否重投；90s TTL 只用于存活判断，绝不能据此判任务结束重跑。别用「伪造进程退出」实现断线——gateway 执行不走 `_handle_task_exit`，伪造退出会把 task_retry 和话题弃权带进来，正好造成误重跑。
- **派发闸门**：`process_one` 加节点维度——节点离线或达并发上限时 `continue` 保留 queued（busy 同款），**绝不走 `_discard_queued`**（那个分支会把 status 直接置 done，活就丢了）。容量按 `hermes_executions.state != 'terminal'` 计数、receipt 终态释放。
- **离线期间的 @**：节点离线 → 心跳到不了引擎 → 发言位超时弃权推进 → queued chat job 派发时 turn_profile 已变 → `_discard_queued('superseded topic turn')` → `settle_thread` 把该话题 pending 全置 expired = 离线期间的 @ 静默消失。防御：节点离线对发言位超时逻辑可见 + 重连后按原话题状态补投/对账；发言位照常超时推进、不挂起 turn（挂起=整个话题卡住等离线节点）。
- **凭据**：连接凭据常驻 ECS、绑定成员、可单独吊销；执行凭据沿用引擎现有 per-execution 收窄（execution token 绑定 project/member/chat-work）。ECS 长期不预置成员 token。
- **fencing**：连接 generation 随派发下发，插件拒旧代帧；重连后只让当前连接接新消息。
- **时序**：configure 回执先于 prompt.submit（引擎现有串行链路天然保证）。
- **首版边界**：成员级可判忙闲；节点（机器）级并发上限首版不承诺。

## 5. 首版范围

- 只支持 hermes runtime；一名成员固定一台机器（一条连接）；不跨节点迁移。
- 依赖引擎主机直接读写文件的本地管理功能首版明确提示不支持。
- 旧本机 spawn/网关链路保留不动；退役评估留到 relay 覆盖本机+远程、能力对等后。
- 首版不做：outbox 重试权、迟到结果归属、messages.kind、skip 原因标记、进度状态通道（relay 有 submission_id 回执链，这些机制不需要；进度沿用思考条/心跳）。

## 6. 验证清单

1. **端到端唤醒**：空闲网关收到群里 @ → 引擎经 relay 建/配会话并提交 → Hermes 回复原话题（从零手工配置启动 gateway，MCP 随插件自动注入正确成员/话题权限）。
2. **断线重连核对**：接单后、回执送达引擎前断线 → 重连后按 submission_id 核对原执行 → 同一派发最多执行一次；断线期间不派新活、重连核对通过后恢复。
3. **插件独立线程连本机 /api/ws**：取票+阻塞 RPC 不卡 gateway 事件循环；identify 身份与 ticket 请求一致。
4. **generation fencing**：旧连接晚到的帧被拒；重连后只让当前连接接新消息。
5. **多成员同机隔离**：同一 ECS 两个 profile 各收一次点名，回复与事件各自回到各自话题。
6. **升级重核摘要**：连接不断、原地替换 MCP 文件，系统重核摘要再配会话。
7. **/stop**：停只停指定执行，不误伤同 profile 并行会话（复用现有叫停链路，验证远程 transport 下有效）。

## 7. 源码依据（已核）

- 引擎侧：`engine/hermes_gateway_client.py`（取票链 identify/session-ticket/对齐）、`engine/hermes_gateway_ledger.py`（state!='terminal' 计占用、unknown/terminal 分离、apply_receipt 终态）、`engine/queue.py`（process_one 判定链无节点维度、_discard_queued 置 done）、`engine/task_access.py`（WORK_ONLY 依赖 execution.thread_id 非空）、`engine/router.py`（settle_thread 置 pending expired、advance_timed_out_turns、_next_speaker）。
- Hermes 侧（本机安装）：`gateway/session_local.py:370-375`（session.create 登记 Platform.LOCAL）、`gateway/session.py:728`（build_session_key 平台名编入路由键）、`gateway/platforms/base.py`（handle_message 无返回值、gateway_session_key 只核对不绑定）、`gateway/session_mutations.py`（_POLICY 含 editor_mcp）、`gateway/session_local_mcp.py`（bind/install/prepare_editor_mcp）、`plugins/platforms/mattermost`/`feishu`（插件常驻持连接的先例）、`gateway/session_automation.py:384`（automation 路径 gateway_session_key 优先于 source 派生）。
- 绑定入口否定证据（bot-36 2026-11-18 核查）：`/resume` 校验归属、`handoff.request` 目标固定默认频道。

## 8. 明确不采用（红线与边界）

- **改 Hermes 源码**（主人明令禁止）：`_redeliver_claimed_obligations` 透传原事件编号的方案直接排除，不在候选/兜底清单内。
- 读 Hermes 内部送达记录反查原编号：内部接口、版本耦合，不作设计依赖。
- 独立 daemon 进程形态：被 gateway 插件形态取代（主人拍板）。
- adapter 数据面（平台入站消息投递）：绑定接口证据不存在，不进基线；等公开绑定入口证据出现再评估。

## 9. 遗留决策点

- 旧本机执行链退役时机：等 relay 覆盖本机+远程、能力对等后另行评估。
- 本地与远程统一：同一 runtime 两套语义（本机 spawn/网关直连 vs 远程 relay）并存期间测试维护成本，留待退役评估。
