# Hermes 网关执行终态与清理分离方案

## 状态

- 方案状态：定稿，按任务流程进入开发与验收。
- 关联：`2026-10-27-hermes-gateway-migration.md`（第一阶段迁移）、`../engineering/HERMES-GATEWAY.md`（执行适配契约）。
- 本方案针对正式网关适配（提交 6d3f3b1 + e9c8d6d + 1649637）遗留的非阻断问题提出，并把凭据延续、修改生效、离线窗口、本地退出清理收口为确定行为。

## 1. 目标与范围

### 1.1 目标

废弃「杀 CLI PID = 停止执行」的旧模型，改为统一执行契约：提交取消/查询回执 → 确认终态 → 释放占用与凭据 → 异步清理资源。

### 1.2 范围

- 引擎重启与接管（本地收尾，bot 在网关继续执行）。
- 停止单个 bot（取消有界 + 确认终态）。
- 凭据两代延续与逐执行撤销。
- 配置 / 规则 / 技能 / SOUL / 记忆的修改生效。
- MCP 离线窗口的请求处理策略。
- 本地退出与清理的预算、持久化字段与失败出口。

### 1.3 范围外

- 重启 gateway：Hermes 网关自身重启走 supervisor 握手（状态/进程分离落盘 + 握手换 PID），与本引擎重启无关。
- Codex 等独立执行路径：保留原有进程生命周期契约（进程退出即执行结束、`codex_run_events` 事件流），不套用本方案的 terminal 回执 / 清理分离 / 凭据代次语义。本方案所有执行相关描述仅适用于 `runtime='hermes'` 的托管执行。

## 2. 核心原则

1. 执行终态与清理完成是两个独立状态机：终态是执行占用的权威释放点，清理是可恢复的异步待办。
2. 引擎重启 ≠ 停 bot：bot 是网关托管执行，引擎重启只做本地收尾，bot 在网关继续跑；引擎启动后凭执行编号接回对账，不取消、不等待、不重复派发。
3. 状态单调：终态为吸收态，有效非终态回执推进状态，过时事件只入审计不回滚。
4. 写前日志：提交/取消均先事务提交本地意图、关联标识与截止时间，再发送请求；终态事实先落盘再触发副作用。

## 3. 状态机

### 3.1 执行状态（吸收态 terminal）

`submitting → queued → running → terminal`（terminal 为吸收态）

- 终态回执落库即释放执行占用 + 撤销执行凭据（逐执行 401）。
- generation / authority_epoch 单调校验；terminal 后不回归。

### 3.2 清理状态（独立于执行状态）

`cleanup_pending → cleanup_retrying（退避） → cleanup_failed（人工）`

- terminal 后进入 `cleanup_pending`。
- `clear_mcp` + 本地资源释放成功 → 清除清理标记。
- 失败 → 退避重试（有限次数），耗尽 → `cleanup_failed` + 告警，停止自动循环。

## 4. 边界定义

### 4.1 执行占用

- `active()` 收紧为只看 `state != 'terminal'`，`cleanup_pending` 不算执行占用（不进入 busy 判定）。
- 终态回执落库即释放执行占用、撤销凭据；通过准入检查的独立会话可进，同一逻辑会话仍受清理禁用约束（见 4.2）。
- 破坏性变更：原 `active()` 以 `cleanup_pending=1` 阻塞清理前同 session 的新 spawn；收紧后该防护由「会话禁用」承接（见 4.2）。

### 4.2 会话禁用（保守基线）

- terminal 后旧 session 标「已结束、清理待处理」，停止占用执行 worker，不用于新执行。
- 同一逻辑会话等清理恢复后才可复用；其他独立会话可运行。
- 不自动 `switch_session` 另建会话（会改变上下文连续性）；自动切新会话需另有明确的历史续接方案，不能悄悄执行 `/new`。

### 4.3 停止单个 bot（取消有界）

- 取消意图持久化：`cancel_reason` + 绝对截止时间。
- `cancel_deadline`（首次 cancel + 30s）= 同步等待返回上限（`cancel()` 不阻塞调用方，超时返回「取消待确认」）。
- `reconcile_deadline`（首次 cancel + 150s）= 自动 cancel/interrupt/查询的硬门槛，超过转 `manual`、停止自动循环、保留安全占用并显示原因。
- `manual` 保持自动重提阻断（不自动重试，转人工）。

### 4.4 引擎重启与接管（本地收尾，bot 继续运行）

已取得 admission 且尚未取消的执行，断线和 unknown 回执按原 `execution_deadline` 保持有界查询。提交阶段的 `started_at + 150s` 仅用于尚未取得 admission 的不确定提交；首次取消后按 `cancel + 150s` 限制取消对账。重启沿用各阶段原有绝对截止时间。长任务断线后继续查询原 admission，直到取得权威终态或耗尽对应预算。

- 停服标记：停止派发新任务、新工具请求。
- 本地收尾（写前日志）：保存执行编号（execution_id / admission_id）+ 已接收操作状态 + 截止时间落盘。
- 有界关闭本地连接：本地 GatewayClient 连接关闭，硬超时 5s，不阻塞引擎退出。
- 引擎退出 + 重启：不取消 bot、不等 bot 终态、不人工；重启按钮给出明确结果。
- 重启入口（UI 重启、start.bat / start.sh 的 service.py restart）走同一本地收尾 + 接管流程；快捷方式（service.py start）走 do_launch 复用健康实例（未启动才 do_start），不涉及重启。
- 启动接管（recover 增强）：接回原任务——对账 bot 状态（prompt.receipt 查询原 admission），已结束收尾、没结束继续跟踪；不重新 prompt.submit（防重复派发），原 admission_id / submission_id 保留。
- 服务就绪与远端接管分离：引擎进程先就绪（健康检查通过），远端接管异步进行，不阻塞引擎就绪。

### 4.5 MCP 离线窗口（bot 经 task_queue MCP 访问引擎）

bot 通过引擎注入的 `editor_mcp`（task_queue）访问引擎 API；引擎离线时请求失败。策略分两类：

- 明确未执行（仅连接建立前失败，或服务明确返回「尚未受理」）：有界重试，指数退避（1s / 2s / 4s / 8s，上限 5 次，总预算 30s），耗尽返回明确错误「引擎离线」，不产生脏状态。
- 结果未知（读取超时、写出后断线、一般 5xx）：按 `operation_id` 查证任务状态，禁止盲重放。幂等键由客户端复用，服务端在业务写入的同一事务内登记结果/去重（仅客户端写前日志不能防重复）；外部副作用缺乏幂等能力时保持结果未知，禁止自动重放。
- 旧 MCP 进程恢复通信：引擎恢复后，旧 MCP 进程凭原 `GROUP_ENGINE_URL`（XDG_GROUP_ENGINE_URL 注入）重新连接同一引擎入口，续用未终态操作；`operation_id` 记录是恢复对账的锚点。

## 5. 凭据方案（唯一实现）

### 5.1 签名密钥存储

- 签名密钥持久化到独立的 `secrets` 目录：`config.data_dir() / "secrets" / "bot-signing.key"`。默认部署为程序根目录下的 `secrets/bot-signing.key`，与数据库所在的 `data/` 分开；指定 `GROUP_DATA_DIR` 或 `--data-dir` 时，跟随该实例的运行数据根目录。三个启动入口使用同一配置解析结果，隔离测试使用分配的测试数据根目录。内容为两代密钥：`{"generation": N, "keys": {"<N>": "<hex>", "<N-1>": "<hex>"}}`。
- 保护：POSIX 目录 0700、文件 0600；Windows 对目录和文件设置运行账户受限 ACL；同目录临时文件与正式文件采用一致保护。密钥及临时文件排除 Git 跟踪、源码包和普通诊断产物。
- 不落 DB（密钥与账本分离，避免密钥进入 group.db 备份与查询日志）。
- `run.token` 不持久化，验证时从对应代密钥重算；凭据与密钥不得进入命令行、日志、报告。
- 原子与串行：文件轮换先确保新 key 原子持久化，再允许该代签发落库；轮换/签发/退役串行，崩溃后按账本保留仍被引用的代次。

### 5.2 代次与执行关联

- `hermes_executions` 表新增 `signing_generation INTEGER`：记录该执行签发时使用的签名密钥代次。
- 已有 `generation` / `authority_epoch` 字段保留原语义（网关回执的 execution_generation / authority_epoch），不承担签名密钥代次。

### 5.3 事务创建 / 轮换 / 退役

- 创建：确认首次初始化且满足 5.6 的升级预检后生成或导入 gen=1 密钥，原子写（写临时文件 + rename）。已有初始化记录却缺失密钥文件时按认证恢复失败处理；写失败保留旧文件。
- 签发：新执行用当前代 gen=N 签发 `run.token`，落库 `signing_generation=N`。
- 退役：仅退役代密钥在所有关联执行（`signing_generation=该代` 且 `state != 'terminal'`）均已终态后删除；检查随执行终态即时触发。当前签发代始终保留。
- 轮换：引擎启动读密钥文件——本次首次生成（刚创建 gen=1）当次不轮换；后续重启时若只有当前代（无上一代）可生成新代；两代中旧代仍有未终态执行则沿用；当前签发代即使零任务也保留（只回收退役代，避免密钥被删空）。连续重启优先保障未终态执行，代数优化不阻塞重启。

### 5.4 多代验证路径（写死）

- 选代机制：遍历验签，不查表。`execution_scope` 遍历当前代 + 上一代 key，各自派生 bot_token 再验 `run.token` 签名，命中即返回 scope 并携带代次；两代都不命中返回 None。
- 职责边界：`execution_scope` 保持纯函数（不访问 DB），入参从单一 key 改为两代 key（调用方从 `bot-signing.key` 读入）；`verify_bot` 双分支保留——先比较 bot_token，再 `execution_scope`。
- bot_token 按代次：`bot_token(profile)` 用当前代 key 签发；通用 bot_token 仅当前代有效（避免旧代无执行范围的 bot_token 绕过逐执行撤销）。旧代只验证已登记、未终态的 `run.token`（有执行范围校验）。`run.token` 用对应代次的 bot_token 签发，签发时落 `signing_generation`。
- 逐执行撤销（401）仍由 `access.py` 查 `hermes_executions.state` 承担，不因多代变化；验签与撤销分离——验签不查表、撤销查表。

### 5.5 逐执行撤销（401）

- 逐执行撤销（现有实现）：公共鉴权 `authorize` 查 `hermes_executions.state`，`state='terminal'` 即返回 401「Execution credential expired」，覆盖所有 BOT_ROUTES（不只 /msg），同代其他执行继续有效。证据在 `engine/access.py` 鉴权路径。
- 代次一致性：access 校验验签命中的代次与账本 `signing_generation` 及执行身份一致，缺记录拒绝。
- 整代回收：该代全部关联执行结束后，才回收该代验证密钥。仅退役整代 key 不能替代逐执行撤销。
- 多代验证保留执行范围校验（project / thread / kind 匹配），旧代只用于已登记、未终态的执行。
- 保留既有非 Hermes 路径（Codex 等）兼容测试，不因凭据代次变更破坏原有契约。

### 5.6 重启失败恢复与首次升级

- 重启失败恢复：密钥文件原子写；写入失败保留旧文件。
- 文件损坏/缺失：禁止自动生成新 key 顶替并使在途凭据失效——保留原文件及执行记录、明确报告认证恢复失败，修复前阻断相关派发。
- 首次旧版本升级：旧版本签名密钥来自 `GROUP_ENGINE_KEY`（env）或进程随机 `_BOT_SECRET`。若 `GROUP_ENGINE_KEY` 已设置，读该 env 作为 gen=1 持久化，旧凭据继续有效；若未设置（无可迁移旧 key）且有在途执行，升级预检暂缓、保持旧服务运行，不得打断在途执行。预检须发生在旧服务退出前。

## 6. 本地退出与清理

### 6.1 退出预算

- 取消同步等待：`cancel_deadline` = 首次 cancel + 30s。
- 自动循环硬门槛：`reconcile_deadline` = 首次 cancel + 150s。
- 本地连接关闭：硬超时 5s。
- 单次 RPC 超时：`min(默认值, 剩余预算)`，连接、读取、重试共同消耗同一截止预算。

以上数值为待验证初值，随实现实测调整。

### 6.2 清理持久化字段

- 执行侧（已有）：`execution_id`、`session`、`started_at`、`state`、`cancel_reason`、`cancel_deadline`、`reconcile_deadline`、`cleanup_pending`、`ended_at`。
- 清理侧（新增）：`retry_count`（已重试次数）、`next_retry_at`（下次重试时间）、`cleanup_deadline`（清理最终期限）。
- 凭据侧（新增）：`signing_generation`（签发代次）。

### 6.3 失败出口

- 清理耗尽：`retry_count` 达到上限（5 次）→ `cleanup_failed` + 告警，停止自动循环，保留 `cleanup_pending=1` 与会话禁用（不恢复 terminal 的执行占用），人工明确重试后才开启新预算。
- 取消超时：超过 `reconcile_deadline` → `manual` + 人工，保留安全占用。
- 重启失败：保留旧密钥文件与 service-restart.json 收据，人工介入。
- 重启只续剩余额度，已耗尽保持 `cleanup_failed`，不自动重试。

## 7. 重启入口与修改生效

### 7.1 三入口调用链

| 入口 | 调用链 | 停机/启动语义 |
| --- | --- | --- |
| UI 重启按钮 | 前端 → `/admin/restart` → `restart.begin()` 分离重启收据 | 收据 + 本地收尾 + 优雅停机 |
| UI 停止 | 前端 → `/admin/shutdown` → `shutdown.begin()` | lifecycle.Shutdown |
| start.bat / start.sh | service.py restart → do_start → do_stop(stop_engine.py) + start_engine.py | 先停旧引擎再启新引擎 |
| 桌面快捷方式 | service.py start → do_launch | 复用健康实例，不经过 Shutdown |

- 重启路径（UI 重启、start.bat / start.sh 的 service.py restart、UI 停止）汇聚到 `engine/lifecycle.py` 的 `Shutdown._run`；启动路径（快捷方式 service.py start）走 do_launch 复用已有实例。
- 三入口须共用同一安装目录、数据目录、配置与实例识别规则（program_root + db_path 对账）；重启路径统一走 4.4 接管，启动路径复用已有实例。

### 7.2 各修改项生效（按字段/消费路径）

| 修改项 | 生效方式 |
| --- | --- |
| 引擎代码 | 引擎重启生效 |
| 模型/推理参数、工作目录、会话 MCP、system prompt（引擎注入） | 每轮唤醒 refresh 调用刷新（删除参数后是否恢复默认待验证） |
| 工具提示词（工具描述） | 下次空闲唤醒刷新（保留会话历史，验证模型实际收到新版或移除旧描述） |
| AGENTS.md | 用户主动 `/new` 刷新；同 sid 的 resume 复用已有 system prompt |
| config 派生字段（toolsets / MCP / terminal / max_turns / checkpoints / reasoning default） | 空闲会话 resume 时 refreshed_policy 官方刷新，保留历史、不新建 session（见 7.3） |
| SOUL.md、MEMORY/USER | 用户主动 `/new` 刷新（原话题换 Hermes 会话） |
| skill 正文（skill_view） | 每次调用现读文件，再次调用即读新版 |
| 工具技能列表（skills_tool） | 目录签名 + 30s TTL；增删目录立即，原地改等 TTL |
| 系统提示技能索引 | 构建 system prompt 时核对文件签名；复用 stored_prompt 不刷新 |
| skill auto_load | per-agent，agent 重建重渲染 |

### 7.3 官方刷新路径（config 派生字段）

- bot-group 的 sessions.attach 调用 session.resume，触发 refresh_resumed_policy：空闲会话 resume 时从 profile 当前 config 重新派生字段并提交新 policy generation，保留会话历史。
- 刷新字段：toolsets、MCP servers、terminal（backend/cwd）、max_turns、checkpoints、reasoning default（除非 pinned/chosen）。
- 不刷新：model/provider（stored session 保持）、creation flags、committed reasoning 与 workspace pick、editor_mcp 的 frozen servers。
- editor MCP 会话的工具集与 editor manifest 一起冻结，profile 的 platform_toolsets 修改不会替换该工具集；引擎通过每轮 editor_mcp mutation 刷新会话工具。普通 CLI 会话未显式选择工具集时，toolsets 随 profile 配置刷新。terminal 的显式 cwd/workspace 选择保持，会话其余配置派生 terminal 字段可刷新。
- 限制：busy（turn running/queued）保持该 turn 准入时的 policy；显式创建参数与已选设置覆盖配置；refresh 失败不 fail resume。
- 精确表述：config 派生字段「下次空闲唤醒生效」，busy 时保持旧 policy 等下次空闲。

项目成员 UI 的 `reasoning_effort` 为显式覆盖，`project_models.arguments()` 将其转换为 `--reasoning`；Hermes profile 的配置默认值由原生 resume 刷新。引擎按项目、profile 身份和 session ID 持久记录已成功应用的 reasoning 覆盖。UI 覆盖被删除且记录存在时，调用官方 `session.mutate(operation="reasoning", payload={"effort": null})` 解除 pin 并读取 profile 当前默认值；成功后清除记录，失败保留以便下次重试。无引擎覆盖记录的会话保持原生选择。此过程只修改对应会话策略，profile 的共享 config.yaml 保持原有内容。

### 7.4 AGENTS / SOUL / 记忆（用户主动 /new）

- AGENTS.md、SOUL.md 和 MEMORY/USER 经 system prompt 注入，会话生命周期 byte-stable；同 sid 复用 stored_prompt，文件修改和删除通过 `/new` 后的新 sid 验证。
- 用户主动 `/new` 在原话题切换 Hermes 会话（同 thread 换 session_id，generation 递增，已实现），SOUL/memory 生效。
- 「自动保留上下文刷新」不在本方案范围；新会话上下文连续性（历史续接）单独评估，不阻塞本方案。

## 8. 改造约束

1. 改造清单覆盖 `active()`、`hermes_execution_active` 唯一索引、`recover()`、本地运行登记与清理字段。
2. 旧清理按 execution_id / started_at / 绑定代次处理，禁止清掉后继执行的占用、心跳或凭据。
3. 终态确认、本地幂等收尾与会话禁用完成后再开放准入，远端清理单独续办。
4. 有界等待落实到每个 RPC/连接超时：单次调用不得越过剩余预算；清理次数、next_retry_at、最终期限持久化；重启只续剩余额度，已耗尽保持 cleanup_failed，人工明确重试后才开启新预算。
5. `_finish` 清理登记改按 execution_id / started_at 精确匹配，不按 session 删除（防误删后继执行）。
6. `clear_mcp` 走 engine 自身 GatewayClient（ensure + ticket），非 run.token 执行凭据；撤销凭据不影响清理重试。

## 9. 验收条件（可证伪）

### 场景 A：取消一直 started（取消有界）

- A1 首次 cancel 后 30s 内返回「取消待确认」，cancel() 不阻塞调用方。
- A2 首次取消起 150s 为硬门槛：超时转 manual、worker 停止自动循环（RPC 调用计数封顶不涨）。
- A3 manual 态保留安全占用，显示事实状态：已发起停止的执行显示「停止请求已发送，结束状态确认超时」，其他查询失败显示「执行状态查询失败」。停止请求本身失败时提示「停止失败：原因」。保留原停止入口，后续是否操作由用户决定。
- A4 cancel/reconcile deadline 用绝对时间戳持久化，重启不重置预算。

### 场景 B：终态后网关断线（清理可恢复）

- B1 terminal 落库即释放执行占用 + 撤销凭据（active() 只看 state != terminal）。
- B2 clear_mcp 失败进持久化清理待办，worker 退出不死循环，独立会话新执行可进（同一逻辑会话仍受清理禁用）。
- B3 有限次数退避重试，耗尽转 cleanup_failed + 告警（RPC 计数有上限、cleanup_pending 保持、独立会话新执行正常进）。

### 场景 C：引擎重启接管（bot 持续执行）

- C1 停派发 + 执行编号/已接收操作状态落盘（写前日志）。
- C2 引擎重启不取消 bot、不等 bot 终态；本地连接有界关闭（硬超时 5s），引擎按时退出重启。
- C3 启动接回原任务：对账 bot 状态，已结束收尾、没结束继续跟踪，不重新提交（防重复派发）。
- C4 网关不可达时点重启，引擎仍按时就绪（不阻塞等待远端终态）。
- C5 bot 跨引擎重启持续执行。
- C6 MCP 请求离线返回策略明确（有界重试 / 按 operation_id 查证），不产生重复操作。
- C7 已接收操作幂等（操作 ID 去重），凭据延续不误伤新执行。

### 场景 D：拆分后新竞态防护

- D1 旧清理迟到不影响新执行（按 execution_id / started_at 精确匹配，非按 session 删）。
- D2 重启不刷新重试预算（计数续、剩余 deadline 不变、耗尽不自动重试）。
- D3 卡住 RPC 不突破总时限（单次 RPC 超时 = min(默认, 剩余预算)）。

## 10. 回归清单

1. 三入口各自预期：快捷方式复用健康实例（未启动才启动）、UI / 脚本重启走本地收尾 + 接管。
2. 连续重启并发：同 bot 多话题跨连续两次引擎重启，凭据沿用有效、各话题执行不串、不重复派发。
3. 迟到清理：A terminal 后清理 worker 迟到触发收尾，不误删后继 B 的登记与资源。
4. 凭据终态失效：同代 A 结束被拒（401）、B 继续可用；跨重启结果一致；/msg 以外的工具同样拒绝已结束执行。
5. 规则 / 工具描述实际刷新：工具描述修改/删除后，模型下一次空闲唤醒收到新版或移除旧描述；AGENTS.md 修改/删除通过 `/new` 后新 sid 分别验证命中新规则及移除旧规则，同 sid 保留原规则为预期行为。
6. 配置参数删除恢复默认：模型/推理参数删除后，下一轮唤醒恢复默认值（7.2 表待验证项）。
