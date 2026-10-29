# Hermes 网关执行适配

## 接口与版本

`engine/hermes_gateway*.py` 使用公开 CLI `hermes gateway ensure --json`、本地 control 协议的 `session-ticket` 和 `/api/ws` 的 `hermes-gateway-v1`。适配器仅调用线协议，Hermes 源码保持独立。运行时核验 profile、协议、安装提交身份，以及 `durable-admission-v1`、`acp-session-mcp-v1` 能力。v0.22.5 验证基线为 `bf0203a794a07aac3ff764972ab9087baa4e4f4e`。

网关版采用独立执行路径；旧版 Hermes 和 Codex 保留现有运行时。安装来源由 `HERMES_BIN`、`HERMES_HOME` 和已有安装定位规则确定。

## 会话、MCP 和原生审批

新会话使用 `source=acp`，通过 `session.mutate editor_mcp` 注入本次执行的 task_queue。每轮先提交空列表以释放旧连接，再提交新凭据。实际工具发现和调用由 Hermes 原生机制完成。

危险命令审批采用 Hermes 原生 `approvals.mode: smart`，配置由管理员在 Hermes 中维护。ACP 编辑策略采用官方 `edit_policy=workspace_session`：普通工作区编辑按原生策略放行，敏感路径和工作区外请求遵循原生审批规则。引擎沿用这些原生能力。

旧 CLI 会话通过官方 `session.export` 和 `session.mutate import` 建立确定 ID 的 ACP 副本，原会话完整保留，映射记录在 `hermes_session_migrations`。活动会话先等待终态。`/new` 使用 `session.create`，绑定 generation 按现有流程递增。恢复、标题查找和思考记录分别使用 `session.resume`、`session.list`、`session.export`。压缩后的逻辑会话由 Hermes 解析。

已有绑定按持久化 session ID 恢复。首次绑定时调用官方 `session.list title=...`；v0.22.5 的原生标题解析返回零个或一个结果。适配器对多候选响应返回 `ambiguous_session_title`，保留原会话及绑定状态；管理员核对标题归属后重试。空结果按原有流程创建会话，唯一结果写入 ID 绑定。

只读状态查询 `existing_session_id()` 优先读取已有绑定；缺少绑定时以 `read_only=True` 解析标题。网关模式通过本地 control `identify` 发现已运行的实例，校验 profile、协议、实例身份与回环地址，再调用 `session.list`，完成后关闭 viewer。命名 profile 可查询承载它的根目录 multiplexer；发现过程保持目录与进程状态。查询中的网关传输、拒绝、配置和超时异常降级为 `None`。旧版模式使用 SQLite `mode=ro` 查询已有 state.db。创建与执行路径继续使用 `ensure`。工作区 GET 对查询层抛出的已知数据库、系统和网关异常返回 `work_session_exists=null`，成员视图仍可读取。

旧 subprocess 路径的执行错误、任务重试、异常退出和成员身份测试显式 mock `hermes_gateway.enabled()` 为 `False`；任务重试复用异常退出夹具。网关查询另用隔离数据库、临时 home 和模拟 control/RPC 回执验证，测试分支由夹具确定。

## 执行与恢复

`hermes_executions` 在 `prompt.submit` 前提交执行身份、submission ID、会话/profile 身份及固定截止时间；回执到达后保存 admission ID、generation 和 authority epoch。引擎重启恢复原记录，并继续查询 `prompt.receipt`。已登记远端执行的投递作业保持已投递状态。

queued 执行通过 `prompt.cancel` 取消；started 执行通过携带 generation 的 `session.interrupt` 取消。只有对应 admission 的 terminal 回执才能结束执行。中断 ACK、WebSocket 断开和查看客户端退出均保留占用。用户停止工作执行时，经原生 `rewind` 回退本次指令；目标消息限定本次执行，回退目标和请求 ID 持久化后重试。

引擎优雅停止/重启先关闭新任务入口：停派发期间新的写工具请求（`/msg`、`/thread`、`/thread/task-update`、`/inbox/seen`、`/task-tools/{operation}`、撤回）明确未受理，返回 503 且带 `X-Group-Not-Accepted:1`；管理查询（GET）与心跳保留。本地执行（Codex/旧 CLI）等待自然结束，默认 120 秒，超时终止；Hermes gateway 托管执行不取消、不等 bot 终态——本地收尾有界关闭全部 viewer 连接（硬超时 5 秒，覆盖挂住 RPC 与并发建立中的连接），保留未终态执行与浏览器资源，停机期间 worker 断线不误记 uncertain。引擎退出后重启，`recover()` 接回原任务：对账 bot 状态（`prompt.receipt` 查询原 admission），已结束收尾、没结束继续跟踪，不重新 `prompt.submit`（防重复派发），原 admission/submission 保留。远端接管异步进行，不阻塞引擎就绪。`start.bat` / `start.sh` 只有确认旧引擎进程退出和端口释放后才启动新引擎；快捷方式复用健康实例不经过停机。停机取消使用 `shutdown` 原因、保留会话历史，仅作用于本地执行；gateway 托管执行跨重启继续运行。

终态立即撤销执行凭据；清理临时 MCP、浏览器资源和引擎记录后解除会话清理禁用。MCP 心跳使用自身 PID，凭据校验约束项目、话题和 inbox 类型。停机按 `session + started_at` 精确区分网关托管执行与本地执行，包含已终态但尚待清理的网关记录；同 session 的后继本地执行继续遵循本地收尾流程。停止失败时重新开放 viewer 连接和执行跟踪。

执行终态与清理完成是两个独立状态机。终态回执落库即撤销执行凭据并释放执行占用：`active()` 只看 `state != 'terminal'`，`cleanup_pending` 不算占用（不进入 busy 判定）；同一逻辑会话仍受「清理禁用」约束，清理恢复后才可复用，其他独立会话可运行。清理待办（`cleanup_pending=1`）按退避序列重试（1/2/4/8 秒）最多 5 次；`retry_count`、`next_retry_at` 与 `cleanup_deadline` 持久化，清理最终期限（`cleanup_deadline`）到期即转 `cleanup_failed` 停止重试。耗尽（重试 5 次或期限到期）转 `cleanup_failed`（`detail` 标记）并停止自动循环，保留 `cleanup_pending=1` 与会话禁用，仅人工明确重试才开启新预算。引擎启动恢复只续剩余额度，已耗尽或期限已过不自动重试；恢复沿用原 admission 与取消原因。取消有界：`cancel_deadline`=首次 cancel+30s、`reconcile_deadline`=首次 cancel+150s，超时转 `manual` 停止自动循环；单次 RPC 超时=min(默认 30s, 剩余预算)（仅取消/对账时生效，正常轮询保持默认 30s，不因 reconcile_deadline 过期而打断长时间运行执行）。清理走引擎自身 GatewayClient（ensure+ticket），非 run.token 执行凭据，撤销凭据不影响清理重试。清理登记按 execution_id/started_at 精确匹配，不按 session 删除，避免迟到清理误删后继执行。

提交回执缺失时，当前公开接口缺少按 submission ID 只读找回 admission 的入口。适配器保留 `unknown`，到固定截止时间转 `manual` 并保留占用。管理员需核对原执行，自动重提保持关闭。已有 admission 的执行在断线后重新取一次性 ticket，并查询原回执。

已有 admission 且未取消的执行发生断线或收到 unknown 回执时，使用原 `execution_deadline` 作为查询预算；提交阶段的 `started_at + 150s` 限制仅用于尚未取得 admission 的执行。首次取消后使用原 `reconcile_deadline`。重启保持这些绝对时间与 admission/submission 身份，长执行跨过提交阶段预算后仍可接回终态。

## 空闲唤醒的 reasoning 覆盖

`configure_tracked()` 将引擎已成功设置的 reasoning 保存到业务库 settings，键由 profile 身份和 session ID 派生，并受 project 隔离。每次唤醒按 UI 显式参数设置 reasoning；显式参数删除后，存在记录的会话通过官方 `reasoning/effort:null` 重置一次，成功即删除记录。RPC 失败保留记录，下次唤醒继续处理。没有引擎覆盖记录的会话保留原生 reasoning 选择；清除完成后的原生选择也保持有效。默认配置在新 viewer 的空闲 resume 路径刷新，持续附着的同一 viewer 重复 resume 会跳过该刷新。

记录自启用该跟踪机制后建立。升级前已有的 pin 缺少引擎归属记录时，维护者需明确设置一次项目 reasoning 后再清空，或在 Hermes 使用原生 reasoning reset；引擎保持未知归属的原生会话选择。

AGENTS.md、SOUL 和记忆通过用户主动 `/new` 刷新。v0.22.5 在同 sid 的 resume 中复用已保存的 system prompt；文件修改或删除后，新 sid 读取当前内容。引擎在原话题更新会话绑定并递增 generation，原会话历史保留。出站载荷夹具、真实模型生成和引擎 `/new` 绑定切换分别记录证据。

空闲会话通过新的 viewer 恢复时，profile 配置派生的 max_turns、checkpoints 和 terminal 字段重新计算；删除配置恢复原生默认值，显式选择的 cwd/workspace 保持。普通 CLI 会话的配置派生 toolsets 随配置刷新。editor MCP 会话的工具集随 editor manifest 冻结；引擎每轮通过 editor_mcp mutation 更新 MCP 工具描述和集合。

`verify_gateway_refresh_live.py` 在隔离 home 中通过公开协议执行配置刷新和模型调用。配置字段以只读的已提交 policy 回执核验；规则与工具描述以模型最终回答及实际 tool_search 结果核验。policy 回执证明派生值变化，max_turns 耗尽和 checkpoint 文件生成属于独立运行行为验证。

## 签名密钥与逐执行撤销

服务启动在恢复执行和启动 worker 前初始化 `config.data_dir()/secrets/bot-signing.key`。文件保存当前代和最多一个退役代，目录和临时文件均受权限保护：POSIX 为目录 0700、文件 0600，Windows 使用当前运行账户的独占受保护 DACL。密钥与签名凭据禁止进入源码包、Git、普通诊断报告及数据库；数据库仅保存初始化标记、代次及密钥摘要用于恢复一致性检查。

首次生成或导入 `GROUP_ENGINE_KEY` 为第 1 代，当次保持该代。后续启动在退役代没有活动执行时轮换；旧代仍有活动执行时保留两代。执行记录的 `signing_generation` 在提交 admission 前落盘，MCP 配置按该记录签发。文件更新使用受限临时文件、fsync 与原子替换，跨进程文件锁和数据库写事务串行化轮换、执行登记和退役。已写文件但数据库提交失败时，下一次启动保留文件中的有效代次继续恢复。

验签纯函数接收密钥集合并返回命中代次；HTTP 身份解析共用一次密钥快照。通用成员凭据只接受当前代。网关执行凭据必须匹配账本中的成员、项目、话题、执行类型及签发代次；终态立即对所有受保护 Bot 入口返回 401。当前代 Codex 和旧版 Hermes CLI 执行按其有效 `wake_runs` 身份验证，要求运行时为 `codex` 或 `hermes`、执行尚未结束且成员/项目/话题一致；旧代仅用于已登记未终态的 Hermes 网关执行。最后一个旧代执行终态提交后立即尝试退役旧密钥，当前代始终保留。

服务重启入口与源码升级流程在请求旧服务退出前执行只读预检。首次升级有在途 Hermes 执行且缺少可迁移的环境密钥时，预检拒绝停机；管理员保留旧进程，等待执行结束后重试。已初始化但文件缺失、格式损坏、代次缺失或摘要不符时，恢复失败并阻断派发，保留文件与执行记录；管理员从同一实例的受控备份恢复匹配密钥，禁止自动生成替代文件。采用尚未包含预检的历史升级器时，应先运行新源码中的预检，确认成功后再安排停机。直接 Python 入口读取进程环境中的 `GROUP_ENGINE_KEY`；服务脚本会先加载实例 `.env`。

`verify_signing_restarts.py` 验证独立 HTTP 进程跨两次重启的凭据延续与撤销。该证据使用隔离执行账本；真实网关的 admission 恢复和重复派发检查随完整重启接管场景验收。

## MCP 请求结果对账

`engine/mcp_http.py` 为 MCP HTTP 调用提供 30 秒总预算。连接建立失败或明确的 `503 + X-Group-Not-Accepted: 1` 按 1/2/4/8 秒退避，最多发送 5 次。耗尽返回 `engine_offline`。读取超时、写出后断线、一般 5xx 和重定向进入结果查证；写操作按原 `operation_id` 查询 `/mcp-operations/{operation_id}`，查证也受同一总预算约束。查询仍无法确认时返回 `operation_unknown`，携带操作 ID。FastMCP 同步工具运行于已有事件循环时，传输在独立线程的事件循环中执行，预算内的请求与等待可取消。

任务 API、发消息、建话题、话题任务修订和已读确认使用 `X-Group-Operation-Id`。客户端为一次调用生成 ID；同一 MCP 进程内，结果未知的相同请求继续查证原 ID。确认完成后释放本地记录，后续显式调用视为新操作。`operation_get(operation_id)` 提供只读查证；结果未知时由维护者核对原业务记录与外部副作用，再决定后续动作。MCP 进程自身退出会失去本地请求映射，错误中的操作 ID 和引擎持久回执仍可用于查证；自动恢复范围限定为原 MCP 进程及仍有效的执行凭据。

引擎先持久登记 `unknown` 预留，再在同一 SQLite 事务内提交业务变更和 `completed` 回执。原业务连接通过请求上下文共享事务；局部提交释放 savepoint，最外层提交决定持久化，事件在提交后发布。相同成员/执行/ID 的并发请求与重放返回原结果或 `unknown`；相同 ID 的不同请求内容返回 409。预留后进程异常且业务事务尚未提交时回滚业务并保留 `unknown`；涉及外部副作用的操作继续保留未知状态，自动重放保持关闭。回执随业务库备份，保留到管理员按数据维护策略处理，自动过期删除保持关闭。

回执按成员和签名执行身份隔离，查询及重复请求继续检查成员资格、项目/话题范围和执行有效性。后继执行拥有独立的回执空间，终态或过期凭据无法取得原结果。旧 MCP 进程可在引擎重启后继续使用原 `GROUP_ENGINE_URL`；凭据延续仍由原执行的有效性决定。

上述行为使用隔离 API 进程和真实 stdio MCP 验证。执行完整网关恢复场景时还需覆盖模型、网关及引擎三方生命周期。升级启用时重启引擎以加载 schema 和 API，并重新加载持有旧 MCP 进程的宿主；网关持有的 MCP 需按项目运行约定重启网关。

## 验证与启用

测试方法见 `scripts/TEST_INDEX.md` 的“网关正式适配”。真实验证使用独立 Hermes home、随机本地 HTTP 端口和隔离引擎数据库，覆盖原生审批配置、两轮 MCP 凭据刷新、回复/已读、取消/回退、重连、历史导出和 `/new`。引擎重启恢复、未知执行占用和旧版/Codex 兼容由相关回归覆盖。原始回执保存在主目录 `reports/tests/`。

部署时先安装引擎依赖，再选择对应 Hermes 安装。按项目运行约定，由管理员重启引擎；Hermes 升级后重启对应网关，使 endpoint 的 code SHA 与安装一致。生产升级与启动验证单独记录。

## 后续范围（话题收束，主人 9092 定）

基于现有网关实现推进的两项遗留工作：

1. **更多斜杠命令**：补齐引擎侧入口与原生能力对接，含 `/steer` 传输通道接入，也含后续原生命令接入范围的梳理。现状 `COMMAND_REGISTRY` 已有 `/new`（session_adapter.switch_session，已接通）、`/stop`（stop.stop_profile，已接通网关执行路径——`hermes_gateway.cancel` 走原生 `prompt.cancel`/`session.interrupt`，本地子进程路径才走 taskkill）、`/steer`（账本层已就绪，传输通道未接入，`#145` 技术障碍）、`/help`（渲染命令表）。`/steer` 只是其中尚未接通的一条，不据此把整项缩成 `/steer`；具体命令清单留给设计确定。
2. **消息工具简化**：统一梳理话题和工作群的注入、回复投递、已读确认；保留 need_reply、合法跳过（null/pass）、DFS 顺序、失败后待处理语义。editor_mcp 解决了身份传递，简化按交互收益独立决定。三处明确方向：
   - **final_response（重新定位，主人 9098 + bot-36 9100 定）**：不是「砍消息工具的替代」，而是「引擎统一接收 bot 正常产出的最终回复并投递、确认已处理消息」的兜底机制——解决 bot 漏调 send/seen 导致的回复未落库、pending 未清、重复唤醒。保留 need_reply 与明确的回复/合法跳过意图；投递成功且语义校验通过才确认本轮对应消息，失败或中断保留待处理。现状引擎未实现（仅测试脚本 mock 字段），属待落地候选。
   - **search_message（新增，主人 9098 提）**：受项目与消息可见范围约束的历史检索，支持关键词及必要筛选、分页，返回消息 ID 与原文出处，供引用与追溯；只读，已读确认仍由处理流程负责。解决 reviewer-02（周岚）绕开引擎自写脚本查消息的问题。现状引擎不存在，属新增。
   - **group_progress（过程消息，主人 9103 + bot-36 9104 定）**：bot 在话题处理任务过程中先发一句预告/进度（「我准备查什么」「当前查到哪里」），显示在当前话题，归属由引擎绑定本次执行。发送后保留发言位、bot 继续处理；不替当前 bot 确认已读、不清其 pending、不记表态或跳过、不释放发言位、不推进 DFS、不因过程消息单独唤醒其他 bot；正文中的 @ 仅作展示不产生调度。过程消息正式进入聊天记录，下一个 bot 正常轮到时会连同其他待处理消息一起读到。正式结果仍由 final_response 或正式发言路径完成投递、确认与交棒。解决 bot 中途发消息即被取消发言位、后续工作做不了的问题。现状引擎不存在，属新增；是否自动隐藏/删除属后续展示设计。实现约束（reviewer-02 9106 + bot-36 9107/9109）：①硬要求是过程消息不执行正式发言的已读确认、清发言位和 DFS 推进，也不因过程消息单独唤醒其他 bot（router.py:737-748「发言即清」写事务：bot 发言时把本人 id≤当前消息 ID 的 pending 全标 seen + `_clear_turn` 清发言位）；身份与权限校验保留，数据库事务可继续用于保证过程消息自身写入一致性；过程消息正式落聊天记录、供下一位注入读取。接口形态（独立接口 vs /msg 内明确区分 progress 的分支）留给实现设计，不锁死。②「不占发言位」= 保留当前已有发言位，不新增、不释放、不转交，现有超时与停止规则继续适用。③范围首期限定 chat 话题场景，work 侧同类能力留给设计单独评估。
