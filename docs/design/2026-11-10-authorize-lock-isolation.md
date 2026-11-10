# req#150 — authorize 数据库锁竞争修复（线程隔离 + 心跳写入迁移 + WAL 实例初始化）

## 需求背景

gateway 模式下用户复现「消息长时间发送中 + 全站页面无法刷新」。话题 10136 已核对根因：
engine/access.py 的 `async authorize`（api.py:23 全路由依赖）在事件循环里直接执行同步
SQLite 段（access.py:137-242），其中 POST /heartbeat 认证路径还会写 wake_runs
（access.py:240-242 UPDATE + commit）。当其他写入方持有写锁（如心跳写、后台写事务）时，
authorize 的 conn.execute 等待 busy_timeout=30000（db.py:44），事件循环被冻结最长 30 秒，
拖住同一进程内的全部 API 与 SSE。用户消息 11142 已授权修复。

## 改动范围

### ① authorize 同步数据库段线程隔离

access.py `async authorize` 拆两步：

- 异步段保留在事件循环：`await request.json()` 等异步依赖解析。
- 同步数据库认证段（成员校验、会话/执行凭据校验、项目/话题边界、幂等回执）整体交由
  `starlette.concurrency.run_in_threadpool` 执行；连接创建、使用与关闭在同一工作线程完成
  （不在事件循环打开/关闭连接，也不跨线程传递 sqlite3.Connection）。

事件循环不再等待库锁；写锁竞争期间普通读接口、健康检查与 SSE 持续响应。

### ② 心跳写入迁移到同步端点

authorize 认证路径里的心跳写（access.py:234-242 UPDATE wake_runs + commit）移出 authorize。
POST /heartbeat 端点（api.py:1347 `def post_heartbeat`，FastAPI 同步端点本就在线程池执行）
接管该写入：保持身份、会话、项目与执行状态校验（topic/identity 不匹配仍 403），并把
「认证到写入之间执行状态可能变化」的竞态按现有语义处理（UPDATE 带 ended_at IS NULL 条件，
未命中时返回可控结果）。

### ③ WAL 按数据库实例初始化

db.py `init_schema` 在实例初始化时执行 `PRAGMA journal_mode=WAL`（每实例一次，失败重试），
覆盖多数据目录（不同 group.db 实例各自初始化）；`get_connection` 保留逐连接配置
（busy_timeout、foreign_keys；WAL PRAGMA 幂等可保留但不依赖它做实例初始化）。
保留 keep_wal_open 的现有空闲连接管理。

## 保持不变

- 鉴权语义：成员凭据、会话/执行凭据、身份不匹配拒绝路径与错误码。
- 会话/项目/话题边界校验（access.py 全部 403 路径）。
- 消息回执幂等与原子提交。
- 后台进程等待修复（fc5d7b0）独立审查结论，本需求不扩展执行期限等功能。
- 不执行生产数据修复或自动重启（验收后通知用户手动重启）。

## 验收标准

1. 隔离库持有写锁并并发心跳时，普通读接口、健康检查和 SSE 持续响应，证据包含请求时间与
   事件循环可调度性；锁释放后心跳正确完成；锁等待超时返回可控结果且资源释放。
2. 鉴权拒绝、会话/项目隔离、执行终态竞态及消息幂等/原子提交回归通过。
3. WAL 初始化覆盖不同数据库实例、初始化失败重试与正常 checkpoint；保留连接写入/checkpoint
   用例通过。
4. 测试遵循 scripts/TEST_INDEX.md，使用工作区分配的隔离资源（隔离端口、监听地址、数据目录）；
   报告写入项目主目录 reports/tests/<版本>/<日期>-<任务ID>/，记录被测提交、命令、结果及遗留项；
   代码、文档与用例索引提交并更新代码索引；审查及验收结论写入原 task 节点；
   引擎代码验收通过后通知用户手动重启。

## 拆解与依赖

纯后端改动（前端只是受影响方，无前端代码变更），拆单任务交后端开发：
authorize 线程隔离 + 心跳写入迁移 + WAL 实例初始化 + 回归。
共享主目录串行开发，开发 → 审查 → 架构师验收 → manager 独立验收，沿原任务节点链推进。
