# Hermes 网关执行适配

## 接口与版本

`engine/hermes_gateway*.py` 使用公开 CLI `hermes gateway ensure --json`、本地 control 协议的 `session-ticket` 和 `/api/ws` 的 `hermes-gateway-v1`。适配器仅调用线协议，Hermes 源码保持独立。运行时核验 profile、协议、安装提交身份，以及 `durable-admission-v1`、`acp-session-mcp-v1` 能力。v0.22.5 验证基线为 `bf0203a794a07aac3ff764972ab9087baa4e4f4e`。

网关版采用独立执行路径；旧版 Hermes 和 Codex 保留现有运行时。安装来源由 `HERMES_BIN`、`HERMES_HOME` 和已有安装定位规则确定。

## 会话、MCP 和原生审批

新会话使用 `source=acp`，通过 `session.mutate editor_mcp` 注入本次执行的 task_queue。每轮先提交空列表以释放旧连接，再提交新凭据。实际工具发现和调用由 Hermes 原生机制完成。

危险命令审批采用 Hermes 原生 `approvals.mode: smart`，配置由管理员在 Hermes 中维护。ACP 编辑策略采用官方 `edit_policy=workspace_session`：普通工作区编辑按原生策略放行，敏感路径和工作区外请求遵循原生审批规则。引擎沿用这些原生能力。

旧 CLI 会话通过官方 `session.export` 和 `session.mutate import` 建立确定 ID 的 ACP 副本，原会话完整保留，映射记录在 `hermes_session_migrations`。活动会话先等待终态。`/new` 使用 `session.create`，绑定 generation 按现有流程递增。恢复、标题查找和思考记录分别使用 `session.resume`、`session.list`、`session.export`。压缩后的逻辑会话由 Hermes 解析。

## 执行与恢复

`hermes_executions` 在 `prompt.submit` 前提交执行身份、submission ID、会话/profile 身份及固定截止时间；回执到达后保存 admission ID、generation 和 authority epoch。引擎重启恢复原记录，并继续查询 `prompt.receipt`。已登记远端执行的投递作业保持已投递状态。

queued 执行通过 `prompt.cancel` 取消；started 执行通过携带 generation 的 `session.interrupt` 取消。只有对应 admission 的 terminal 回执才能结束执行。中断 ACK、WebSocket 断开和查看客户端退出均保留占用。用户停止工作执行时，经原生 `rewind` 回退本次指令；目标消息限定本次执行，回退目标和请求 ID 持久化后重试。

引擎优雅停止先关闭新任务入口，并等待正在执行的任务自然结束，默认等待 120 秒。超时后通过上述官方取消接口停止 Hermes 执行，等待 terminal 与临时 MCP 清理完成，再释放浏览器资源并退出引擎；共享网关进程保留。终态或清理尚未确认时，本次停机失败并恢复服务入口。`start.bat` / `start.sh` 只有确认旧引擎进程退出和端口释放后才启动新引擎。停机取消使用 `shutdown` 原因，保留会话历史。

终态立即撤销执行凭据；清理临时 MCP、浏览器资源和引擎记录后释放占用。MCP 心跳使用自身 PID，避免多个会话共享网关 PID；凭据校验约束项目、话题和 inbox 类型。引擎停机按执行发送取消，保持 HTTP/MCP 可用直到收尾完成。

提交回执缺失时，当前公开接口缺少按 submission ID 只读找回 admission 的入口。适配器保留 `unknown`，到固定截止时间转 `manual` 并保留占用。管理员需核对原执行，自动重提保持关闭。已有 admission 的执行在断线后重新取一次性 ticket，并查询原回执。

## 验证与启用

测试方法见 `scripts/TEST_INDEX.md` 的“网关正式适配”。真实验证使用独立 Hermes home、随机本地 HTTP 端口和隔离引擎数据库，覆盖原生审批配置、两轮 MCP 凭据刷新、回复/已读、取消/回退、重连、历史导出和 `/new`。引擎重启恢复、未知执行占用和旧版/Codex 兼容由相关回归覆盖。原始回执保存在主目录 `reports/tests/`。

部署时先安装引擎依赖，再选择对应 Hermes 安装。按项目运行约定，由管理员重启引擎；Hermes 升级后重启对应网关，使 endpoint 的 code SHA 与安装一致。生产升级与启动验证单独记录。
