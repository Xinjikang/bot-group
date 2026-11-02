# Hermes 原生忙闲观察与自发执行状态展示（req#142）

日期：2026-11-02。项目：bot-group。基线：话题 10126 方案汇总 10339 及更正 10344、10354、10361、10363、10365；用户 10375 授权。

## 背景与根因

Hermes 收到异步子任务完成回调后可在原生会话中自动续跑，此轮执行可能没有 `hermes_executions` 记录。当前 `session_busy` 经 `find_bot_processes` 调用 `hermes_gateway.process_state`，仅依据引擎记录判空闲或回退进程查询，投递阶段 attach 原生会话才发现 running/pending，任务退回 queued 保留原 ID 后反复被选中，导致后方独立话题和工作会话饥饿，共用队列中的 Codex、DSH 同样可能受阻。证据：`reports/research/2026-11-02-wake-stall/evidence.json`（实际时序 + 通用队列 20 轮模拟复现，尚未构成三种 runtime 真实路径验证）。

## 成果与方案

### 成果 1：Hermes 原生忙闲预检查

按 runtime 和会话绑定接入 Hermes 原生状态；内存已知占用可快速判忙，引擎执行/清理占用和原生 running/pending 均须纳入正确判断。`process_state` 当前 93-95 行返回 False 会继续进程查询，96 行 terminal 且清理完成返回 [] 直接判空闲，两条路径均需核对；具体现场分支仅按证据归因。

缺口位置：`queue.process_one` 逐候选调 `wake.session_busy`（`wake.py:111-116`）→ 内存 `_running_sessions` + `find_bot_processes`（`wake.py:141`）；Codex/DSH 查 `wake_runs`（`_find_codex_processes`，`wake.py:198-212`），Hermes 托管会话查 `hermes_gateway.process_state`（`hermes_gateway.py:85-102`），其余回退本地进程匹配。投递阶段经绑定实时 attach 原生会话判定准确，预检查与投递两数据源不一致即缺口。

判定顺序：内存已知占用（零成本路径）先判忙；本地判空闲的候选再查原生状态；保留 CLI/Codex/DSH 现有查询语义。历史归因：`a057284` 初版统一队列按 ID 取队首；`1b1b1d6` 引入按会话跳过 busy 与 busy 退回保留原 ID（正确设计，保持不变）；`6d3f3b1` 网关适配未接入原生忙闲（缺口引入点）。

- 原生查询失败或未知时暂缓目标会话，其他独立会话继续获得调度机会。
- 设置查询超时及明确性能边界；短 TTL 缓存按原生会话和绑定代次隔离，/new 或绑定切换失效，覆盖缓存空闲期间新执行启动的竞态。
- 保留 queue.py 按会话跳过 busy 的调度规则、第 143 行退回逻辑、任务原 ID 及原顺序；同话题按轮次、同工作会话按请求顺序，独立话题和工作会话继续调度。
- 保留 CLI/Codex/DSH 现有运行时语义。

### 成果 2：/thinking 原生执行观察

`/thinking` 补充原生执行观察，准确绑定成员、项目、work/chat 和话题，执行结束及时撤下活动状态；观察记录与引擎主动派发记录区分，仅在有证据时关联任务和消息。前端复用现有 `pollThinking`/思考气泡渲染（run.state 权威状态），按实际需要配合前端。

## 真机验收与证据分层

- 模拟层：20 轮复现（实际 `process_one` + 隔离数据库，mocked gateway/内存 SQLite）与 Hermes/Codex/DSH 混合调度用例，证队列逻辑，落 `scripts/` 并同步 `scripts/TEST_INDEX.md`；模拟结果不得顶替真实运行证据。
- 真机层：真实异步子任务完成回调驱动 Hermes 自发续跑（与 2026-11-02 现场同款时序），只有真机或真实网关回执能产生；使用引擎分配的隔离端口、监听地址、数据目录与专用测试账号，不污染生产数据，结束释放本次启动资源。
- 证据分层记录：模拟与真实分别落 `scripts/` 与项目主目录 `reports/tests/`，标注三 runtime 各自验证层级及未覆盖项。

## 拆解与依赖

按独立成果拆任务：①后端忙闲预检查；②后端 /thinking 观察（复用成果 1 的原生状态查询）；③前端 /thinking 展示配合（依赖②）。

共享主目录串行修改，开发、审查、返修、验收沿原任务节点链推进。验收后通知用户手动重启引擎，MCP 变化按实际宿主说明重载要求。
