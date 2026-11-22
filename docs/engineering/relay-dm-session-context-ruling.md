# 私聊工具身份核验的宿主会话来源口径（req#173 task#324）

本文记录插件私聊工具（listen/register）身份核验可用的宿主会话来源字段，供插件实现与验收引用。核实对象为 Hermes 宿主源码（本机安装目录 `hermes-agent/`，只读核实，未改宿主源码），核实时间 2026-11-20。

## 一、字段定位（gateway/session_context.py）

宿主为每个 gateway turn 绑定一组会话来源 ContextVar（`_SESSION_VARS`，共 20 个）。与私聊身份核验相关的三个维度如下：

| 维度 | 变量名 | 语义 |
| --- | --- | --- |
| 渠道 | `HERMES_SESSION_PLATFORM` | 消息平台（feishu/telegram/discord 等）；本地会话（CLI/TUI/desktop）该值为空，改看 `HERMES_SESSION_SOURCE`（cli/tui/desktop/acp 等） |
| 私聊类型 | `HERMES_SESSION_CHAT_TYPE` | 归一化枚举：`dm` / `group` / `channel` / `thread`。飞书 adapter 把 p2p 归一化为 `dm`、其余为 `group`（plugins/platforms/feishu/adapter.py `_resolve_source_chat_type`） |
| 发送者 | `HERMES_SESSION_ACTOR_ID` / `HERMES_SESSION_ACTOR_NAME` | 本回合受信发送者。当轮次事件被合并（steer/redirect 折叠进另一发送者）时两者为空——空 = 无单一发送者 |

区分要点：`HERMES_SESSION_USER_ID` 是会话路由不是发送者，共享聊天（Telegram observe mode）下为空；不得把 USER_ID 当发送者做授权，只可作审计。该口径与宿主官方插件文档（website/docs/developer-guide/plugins/index.md）及宿主授权读法（gateway/authz_mixin.py `_principal_authorized`）一致。

## 二、可读位置与边界

1. 插件私聊工具（`ctx.register_tool` 注册的 handler）由 gateway dispatcher 在 turn 上下文内调用，handler 运行在 gateway 进程内，可直接读取上述 ContextVar：`from gateway.session_context import get_session_env`，读到的即当前 turn 的权威绑定值。
2. dispatcher 注入 handler 的 kwargs（session_id/task_id 等）是会话句柄不是身份，不可作为授权依据（沿用既有结论，只作审计日志）。
3. 边界：随包 task_queue MCP 是 stdio 子进程，其环境由 `_build_safe_env` 构造（tools/mcp_tool_config.py），白名单不含 `HERMES_SESSION_*`；会话来源桥接 `_inject_session_context_env` 只服务 terminal/execute_code 等 spawn surface，不覆盖 MCP stdio 启动链。因此随包 MCP 进程读不到这些字段——私聊身份核验必须留在插件工具层（gateway 进程内），不能挪进随包 MCP 进程。

## 三、私聊核验口径（建议）

三个维度逐项核对，任一步缺失或为空即默认拒绝：

1. 渠道白名单：`HERMES_SESSION_PLATFORM`（空则 `HERMES_SESSION_SOURCE`）必须在授权渠道白名单内；
2. 私聊类型：`HERMES_SESSION_CHAT_TYPE == "dm"`（listen/register 是管理员私聊操作，group/channel/thread 一律拒绝）；
3. 发送者：`HERMES_SESSION_ACTOR_ID` 非空且命中管理员白名单；为空（无单一发送者）直接拒绝。

CLI 入口的授权不受上述字段影响，按本地 OS 主体核验（Windows 取内核身份而非 getpass.getuser()）。

## 四、隧道白名单断点坐实

1. MCP loopback 隧道路径白名单 `_HTTP_TUNNEL_PREFIXES = ("/health", "/projects/", "/tasks/", "/group/", "/auth/status")`（engine/relay_server.py），不覆盖 `GET /members`（engine/api.py 的 group_list_members 落点）——经隧道请求 /members 必 403。
2. `GET /members` 走 access.py 的 `require_project(profile, project)`——转发只注入 X-Group-Profile 而无成员凭据时必 401。修复方向为按通道配对成员签发有效成员凭据并补白名单。

## 五、取得与传入链路（manager 追问补充，2026-11-20）

**问 1：宿主从哪里取得私聊渠道和发送者标识？**

源头 = 平台 adapter 入站事件。以飞书（本仓宿主平台）为例（plugins/platforms/feishu/adapter.py）：

- `_process_inbound_message`（2561 行起）接收 WebSocket 事件，从事件消息取 sender_id 与 chat_type；发送者主标识 `sender_primary` 取 `sender_id.open_id / user_id / union_id`（2586-2589）；`_resolve_sender_profile` 解析发送者 profile（user_id / user_name / user_id_alt）（2598）；
- `build_source(...)`（2599-2609）构造 SessionSource：`chat_type` 经 `_resolve_source_chat_type` 归一化（p2p→"dm"，其余→"group"，3192 行），`user_id` = 发送者 profile 的主 ID；
- 2620-2628：包成 MessageEvent 进入平台无关的入站链。

平台无关路径：任何 adapter 均产出 `SessionSource(platform, chat_id, chat_type, user_id, ...)` + `MessageEvent`，会话来源字段的取值与飞书同构。

**问 2：怎样传入 gateway.session_context？**

- `gateway/session.py build_session_context`（1460-1479）：`actor = event if event.user_id 非空 else source`——**发送者（actor）= 本回合事件身份，会话路由（source）= 会话级身份**；SessionContext.actor_user_id / actor_user_name / turn_event 由此确定（SessionContext 定义 177-203，183-188 注释即此语义）。
- `gateway/run.py _set_session_env`（3910-3940）：把 SessionContext 逐字段绑定进 ContextVar——`platform→HERMES_SESSION_PLATFORM`、`chat_type→HERMES_SESSION_CHAT_TYPE`、`user_id→HERMES_SESSION_USER_ID`、`actor_id/actor_name→HERMES_SESSION_ACTOR_ID/_NAME`（actor 为 None 时 set_session_vars 内部回退路由 user，gateway/session_context.py:182）。
- 每条消息入站先 `reset_session_vars()`（run_inbound.py:164-168，防跨会话泄漏），绑定范围 = 当前 turn 的任务上下文；插件工具 handler 在 turn 内被 dispatcher 调用，进程内读 ContextVar 即当前 turn 的权威值。
- 准入链：无 user_id / 未授权的入站在绑定前即被丢弃（run_inbound.py:228-233）——到达插件工具 handler 的 turn 必然已过宿主 `_is_user_authorized_for_source` 闸门；但插件自己的管理员白名单（BOT_GROUP_RELAY_ADMIN）是独立授权层，仍须 handler 内逐项核验。

**问 3：字段缺失时怎样拒绝请求？**

缺失三态（gateway/session_context.py）：

1. 变量从未绑定（`_UNSET`）：`get_session_env` 回退 os.environ、再回退 default，插件读回空串（220-235）；
2. 明确清空（`""`）：非消息 turn（如插件命令 handler 无 turn 上下文）或 `clear_session_vars` 之后（192-203）；
3. ACTOR 特殊缺失：`_bound_session_value`（224-227）在轮次事件被折叠（turn_event.user_id 为空）时强制返回空串——「本轮无单一发送者」。

拒绝语义：插件 `_check_authorized`（本仓 plugins/hermes/bot-group-relay/tools.py:46-56）已实现 fail-closed 骨架（白名单空→拒；caller 空→拒；不在名单→拒）。补全要求 = 把渠道（PLATFORM 为空或非白名单→拒）与类型（CHAT_TYPE ≠ "dm"→拒）加入同一条 fail-closed 链。**判定一律「空或非白名单 = 拒绝」，绝不拿 USER_ID 当发送者兜底**；session_id/task_id 只进审计日志。

## 六、代码位置索引（供后端接入与审查复核）

| 事项 | 位置 |
| --- | --- |
| 会话来源变量定义与读取 | gateway/session_context.py:34-50（定义）、159-189（set_session_vars）、220-235（get_session_env / ACTOR 折叠） |
| SessionContext 与 actor 选择 | gateway/session.py:177-203（定义）、1460-1479（build_session_context） |
| 逐字段绑定传入点 | gateway/run.py:3910-3940（_set_session_env） |
| 入站重置与未授权丢弃 | gateway/run_inbound.py:164-168、228-233 |
| 飞书入站源构造 | plugins/platforms/feishu/adapter.py:2561-2617、3192（p2p→dm） |
| 插件侧现有核验（fail-closed 骨架） | 本仓 plugins/hermes/bot-group-relay/tools.py:29-56 |
