# Hermes 串行 RPC、心跳与初始化恢复

## 问题与边界

Hermes 在同一个 WebSocket 上串行处理控制 RPC。连接池的应用层 `ping` 使用 3 秒超时；配置模型、工作区或 MCP 的请求等待较久时，排在其后的心跳无法及时返回。将这种等待判为连接故障会关闭共享连接，使已经提交的配置只剩未知结果。

初始化发生在 `hermes_executions` 和 `prompt.submit` 之前。这个阶段需要核对配置请求本身的回执；执行阶段继续使用已有 admission/submission 账本恢复。

## 连接与心跳

共享连接上的租约 RPC 持有 profile entry 的互斥锁，覆盖发送、等待响应及订阅登记。维护线程尝试非阻塞取锁；已有 RPC 时跳过本次心跳。锁空闲后继续使用 20 秒检查间隔与 3 秒 ping 超时。真实空闲连接超时仍进入错误状态并关闭。

原始 `GatewayClient` 保留请求 ID 分发和独立接收线程。租约互斥范围与原生控制方法的串行调度一致，事件接收持续运行，退出流程仍可直接关闭 socket 以中止等待。

## 初始化恢复

`initialization_rpc` 只接受 `runtime.describe`、`session.list`、`session.resume`、`session.create`、`session.mutate`。池租约发生传输错误后最多重连一次并重试一次；重连只发现已运行的实例，安装版本变化时要求重启。若池已恢复连接，当前租约使用该连接；其他租约继续保留各自绑定，防止旧执行隐式切换连接身份。

创建和 mutation 的请求 ID 在首次发送前生成。重试完整复用原 request ID、payload、expected revision 和 execution generation。原生 mutation 事务先查询对应请求摘要的持久回执，已提交的请求返回原结果；未提交的请求继续经过版本、代次、权限和忙碌检查。明确拒绝直接向上返回；第二次传输失败结束本次恢复。

每项配置成功后通过 resume 读取当前快照并恢复订阅，配置指纹沿用完成后的持久化规则。提交 prompt 前再次检查快照的 running/pending；会话已忙时返回调度等待，保留原任务。`prompt.submit` 不进入初始化重试入口，执行期断线由原账本按 admission/submission 核对。

## 诊断与验证

日志记录进程内连接标识、RPC 方法与序号、等待时长、WebSocket 收发关闭码，以及维护/心跳失败阶段。远端关闭文本、参数、令牌、prompt、MCP 环境和持久请求 ID 均不写入新增诊断。

回归使用临时 home、随机回环端口、实际 Pool/Lease/GatewayClient 和串行合成服务端。覆盖慢请求心跳互斥、真实心跳超时、提交前后丢包、创建回执重放、重试耗尽、明确拒绝、共享连接继承、版本变化、忙碌复核、prompt 单次发送及诊断脱敏。执行方法见 [测试索引](../../scripts/TEST_INDEX.md)。
