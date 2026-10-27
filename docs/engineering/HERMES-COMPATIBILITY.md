# Hermes 会话兼容

引擎按实际接口检查 Hermes 会话能力。已验证的源码基线为 v0.21.1 (`cdf4c765213d7f4f7562f642e2b2df00d42c8ffd`) 和 v0.21.3 (`afc3b7c6f3`)；其他构建需满足相同接口契约。

## 入口与源码

`engine.hermes_runtime` 供安装检查、唤醒和会话适配共用。入口按 `HERMES_BIN`、`HERMES_HOME/bin`、PATH、默认源码虚拟环境顺序选择。Windows 继续使用原生 EXE 传递多行正文；仅支持已知的纯转发 CMD/BAT 包装。

从实际入口附近识别含 `hermes_state.py` 和 `hermes_state_registry.py` 的源码目录，支持源码虚拟环境与根目录 bin 布局。`HERMES_HOME/hermes-agent` 存在时，识别结果必须与其一致。运行中已导入的 Hermes 状态模块也必须属于同一源码；更换安装后重启引擎。

`HERMES_HOME` 表示引擎管理的 Hermes 根目录，命名成员的数据库位于其 `profiles/<name>/state.db`。profile 范围的 home 保持 CLI 管理模式。配置显式入口时同时配置对应的 home，避免默认目录归属歧义。

## 能力与会话

安装与启动检查静态读取源码声明，输出源码路径、版本、ID 模式及兼容状态，保持数据库和源码执行隔离。执行前检查实际导入来源、`hermes_state_registry.acquire` 与 SessionDB 的 `create_session`、`get_session`、`set_session_title`、`resolve_session_by_title`、`delete_session_if_empty`、`get_compression_tip`、`close`。

新版调用 `hermes_state_ids.new_session_id()`。该模块文件缺失的旧版采用其 CLI 原有的时间戳加六位 UUID 十六进制格式。模块存在时的内部依赖、语法或接口异常会明确失败。数据库创建、标题设置和失败清理由原生 SessionDB 执行。

已绑定会话继续用真实 session_id；标题仅用于旧会话首次迁移。`/new`、generation、失效绑定检查及压缩续接保持既有行为。

## MCP 注入

固定 MCP 写在 `HERMES_HOME/config.yaml` 顶层 `mcp_servers`（`{name: {command, args, env}}`），网关启动时加载，会话快照 `info.mcp_servers` 列出已加载名称。该字段缺失或类型异常按「未知」处理，不能当作空列表。

会话级 MCP 走 `session.mutate` 的 `editor_mcp` 操作，必填 `session_id`、`request_id`、`expected_revision`、`expected_generation`、`operation`、`payload={"mcp_servers":[spec…]}`。`spec` 为 ACP `McpServerStdio`，`env` 必须写成 `[{name, value}]` 列表。该操作只对 `policy.source == "acp"` 的会话开放；`session.create(source="tool"|"cli")` 的会话调用返回 `invalid_params`。

编辑器 MCP 的明文列表只保留在网关进程内，落盘与回执只有 fingerprint，网关重启后不恢复，客户端需重新注入。工具清单在 admission 起始冻结：同一会话再次更新且工具 schema 未变时复用既有连接（进程内环境变量不刷新）；schema 变化后新会话首次执行才会加载新清单。需要「换地址或换凭据立即生效」时，重建会话或重启网关。

网关 authority 端口只提供 ws/health 等路由，`/api/mcp/servers` 在裸 `gateway run` 下返回 401；配置写入需另起 `hermes serve` 的 dashboard，或离线修改 config 后重启网关。

验证：`python -B scripts/gw_mcp_probe.py` 覆盖清洁基线、固定配置、两项反向隔离与会话级注入，详见 [测试索引](../../scripts/TEST_INDEX.md) 与 `reports/research/hermes-gateway/evidence-2026-10-28-task253/`。

## 故障处理

- `HermesCompatibilityError`：检查日志中的缺失模块或接口，更新或修复 Hermes 后重启引擎。
- `HermesConfigurationError`：统一 `HERMES_BIN` 与 `HERMES_HOME` 指向的安装；修正后重启引擎。
- 两类异常均归为 `configuration`，聊天给出对应操作提示，具体路径与模块详情保留在诊断日志。

Hermes 检查结果单独报告，服务本体与 Codex 保持可用。静态检查通过后，运行期仍会验证实际导入依赖；版本字符串用于诊断，完整可用性以实际接口和隔离运行结果为准。

## 验证

运行 `scripts.test_hermes_compatibility`、`scripts.test_execution_errors`、`scripts.test_install`、`scripts.test_hermes_transport` 和 `scripts.test_runtime_fork`。对每份真实源码运行 `scripts/verify_hermes_compatibility.py --source <源码目录>`，使用临时 home 和数据库验证创建、恢复、标题迁移、历史保留、`/new`、压缩续接、失效绑定和独立进程重启恢复。详见 [测试索引](../../scripts/TEST_INDEX.md)。
