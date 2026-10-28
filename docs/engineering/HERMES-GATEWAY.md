# Hermes 网关执行适配

## 接口与版本

`engine/hermes_gateway*.py` 使用公开 CLI `hermes gateway ensure --json`、本地 control 协议的 `session-ticket` 和 `/api/ws` 的 `hermes-gateway-v1`。适配器仅调用线协议，Hermes 源码保持独立。运行时核验 profile、协议、安装提交身份，以及 `durable-admission-v1`、`acp-session-mcp-v1` 能力。v0.22.5 验证基线为 `bf0203a794a07aac3ff764972ab9087baa4e4f4e`。

网关版采用独立执行路径；旧版 Hermes 和 Codex 保留现有运行时。安装来源由 `HERMES_BIN`、`HERMES_HOME` 和已有安装定位规则确定。

## 会话、MCP 和原生审批

新会话使用 `source=acp`，通过 `session.mutate editor_mcp` 注入本次执行的 task_queue。每轮先提交空列表以释放旧连接，再提交新凭据。实际工具发现和调用由 Hermes 原生机制完成。

危险命令审批采用 Hermes 原生 `approvals.mode: smart`，配置由管理员在 Hermes 中维护。ACP 编辑策略采用官方 `edit_policy=workspace_session`：普通工作区编辑按原生策略放行，敏感路径和工作区外请求遵循原生审批规则。引擎沿用这些原生能力。

旧 CLI 会话通过官方 `session.export` 和 `session.mutate import` 建立确定 ID 的 ACP 副本，原会话完整保留，映射记录在 `hermes_session_migrations`。活动会话先等待终态。`/new` 使用 `session.create`，绑定 generation 按现有流程递增。恢复、标题查找和思考记录分别使用 `session.resume`、`session.list`、`session.export`。压缩后的逻辑会话由 Hermes 解析。

已有绑定按持久化 session ID 恢复。首次绑定时调用官方 `session.list title=...`；v0.22.5 的原生标题解析返回零个或一个结果。适配器对多候选响应返回 `ambiguous_session_title`，保留原会话及绑定状态；管理员核对标题归属后重试。空结果按原有流程创建会话，唯一结果写入 ID 绑定。

## 执行与恢复

`hermes_executions` 在 `prompt.submit` 前提交执行身份、submission ID、会话/profile 身份及固定截止时间；回执到达后保存 admission ID、generation 和 authority epoch。引擎重启恢复原记录，并继续查询 `prompt.receipt`。已登记远端执行的投递作业保持已投递状态。

queued 执行通过 `prompt.cancel` 取消；started 执行通过携带 generation 的 `session.interrupt` 取消。只有对应 admission 的 terminal 回执才能结束执行。中断 ACK、WebSocket 断开和查看客户端退出均保留占用。用户停止工作执行时，经原生 `rewind` 回退本次指令；目标消息限定本次执行，回退目标和请求 ID 持久化后重试。

引擎优雅停止先关闭新任务入口，并等待正在执行的任务自然结束，默认等待 120 秒。超时后通过上述官方取消接口停止 Hermes 执行，等待 terminal 与临时 MCP 清理完成，再释放浏览器资源并退出引擎；共享网关进程保留。终态或清理尚未确认时，本次停机失败并恢复服务入口。`start.bat` / `start.sh` 只有确认旧引擎进程退出和端口释放后才启动新引擎。停机取消使用 `shutdown` 原因，保留会话历史。

终态立即撤销执行凭据；清理临时 MCP、浏览器资源和引擎记录后释放占用。MCP 心跳使用自身 PID，避免多个会话共享网关 PID；凭据校验约束项目、话题和 inbox 类型。引擎停机按执行发送取消，保持 HTTP/MCP 可用直到收尾完成。

执行终态与清理完成是两个独立状态机。终态回执落库即撤销执行凭据并释放执行占用：`active()` 只看 `state != 'terminal'`，`cleanup_pending` 不算占用（不进入 busy 判定）；同一逻辑会话仍受「清理禁用」约束，清理恢复后才可复用，其他独立会话可运行。清理待办（`cleanup_pending=1`）按退避序列重试（1/2/4/8 秒）最多 5 次；`retry_count`、`next_retry_at` 与 `cleanup_deadline` 持久化，清理最终期限（`cleanup_deadline`）到期即转 `cleanup_failed` 停止重试。耗尽（重试 5 次或期限到期）转 `cleanup_failed`（`detail` 标记）并停止自动循环，保留 `cleanup_pending=1` 与会话禁用，仅人工明确重试才开启新预算。引擎启动恢复只续剩余额度，已耗尽或期限已过不自动重试；恢复沿用原 admission 与取消原因。取消有界：`cancel_deadline`=首次 cancel+30s、`reconcile_deadline`=首次 cancel+150s，超时转 `manual` 停止自动循环；单次 RPC 超时=min(默认 30s, 剩余预算)（仅取消/对账时生效，正常轮询保持默认 30s，不因 reconcile_deadline 过期而打断长时间运行执行）。清理走引擎自身 GatewayClient（ensure+ticket），非 run.token 执行凭据，撤销凭据不影响清理重试。清理登记按 execution_id/started_at 精确匹配，不按 session 删除，避免迟到清理误删后继执行。

提交回执缺失时，当前公开接口缺少按 submission ID 只读找回 admission 的入口。适配器保留 `unknown`，到固定截止时间转 `manual` 并保留占用。管理员需核对原执行，自动重提保持关闭。已有 admission 的执行在断线后重新取一次性 ticket，并查询原回执。

## MCP 离线窗口与操作回执

`engine/mcp_http.py` 为 MCP HTTP 调用提供 30 秒总预算。连接建立失败或明确的 `503 + X-Group-Not-Accepted: 1` 按 1/2/4/8 秒退避，最多发送 5 次。耗尽返回 `engine_offline`。读取超时、写出后断线、一般 5xx 和重定向进入结果查证；写操作按原 `operation_id` 查询 `/mcp-operations/{operation_id}`，查证也受同一总预算约束。查询仍无法确认时返回 `operation_unknown`，携带操作 ID。FastMCP 同步工具运行于已有事件循环时，传输在独立线程的事件循环中执行，预算内的请求与等待可取消。

任务 API、发消息、建话题、话题任务修订和已读确认使用 `X-Group-Operation-Id`。客户端为一次调用生成 ID；同一 MCP 进程内，结果未知的相同请求继续查证原 ID。确认完成后释放本地记录，后续显式调用视为新操作。`operation_get(operation_id)` 提供只读查证；结果未知时由维护者核对原业务记录与外部副作用，再决定后续动作。MCP 进程自身退出会失去本地请求映射，错误中的操作 ID 和引擎持久回执仍可用于查证；自动恢复范围限定为原 MCP 进程及仍有效的执行凭据。

引擎先持久登记 `unknown` 预留，再在同一 SQLite 事务内提交业务变更和 `completed` 回执。原业务连接通过请求上下文共享事务；局部提交释放 savepoint，最外层提交决定持久化，事件在提交后发布。相同成员/执行/ID 的并发请求与重放返回原结果或 `unknown`；相同 ID 的不同请求内容返回 409。预留后进程异常且业务事务尚未提交时回滚业务并保留 `unknown`；涉及外部副作用的操作继续保留未知状态，自动重放保持关闭。回执随业务库备份，保留到管理员按数据维护策略处理，自动过期删除保持关闭。

回执按成员和签名执行身份隔离，查询及重复请求继续检查成员资格、项目/话题范围和执行有效性。后继执行拥有独立的回执空间，终态或过期凭据无法取得原结果。旧 MCP 进程可在引擎重启后继续使用原 `GROUP_ENGINE_URL`；凭据延续仍由原执行的有效性决定。

上述行为使用隔离 API 进程和真实 stdio MCP 验证。执行完整网关恢复场景时还需覆盖模型、网关及引擎三方生命周期。升级启用时重启引擎以加载 schema 和 API，并重新加载持有旧 MCP 进程的宿主；网关持有的 MCP 需按项目运行约定重启网关。

## 验证与启用

测试方法见 `scripts/TEST_INDEX.md` 的“网关正式适配”。真实验证使用独立 Hermes home、随机本地 HTTP 端口和隔离引擎数据库，覆盖原生审批配置、两轮 MCP 凭据刷新、回复/已读、取消/回退、重连、历史导出和 `/new`。引擎重启恢复、未知执行占用和旧版/Codex 兼容由相关回归覆盖。原始回执保存在主目录 `reports/tests/`。

部署时先安装引擎依赖，再选择对应 Hermes 安装。按项目运行约定，由管理员重启引擎；Hermes 升级后重启对应网关，使 endpoint 的 code SHA 与安装一致。生产升级与启动验证单独记录。
