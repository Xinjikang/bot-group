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
