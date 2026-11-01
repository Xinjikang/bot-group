# 修复新增 DSH 成员后项目状态刷新失败及会话误判

新增 DSH 成员后，项目「成员与目录」页刷新状态返回 500，且 DSH 成员的会话绑定会误判其他宿主绑定为有效会话。

## 现状与问题

`project_workspaces.view` 对每个成员查 `work_session_exists`，调用 `session_adapter.existing_session_id`。该函数按 `bots.runtime` 分叉，但只有 `codex` 分支，没有 `dsh` 分支：

- DSH 无绑定时，落到 `resolve_session_by_title`（Hermes legacy 查询）→ `hermes_gateway.connection` → `bot_identity.hermes_home`，对 runtime 非 hermes 抛 `ValueError('Hermes state is unavailable for this runtime')`，`view` 未捕获 → 整个刷新 500。
- DSH 有绑定时，当前通用读取未校验 runtime，会把其他宿主的绑定当作有效会话（异种绑定误读）。

现场证据：`logs/engine-startup.log` 中 `GET /projects/bot-group/workspaces → 500`，堆栈 `project_workspaces.py:182 → session_adapter.py:326 → hermes_gateway.py:32 → bot_identity.py:51 ValueError`。

## 范围

包含：

- `existing_session_id` 补 DSH 显式分流，复用现有 `dsh_session_id` 及已解析 bot_id。
- 覆盖两个 DSH profile、Hermes/Codex 混合宿主、有效/无/空/异种绑定、项目/话题作用域与只读回归。

不包含：

- 其他 runtime（Hermes/Codex）的会话读取改动。
- DSH 会话绑定生命周期本身的其他问题。

## 方案

`existing_session_id` 在 codex 分支后增加 dsh 分支，直接调用现有 `dsh_session_id`（已有 runtime 逐行校验）：

- 有效 DSH 绑定返回原生 session id。
- 无绑定/空绑定/异种绑定（runtime != 'dsh'）返回 None，不落到 Hermes legacy 查询。
- 查询保持只读，复用 `_dsh_bot_id` + `dsh_session_id`，最小改动。

## 验收标准

1. 新增两个 DSH profile 后刷新状态不再 500，成员正常显示。
2. DSH 有效绑定返回真实 session id；无绑定/空绑定返回无会话，不触发 Hermes fallback。
3. 异种绑定（Hermes/Codex 残留）不被当作 DSH 有效会话。
4. Hermes/Codex 成员的 work_session_exists 检查不受影响（回归）。
5. 项目/话题作用域隔离正确，查询保持只读。

## 未决事项

无
