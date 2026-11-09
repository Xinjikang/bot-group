# Hermes 旧版会话状态检查分流

## 问题与范围

旧版 Hermes CLI 已有 `runtime='hermes'` 的会话绑定时，唤醒预检查进入 `_native_session_state()`。该函数直接查询新版网关，旧版 control identify 缺少新版所需字段时返回 `transport_error`；原排队作业持续暂缓，话题保留当前发言成员。

执行入口已经通过 `hermes_runtime.gateway_source()` 区分 CLI 与网关。状态预检查应复用同一能力判定。版本字符串用于诊断，状态表中的 `runtime='hermes'` 同时覆盖两条执行路径。

## 实现

`hermes_gateway.process_state()` 继续优先读取执行账本。活动执行、未知执行与待清理终态继续保护对应会话，较新的 `wake_runs` 继续覆盖旧网关终态记录。

`_native_session_state()` 在找到绑定并检查 `switching` 后调用 `enabled()`。旧版返回进程检查哨兵 `False`，由 `wake.find_bot_processes()` 按 profile、引擎会话名和原生会话 ID 查询 CLI 进程；空进程集合表示空闲，匹配 PID 表示忙，查询未知继续暂缓。该分流先于原生忙闲缓存与连接获取。

网关版继续核验原生 running/pending、租约释放、错误退避和持久诊断。网关查询失败保持暂缓，连接协议与身份校验沿用现有契约。DFS 发言顺序、队列 claim、投递和终态规则保持既有语义。

## 升级与恢复

管理员更新包含本修复的 bot-group 源码后，手动重启引擎并刷新页面。安装或更换 Hermes 后同样重启引擎，以重新解析源码归属并清除进程内查询缓存。

仍有效的 `queued` 作业由正常调度继续处理，沿用原作业 ID、入队时间、话题发言位、原生会话和绑定代次。CLI 仍运行或进程状态未知时继续等待。已停止、成员停用、话题关闭或换轮后的作业继续受既有有效性检查约束。

Hermes 的已验证版本和运行时接口要求见 [兼容契约](../engineering/HERMES-COMPATIBILITY.md)。本修复针对状态检查分流；其他构建的模型、MCP 和完整消息回复链路仍需隔离验收。

## 验证

`scripts/test_hermes_legacy_scheduling.py` 使用隔离 SQLite、临时源码布局与模拟进程/RPC，覆盖旧版已有绑定的空闲、忙碌和未知状态，原生错误缓存隔离，会话切换和执行账本保护，新版网关故障继续暂缓，以及重启后原话题作业的恢复。

原生 busy、观察租约、跨运行时推进、持续失败诊断、换证、网关执行及 Windows 真实临时进程查询复用现有回归。运行命令、依赖和资源边界见 [测试索引](../../scripts/TEST_INDEX.md)。
