# 消息发送与持久化

## 普通消息事务

`engine.api.post_msg` 校验身份与消息后调用 `router.route_message`。路由在同一写事务中保存消息、inbox、话题轮次/发言位与唤醒作业，通过 `wake.enqueue(commit=False)` 登记工作并统一提交。队列消费者在提交后读取作业。成员、项目就绪、部分唯一索引去重与 DFS 顺序沿用队列契约。

HTTP 路由在处理异常后关闭连接，回滚尚未提交的整笔消息及唤醒状态。直接调用路由的维护工具须在失败时回滚或关闭连接。MCP 幂等操作通过事务 lease 使用 savepoint，最终持久提交由外层操作回执事务负责。

## 耗时日志

- `message.send`：用户发送处理的 `message_id`、`thread_id`、`outcome`，以及 `connect_ms`、`route_ms`、`publish_ms`、`total_ms`。计时从消息形状校验后、获取数据库连接前开始，结束于连接关闭；HTTP 排队、鉴权和网络传输由外层服务统计。
- `message.persist`：写锁取得 `lock_ms`、消息与收件箱处理 `prepare_ms`、唤醒入队 `enqueue_ms`、事务提交 `commit_ms` 和 `total_ms`。用户消息逐条记录，Bot 消息在该阶段达到 100 ms 时记录。lease 的提交耗时表示释放 savepoint。

日志省略消息正文、附件内容、凭据和异常原文。将同一持久化消息 ID 的两条日志关联，可区分连接开销、写锁等待、SQL 处理、磁盘提交及事件发布。原始现场消息在引入日志前的耗时需依靠已有证据判断。

## 临时发送卡顿诊断

`TEMP_SEND_TRACE` 是测试代码。主输入框对每次实际发送生成随机 `trace_id`，通过 `/msg?send_trace=<id>` 关联两端耗时。浏览器控制台打印 `[TEST TEMP_SEND_TRACE] start` 和 `finish`，记录提交至发起请求、浏览器发出请求前的等待、响应头和响应体到达、JSON 回执读完，以及按钮 DOM 状态恢复的耗时。浏览器未提供的计时字段显示 `null`。需要跨页面刷新留存时，在 Chrome 控制台开启保留日志。

引擎日志用同一编号记录 `test.message_send.request`、`message.send` 和 `test.message_send.response`。`pre_handler_ms` 从 ASGI 入口算到消息处理函数开始，包含身份校验和线程等待；`headers_ms`、`response_ms` 从入口算到响应头、响应体交给 HTTP 服务。浏览器与引擎均使用本地单调时钟计算时长。诊断只记录随机编号、状态、耗时和现有消息处理日志字段。

卡顿定位并解决后，撤回临时诊断提交以移除 `TEMP_SEND_TRACE`、浏览器缓存版本标识及相应测试和本文临时小节。原有 `message.send`、`message.persist` 继续用于写入耗时诊断。

## 页面反馈

主输入框发送按钮按项目/话题目标显示发送中状态，阻止同一目标并发提交。发送期间文本保持可编辑；成功时沿用目标与文本一致性校验清空草稿，失败保留草稿。切换项目或话题时刷新目标状态。页面仍由服务器确认和 SSE 更新消息，闭合话题保留禁用状态。界面契约见 [UI 规范](../ui-spec/UI-SPEC.md)，设计与测量边界见 [发送延迟设计](../design/2026-11-06-message-send-latency.md)。

主输入框收到成功回执后主动刷新当前消息列表。同一视图已有列表请求时，将期间的新消息通知和发送回执合并成一次补拉。切换项目或话题时丢弃旧视图的补拉与迟到响应。已读确认和未读汇总异步执行，列表获取与渲染完成即可接受新的刷新。

相关隔离回归与浏览器命令见 `scripts/TEST_INDEX.md`。
