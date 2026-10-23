# 测试脚本索引

手工拆解凭据与鉴权测试隔离：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_task_api scripts.test_access_contract scripts.test_codex_driver_regressions scripts.test_hermes_transport scripts.test_requirement_lifecycle scripts.test_task_brief -q`。同进程串联验证健康接口鉴权与后续 Bot 启动，覆盖缺省/null/空凭据生成、非法类型拒绝、跨成员修订、已有空锁恢复、发布及完成提交。真实手工 stdio MCP 链路运行 `.venv\Scripts\python.exe -X utf8 -B scripts/test_mcp_e2e.py`；自动执行和手工成员分别使用隔离凭据。

自定义数据目录升级：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_release_upgrade scripts.test_config scripts.test_upgrade -q`。使用隔离发布包、真实环境变量和临时数据库，覆盖程序目录内外的自定义数据目录、成功升级、失败回滚、数据库/附件/配置保留，以及依赖子进程继承目录。网络、生产停机与真实依赖安装使用模拟边界。

Codex 新建执行配置与入组双提醒：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_profile_create scripts.test_codex_sandbox_notice scripts.test_discovery scripts.test_codex_project_access -q`。覆盖默认/来源创建统一 `on-request + auto_review + workspace-write`、Windows 实现选择、命名配置覆盖、来源只读、重试、两条路径目录授权，以及已有 Bot 只提醒并保留审批/沙箱设置。`python -X utf8 -B scripts/test_profile_create_browser.py --evidence <报告目录>` 检查双提醒、单独智能审批提醒、切换与刷新、可直接加入、创建规则文案和窄屏布局。Windows 原生探针：`.venv\Scripts\python.exe -X utf8 -B scripts/verify_codex_profile_sandbox.py --evidence <全新报告子目录>`，使用已安装 Codex CLI、Git 和 PowerShell，在隔离 home/仓库内验证生成配置下的沙箱命令启动、文件读取和 Git 状态；无需认证和模型调用，命令原始输出保存在 `commands.json`。嵌套受限宿主可能返回 `no home dir`，此时需在允许启动子沙箱的宿主运行；探针保持现有 Bot 配置原样。

Codex 入组沙箱提示与对话框校验：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_codex_sandbox_notice scripts.test_discovery scripts.test_codex_project_access -q`，使用临时配置与 SQLite 验证独立 home、命名继承、缺省/异常/完全访问、读取不写配置及提示后正常加入。`python -X utf8 -B scripts/test_profile_create_browser.py --evidence <报告目录>` 使用已安装 Playwright 的 Python 和 Chrome，覆盖中文/保留 Profile 标识的红色错误、修正清除、必填与端口校验、服务端错误、沙箱提醒后直接加入、切换候选、刷新、390px 深浅主题。`python -X utf8 -B scripts/test_alert_dialog.py` 回归通用弹窗队列与焦点。受限环境将 TEMP/TMP 指向已授权的隔离临时目录。

测试项目模板及三类角色参考：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_testing_template scripts.test_algorithm_template scripts.test_product_prototype_template scripts.test_project_templates scripts.test_project_import_supplement scripts.test_project_setup_history scripts.test_project_setup scripts.test_template_role_rename scripts.test_release_upgrade.ReleaseUpgradeTests.test_source_archive_keeps_public_docs_and_template_resources_only -q`。使用仓库外临时 Git/SQLite/宿主，覆盖三角色授权、用例评审返修、产品失败保留与报告验收、PRD/原型/用例/脚本索引、15 个骨架文件、角色改名、两种目录模式、导入保留与 doc 复用、历史恢复、源码包及最新参考。浏览器使用 `python -X utf8 -B scripts/test_project_templates_browser.py --evidence <报告目录>/browser.json --screenshot <报告目录>/wizard.png`，覆盖六类模板、三类新模板完整创建刷新、角色参考复制下载和 390px 窄屏。受限环境设置仓库外已授权 TEMP/TMP，结果与截图保存到项目主目录 reports/tests。

算法研发模板：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_algorithm_template scripts.test_project_templates scripts.test_project_import_supplement scripts.test_project_setup_history scripts.test_project_setup scripts.test_template_role_rename scripts.test_release_upgrade.ReleaseUpgradeTests.test_source_archive_keeps_public_docs_and_template_resources_only -q`。使用隔离 SQLite、Git、宿主与项目目录，覆盖四角色授权、返修复审、角色改名、shared/worktree、11 个骨架文件、用户管理工程布局、方案内实验计划、报告 Git 排除、导入保护和旧 doc 目录复用、历史模板恢复及源码包资源。`python -X utf8 -B scripts/test_project_templates_browser.py --evidence <报告目录>/browser.json --screenshot <报告目录>/wizard.png` 使用已安装 Playwright 的 Python 与 Chrome，覆盖五类模板参考、算法四角色创建、刷新、复制下载和 390px 窄屏。受限环境将 TEMP/TMP 指向仓库外已授权的隔离临时目录。

公开安装入口：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_public_installer scripts.test_bootstrap scripts.test_release_upgrade scripts.test_windows_bat -q`。覆盖真实 PowerShell 5.1 执行、Python 探测、固定版本与摘要、失败退出及环境恢复、发布资产和默认升级源；下载使用隔离夹具。公开发布后还须从 README 命令经匿名 HTTPS 下载，在空白目录完成真实依赖安装、BAT 启停、HTTP 页面和默认升级检查，并把证据保存在主目录 `reports/tests/`。

公开端到端：`python -X utf8 -B scripts/verify_public_install.py --shell powershell --port <隔离端口> --evidence <新的报告目录>`，CMD 入口使用 `--shell cmd`。从 README 读取用户命令，真实匿名下载正式 Release、创建 venv、安装依赖、同版本重装、空 Python PATH 下 BAT 启停、HTTP 及默认升级查询；脚本隔离实例、数据和运行时目录并在结束时清理测试服务。运行目录必须为空，避开生产端口 8180。

Codex 进程探针 `spike_codex_mcp_residual.py`、`spike_codex_wait_semantics.py` 从 `CODEX_BIN` 或 `PATH` 定位可执行文件。`--help` 可检查参数入口；`--live` 会调用真实模型。目录选择器浏览器回归使用通用 `developer` 用户目录夹具。

访问控制回归：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_access_contract scripts.test_requirement_lifecycle -q`。覆盖所有业务路由匿名拒绝、成员凭据绑定、管理员边界、项目/话题/收件箱/心跳范围、Unicode 密码、登录限流、SSE cookie 和终态任务门禁。均使用隔离 SQLite。

浏览器认证回归：`python -X utf8 -B scripts/test_access_browser.py`。使用已安装 Playwright 与 Chrome 的 Python，启动独立 API 验证中文密码登录、真实 SSE、刷新后重连、cookie 范围与退出；不启动生产队列。受限环境可将 `TEMP/TMP` 指向已授权临时目录。

发布卫生回归：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_release_upgrade.ReleaseUpgradeTests.test_source_archive_keeps_public_docs_and_template_resources_only -q`，覆盖备份、临时、补丁残留及内部资料的归档排除，以及软件和产品原型模板资源的保留。

本索引记录可复用用例、运行条件和命令。执行结果、日志、截图与现场证据写入**项目主目录** `reports/tests/`，技术调研放在 `reports/research/`；worktree 使用引擎注入的“项目主目录”拼接对应路径。需求、设计与工程文档随当前工作区提交，审查和验收结论写入任务节点。技术契约见 [文档索引](../docs/INDEX.md) 和 [存放约定](../docs/guides/DOCUMENT-STORAGE.md)。

## 运行约定

- 按改动范围选择相关用例，先核对脚本的依赖、临时目录、端口、进程和宿主访问范围。使用临时 SQLite、测试 profile 和隔离服务，生产实例保持独立。
- Python 通常使用项目 `.venv` 的 Python 3.11+。浏览器脚本另外需要 Playwright 与其可用浏览器；MCP 脚本需要项目 MCP 依赖。平台或可选宿主依赖缺失时记录具体跳过原因。
- Git 初始化/worktree 测试将 `TEMP`、`TMP` 指向仓库外的独立临时目录，避免被当前仓库的父目录检测干扰。进程树测试只允许终止其自身创建的进程。
- `verify_*` 和 `spike_*` 先阅读脚本参数与 `--help`；真实模型、认证复用、交互桌面或运行实例探针需明确授权后执行。单元测试和隔离浏览器通过分别记录。
- 脚本的 `--evidence`、`--screenshot`、`--screenshot-dir` 等输出参数使用主目录测试报告绝对路径。短期缓存可用分配的测试数据目录，留档时归入测试报告。

```powershell
# 替换模块名运行相关 Python 回归
.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_project_rules scripts.test_project_templates -q
# Node VM 回归
node scripts/test_member_loading.cjs
# 浏览器脚本使用已安装 Playwright 的解释器
python -X utf8 -B scripts/test_project_templates_browser.py
```

## Python 单元与隔离集成

调研、写作和产品原型 worktree 说明：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_project_templates scripts.test_template_role_rename scripts.test_product_prototype_template -q`。使用仓库外临时目录及隔离 Git/SQLite，覆盖三类项目的阶段分支、实际角色名称、审查与合并交接、预览到落盘及初始化快照，并回归软件模板和共享目录模式。

软件 UI 规范：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_project_templates scripts.test_template_role_rename scripts.test_project_import_supplement scripts.test_project_setup scripts.test_project_rules -q`。使用隔离数据库、项目目录与宿主配置，覆盖 `docs/ui-spec/UI-SPEC.md` 生成、角色替换、Git 初始化及 worktree 文件同步、导入补齐和已有规范保留、旧 `doc/` 复用，以及 work/chat 项目路径注入。

唤醒提示词与项目规则：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_project_rules scripts.test_prompt_entry_points scripts.test_workspace_resources scripts.test_codex_project_access -q`。验证共享目录和 worktree 的规则加载、主目录与当前目录及资源注入、项目隔离、静态规则组合，以及独立工作区的共享报告目录授权；目录分类和报告位置由项目规则维护。

Codex 文件认证：`test_codex_auth.py` 使用临时 home 和合成 JWT 验证有效期、按需复制、401 补查、主账户缺失/过期、不同账户、原子写失败、并发检查与原生更新保留；`test_codex_driver_regressions.py` 验证启动前同步及每次执行只补查一次；`test_profile_create.py` 验证默认继承主账户与旧客户端兼容。命令：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_codex_auth scripts.test_profile_create scripts.test_codex_driver_regressions -q`。浏览器回归：`python -X utf8 -B scripts/test_profile_create_browser.py`，使用已安装 Playwright 的 Python 和 Chrome，覆盖 Codex 认证选项移除及 Hermes 原行为。

安装目录回归：`test_config.py` 与 `test_install.py` 验证全新安装默认在程序根目录创建并读取 `.env`、数据路径一致、重复安装保留配置，以及显式数据目录和命令行的优先级。使用临时目录和模拟启动，运行命令：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_config scripts.test_install -q`。

MCP API 与运行时配置：`test_task_api.py` 验证成员/项目/执行校验、参数白名单、管理员边界和数据库无关的客户端；`test_mcp_runtime.py` 验证临时覆盖隔离、配置优先级、清理及 Codex 参数。运行 `.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_task_api scripts.test_mcp_runtime scripts.test_hermes_transport -q`。设置 `HERMES_TEST_SOURCE` 为 Hermes 源码目录、`HERMES_TEST_PYTHON` 为其解释器绝对路径可运行原生配置加载用例；只读取宿主代码，配置使用临时目录，无模型调用。`test_mcp_e2e.py` 使用真实 MCP 与隔离 HTTP 服务验证任务完整流程及运行中数据库位置变更。

执行凭据与事务边界：`test_task_api.py` 覆盖绑定凭据省略上下文、CHAT/WORK 边界、执行过期、请求校验与写入间撤权、拆解凭据跨成员复用及话题修订入口；`test_access_contract.py` 和 `test_mcp_runtime.py` 验证签名凭据注入。`test_mcp_e2e.py` 同时验证自动执行凭据与手工成员凭据，运行 `.venv\Scripts\python.exe -X utf8 -B scripts/test_mcp_e2e.py`。

附件权限与本地图片话题：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_image_attachments scripts.test_thread_creation -q`，覆盖撤销成员资格、项目归档/移除、免密码图片话题、附件绑定及幂等重试。`python -X utf8 -B scripts/test_image_browser.py` 使用隔离 API 和真实 Chrome 验证图片话题创建、刷新持久化、发送、预览与跨项目草稿隔离。

维护请求鉴权：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_upgrade.SafeStopTest -q`，覆盖 `.env` 密码与进程环境优先级、带鉴权健康检查/停机、实例路径匹配、端口拒绝连接及退出失败。网络与退出动作使用隔离夹具。

以下名称均位于 `scripts/`，可用 `scripts.<不含 .py 的文件名>` 传给 unittest；独立脚本的额外参数以源码为准。

软件模板规则调整复用 `scripts.test_project_templates` 与 `scripts.test_template_role_rename`，覆盖 shared/worktree 预览与落盘一致、角色替换和模板快照；命令为 `.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_project_templates scripts.test_template_role_rename -q`。新建项目 Git 基线与成员工作区使用隔离用例 `scripts.test_project_setup` 验证，覆盖 docs 需求设计入库、reports 两类报告排除及主目录授权。协作回归使用 `test_flow.py`、`test_requirement_gate.py`、`test_review_control_regressions.py`、`test_prompt_entry_points.py`。

产品原型模板：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_product_prototype_template scripts.test_project_templates scripts.test_template_role_rename scripts.test_project_import_supplement scripts.test_project_setup_history scripts.test_project_setup -q`。使用仓库外临时 SQLite/Git/宿主目录，覆盖四角色授权、UX 返修复审与负责人关闭、两种目录模式、角色改名、docs/materials/ui-spec/outputs 分类及版本化、references 目录生成、模板资源与清单一致且无遗留空目录、默认 UI 规范及跨目录引用、doc 复用与既有设计资料保护、创建导入和历史模板恢复。`python -X utf8 -B scripts/test_project_templates_browser.py --evidence <测试报告绝对路径> --screenshot <测试截图绝对路径>` 使用 Playwright 与 Chrome，覆盖五类模板参考、产品原型和算法研发四角色创建及分类目录生成、刷新后参考、复制下载和窄屏。

| 范围 | 用例 |
| --- | --- |
| 配置、认证、安装 | `test_config.py`、`test_config_cli.py`、`test_config_cross_process.py`、`test_auth_status.py`、`test_install.py`、`test_bootstrap.py`、`test_mcp_lock.py` |
| 发布、升级与服务 | `test_release_upgrade.py`、`test_upgrade.py`、`test_service.py`、`test_service_restart.py`、`test_windows_bat.py`、`test_engine_shutdown.py`、`test_process_tree.py` |
| 项目创建、导入和模板 | `test_project_readiness.py`、`test_project_import.py`、`test_project_import_supplement.py`、`test_project_initialization.py`、`test_project_setup.py`、`test_project_setup_history.py`、`test_project_templates.py`、`test_project_templates_live_dispatch.py`、`test_import_agents_edit.py`、`test_template_role_rename.py` |
| 项目身份与生命周期 | `test_project_lifecycle.py`、`test_project_removal.py`、`test_project_members_migration.py`、`test_work_project_identity.py`、`test_project_owner.py`、`test_pm_bot_id_migration.py`、`test_sb_bot_id_migration.py`、`test_builtin_user_migration.py` |
| 角色与授权 | `test_project_permissions.py`、`test_project_roles.py`、`test_permission_roles.py`、`test_role_entity.py`、`test_expected_role_gate.py` |
| 工作区、资源和规则 | `test_project_workspaces.py`、`test_workspace_resources.py`、`test_project_rules.py`、`test_agents_md.py`、`test_directory_list.py`、`test_directory_picker.py` |
| 成员与配置 | `test_discovery.py`、`test_member_identity.py`、`test_identity_repair.py`、`test_member_disabled.py`、`test_profile_create.py`、`test_profile_config.py`、`test_codex_profile_config.py`、`test_codex_project_access.py`、`test_project_models.py` |
| 需求、拆解与门禁 | `test_requirement_fields.py`、`test_requirement_gate.py`、`test_requirement_gate_reason.py`、`test_req_archive.py`、`test_decompose_chat.py`、`test_decompose_finalize.py`、`test_decompose_stale_after_done.py`、`test_decompose_state_machine.py` |
| 任务、交接与并发 | `test_flow.py`、`test_task_brief.py`、`test_task_history.py`、`test_task_retry.py`、`test_admin_task_ops.py`、`test_group_task_update_credential.py`、`test_review_control_regressions.py`、`test_review_regressions.py` |
| 消息投递与读取 | `test_group_send_thread_id.py`、`test_empty_sender.py`、`test_read_inbox_batch.py`、`test_inbox_seen_gate.py`、`test_need_reply_nullable.py`、`test_recipient_need_reply.py`、`test_reply_project.py`、`test_retract.py`、`test_mention_scope.py`、`test_self_mention.py`、`test_speak_read_granularity.py` |
| 消息注入、执行与分页 | `test_message_injection.py`、`test_message_degrade_pagination.py`、`test_message_execution.py`、`test_message_pagination.py`、`test_message_stale_failure.py`、`test_signal_prefix.py`、`test_prompt_entry_points.py`、`test_hermes_transport.py` |
| 话题与轮次 | `test_thread_creation.py`、`test_chat_isolation.py`、`test_topic_boundary_regressions.py`、`test_topic_restart_recovery.py`、`test_topic_skip.py`、`test_topic_unread.py`、`test_member_add_turn.py`、`test_skip_turn.py`、`test_turn_timeout_active.py`、`test_p2p_stale.py` |
| 扫描、队列与进程 | `test_scan_order.py`、`test_scan_requirements.py`、`test_queue_claim_race.py`、`test_abnormal_exit.py`、`test_pids_alive.py`、`test_wake_pid_watch.py`、`test_wake_index_migration.py`、`test_is_chat_process.py`、`test_runtime_fork.py` |
| 会话、命令与宿主 | `test_session_binding.py`、`test_session_switch.py`、`test_binding_column_order.py`、`test_member_session_reset.py`、`test_codex_session_mapping.py`、`test_codex_driver_regressions.py`、`test_commands_dispatch.py`、`test_stop_scope.py`、`test_steer_ledger.py`、`test_slash_injection_review.py` |
| 可选 Hermes 集成 | `test_hermes_embed_entry.py`、`test_hermes_session_restore.py`、`test_hermes_steer_delivery.py`；先核对脚本中的宿主依赖和运行边界 |
| 思考、图片与浏览器资源 | `test_active_entries_session.py`、`test_reasoning_window.py`、`test_thinking_run_identity.py`、`test_image_attachments.py`、`test_background_storage.py`、`test_browser_resources.py`、`test_browser_harness_integration.py` |
| 前端静态契约 | `test_gate_reason_frontend.py` |

## Node VM 回归

使用 `node scripts/<文件名>` 运行，测试数据由脚本构造。

| 范围 | 用例 |
| --- | --- |
| 项目与成员 | `test_project_views.cjs`、`test_member_loading.cjs`、`test_member_labels.cjs`、`test_member_mentions.cjs` |
| 需求与任务 | `test_empty_requirements.cjs`、`test_requirement_details.cjs`、`test_task_history.cjs` |
| 消息与话题 | `test_composer_limit.cjs`、`test_reply_editor.cjs`、`test_message_pagination.cjs`、`test_mentions.cjs`、`test_mention_persistence.cjs`、`test_topic_page_grouping.cjs`、`test_topic_unread.cjs`、`test_hidden_topics.cjs`、`test_system_notification.cjs` |
| 思考与命令 | `test_direct_thinking_order.cjs`、`test_thinking_lifecycle.cjs`、`test_thinking_cache.cjs`、`test_wake_liveness.cjs`、`test_command_controls.cjs`、`test_command_feedback.cjs` |

## 浏览器回归

使用 `python -X utf8 -B scripts/<文件名>`。脚本通过合成 HTTP 或隔离服务验证真实 DOM、交互和刷新，先确认解释器的 Playwright/浏览器依赖。

| 范围 | 用例 |
| --- | --- |
| 项目与模板 | `test_project_entry_browser.py`、`test_project_import_browser.py`、`test_project_setup_browser.py`、`test_project_templates_browser.py`、`test_project_settings_browser.py`、`test_project_history_browser.py`、`test_project_removal_browser.py`、`test_project_menu_loading_browser.py`、`test_workspace_save_browser.py` |
| 成员、角色与配置 | `test_profile_create_browser.py`、`test_profile_soul_templates_browser.py`、`test_codex_config_browser.py`、`test_project_models_browser.py`、`test_member_name_browser.py`、`test_member_role_dropdown_browser.py`、`test_member_mentions_browser.py` |
| 消息与需求 | `test_message_navigation_browser.py`、`test_requirement_details_browser.py`、`test_thread_creation_browser.py`、`test_image_browser.py` |
| 外观和控件 | `test_alert_dialog.py`、`test_appearance_browser.py`、`test_background_storage_browser.py`、`test_directory_picker_browser.py`、`test_fixed_controls_browser.py`、`test_thinking_cache_browser.py`、`test_thinking_compaction_browser.py`、`test_service_restart_browser.py` |

## MCP 协议与专项探针

MCP 独立入口：`test_mcp_handshake.py`、`test_mcp_e2e.py`、`test_mcp_message_execution.py`。先核对其临时数据库和执行模式，使用已安装 MCP SDK 的解释器直接运行。

| 类型 | 脚本与运行条件 |
| --- | --- |
| 合成页面与任务专项 | `verify_message_selection.py`、`verify_hide_chain.py`、`verify_task_history.py`、`verify_zindex.py`；阅读夹具和参数后运行 |
| 安装与发布专项 | `verify_release_install.py`、`verify_release_browser.py`；使用隔离程序/数据目录、独立服务及端口，覆盖安装升级和恢复 |
| 项目迁移 | `verify_project_readiness_migration.py`；指定源数据库只读检查和临时副本，证据输出到测试报告 |
| 本地原生运行时 | `verify_project_model_resume.py`；临时 home 与本地模拟服务；`verify_directory_picker_native.py`、`verify_directory_picker_live.py` 需要交互桌面，后者优先使用 `--isolated` |
| 真实模型，显式启用 | `verify_codex_driver_live.py`、`verify_project_templates_live.py`、`spike_codex_developer_instructions.py`、`spike_codex_subprocess.py`、`spike_codex_wait_semantics.py`、`spike_codex_mcp_residual.py`；阅读参数，明确授权后用 `--live`，记录认证复用与额度消耗范围 |

模板真机探针额外要求绝对 `--evidence` 输出路径，以及对应场景的 `--codex-home` / `--hermes-home` 认证来源；可用 `--hermes-bin` 指定原生入口。先明确来源认证复用与模型出站范围，再执行 `--live`。示例中的目录由操作者填写项目主目录及获准的宿主 home。

测试支持模块：`project_test_support.py`、`background_test_support.py`、`unified_test_support.py`。它们供测试导入，维护时同步检查相关调用方。

## 维护

用户显示称呼：`test_user_display_name.py` 使用隔离 SQLite 和真实 API 验证持久化、旧值保留、身份隔离和输入校验；运行 `.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_user_display_name -q`。`test_user_display_name_browser.py` 使用已安装 Playwright 的 Python 和 Chrome，验证右上角设置、刷新及独立浏览器上下文、保存失败恢复和窄屏布局；运行 `python -X utf8 -B scripts/test_user_display_name_browser.py`，称呼读写转接隔离 API，其余页面接口使用测试数据。

新增或重命名脚本时更新所属分类、运行条件与命令。用 `rg --files scripts -g 'test_*' -g 'verify_*' -g 'spike_*'` 核对当前文件集合。完整 unittest discovery 会收集可选环境用例，按任务范围执行相关集合即可。测试报告记录被测提交、实际命令、结果、跳过原因和遗留问题。
