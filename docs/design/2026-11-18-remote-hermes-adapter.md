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

### 2.1 目录布局（bot-36 2026-11-18 拟定）

- 插件源码：bot-group 仓库 `plugins/hermes/bot-group-relay/`，随 bot-group 版本发布。
- **发布资产（2026-11-18 bot-43 定，回应主人「ECS 装插件不必下整仓」）**：发布工具（`engine/maintenance/make_release.py`）产第二个资产 `bot-group-relay-<version>.tar.gz`，只含 `plugins/hermes/bot-group-relay/` + `mcp/` + `requirements.lock` + 安装脚本，自带 manifest 与 SHA256，与主包同一 tag 构建。ECS 下载解到 `$HERMES_HOME/plugins/bot-group-relay/`，安装脚本建 venv 按锁文件装依赖——不 clone 仓库、不手动 pip。
- **协议版本配套（2026-11-18 bot-43 定）**：插件握手带插件包版本与中继协议版本，引擎不匹配即拒——插件必须与引擎同 tag 配套；MCP 入口形态不变（不改 wheel，`mcp/server.py` 原样随包）。
- MCP：沿用仓库现有 `mcp/` 为唯一源码，打包时随插件带上，只维护一份。
- **MCP 运行解释器（2026-11-18 定）**：插件自带独立 venv——安装时创建并安装依赖（依赖清单随插件包分发，含 mcp>=1.4/httpx/psutil/fastapi/uvicorn/websockets/pyyaml/ruamel.yaml/tomlkit/Pillow/python-dotenv 等，见 mcp/requirements.txt）。不用 Hermes 自带 venv（已核：本机 Hermes venv 无 mcp 包）；安装时配一次、零手动 pip。握手时上报解析后的解释器绝对路径、MCP 入口绝对路径、版本与内容摘要，引擎据此渲染 session.configure。
- ECS 安装位置：运行 gateway 的 Hermes 用户插件目录 `$HERMES_HOME/plugins/bot-group-relay/`（默认 home 即 `~/.hermes/plugins/bot-group-relay/`），由 Hermes gateway 加载。
- 引擎侧接收连接与派发的代码在 bot-group `engine/`。

### 2.2 双向接入（2026-11-20 主人 12589/12595/12619/12637 定调，#173 执行基线）

- **范围**：本机引擎（办公/家用电脑）↔ 个人云端 Hermes（ECS）。产品不运营任何公网配对/中继服务（主人 12583）；远程节点是用户自己的机器。
- **规则：谁可达，谁监听**（与公网/局域网/组网工具无关）：
  - 方向 A「引擎监听」：引擎侧有可达入口时，插件 `register` 主动连引擎 WS 端点注册（#168/#315/#316 已交付基础链路）。
  - 方向 B「插件监听」：云端有可达端口时，插件 `listen` 开监听（绑本机具体网卡地址，禁 0.0.0.0/:: 通配；云上公网 IP 是 NAT 映射不能当 bind，监听绑网卡私网地址、页面给对端公网地址/域名），引擎出站拨入。
- **插件两工具**（主人 12612）：`listen` / `register`；CLI 子命令一一对应；Hermes 管理员私聊可用同一操作（私聊按渠道发送者身份核验、CLI 按授权本地 OS 身份核验，共用签发逻辑与审计）。日志只记签发事件，不记链接/凭据；非交互终端链接写 0600 文件。
- **配对**（自托管）：一次性短时配对链接含入口地址+证书指纹+一次性凭据；导入时先显示设备身份与短指纹（8 位可读值）供确认，兑换原子、首次成功即作废；配对身份/公钥跨方向复用，切方向不重配对。可达性/证书在导入时校验，失败明确报错不保存。
- **首次接入（req#175）**：两种方向都读取各自 Hermes 宿主的当前 profile，首次接入不接受手填成员覆盖。管理员确认后，引擎为每个新远端登记独立的成员标识（node_member）和 Bot。宿主 profile 用于握手，成员标识用于项目入组、点名、来源绑定和连接切换。同名本机和多个远端可以共存。旧节点沿用原成员标识。
  - 「云端连本机」：`POST /relay/invitations` 签发短时单次邀请，返回 `pairing_id`（字符串，如 `7b6a3f32818987c9a174cc14dbe11736`）、`invite_token`（秘密字符串，只返回一次）、`expires_at`（Unix 秒数）、`engine_url`（字符串）和 `listen_configured`（布尔）。邀请可先签发；连接前通过 `/admin/listen` 启用本机具体网卡，空绑定也可启用。未绑定地址只能在有效邀请窗口进入 `/relay/ws` 递交握手，其他路径仍按来源拦截。有效握手锁定本次宿主身份和实际 socket 来源，不建立节点、有效连接凭据、来源绑定或 relay 通道。
  - **确认接口（云端连本机）**：管理员 `GET /relay/invitations/{pairing_id}` 查询；`POST .../confirm`、`POST .../cancel` 无请求正文，不接收成员或来源参数。响应含 `pairing_id`（字符串）、`state`（`waiting` / `pending_confirmation` / `confirmed` / `failed` / `cancelled` / `expired`）、`direction: "listen"`、`expires_at`（Unix 秒数）、`member` 和 `peer_ip`（待确认、已确认或重复配对失败时为字符串，其余为 `null`）。重复提交已登记的凭据承诺时返回 `reason: "name_conflict"`，`stage` 为 `identity`（首次握手）或 `confirmation`（确认竞态）；不消费邀请、不开放来源，也不修改旧节点。无效邀请、来源或证明不能改变其他尝试的状态。已确认的 GET 另返回 `connected`（布尔）；不存在 404，非法状态或重复配对 409，非管理员 403。SQLite `BEGIN IMMEDIATE` 内复核并一次性消费、登记、绑定来源；重复确认只读原结果。
  - **中断恢复**：插件将本轮临时材料保存在本机，仅在管理员确认后持久化连接凭据。回执丢失，在邀请有效期内以同一实际来源和原配对材料重启 `register`，查询已确认状态后以持久凭据正常重连；确认不再执行，回执不传凭据。无原配对材料或超过恢复窗口时，由管理员在已确认节点上重签连接凭据并重新配置插件。内置 HTTP/WS 仅用于可信私网，跨不可信网络使用受信 TLS 入口。
  - **本机连云端**：管理员 POST `/relay/pairings/preview`，正文必填字符串 `pairing_url`（远端插件签发的 WSS 链接）；引擎先核验证书指纹，再验证对端插件上报的宿主成员、配套协议和未使用的票据。核验不兑换、不写正式节点、不建数据通道；成功返回 `state: "pending_confirmation"`、`pairing_id`（32 位十六进制字符串）、`direction: "dial"`、`member`（如 `office-hermes`）、`host`（字符串）、`port`（整数）、`tls: true`、`fingerprint_short`（8 位十六进制字符串）、`expires_at`（Unix 秒数，不超过 120 秒和链接剩余有效期）。本机无需开启监听。
  - **出站确认与取消**：管理员 GET `/relay/pairings/{pairing_id}` 读取同一结果，POST `.../confirm` 携带同一 `pairing_url`，POST `.../cancel` 不带正文。页面仅在当次表单内暂存原始链接，不放在 URL、持久浏览器存储或日志。预览和确认复核都只读核验，插件不预留、不消费票据；未确认的引擎只存脱敏端点、链接摘要和截止时间，不生成连接凭据。SQLite `BEGIN IMMEDIATE` 内复核原链接、到期、取消和同一 profile、证书指纹的重复配对，只有提交确认事务才为节点生成凭据并开始拨号。失败、取消和过期不留可激活预留；截止时记录 `expired`，可用原 ID 读取，不依赖再次预览回收。仍有效的链接可立即重新预览和确认。
  - **首次激活和恢复**：确认后的首次连接在固定证书指纹的 WSS 上带原票据，插件校验其时限和身份并原子兑换、启用已登记凭据。引擎收到认证回执即清除一次性票据；票据到期后由拨号检查或后台扫描清除，即使首次拨号未启动也会回收。回执丢失时，下次拨号优先用同一已生效凭据认证，不二次兑换。`confirmed` 表示已登记，GET 的 `connected` 布尔值另表示是否在线；登记后若原链接未及激活且已到期或被换发，使用现有节点的拨号端点配置及新链接恢复，不重复登记。新预览仅接受公布 `after_confirm` 激活方式的配套插件；已生效的持久凭据仍按原认证路径重连。
  - **出站错误**：输入无效 400；非管理员 403；ID 不存在 404；取消、过期、链接与选定结果不符、重复配对或旧 `/relay/pairings/import` 未经确认一律不能登记（409）；网络、证书、票据或身份验证失败 502。可读取的失败投影用 `state: "failed"` 和脱敏 `stage` / `reason`（如 `tls` / `fingerprint_mismatch`、`auth` / `pairing_refused`、`confirmation` / `name_conflict`）。状态接口不返回链接、票据或连接凭据；`/relay/nodes` 管理已登记节点。
- **远程节点窗口**：从“添加远程 Hermes”选择“本机连云端”或“云端连本机”。展示核验结果并经管理员确认后生成独立节点卡。全局“本机监听配置”入口紧邻添加按钮；需要入站监听时共用该弹窗，启用后续办原操作，取消保留原方向。已有节点在卡内切换方向并查看连接阶段、诊断和重试。窗口状态与文案见 [UI 规范](../ui-spec/UI-SPEC.md)。
- **切换（先断后建，主人 12619 拍板）**：用户点切换 → 记录切换意图+目标方向（随节点持久化）→ 暂停派发 → 断旧连接 → 建新连接 → 认证通过按 submission_id/session.resume 核对在途 → 恢复派发。状态由动作驱动：`offline(reason=switch)` 与 `offline(reason=unexpected)` 区分（切换中不触发任务重试/告警，页面显示「切换中」）；新连接认证成功/用户切回/引擎重启分别清除切换态；不设计时器、不做 draining/双通道。同一节点同一时刻一条活动通道。切换期间同一条点名最多执行一次；结果不明执行保留「待核对」不自动重派。
- **新登记节点的 MCP**：没有显式管理员模板时，沿现有同步快照请求，从本次已认证连接代取得插件解释器和随包入口的绝对路径，只发回该节点的 Hermes 会话。缺失、旧代或方向为拨号而未报告代理端口时拒绝派发，不用引擎本地路径兜底。云端主动连接时使用部署配置的引擎地址；默认回环且另有本机监听地址时，使用已配置的网卡地址。已有管理员模板保留优先级，身份和会话授权仍由引擎签发。
- **方向 B 的 MCP**：ECS 本地 loopback 代理（插件提供），MCP `GROUP_ENGINE_URL` 指向它，请求沿已认证通道回引擎按成员权限执行；验证用一次带项目权限的只读调用并确认走隧道。
- **沿用已坐实约束**：多监听时第二 Server 禁用 lifespan（engine/main.py:136 app 挂 `lifespan → _engine_lifespan`）；远程监听 uvicorn.Config 显式 `proxy_headers=False`、不读 XFF；监听失败重试+失败保持原监听并报错、不回退通配；主 Server 独占信号处理。

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
- **离线期间的 @（offline 点名记录状态机）**：节点离线 → 心跳到不了引擎 → 发言位超时弃权推进 → queued chat job 派发时 turn_profile 已变 → `_discard_queued('superseded topic turn')` → `settle_thread` 把该话题 pending 全置 expired = 离线期间的 @ 静默消失。防御=**窄口径点名记录三态状态机**（唯一键 (message_id, member)，闸门每次检查 upsert 不新增，调度器反复检查只显示一条）：
  - `offline_waiting`：闸门发现离线、发言位仍有效——当场记「等待成员上线」；成员在发言位仍有效时回来，正常投递一次并更新原点名；
  - `submitted`：投递成功，附 submission_id——**本次注入快照里的所有点名一起标 submitted**（不能只标触发那条，否则同话题排队的其他 @ 会一直停在等待态、被误判未投递）；
  - `offline_expired`：发言位到期且确认从未提交——显示「成员离线，未投递」，话题收口保持收口，用户重新点名=新 @ 边。
  口径：心跳 90s 到期发言位即推进，「等待成员上线」是短窗口状态，UI 不得写成长期等待。此记录不是 adapter outbox，与断线语义（submission_id 核对 in-flight 执行）是两个场景；发言位照常超时推进、不挂起 turn（挂起=整个话题卡住等离线节点）。
- **凭据**：连接凭据常驻 ECS、绑定成员、可单独吊销；执行凭据沿用引擎现有 per-execution 收窄（execution token 绑定 project/member/chat-work）。ECS 长期不预置成员 token。
- **fencing**：连接 generation 随派发下发，插件拒旧代帧；重连后只让当前连接接新消息。
- **时序**：configure 回执先于 prompt.submit（引擎现有串行链路天然保证）。
- **首版边界**：成员级可判忙闲；节点（机器）级并发上限首版不承诺。

## 5. 首版范围

- 只支持 hermes runtime；一名成员固定一台机器（一条连接）；不跨节点迁移。
- 依赖引擎主机直接读写文件的本地管理功能首版明确提示不支持。
- 旧本机 spawn/网关链路保留不动；退役评估留到 relay 覆盖本机+远程、能力对等后。
- 首版不做：outbox 重试权、迟到结果归属、messages.kind、进度状态通道（relay 有 submission_id 回执链，这些机制不需要；进度沿用思考条/心跳）。skip 原因标记只保留 offline-non-delivery 窄口径记录（见第 4 章），其余 skip 原因首版不做。

## 6. 验证清单

1. **端到端唤醒**：空闲网关收到群里 @ → 引擎经 relay 建/配会话并提交 → Hermes 回复原话题（从零手工配置启动 gateway，MCP 随插件自动注入正确成员/话题权限）。
2. **断线重连核对**：接单后、回执送达引擎前断线 → 重连后按 submission_id 核对原执行 → 同一派发最多执行一次；断线期间不派新活、重连核对通过后恢复。
3. **插件独立线程连本机 /api/ws**：取票+阻塞 RPC 不卡 gateway 事件循环；identify 身份与 ticket 请求一致。
4. **generation fencing**：旧连接晚到的帧被拒；重连后只让当前连接接新消息。
5. **多成员同机隔离**：同一 ECS 两个 profile 各收一次点名，回复与事件各自回到各自话题。
6. **升级重核摘要**：连接不断、原地替换 MCP 文件，系统重核摘要再配会话。
7. **/stop**：停只停指定执行，不误伤同 profile 并行会话（复用现有叫停链路，验证远程 transport 下有效）。
8. **离线 @ 场景**（bot-36/bot-43 2026-11-18 定，两种结局都要验）：①成员先离线 → 群里 @ 它 → 话题超时收口 → 成员重连：原点名可查「成员离线，未投递」（offline_expired），收口话题保持收口，由用户决定是否重新点名；②成员离线时被 @、发言位仍有效时回来：显示「等待成员上线」（offline_waiting）→ 正常投递一次 → 状态转 submitted、原点名更新，同话题快照内其他排队点名一并转 submitted。
9. **首装拉起 MCP**（bot-43 2026-11-18 提，bot-36/bot-43 同轮收紧）：全新 ECS 只装插件包，不改 Hermes 配置、不手动 pip，首个会话成功拉起 MCP 并实际调用一次带项目上下文的只读工具（如 `task_list(project=...)`，fixture 中该成员在该项目 active 且至少一条任务）保留成功响应——一次验证解释器、依赖、入口、会话凭据与项目授权；留证同时抓本次会话 MCP 配置（command/args/env 绝对路径）与握手上报的解释器/入口/摘要比对，确认走的是插件自带 venv 与随包入口，排除机器上残留的全局 task_queue 配置。**取证纪律（2026-11-18 bot-36/bot-43 定）**：报告按白名单只摘解释器路径、入口路径、包摘要、预置任务 ID（用 task_list 返回的任务 ID 核验权限与 MCP 身份）；env 中 GROUP_ENGINE_TOKEN（hermes_session_auth.py:167 注入）只记录「已注入」，不落值；报告生成前用内存中本次 token 值 grep 报告与日志，命中即失败；摘要只覆盖 MCP 包与依赖清单，不含 env 或令牌（防摘要成为凭据侧信道）。

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
