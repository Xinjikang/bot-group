# mcp/ — 统一群聊与任务管理 MCP

更新：2026-10-20。`server.py` 使用 FastMCP，通过 stdio 同时提供群聊和项目/需求/任务工具。全部工具调用引擎 HTTP API，数据库定位、事务和业务校验由引擎处理。MCP 配置仅包含工具服务入口、成员身份、引擎地址及本次执行上下文。架构见 [工程说明](../docs/engineering/ENGINEERING.md)。

## 连接成员

MCP 是 Codex、Hermes 调用 bot-group 工具的接口，用于读取消息、参与群聊和处理任务。宿主启动 `mcp/server.py`，通过进程的标准输入和标准输出通信，这种连接方式称为 stdio。

1. 完成 bot-group 安装并启动引擎，准备好所选 Codex 或 Hermes 成员的模型与认证。
2. 打开项目详情中的“成员与目录”，通过“添加成员”选择已有成员或创建新成员，再配置角色、项目权限和工作目录。操作见[成员创建说明](../docs/engineering/PROFILE-CREATE.md)。
3. 按下表检查工具接入。引擎负责在实际执行时提供项目、话题和会话上下文。

| 使用方式 | MCP 配置 |
| --- | --- |
| 引擎唤醒 Codex 成员 | 启动参数注入工具服务入口、成员身份、API 地址和执行上下文 |
| 引擎唤醒 Hermes 成员 | 每次运行生成独立覆盖文件，通过 `HERMES_MANAGED_DIR` 指定给该子进程 |
| 新建或接入已有成员 | 模型、认证及个人设置保存在成员配置中；引擎运行时提供协作 MCP |

### 运行时配置

两个宿主的协作 MCP 都使用 `task_queue` 名称。引擎提供的同名字段优先；其他 MCP 名称保持原样。配置层按字段合并，旧条目中未覆盖的字段可能保留；使用不同名称保存的同一服务会被视为另一项 MCP。

Hermes 需要支持 `HERMES_MANAGED_DIR`。覆盖文件位于引擎数据目录 `runtime/mcp/hermes-<随机值>/config.yaml`；每次执行独立生成，退出或启动失败后清理。已有管理配置的其他字段及 `.env` 被保留到该次覆盖目录，成员的原始配置文件保持原样。管理目录中的临时文件应按本机配置资料保护。

已有 Hermes `task_queue` 若为 HTTP 配置，合并后会残留 `url` 并与引擎的 stdio 入口冲突，启动时会提示先移除旧条目。维护者清理旧配置后，再由引擎启动成员即可。普通 stdio 旧条目的命令、参数、API 地址和身份由本次注入覆盖。

新配置在下一次启动成员进程时生效。群聊发送仍需引擎建立的有效执行上下文；手动启动 MCP 可用于协议检查。

### 离线与结果未知

连接建立失败按 1/2/4/8 秒退避，最多 5 次、总预算 30 秒，耗尽返回 `engine_offline`。已经发出且回复丢失的写操作，MCP 自动按操作 ID 查证；仍无法确认时返回 `operation_unknown` 和 `operation_id`。调用 `operation_get(operation_id)` 可继续只读查证，`completed` 含原结果，`unknown` 需要维护者核对。原 MCP 进程内重复相同的未知请求会查询原 ID；更改参数会形成新请求，需先确认原操作结果。引擎重启后，原 MCP 进程继续使用同一引擎地址。凭据必须仍有效，回执按成员和执行隔离。事务、崩溃与升级边界见 [MCP 离线窗口与操作回执](../docs/engineering/HERMES-GATEWAY.md#mcp-离线窗口与操作回执)。

## 群聊工具

| 工具 | 当前契约 |
| --- | --- |
| `group_send(text, need_reply)` | 两个参数均必填；项目、thread_id、session、wake_started_at 从执行上下文取得。text 可为 null，用于允许静默跳过的发言位 |
| `group_read_inbox(after_id=0, upper_id=None)` | 默认按本进程身份读取；work 只读本项目点对点未读（每批最多 100），chat 读取本话题现场。分页参数供注入降级后的固定水位补读 |
| `group_read_image(attachment_id)` | 读取引擎保存的图片附件 |
| `group_mark_seen(message_ids)` | 标记自己的已读，受回复门禁检查 |
| `group_get_thread(thread_id)` | 读取话题上下文 |
| `group_create_thread(title, members, opening, task_id=None)` | 创建话题，可关联任务 |
| `group_task_update(thread_id, task_id, fields)` | 话题修订代理；MCP 自动附执行凭据，引擎检查话题、任务和授权 |
| `group_list_threads()` | 查询话题列表 |
| `group_list_members(role=None)` | 查询当前项目有效成员；按项目角色筛选后选择具体成员用于 @ 和 next_bot |

## 身份与执行范围

`TASK_QUEUE_ROLE` 保存具体引擎成员 profile；`HERMES_PROFILE` 用于 `X-Group-Profile` header，缺失时回退到前者。`GROUP_ENGINE_TOKEN`（兼容 `XDG_GROUP_ENGINE_TOKEN`）提供绑定该成员的凭据，MCP 通过 `X-Group-Key` 发送。项目角色由 `project_members.role_id` 关联角色实体。凭据生命周期和资源范围见 [访问控制](../docs/engineering/ACCESS-CONTROL.md)。

引擎在 spawn 时提供 `XDG_GROUP_ENGINE_EXECUTION` JSON，包含 profile、project、thread_id、session、wake_started_at。`group_send` 必须取得有效且匹配成员的执行上下文。chat 回复当前话题；work 发送本项目点对点消息，正文必须 @ 接收者。会话解析保留历史宿主命令行兼容路径，发送执行校验仍以有效上下文为门槛。

`need_reply` 显式传 true/false；确认类和收口类消息使用 false。话题收口进入 settled，用户新消息恢复讨论；关闭及结论由用户单独操作。使用 `tool_call` 时将参数放入 arguments：

```json
{"name":"mcp__task_queue__group_send","arguments":{"text":"话题回复","need_reply":true}}
```

```json
{"name":"mcp__task_queue__group_send","arguments":{"text":"@default 点对点消息","need_reply":false}}
```

两个示例分别用于引擎启动的 chat 和 work 执行；连接名前缀以宿主实际配置为准。

## 需求、任务与权限

`project_*`、`req_*`、`task_*` 具体签名见 `server.py` 的工具注册。工具身份来自本进程；项目授权按调用读取，成员直接授权与角色授权取并集。

需求和任务操作经 `POST /task-tools/{operation}` 访问引擎。请求体含 `arguments` 和可选 `execution`，操作名及参数使用固定白名单；成员身份由 HTTP 身份头确定。引擎检查项目成员有效性，存在执行上下文时检查运行记录、项目和话题一致性，执行类操作要求 work 上下文。业务层继续检查具体权限、拆解凭据、任务状态和依赖。API 失败时工具返回错误。

项目列表仅返回该成员启用的项目。项目新增、移除和归档沿用项目管理 API 的管理员限制，Bot 调用这些管理操作会被拒绝。

chat/work 均可在授权范围内创建需求、拆解任务、补齐草稿与发布；`task_claim/task_log/task_complete/task_block` 仅 work 注册，并有运行时守卫。拆解通过执行会话凭据占用需求，支持分批与幂等提交；任务需包含 description、design_basis、acceptance_criteria 和 prerequisites，接手人使用具体 `next_bot`。`task_create(..., complete=true)` 显式提交拆解完成，也支持 `tasks=[]` 提交已有批次。详见 [任务契约](../docs/engineering/TASK-BRIEF-CONTRACT.md) 和 [拆解机制](../docs/design/DECOMPOSE-AUTH-REDESIGN-2026-10-11.md)。

## 运行与验证

以下命令供维护者检查进程启动。先在项目根目录打开 PowerShell，将示例成员标识和引擎地址替换为实际值。成员内部标识可在已登录浏览器访问 `/members` 后读取 `profile` 字段。

```powershell
$env:TASK_QUEUE_ROLE = 'bot-12'
$env:HERMES_PROFILE = $env:TASK_QUEUE_ROLE
$env:GROUP_ENGINE_URL = 'http://127.0.0.1:8180'
$env:GROUP_ENGINE_TOKEN = '<管理员通过 /auth/member-token 获取的成员凭据>'
.venv\Scripts\python.exe mcp\server.py
```

运行后进程等待 MCP 客户端通过标准输入发送协议消息；完整接入由宿主按配置启动该进程并连接。

实际 Bot 执行由引擎建立会话和上下文。MCP 的 API 地址按 `GROUP_ENGINE_URL`、`XDG_GROUP_ENGINE_URL`、`http://127.0.0.1:8180` 的顺序选择；引擎启动成员时会注入实际监听地址。引擎更换数据库位置后，MCP 继续访问同一 API 地址即可。依赖随项目安装，清单见 `requirements.txt`，发布安装使用根目录 `requirements.lock`。

引擎管理的执行自动注入成员凭据。手工接入的 MCP 在引擎使用默认随机密钥重启后，需要更新成员凭据并重新加载；设置稳定的引擎侧 `GROUP_ENGINE_KEY` 可保持派生凭据稳定。主密钥只配置在引擎，MCP 接收派生后的单成员凭据。

验证入口：`python -B scripts/test_mcp_handshake.py`、`python -B scripts/test_mcp_e2e.py`，以及 [测试索引](../scripts/TEST_INDEX.md) 中的消息执行、项目权限和拆解回归。测试使用临时数据及隔离进程。更新 MCP 代码后重新加载承载进程；Hermes 网关持有的 MCP 通过重启网关加载。
