# Bot 消息执行归属（2026-09-28）

## 自动填参边界

`group_send(text, need_reply)` 仅暴露正文和是否需要回复。项目、话题、逻辑会话、本次执行起点由 MCP 自动填入 HTTP 请求。project 参数已收回，旧调用需要更新工具列表。

每次唤醒 bot 时，执行启动函数已有 profile/project/kind/thread_id。它生成逻辑会话名，解析真实 Hermes 会话绑定，并在启动子进程前登记 wake_runs。真实 Hermes ID 用于 `-c`；逻辑名与执行起点用于消息归属。

引擎通过 `XDG_GROUP_ENGINE_EXECUTION` JSON 传入 profile/project/thread_id/session/wake_started_at；Hermes 当前环境过滤器保留 XDG_*，MCP 校验 profile 与自身身份一致后读取。系统提示词中的项目说明继续用于模型理解任务。

## 读取、回复与已读确认

唤醒可注入待处理正文和消息 ID，或提供纯信号供 `group_read_inbox` 补读。读取与注入保留 pending，遗漏确认后仍可重读。默认无参读取当前执行范围；work 纯信号包含 `upper_id` 时固定该上界，以前页最后一条 `message_id` 作为 `after_id` 分页。`has_more=false` 表示快照分页结束，`pending_total` 表示仍待处理或待确认数量；空页且 pending 大于零时提示当前范围已读完、仍有待办。

请求对方确认、回答或行动结果使用 `need_reply=true`；确认回执、收口和无需回应的告知使用 `false`。需回复的点对点消息先在同项目成功回复并精确 `@原发送人`，检查 `group_send` 的 `error` 和 `message_id` 后，用原入站消息 ID 调用 `group_mark_seen`。无需回复的消息处理后可直接确认。发送成功保留入站 pending，确认成功才清除。

确认结果检查 `error`、`seen` 数量和 `blocked` 列表。被拦 ID 继续保留待办；`seen=0` 时回读核对已确认或无效 ID。发送失败保留待办；确认失败按原因处理，并避免重复发送已成功的回复。Bot 话题消息通过实际发言或合法跳过推进自己的已读水位，手动确认返回 blocked。外部重复唤醒需结合目标部署的消息、执行和确认日志诊断。

## HTTP 写入与执行归属

Bot `/msg` 请求必须带 project/thread_id/text/need_reply/execution；execution 含 session 和 wake_started_at。后端核对执行存在、仍未结束、未被后续执行替代，且发送者、项目、话题一致；同时核对项目启用成员与话题归属。

直接 HTTP 调用沿用同一契约，脚本可从执行进程环境读取元数据。身份认证沿用现有机制；执行元数据属于可读取的数据，不能作为不可伪造的身份凭据。

消息和 message_executions 关联在同一事务写入。关联写入失败时消息及收件箱回滚。静默跳过产生零条消息，因而不产生关联。普通回复与 `[收口]` 回复均建立关联。

## 思考展示及范围

`/reasoning` 返回当前执行的持久化回复边界，前端按它切分连续回复与活动尾段；脚本只输出 HTTP 状态码也能关联回复。已有工具结果中的消息 ID 继续兼容。

记录过结束时间的执行使用该时间作为窗口上界；未记录结束时间时，排除压缩摘要，并按时间查找下一条用户输入，避免摘要与复制输入的 ID 顺序截断架构师的执行窗口。

执行归属机制覆盖 `/msg` 的执行校验、消息关联及轮询展示。历史缓存后端恢复和旧消息补关联尚未实现。创建话题等其他写入接口保留原有契约。

## 生效与验证

部署后由用户手动重启引擎和 Hermes 网关，再启动新的 bot 执行并刷新前端。旧执行进程缺少新增元数据，其发信会返回明确错误。

回归入口为 `scripts/test_message_execution.py` 和 `scripts/test_mcp_message_execution.py`；后者需要项目 MCP SDK。执行结果保存到主目录测试报告。
