# /steer 运行中纠偏传输通道接入设计

- 需求：req#136「接通现有网关执行的 /steer 运行中纠偏」
- 作者：architect（苏瑾）
- 日期：2026-10-30
- 状态：实施设计（拆解依据）
- 前置：req#83/task#145 已交付账本层（engine/steer_ledger.py），本设计只补传输通道。

## 1. 目标与范围

在现有 Hermes 网关集成上，把 `/steer @目标 正文` 的纠偏送入「正在运行的指定执行」，并给出可信回执。

- 范围：网关（hermes）执行路径。子进程 / Codex 后端保持现有行为（不支持 steer，明确报告能力边界）。
- 不改 Hermes 源码。复用 task#145 账本 + 现有权限校验 + 话题 DFS 调度语义。

## 2. 接口核实结论（本次核实，2026-10-30）

### 2.1 结论：网关公开运行中纠偏接口 = `session.steer`

`session.steer` 在 **v0.22.5 稳定版 tag 即存在**，非 main 增量：

```
git show v0.22.5:gateway/session_busy_controls.py
  :13  'session.steer': partial(correct, connection, verb='steer'),
  :175 async def correct(connection, ref, params, *, verb):
```

实际安装树 E:\home\admin\hermes\hermes-agent（v0.22.5+2381.g0431192）同文件同实现，契约一致。

> 补遗：docs/design/2026-10-27-hermes-gateway-v0225-call-table.md 的五类动作表未列出 `session.steer`，本次核实补上，作为该表 §3 的增量。

### 2.2 `session.steer` 契约（session_busy_controls.py:175-217）

- 参数（第 176-178 行，非法即 `invalid_params`）：
  - `session_id`（经 ref）
  - `profile`（可空，null=owner）
  - `text`（str，strip 后非空）
  - `execution_generation`（int，必填，精确匹配当前 running 代际）
- 授权（第 180 行）：`session:control`。引擎 gateway client 经 ticket 认证为 owner，默认具备（call-table §2 能力集 `session:control` 默认授予）。
- 返回三种（第 186-217 行）：
  1. `{'voice_stopped': True}` — 本地 voice stop 短语（引擎场景不触发，忽略）。
  2. `{'status': 'queued', 'text', 'execution_generation', 'authority_epoch'}` — 文本已投递运行中 agent（agent.steer 返回 True 或 latched 给本代际待 adopt）。
  3. `{'status': 'rejected', 'text', 'execution_generation', 'authority_epoch', 'delivered': False}` — 无 agent 可收（turn 已结束 / agent 尚未 adopt）。
- 拒绝（raise RuntimeStoreError，引擎经 GatewayRefusal 收 `reason`）：
  - `stale_generation` — execution_generation 与当前 running 不符（check_approval_generation，第 191 行）。
  - `unsupported_control` — managed worker 无 owner agent（第 194-195 行）。
  - `execution_not_ready` — agent 无 steer 方法（第 213 行）。
  - `permission_denied` / `invalid_params`。

### 2.3 与 `prompt.steer_queued` 的区别（为何选 session.steer）

- `prompt.steer_queued`（session_queued_controls.py:136）操作「排队中的 admission 转移」：先有 queued admission_id 再把其文本转给 running claim，以 admission_id 为 ticket 有 settlement（steered/interrupted）。
- 引擎场景是「目标 bot 已有 running turn，用户临时发 /steer」，直接走 `session.steer` 更贴切：一次性文本直投运行中 agent，无 admission 转移开销。
- 代价：`session.steer` 无 ticket settlement，即时回执只到「文本已投递（latch 或入 pending 队列）」，是否实际插入模型上下文需引擎侧观察（见 §4.4）。

### 2.4 引擎已有能力（无需新增）

- `engine/hermes_gateway_ledger.py` `hermes_executions` 表已持久化 admission 身份：
  - `session_id`、`profile_id`、`admission_id`、`generation`（= receipt 的 `execution_generation`，apply_receipt 第 149/159 行）、`authority_epoch`、`kind`、`thread_id`、`state`。
- state 状态机（apply_receipt 第 164/171 行）：`submitting`（queued）→ `running`（started）→ `terminal`；另有 `unknown`/`manual`/`cancelling`。
- **「运行中可 steer」判定 = `state == 'running'` 且 `generation` 非空。**

## 3. 现状缺口

`engine/commands.py:134-160` `_handle_steer` 当前只落账本 `new_operation` 后立即 `report_leftover`（result='传输通道尚未接入，纠偏未投递'，第 154 行），从未发起传输。

## 4. 设计

### 4.1 目标定位（多会话精确隔离）

对每个 target（bot profile 名）：

1. 定位维度：`project + profile + kind + thread_id` 完整目标身份。kind = chat（thread_id 非空）或 work（thread_id 为 NULL/0），沿用 `_handle_steer` 第 147 行现有判定。
2. 查活跃执行（含 project 条件；work 的 thread_id 用 COALESCE 统一，避免 `thread_id=?` 对 NULL 永不匹配）：
   `SELECT session_id, generation FROM hermes_executions WHERE project=? AND profile=? AND kind=? AND COALESCE(thread_id,0)=? AND state='running' AND generation IS NOT NULL`。
   （chat 传真实 thread_id，work 传 0，与 steer_operations/session_bindings 的 thread_id 空值约定一致。）
3. 无匹配（目标空闲 / 已结束 / 未提交）→ 不发起传输，账本走 `finalize_target_ended`（或明确「无运行中 turn」），命令返回精确状态。
4. 匹配多条（同一完整目标身份出现多个 running 行 = 身份歧义）→ 拒绝投递，账本走 `mark_pending_verify`（明确「目标身份歧义」），保留正文待人工核对；不取 generation 最大者（缺乏安全依据）。

### 4.2 传输调用

新增 engine 侧传输函数（建议 `engine/hermes_gateway_steer.py` 或并入 hermes_gateway.py）：

1. `hermes_gateway_client.discover(home)` 拿 endpoint，`GatewayClient(endpoint)` 建连接。
2. `client.rpc('session.steer', session_id=session_id, text=text, execution_generation=generation)`。
3. 返回映射见 §4.3；`GatewayTransportError` / 超时 → 回执不确定，见 §4.5。
4. 连接用完 `client.close()`（对齐 hermes_gateway.py 现有 client 生命周期）。

### 4.3 回执映射（对接 steer_ledger）

| session.steer 结果 | 账本动作 | 终态/层级 |
|---|---|---|
| 发起前（已定位 running） | `record_delivery(session_id, generation, run_started_at)` | delivering（通道接受层，绑定 session/generation/run） |
| `status='queued'` | 保持 delivering，等实际插入证据（§4.4） | delivering（文本已 latch / 入 pending 队列，未到 inserted） |
| `status='rejected'` 且带 `delivered: False` | `report_leftover` → undelivered | 目标已结束 / 无 agent |
| `status='rejected'` 但缺 `delivered` 字段 | 保守 `mark_pending_verify` | 回执语义不完整，不误报确定未投递 |
| 拒 `stale_generation` | `finalize_target_ended` | 目标运行已结束 |
| 拒 `unsupported_control` / `execution_not_ready` | `report_leftover` → undelivered | 能力边界，明确报告 |
| 拒 `permission_denied` / `invalid_params` | 已 `record_delivery` 则 `mark_pending_verify`（不留 delivering 悬空）；未发起则命令层报错 | 参数/权限问题，保留准确账本结果 |

### 4.4 回执五层落地口径

账本五层（steer_ledger.py:33）：queued / inserted / persisted / model_requested / behavior_observed。

- `session.steer` 返回 `queued` 只代表「通道接受」：`session_busy_controls.correct` 在 agent 尚未 adopt 时也返回 queued（latch 到 pending_steers），`interrupt_control.steer` 只写 pending 队列。该层只落到 delivering，**不是 inserted**。
- `inserted`（文本实际插入会话历史）需观察目标执行历史出现带 `[OUT-OF-BAND USER MESSAGE ...]` marker 且正文匹配该 operation 的 user 行，由执行观察路径核对后 `report_consumed(deliver_layer='inserted')`。
- `model_requested`（进实际模型请求）与 `behavior_observed`（行为按纠偏改变）无即时 RPC 回执，留验收观测：隔离测试让目标跑长任务 → /steer 注入 → 断言后续行为改变；`behavior_observed` 由验收记录，不由传输通道即时上报。
- 明确不虚报：未观测到对应层证据就不报该层。

### 4.5 回执不确定（不误报、不盲目重投）

- `GatewayTransportError`（RPC 超时 / 断连，outcome unknown）→ `mark_pending_verify`（steer_ledger.py:160），账本挂 pending_verify，交给扫描器 `reconcile_targets` + TTL `expire_stale` 兜底。
- 不重投：与 admission 相同纪律（transport loss 不触发 resubmission，hermes_gateway_ledger.py:227 注释同口径）。

### 4.6 竞态处理

- `/stop` 与 `/steer` 并发：stop 后 state → cancelling/terminal，steer 查不到 running 或 `stale_generation` → 精确反馈「目标已停止/结束」。注意 v0.22.5 停止语义（call-table §3.4）：started 执行靠 `session.interrupt(execution_generation)` 硬中断，与 `session.steer` 用同一 execution_generation 锚定，天然互斥。
- `/new` 与 `/steer` 并发：session_bindings.generation 变化（跨 `/new`）→ 旧运行纠偏作废，账本 `generation_stale`（见 §4.8 双代际分离，勿与 session.steer 的 stale_generation 回执混淆）。
- 目标空闲：state != running → 「目标当前无运行中 turn」，不误报已送达。

### 4.7 命令帮助与状态展示

- `COMMAND_REGISTRY['steer']`（commands.py:194-202）更新 help/menu_description/busy_policy，反映「已接入网关运行中纠偏，子进程/Codex 后端不支持」。
- 命令返回 detail 区分：`queued`（已送达运行中执行）、`target_ended`/`undelivered`（未送达 + 原因）、`pending_verify`（回执待核实）。

### 4.8 generation 双代际分离（对账防误判）

引擎存在两套 generation，语义不同、不可混用：

- `hermes_executions.generation` = 网关 admission receipt 的 `execution_generation`，用于 `session.steer` / `session.interrupt` 的精确运行绑定。
- `session_bindings.generation` = 引擎 `/new` 会话切换计数（每次切换递增），用于会话级失效判定。

`steer_operations.generation` 在投递时绑定的是**网关 execution_generation**（来自 hermes_executions.generation），不是 session_bindings.generation。`steer_ledger.reconcile_targets` 现行实现将 `steer_operations.generation` 与 `session_bindings.generation` 直接比较判定 generation_stale，属代际混用、会误判。实现须分开对账：

- 投递精确绑定：用 hermes_executions.generation（网关 execution_generation），`session.steer` 的 `stale_generation` 回执据此判定；
- `/new` 失效：用 session_bindings.generation 判跨 `/new` 失效（旧运行纠偏作废）；
- 两套代际分别对账，不交叉比较。

## 5. 验收标准（对应 req#136 acceptance）

1. 隔离环境 `/steer @目标 正文` 到达正在运行的指定执行，取得 inserted 消费证据，后续行为按纠偏改变（behavior_observed 观测记录）。
2. 同 bot 多话题、chat/work 并发精确定位，纠偏不串会话、不改变 DFS 发言顺序。
3. 目标不存在/已结束、子进程/Codex 后端、连接失败、回执丢失、与 stop/new 竞态，均有准确状态 + 保留正文，不误报已送达、不盲目重投。
4. 复用账本 + 权限校验；命令帮助与实际能力一致；/new、/stop、正常消息调度回归通过。
5. 提交设计/代码/测试 + 索引更新；隔离测试报告记录真实接口链路与遗留项；经独立评审 + 架构验收；生产重启由用户执行。

## 6. 边界

- 仅网关（hermes）执行路径。子进程/Codex 后端：能力边界，明确报告不支持 steer。
- model_requested/behavior_observed 不即时回执，属验收观测，不阻塞首期传输通道交付。
- Hermes 源码零改动；只经公开 RPC（session.steer）+ 配置 + 引擎自身代码。
