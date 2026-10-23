# bot-group 工程说明

本文描述系统架构与维护契约。完整专题入口见 [文档索引](../INDEX.md)，资料存放规则见 [文档与测试报告](../guides/DOCUMENT-STORAGE.md)。部署现场是否已加载代码，以实例与实际验证为准。

## 1. 系统边界与身份

bot-group 是常驻协作服务：保存项目、需求、任务、消息和执行记录，调度 Hermes/Codex 子进程，通过同源 Web 页面提供管理与对话入口。主要对象如下。

| 对象 | 当前约定 |
| --- | --- |
| 项目 | `projects.name` 是稳定 ID，`display_name` 是可编辑展示名；业务记录按 project 隔离 |
| Bot | `bots` 保存宿主类型、物理 profile、配置根目录、runtime 和统一显示名；物理身份由宿主、配置根和 profile 共同确定 |
| 成员 | `members.profile` 是引擎成员标识，`bot_id` 关联 Bot；`project_members` 保存项目内启停、角色和模型覆盖 |
| 角色与授权 | `project_roles.id` 是稳定角色 ID；有效权限取成员直接授权与所属角色授权的并集，成员需在项目内启用 |
| 人侧身份 | 管理员 token 或未设密码时的 dev 模式；内置 `user` 参与消息展示，调度时排除 |
| 执行 | 以项目、成员、kind、话题、session 和启动时间定位；chat/work 分开，运行状态与消息送达分别记录 |

角色由项目定义，任务交接使用花名册中的具体成员 `next_bot`。负责人参与协调和异常通知，其业务操作同样经过项目授权。详见 [项目配置](PROJECT-CONFIGURATION.md)、[Codex 配置目录及入组授权](../guides/CODEX-PROFILE-HOMES.md)。

## 2. 总体架构与代码入口

```text
浏览器 /app ── HTTP、SSE ── FastAPI（main / api）
                                  │
           消息路由、任务池、项目配置、身份与权限
                                  │
                       SQLite group.db
                                  │
                 queue + scanner → wake
                                  │
                  Hermes / Codex 子进程
                                  │
                  统一 MCP mcp/server.py
                    ├─ 群聊工具 → HTTP API
                    └─ 任务工具 → 引擎 HTTP API → engine.taskpool → group.db
```

`main.lifespan` 初始化数据库与恢复记录，启动队列及扫描线程，并建立 SSE 跨线程投递所需的事件循环。前端由同一服务静态托管。MCP 作为 HTTP 客户端调用引擎，数据库访问集中在引擎进程；程序目录与持久数据目录可分别配置。

| 子系统 | 主要代码 | 职责与专题 |
| --- | --- | --- |
| 服务与配置 | `main.py`、`api.py`、`config.py`、`auth.py`、`events.py` | HTTP、身份检查、SSE、有效实例配置；[安装部署](../guides/RELEASE-GUIDE.md) |
| 数据与任务 | `db.py`、`schema.sql`、`taskpool.py`、`task_brief.py`、`requirement_gate.py`、`task_retry.py`、`taskqueue_proxy.py` | 迁移、事务、需求拆解、任务领取/流转/重试；[任务契约](TASK-BRIEF-CONTRACT.md)、[拆解设计](../design/DECOMPOSE-AUTH-REDESIGN-2026-10-11.md) |
| 消息与执行 | `router.py`、`delivery.py`、`mentions.py`、`message_execution.py`、`queue.py`、`scanner.py`、`wake.py`、`heartbeat.py` | 发言轮次、未读、注入、派发与运行判活；[消息注入](../design/MESSAGE-INJECTION-DESIGN-2026-09-27.md)、[执行归属](BOT-MESSAGE-EXECUTION.md) |
| 会话与控制 | `session_adapter.py`、`commands.py`、`steer_ledger.py`、`stop.py` | 会话绑定、命令、纠偏回执与范围内停止；[命令与回执](../design/SLASH-COMMAND-DESIGN-2026-09-27.md) |
| 宿主与配置 | `bot_identity.py`、`discovery.py`、`profile_create.py`、`profile_config.py`、`codex_profile_config.py`、`codex_project_access.py`、`project_models.py` | 双宿主发现、入组、配置读写、目录授权、项目模型覆盖；[创建 profile](PROFILE-CREATE.md)、[统一模型编辑](UNIFIED-MODEL-EDITOR.md) |
| 项目准备 | `project_import*.py`、`project_initialization.py`、`project_setup*.py`、`project_templates.py`、`project_readiness.py`、`project_team.py` | 七步向导、预览/revision、Git/文件/团队提交、恢复及就绪门禁；[向导](PROJECT-SETUP-WIZARD-2026-10-14.md) |
| 项目管理 | `project_permissions.py`、`project_rules.py`、`agents_md.py`、`project_workspaces.py`、`project_removal.py`、`workspace_resources.py`、`directory_list.py`、`directory_picker.py` | 角色授权、规则同步、worktree、逻辑移除、隔离资源与目录导航；[项目配置](PROJECT-CONFIGURATION.md)、[工作区资源](WORKSPACE-RESOURCES-2026-10-14.md) |
| 内容与观察 | `attachments.py`、`reasoning.py`、`codex_reasoning.py`、`appearance.py` | 图片附件、双宿主推理读取、服务端背景；[附件](IMAGE-ATTACHMENTS.md)、[缓存与背景](BROWSER-CACHE-LIFECYCLE-2026-10-17.md) |
| 生命周期 | `lifecycle.py`、`process_tree.py`、`browser_resources.py`、`restart.py` | 退出排空、进程树终止、专属浏览器回收、重启回执；[服务重启](SERVICE-RESTART-2026-10-17.md) |

表内 Python 模块均位于 `engine/`；MCP 位于 `mcp/`，前端位于 `frontend/`，安装、发布、启停和测试入口位于 `scripts/`。

## 3. HTTP 与 MCP 契约

HTTP 路由以 `engine/api.py`、`engine/main.py` 为准。FastAPI 默认 `/docs` 与 `/openapi.json` 提供已启动实例的 HTTP 模式；MCP 签名以 `mcp/server.py` 的注册工具为准。Bot 工具与底层 HTTP 请求的参数分别遵循各自契约。

| 接口组 | 代表入口 | 说明 |
| --- | --- | --- |
| 登录与实例 | `GET /auth/status`、`POST /auth/login`、`GET /health` | 免密探测、限流登录、token；health 含 PID、instance_id 及优雅退出能力，管理员可读取程序根和数据库路径 |
| 用户称呼 | `GET/PATCH /user/display-name` | 用户身份或 dev 模式读取/修改，Bot 请求返回 403；请求只接受 `display_name`，响应含固定 `profile: user`，使用 `Cache-Control: private, no-store` |
| 消息与话题 | `POST /msg`、`POST /thread`、`GET /thread`、`GET /threads`、`GET /messages` | 发言、建话题、状态与历史；创建话题支持幂等请求，见 [创建幂等](THREAD-CREATION-IDEMPOTENCY-2026-10-14.md) |
| 未读与观察 | `GET /inbox`、`POST /inbox/seen`、`GET /inbox/summary`、`GET /thinking`、`GET /reasoning`、`GET /events` | 按项目/话题读取、标已读、执行与推理窗口、SSE |
| 项目与成员 | `/projects`、`/members`、`/bots/register`、项目 roles/permissions/workspaces 系列 | 管理操作逐入口检查管理员或业务权限；专题见索引 |
| 任务控制 | `/tasks`、`/requirements/{requirement_id}`、`/thread/task-update`、`/admin/task/{task_id}/…` | 列表详情、话题修订代理、人侧任务控制；保留终态与并发检查 |
| 生命周期 | `GET/POST /admin/shutdown`、`GET/POST /admin/restart` | 管理员发起与查询；重启 POST 返回 202 |

`group_send(text, need_reply)` 只接受正文和明确的回复要求。项目、话题与执行标识由引擎提供的 `XDG_GROUP_ENGINE_EXECUTION` 推导，执行上下文须有效且匹配成员。work 发点对点消息；chat 发当前话题消息。`group_read_inbox(after_id=0, upper_id=None)` 默认按当前执行分流，分页参数用于正文注入降级后的固定水位补读。图片通过 `group_read_image` 获取。完整示例见 [MCP README](../../mcp/README.md)。

`TASK_QUEUE_ROLE` 保存具体引擎成员 profile；`HERMES_PROFILE` 用于群聊 header 身份，缺失时回退到前者。成员请求同时携带绑定该 profile 的 `X-Group-Key` 凭据；项目角色保存在数据库。路由默认仅向管理员开放，成员入口逐项校验项目、话题与自身资源范围。认证、事件流 cookie 和凭据轮换见 [访问控制](ACCESS-CONTROL.md)，部署边界见 [部署说明](../guides/DEPLOY.md)。

## 4. 数据模型与迁移

业务共用 `group.db`，连接使用 SQLite WAL 与 busy timeout。表结构以 `schema.sql` 加 `db.init_schema` 的增量迁移为准；跨模块读取统一通过配置解析数据库路径。

| 数据组 | 表 | 关键关系 |
| --- | --- | --- |
| 项目与身份 | `projects`、`bots`、`members`、`project_members`、`project_roles`、`project_permissions`、`settings` | 项目稳定 ID、Bot 物理身份、项目成员与角色授权；角色授权以 `role:<id>` 存储 |
| 准备与目录 | `project_initializations`、`project_setup_requests`、`project_workspaces`、`project_workspace_modes`、`workspace_resources`、`project_rule_copies`、`project_config_events` | 预览/提交记录、幂等请求、成员目录、资源版本、规则副本指纹和审计 |
| 消息 | `threads`、`thread_create_requests`、`messages`、`inbox`、`attachments` | 话题成员、轮次与发言位；消息项目和回应树；按接收者记录未读；附件绑定消息与项目 |
| 需求与任务 | `requirements`、`decompose_requests`、`tasks`、`nodes`、`events` | 需求拆解状态/占用与幂等；任务说明、依赖、具体接手人；节点及变更历史 |
| 调度与运行 | `wake_jobs`、`wake_runs`、`message_executions`、`codex_run_events`、`browser_resources` | 排队、启动/退出、消息执行归属、Codex 事件及专属资源登记 |
| 会话与命令 | `session_bindings`、`session_switch_ops`、`commands`、`steer_operations` | 项目/Bot/kind/话题绑定真实会话，切换记录、命令结果和纠偏分层回执 |

话题状态为 `open / settled / closed`；inbox 为 `pending / seen / expired / failed`，整组陈旧终态见 [消息注入](../design/MESSAGE-INJECTION-DESIGN-2026-09-27.md)。chat 队列活动项按 `(profile, thread_id)` 去重，work 按 `(profile, project)` 去重。`wake_runs` 保存启动器 PID、runtime 会话与退出记录；消息携带的执行信息通过服务端校验后关联到具体运行。

业务关键写入通过事务、条件更新和版本校验解决并发：例如任务领取/交接、需求拆解占用、话题发言位、成员变更和项目归档。文件/Git 操作另有互斥、指纹及失败恢复，跨 SQLite 与文件系统的边界见对应专题。历史数据迁移在启动时执行；升级先在独立数据库副本验证迁移。

任务创建与推进要求父需求处于 `open/blocked` 且未归档；领取、扫描和重试使用相同门禁。拆解完成要求所有未取消任务已发布且说明完整，失败时本次创建事务回滚。详细行为见 [生命周期门禁](ACCESS-CONTROL.md#需求终态与任务推进)。

## 5. 调度、消息与会话

### 消息与话题

点对点消息具有项目归属，正文 @ 接收成员；话题消息由 thread_id 确定项目与成员。普通 Bot 发言检查话题成员资格和当前发言位，user 可插话。消息保存后按接收者计算回复要求并分发 inbox；处理完成由 Bot 标记自己的已读，显式回复要求参与已读门禁。

话题按轮次及回应树 DFS 推进，独立发言为根、@ 回应形成子树，深度上限为 4。新增成员补历史未读并等待合法发言位。普通用户插话归当前轮，正在进行的发言继续完成；轮次领取与话题状态在队列消费时再次核验。

| 状态 | 自动讨论 | 用户操作 |
| --- | --- | --- |
| open | 按轮次派发；全员跳过或静默超时可收口 | 发消息、收口、关闭 |
| settled | 暂停自动派发，清除发言位并过期当前未读/取消排队唤醒 | 新消息恢复 open 并开始新轮；可关闭 |
| closed | 停止讨论并拒绝发言 | 重新打开后进入 settled，等待新消息 |

静默收口阈值为 30 分钟。`[收口]` 暂停讨论；关闭时才填写结论。收口操作保留执行进程，后续自主发言受话题状态约束。

### 注入与派发

`wake` 在 spawn 前读取当前执行范围内的 pending 快照，注入消息正文、ID、发送者、有效回复要求及附件索引。超过命令预算时整段退化为纯信号，携带固定上界，通过 `group_read_inbox` 分页补读。控制字段与消息正文分开处理。Windows Hermes 入口解析为可验证的原生启动器以保留多行参数，见 [参数传输](HERMES-ARGUMENT-TRANSPORT-2026-10-17.md)。

扫描周期为 **180 秒**，五个来源依次为 inbox、任务、话题静默、轮次收口、需求拆解。调度按项目就绪、归档/移除、成员启停、任务状态和执行存活过滤。待领任务要求已发布、说明完整、前置任务全部 done，并精确派发给 `next_bot`。任务失败按 `task_retry` 共尝试三次，最终 technical blocked 并通知有效负责人或 user；人工停止、撤回、完成和交接保留各自结果。

需求扫描处理 `pending` 或占用超时的 `decomposing` 状态。通过门禁后，从有效 `task.create` 授权成员按 role_id、profile 顺序确定一名候选；无候选时记录 gate_reason，并尝试通知已配置负责人。拆解以凭据占用、续期及幂等键控制多次提交；`task_create(..., complete=True)` 显式提交完成，也支持 `tasks=[]` 完成已有批次。

### 宿主与会话

Hermes 使用原生 chat 子进程及 `session_adapter` 管理的会话；Codex 使用 `exec --json`，按绑定的原生 thread_id 恢复，输出事件关联执行记录。两者共用项目、工作目录、模型覆盖、消息注入与执行标识。Codex 的审批、reviewer、网络和其他策略沿用所选 profile；入组与显式目录变更按 [目录授权规则](../guides/CODEX-PROFILE-HOMES.md) 保存文件系统授权。

chat 与 work 分别排队和定位会话，同一成员可同时参与不同执行范围；同一范围通过队列及运行检查控制重复启动。`/new` 和 `/stop` 按目标宿主和执行范围处理；当前 `/steer` 传输入口返回能力不可用，账本与适配设计见 [斜杠命令专题](../design/SLASH-COMMAND-DESIGN-2026-09-27.md)。投递、消费证据、推理展示与实际执行效果分别核验。

启动恢复核对历史执行与进程状态，重新排队适合恢复的任务，并保留话题当前发言位、轮次、回复对象和未读。无法确认退出的执行先延迟处理，避免重复派发。

## 6. 前端

右上角“我的称呼”保存到引擎数据库内置 `user` 行的 `bots.display_name`；所有项目共用同一值。新库默认“用户”，现有内置行保留原值；从仅有旧 `members.user` 的数据库迁移时沿用该行名称。修改称呼同步旧成员行的展示字段，身份 ID、profile、消息发送者、权限、路由及调度规则保持独立。Bot 对话中的称呼由个人配置决定，通用提示使用“用户”。

`GET/PATCH /user/display-name` 只接收称呼字段，长度为 1–200 个 UTF-16 码元，去除首尾空白并拒绝空值、控制字符、换行及无效 Unicode。数据库事务成功后广播 `user.display_name.changed`；页面启动、变更事件与 10 秒轮询从服务端读取。浏览器仅在页面内存保存当前展示值，localStorage/sessionStorage 均不保存称呼。消息署名、头像和 @ 高亮随称呼刷新，消息正文与路由中的 `user` 标识保持原值。

`frontend/index.html` 提供三栏 SPA：话题、消息、任务；独立 JS 模块负责项目向导/设置、成员身份、消息导航、附件、命令、资源表单及重启。共享外观位于 `appearance.css`，弹窗共用 `dialogs.js/css`。模块入口见 [前端 README](../../frontend/README.md)。

登录入口探测 `/auth/status`，仅 `configured === false` 直接进入；已有 token 沿用登录态。SSE 已连接显示绿色，连接/重连中显示黄色，连接关闭显示灰色；重启期间显示退出阶段。消息分页、SSE 增量与异步响应按当前视图隔离，未变化节点、选区和草稿尽量保留，详见 [刷新范围](FRONTEND-REFRESH.md)、[消息导航](MESSAGE-NAVIGATION.md)。

项目详情统一管理成员、角色、权限、目录和配置。模型编辑默认保存项目覆盖，“应用到全局”更新全局并清空当前项目覆盖；描述、SOUL/AGENTS、环境变量走全局配置。新建/导入共用七步向导：项目信息、协作方式、角色、规则、成员、资源、确认。预览提供 revision，确认阶段才写入；相关恢复与边界见 [向导](PROJECT-SETUP-WIZARD-2026-10-14.md) 和 [清理与迁移](PROJECT-SETUP-CLEANUP-2026-10-15.md)。

推理按 runtime、session 和启动时间窗口读取 Hermes/Codex 源记录；浏览器缓存气泡内容、布局和采集游标。结束运行保留 7 天、最多 100 次，持久化体积上限为 512Ki UTF-16 码元；配额不足逐级裁剪并保留提示。当前运行优先，刷新后继续必要补查。自定义背景通过 `/appearance/background` 保存在数据目录，同一服务共享。详情见 [缓存生命周期与背景](BROWSER-CACHE-LIFECYCLE-2026-10-17.md)。

## 7. 协作与授权

Bot 通过统一 MCP 进行群聊及任务操作。身份从进程配置和执行上下文取得，角色及权限按当前项目读取；会话恢复继续保留项目隔离。

| 操作 | 边界 |
| --- | --- |
| 讨论、查询、创建需求、拆解与草稿修订 | chat/work 均可使用相应工具，写入受项目授权、需求门禁和凭据约束 |
| 领取、节点交付、完成、block | `task_claim/task_log/task_complete/task_block` 仅 work 注册并受运行时守卫保护 |
| 话题任务修订 | `group_task_update` 经 HTTP 代理检查话题、关联任务、权限和执行凭据 |
| 人侧控制 | 管理员端点按身份检查；任务/需求控制保留终态及并发条件 |
| 交接 | 查询本项目花名册，选择具体 `next_bot`，按任务节点和依赖推进 |

公共协作规则、项目配置、工作目录和分配资源由引擎注入。主目录 `AGENTS.md` 是项目规则来源，每次唤醒提供当前规则快照，恢复会话同样取得更新；独立工作区维护受管 `AGENTS.override.md`，按指纹核验和同步。详见 [协作入口](COLLABORATION-ENTRY-2026-10-23.md)、[项目配置](PROJECT-CONFIGURATION.md)、[工作区资源](WORKSPACE-RESOURCES-2026-10-14.md)。

## 8. 配置、安装与生命周期

`config.py` 统一解析配置。监听默认 `127.0.0.1:8180`；host/port 使用命令行 > 环境 > 数据目录 `.env` > 默认值。数据目录选择顺序为命令行 > 环境（`GROUP_DATA_DIR` / `DATA_DIR`）> 程序根目录，各平台均默认使用程序根目录。数据目录定位发生在读取 `.env` 之前，显式配置的目录继续优先使用。

MCP 工具经 HTTP 调用引擎；`task_api.py` 提供需求与任务操作的固定白名单及输入、成员和执行校验，再调用任务业务模块。Codex 通过命令行、Hermes 通过每次运行的配置覆盖目录接收 API 地址、身份和上下文。`mcp_runtime.py` 管理 Hermes 临时配置的创建与清理；成员配置生成时仅保存个人设置。详见 [MCP 接入与 API 契约](../../mcp/README.md)。

`GROUP_DB` 可覆盖数据库路径；附件默认跟随数据库目录，可用 `GROUP_ATTACHMENTS_DIR` 覆盖。日志、备份、安装状态及背景跟随数据目录。`service.py` 导入该目录 `.env` 中尚未定义的环境变量；直接启动 `python -m engine.main` 时，进程环境控制数据库、密码等环境项。Bot 回调 URL 优先 `GROUP_ENGINE_URL`，其次 `XDG_GROUP_ENGINE_URL`，最后按 host/port 派生。

安装采用专用 `.venv` 与发布锁定依赖。`bootstrap` 负责下载校验和安装，`install` 检查/安装或修复旧环境就绪记录，`service start/stop` 负责启停，`upgrade` 负责正式版本发现、资产校验、备份、文件级升级及失败恢复。执行方式和受管文件边界见 [发布指南](../guides/RELEASE-GUIDE.md)。

重启按钮位于主题右侧、连接状态左侧，调用管理员接口启动独立隐藏助手，助手复用 `start.bat`。引擎退出依次经过 `draining → terminating（超时需要时）→ releasing → stopping`：先关闭新执行入口，最多等待 120 秒，超时由引擎终止匹配的 Bot 进程树，核验执行收尾，再释放登记浏览器资源。清理失败记录 failed；最终退出尚未开始时恢复入口，供处理后重试。

脚本确认原 PID 与端口释放后启动新实例；成功回执要求新 instance_id、程序根和数据库一致。页面刷新恢复同一操作轮询。停机客户端默认等待 180 秒，页面每次进入重启轮询最多等待 300 秒（刷新恢复时重新计时）；120 秒是 Bot 自然结束等待阶段。具体错误、日志和进程树保护边界见 [服务重启](SERVICE-RESTART-2026-10-17.md)、[浏览器生命周期](BROWSER-LIFECYCLE-2026-10-16.md)。

## 9. 验证与维护

前端视觉和组件规则见 [UI 规范](../ui-spec/UI-SPEC.md)。前端成员维护现行主题、布局和状态约定，审查员按规范版本核验；规范与实现同步提交。

测试入口统一见 [scripts/TEST_INDEX.md](../../scripts/TEST_INDEX.md)。测试覆盖 Python 单元/隔离集成、Node VM、真实 Chromium、MCP 协议及显式现场探针；一次 unittest discover 只覆盖其中一部分。测试先确认临时数据库、目录、端口、进程和宿主 profile 的范围，生产数据保持独立。安装/升级测试需要隔离正常与回滚两条停机路径，见 [停机隔离记录](SHUTDOWN-ISOLATION-FIX-2026-10-17.md)。

按改动范围运行必要验证。纯文档更新检查路径、链接、源码契约及差异；行为变更运行对应回归并记录实际结果。开发完成同步文档、测试索引和 CodeGraph，提交后报告。引擎变更验收后由用户手动重启并刷新页面；MCP 由对应宿主重新加载，Hermes 网关持有的 MCP 需重启网关。

维护文档时更新本文及对应专题，需求与方案分别写入 `docs/requirements/` 和 `docs/design/`，随当前工作区分支提交。审查与验收结果写入任务节点；shared/worktree 成员以引擎注入的“项目主目录”拼接 `reports/`，保存测试结果和证据。独立专题新增或改名时同步 [文档索引](../INDEX.md)。
