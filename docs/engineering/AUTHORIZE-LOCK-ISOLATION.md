# 认证数据库隔离与心跳写入

## 请求处理边界

`engine/access.py` 的异步 `authorize` 保留请求体解析和路由检查，随后通过
`starlette.concurrency.run_in_threadpool` 完成整个同步数据库认证段。工作线程创建连接、
校验成员与执行凭据、检查项目和话题范围并关闭连接，返回可序列化的执行快照。
读取认证保持只读。请求取消时由工作线程完成连接释放。

心跳写入由同步 `POST /heartbeat` 端点负责。端点在线程池中创建独立连接，取得
`BEGIN IMMEDIATE` 写锁后复查成员资格、会话授权、项目、话题和执行状态，再更新
`wake_runs.pid`。更新精确匹配 `(session, started_at)`，附加 `ended_at IS NULL` 条件，
命中一行后提交。鉴权、复查或写入异常及提前返回均通过关闭连接回滚当前事务。
SQLite 使用默认串行化隔离（`read_uncommitted=0`）；WAL 读者读取已提交快照。

持久会话的本次心跳始终绑定鉴权阶段确定的 `execution_id`。写锁等待期间原执行结束、
下一轮开始时，旧 PID 保持属于原执行。当前执行为空、轮次变化或 wake 行已结束时返回
`idle=true`，跳过内存活跃续租。逐执行凭据在终态复查时继续返回 401；成员撤权继续返回 403。

## 心跳接口

成员使用 `X-Group-Profile`（string，必填，例如 `worker`）和 `X-Group-Key`
（string，必填，签名凭据）认证。

| 请求字段 | 类型 | 必填性 | 示例 | 约束 |
| --- | --- | --- | --- | --- |
| profile | string | 可选 | `worker` | 与认证成员一致 |
| pid | integer | 网关执行必填，手工/原生成员调用可选 | `1234` | 网关执行要求正整数，布尔值及数字字符串返回 422 |
| thread_id | integer 或 null | 工作会话可省略，话题会话必填 | `42`、`null` | 与会话或执行归属一致，话题成员资格有效 |
| project | string | 可选 | `sample` | 持久会话需匹配绑定项目；携带话题时需匹配话题项目 |

| 响应字段 | 类型 | 出现条件 | 示例 |
| --- | --- | --- | --- |
| ok | boolean | 成功响应必有 | `true` |
| profile | string | 成功响应必有 | `worker` |
| idle | boolean | 会话空闲、鉴权后换轮或 wake 行已结束时出现 | `true` |
| detail | string | HTTP 错误响应必有 | `Database busy; retry the request` |

连接的正常忙等待预算保持 30000ms。认证读或心跳写遇到 SQLite `BUSY`/`LOCKED`
（含扩展错误码）时返回 HTTP 503，响应头 `Retry-After: 1`，连接由当前工作线程释放。
该响应允许客户端稍后重试。其他数据库异常继续传播，供维护者定位实际故障。
消息回执、幂等键及业务写入继续沿用原有单事务边界。

## WAL 初始化与维护

`db.init_schema` 在迁移前初始化目标连接对应数据库的 WAL。初始化依据数据库实际的
`journal_mode`：已经是 WAL 时保持当前模式，其他文件数据库执行一次模式切换并校验结果。
失败继续抛出，下一次显式初始化重新尝试。不同路径、同路径替换文件和独立内存数据库
各自按实际状态处理；内存数据库保留 SQLite 的 memory 模式。

运行期 `get_connection` 只配置逐连接的 `busy_timeout=30000`、`foreign_keys=ON` 和
行工厂。配置失败关闭刚创建的连接。每个独立入口在服务请求前显式调用 `init_schema`。
`keep_wal_open` 在建立空闲保留连接时准备 WAL，保留现有启动顺序；保留连接持续处于
事务外，其他连接可写入和执行 checkpoint，退出时释放该连接。

## 验证与生效

测试入口和资源参数见 [测试索引](../../scripts/TEST_INDEX.md)。单元回归覆盖连接线程归属、
认证只读、锁超时与恢复、成员撤权、执行终态、会话换轮、WAL 多实例及初始化失败重试。
真实 HTTP 探针使用显式分配的监听地址、后端端口和测试数据目录，运行真实业务路由及 SSE，
使用合成执行凭据并隔离原生网关；记录请求耗时、事件循环调度与资源释放。

设计依据见 [锁竞争修复设计](../design/2026-11-10-authorize-lock-isolation.md)。引擎代码经
审查及验收后，由管理员手动重启生效。
