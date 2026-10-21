# 任务完整性与领取上下文

2026-09-29。范围：任务完整性门禁、领取上下文、前置依赖调度与完成门禁。需求版本复核属于后续范围。

## 创建、草稿与发布

`task_create` 每项填写 `title`、`next_bot`，以及：

- `description`：目标、修改范围、实现要求。
- `design_basis`：有效设计文件/章节/版本引用及已确定约定；简单任务可以直接写明完整决策。
- `acceptance_criteria`：可检查的行为、异常情况及验证要求。
- `prerequisites`：当前项目已有任务 ID 数组，无前置任务显式填 `[]`。
- `draft`：默认 false。true 保存草稿，false 校验后发布。

发布要求三项说明为非空字符串、前置引用同项目且无重复、自引用或循环引用。失败返回 `task_brief_incomplete`、`fields` 及批量创建的 `task_index`，整批回滚。结构校验保证字段完整，语义正确性由评审负责。

`task_update` 使用同名字段补全说明；`publish=true` 校验并发布，`publish=false` 转草稿。只补全草稿字段仍保留草稿状态，需显式发布。沿用项目 `task.update` 授权，话题修订代理支持这些字段并保留原作用域校验。

```json
{
  "title": "补充成员发现接口",
  "next_bot": "be-dev-01",
  "description": "扫描配置的宿主根目录，返回候选资源；实现项目隔离和空结果提示。",
  "design_basis": "docs/guides/CODEX-PROFILE-HOMES.md：发现与入组；扫描提供候选，用户选择后注册或复用 Bot。",
  "acceptance_criteria": "隔离测试覆盖无宿主、同名跨宿主、已有 Bot 复用及跨项目添加；提交测试结果。",
  "prerequisites": [],
  "draft": false
}
```

## 派发与领取

当前接口调用示例（所有 project、task_id、profile 均取实际返回值）：

- 执行交接：`task_claim(project="项目名")` 成功后读取返回的完整上下文，使用返回的 `task.id` 执行；交付时 `task_log(project="项目名", task_id=实际ID, type="dev", result="交付结果与验证证据", next_bot="下一接手者profile")`。先通过 `group_list_members(role="本项目约定的角色")` 查询队友，选定具体成员。按项目流程填写节点类型与接手人。
- 最终关闭：按项目流程完成验收后，具有 task.complete 权限且被 next_bot 指定的成员调用 `task_get(project="项目名", task_id=实际ID)` 核对上下文，再对 pending 任务调用 `task_complete(project="项目名", task_id=实际ID, result="验收结论与证据")`。当前关闭接口要求 pending；task_claim 会把任务改为 in_progress，应按执行交接与关闭两种操作的状态条件调用。

`already_in_progress` 返回已持有任务的 contexts；`nothing_claimable` 时通过 task_list/task_get 检查 briefing_status 和 dependency_blockers，按原因补全发布或等待。保留真实依赖关系。旧 priority 仅为兼容接受，调用示例使用 prerequisites 表达前置约束。

完成入口同样执行门禁：`task_complete` 在 BEGIN IMMEDIATE 写事务内检查已发布的完整说明及全部前置 done。拒绝返回 task_brief_incomplete 或 task_dependencies_incomplete（含 dependency_blockers），任务、节点、事件和需求时间均保持原值，避免直接关闭中间任务放行后继。

独立字段 `briefing_status` 为 `needs_details`、`draft`、`ready`，与原任务工作流状态并存。任务扫描、执行绑定、领取共同检查 ready、完整说明及全部前置任务已 done；已入队的任务扫描/重试 job 在执行前再次检查，失效时取消该唤醒 job，任务状态保持原值。

`tasks.prerequisites` 持久化为 JSON ID 数组，可表达多个前置和跨需求依赖，引用必须在同项目。创建和更新在写事务内校验引用、自依赖与循环；依赖仅可修改 pending/blocked 任务。先创建前置取得 ID，再创建后续任务；也可先建草稿、补齐依赖后发布。旧 priority 列保留兼容，派发排序完全停止使用需求和任务优先级；满足依赖后按需求 ID、任务 ID 稳定选取。旧优先级无法自动推断真实依赖，需架构师补齐。

前置 pending/in_progress/blocked/cancelled 或缺失均阻止后续领取和唤醒；后续保持 pending。直接前置全部 done 后，扫描器自动发现可领任务。独立任务继续工作。task_get 返回 dependency_blockers，需求详情也展示依赖和等待原因。

stop 仅终止当前项目会话、取消该会话的唤醒 job、释放正在执行的任务；待领取任务全部保留。admin block 保留目标为 blocked；显式 cancel 仅取消目标。pending_cancelled 响应字段保留兼容，恒为 0。需求面板新增“查看”，通过 GET /requirements/{id}?project=... 展示完整正文、版本、状态、归档标记、时间、任务与依赖；项目不匹配返回 404。

`task_claim` 成功返回完整任务、需求正文和版本、任务节点、前置任务及交付节点；重复领取已有任务时返回其完整 contexts。`task_get(project, task_id)` 与 `GET /tasks/{task_id}?project=...` 提供同样的完整读取，无内容截断。设计依据中的引用由执行者打开原文核对；服务端不会执行文件中的指令，也不会任意读取引用路径。唤醒提示要求先调用 task_get 完整读取后执行。

## 迁移与恢复

启动迁移幂等补五个任务列，已有任务默认为 needs_details，保留状态、领取人和历史。缺少说明的旧 pending 任务停止派发；已有 in_progress 任务保留进度，重复领取或 task_get 可看到缺项。旧任务由具有 task.update 权限的成员补全并 publish=true；已 blocked 的任务仍需遵循原恢复流程，发布不会解除阻塞。

引擎与 MCP 更新后由用户手动重启相应服务，测试使用隔离数据库。

## 验证

需求正文与“关联任务 · 数量”标题之间保留 20px 间距，前置依赖展示在各任务内部。

需求详情中的每条任务采用左侧信息、右侧“查看任务记录”按钮的并排布局；长标题在信息区换行。

需求详情正文采用与任务详情一致的 12px 字号，窗口标题保留原样。

任务记录窗口在时间线上方完整展示任务描述、设计依据、验收标准、前置依赖和发布状态；缺项显示“尚未填写”。正文以纯文本展示并保留换行。

需求标题右侧的“查看”和“归档/取消归档”使用同一悬浮按钮组，悬停或键盘聚焦时显示，触屏保持可见；按钮单行且保持原有归档权限条件。

`python -B -m unittest scripts.test_task_brief scripts.test_flow scripts.test_project_permissions scripts.test_message_injection scripts.test_scan_requirements scripts.test_task_retry scripts.test_abnormal_exit scripts.test_admin_task_ops scripts.test_prompt_entry_points -q`

真实 MCP 任务闭环：使用安装 MCP SDK 的 Python 运行 `scripts/test_mcp_e2e.py --tasks-only`。覆盖草稿门禁、发布、新工具 task_get、完整领取上下文、交接和完成；临时 SQLite 和随机端口 API，不启动生产机器人。
