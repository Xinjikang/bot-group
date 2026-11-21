# 未完成任务在当前工作会话自动续办与恢复受阻提醒

版本：2026-11-21　状态：定稿（req#176 拆解依据）

## 背景与目标

引擎派发的任务执行可能因 Hermes 原生执行缺完成回执而停在 manual，自动核对停止；催办唤醒已排队，却因旧执行占用会话长时间等待（02:00 排队、12:42:58 才恢复）。目标是：让负责成员及时接续原任务，并让确需用户介入的问题对用户可见。

本需求只涉及任务续办，不迁移生产会话、不修改历史任务记录；旧会话历史与缺失回执的取证独立于恢复流程。

## 方案

以任务队列为事实源，超时后按下列流程恢复：

1. 超时扫描：识别停在 manual / 长时间无推进的任务。
2. 重新核对：任务状态、当前环节、负责人；确认原成员已停止推进后才续办。
3. 恢复投递：在该成员当前 B 会话沿用同一 task_id 与最新节点继续，无需访问原会话 A。
4. 复用已有唤醒：优先复用已排队的唤醒，随附已知后台进程句柄与待核对结果，避免重跑已发出的命令。
5. 归属校验：投递与接手时校验归属，同一任务同时只允许一处推进。
6. 去重：按每次恢复去重，同次恢复只接手一次；后续发生新的停滞仍可催办。
7. 受阻提醒：自动续办受阻且确需人工处理时，向用户发可见提醒（任务 ID、卡点、需要执行的动作），并记录投递结果供查询。

## 关键设计点

### 识别停滞（事实源 = 任务队列，不是执行记录）

停滞判据 = 任务未完成 + 仍归该成员 + 该成员已停止推进，三者同时成立：

- 任务侧：`status='in_progress' AND claimed_by=<成员>`（或 `pending AND next_bot=<成员>`），且 claim 后超过 `CLAIM_STALE_SECONDS`（2h）无新节点。
- 执行侧：该成员当前 work 会话存在停滞执行——`hermes_executions.state='manual'`（回执缺失、自动核对已停），或 `state='waiting_delegation'` 且存在 consumed=0 的委托/进程等待超过执行期限。

任务已完成或已易主则不续办。

### 复用已排队唤醒（解开会话占用症结）

症结两层，都让已排队的催办 job 无法投递：

1. 内存层：manual 执行未走 `_finish`，其 session 残留在 `_running_sessions`，`session_busy` 第一道检查即判忙。
2. 数据库层：`hermes_gateway.process_state` 对 `state!='terminal'`（含 manual）返回 None，`find_bot_processes` 透传 None，`session_busy` 的 `None != []` 恒 True；队列 worker 每个周期跳过该 job，直到执行被人工清理。

修复：停滞执行（manual / 等待超时）且任务未完成且仍归该成员时，会话判「可接续」而非「忙」，已排队的唤醒正常投递；投递落地复用原 execution 的接续路径（注入 task_id + 后台进程句柄 + 待核对结果），不重跑已发出的命令。

manual 接续的落点（现缺口）：`hermes_delegations.direct_wait` 只匹配 `state='waiting_delegation'`、`enqueue_direct` 也只接受 `waiting_delegation`，manual 会在此被拒。恢复信号进入 B 会话的路径 = 扩展接续入口接受 manual：恢复信号作为新的 delegation_input 注入原 execution，worker 以 manual 接续模式重启，在 poll 里把恢复信号 submit 到 B 会话（session_id 不变），bot 醒来读任务节点继续；追回执（discover_turns）的失败不得阻塞恢复信号的 submit（恢复与回执核对解耦）。不重发原命令——恢复信号只带 task_id + 后台进程句柄 + 待核对结果。

### 防并发接手（同一任务单点推进）

- 归属校验：投递与接手都复核 task 的 claimed_by/next_bot 仍指向该成员，易主即放弃。注意 `hermes_delegations.owned` 只核对会话绑定（generation / 授权 / 成员活跃 / 话题状态），任务归属仅在 execution.task_id 非空时才查；点对点派活的 task_id 常为空（现场 execution 的 task_id=None），owned 会漏掉归属校验——续办前必须从任务队列（tasks 表）单独核对 claimed_by/next_bot 指向该成员。
- 单点推进：execution 级「本轮恢复已投递」标记，同次恢复只投递一次。
- 队列兜底：`wake_jobs` 的 partial unique index（work 按 profile+project）保证同成员同项目同一时刻只有一个 queued/running job。

### 重启后的投递去重

- `recover_on_startup` 已把 running 的 job 重置回 queued。
- 接续投递按 execution 记录幂等：`hermes_delegation_inputs` 的 `UNIQUE(execution_id, message_id)` 防同一消息重复注入。
- 恢复信号随附本次恢复标识（幂等键 = execution_id + 停滞轮次，持久化到执行记录），重启前后使用同一标识：重启后重扫命中同一标识即不重复投递同一恢复；后续出现新的停滞（新轮次）仍可再次催办。

### 用户可见提醒

自动续办受阻（任务已易主 / 已完成 / 无法接续）且确需用户处理时，引擎发一条可见消息给用户，写清任务 ID、卡在哪、需要用户做什么，并记录该消息投递结果（message_id）供查询。

## 验收场景

在隔离环境复现「旧执行占用、回执缺失、催办已排队」：

1. 夹具：旧执行停在 manual（回执缺失、自动核对已停）+ 催办已排队；达到恢复条件后，当前会话 B 实际收到恢复信号并在原任务追加节点，全程无需访问会话 A。
2. 边界覆盖：原执行仍活跃、后台进程仍运行或已有结果、任务已完成或已易主、扫描并发、引擎重启、迟到或重复回调——各自得到正确处理，不误续办、不重复接手。
3. 同次恢复只接手一次；已发命令不重复启动；其他成员工作不受影响。
4. 再次停滞可再次催办。
5. 模拟自动续办受阻，核验用户端实际显示任务 ID、卡点与需要执行的动作，且投递结果可查。

## 与既有机制的关系

- 复用已有唤醒与派发链路（不新建第三种调度机制）。
- 复用任务节点链（task_id + 最新节点）与归属校验，不迁移会话、不改历史任务记录。
- 生产数据库变更与引擎重启由用户决定上线时点。
