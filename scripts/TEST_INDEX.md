# 测试脚本索引

任务链与三层结构提示：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_prompt_entry_points scripts.test_project_templates scripts.test_template_role_rename scripts.test_product_prototype_template scripts.test_algorithm_template scripts.test_testing_template scripts.test_flow -q`，随后运行 `.venv\Scripts\python.exe -X utf8 -B scripts/test_mcp_handshake.py`。覆盖 work/chat 公共定义、六类模板预览与落盘中的完整任务链及返修路径、角色替换、同一任务的实际节点流转，以及两种模式真实 MCP 暴露的第一个接手人说明。使用隔离 SQLite、项目和宿主配置；这些检查验证提示与工具契约，真实模型的自主拆解行为需在隔离场景另行验收。

Hermes 兼容：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_hermes_compatibility scripts.test_execution_errors scripts.test_install scripts.test_hermes_transport scripts.test_runtime_fork -q`。覆盖新旧 ID、入口与源码身份、静态能力、导入错误、配置提示、多行传输及 Codex 隔离。对 v0.21.1 `cdf4c765` 和 v0.21.3 `afc3b7c6f3` 的真实源码分别执行 `.venv\Scripts\python.exe -X utf8 -B scripts/verify_hermes_compatibility.py --source <源码目录>`；脚本在临时 home/SQLite 与独立子进程中检查创建、恢复、标题迁移、历史保留、`/new`、generation、压缩续接、失效绑定及重启恢复。源码只读，使用本地已取得的对应提交，报告保存到项目主目录。契约见 [Hermes 兼容性](../docs/engineering/HERMES-COMPATIBILITY.md)。

Hermes 兼容性配套回归：设置 `HERMES_TEST_SOURCE` 为本机 Hermes 源码根，运行 `.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_maintenance_namespace scripts.test_slash_injection_review scripts.test_service -q`，验证实际会话 API 与维护模块双向导入、会话控制和安装/启动入口共存。

当前发布验收说明：[v0.1.6](../docs/releases/v0.1.6.md)。本版组合执行需求恢复、正式 Release 验收，以及设置菜单和服务重启浏览器回归；报告保存到主目录 `reports/tests/v0.1.6/2026-10-27-release/`。

需求恢复与空唤醒：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_requirement_recovery scripts.test_scan_requirements scripts.test_decompose_state_machine scripts.test_decompose_finalize scripts.test_requirement_lifecycle scripts.test_requirement_gate scripts.test_requirement_gate_reason scripts.test_message_injection scripts.test_task_retry scripts.test_queue_claim_race -q`。使用独立 SQLite 和模型投递记录器，覆盖 blocked 子任务连续扫描及显式恢复、部分拆解续作上下文、有效租约与拆解完成、来源撤回、授权/成员/项目门禁、负责人配置协调、存量队列取消、消息/任务合并保留及领取期间状态变化。生产数据及真实模型调用保持隔离；报告记录被测提交与实际命令。契约见 [需求恢复](../docs/engineering/REQUIREMENT-RECOVERY.md)。

页面复用：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_service scripts.test_ui_presence scripts.test_service_restart scripts.test_desktop scripts.test_app_upgrade -q`。覆盖管理员权限、同源登记门禁、回环地址归并、远端隔离、连接关闭、占位释放/过期、浏览器失败重试及维护启动。运行 `python -X utf8 -B scripts/test_ui_presence_browser.py` 验证真实 Chrome、实际 `start.bat` 与临时桌面快捷方式的首次打开、快速重复、登录页、后台页、刷新、多页面和全部关闭后重新打开；隔离服务端口与数据目录，浏览器打开请求使用记录器，实际焦点切换受浏览器策略限制。

桌面图标：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_desktop -q`。验证 ICO 七种尺寸、资源被发布清单收录，以及临时桌面中已有快捷方式从默认图标更新为安装目录内 ICO，保持启动参数、自定义数据目录及多实例共存。

成员识别与交接规则：复用 `.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_prompt_entry_points scripts.test_project_templates scripts.test_template_role_rename scripts.test_product_prototype_template scripts.test_algorithm_template scripts.test_testing_template scripts.test_project_setup_history -q`。验证 work/chat 静态提示词组成、六类模板预览与落盘、角色替换及初始化历史恢复。人工核对成员识别条款仅包含信息不足时查询和交接前取得有效 profile 与角色信息两项要求；六类模板保留职责与交接标准。上述检查覆盖规则与生成链路，模型实际调用行为另行隔离验证。

启动、桌面入口与自动刷新：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_service scripts.test_desktop scripts.test_install scripts.test_service_restart scripts.test_app_upgrade scripts.test_app_upgrade_integration -q`。覆盖 BAT 冷启动、重复启动复用同一实例、健康后打开配置地址、失败/维护流程保持浏览器调用次数、实例身份与数据目录隔离、真实 Windows `.lnk` 创建/幂等/多实例及特殊字符路径。桌面验证全部使用临时目录与惰性启动替身。Chrome 回归使用 `python -X utf8 -B scripts/test_service_restart_browser.py --evidence <主目录报告目录>/restart-browser` 和 `python -X utf8 -B scripts/test_app_upgrade_browser.py --evidence <主目录报告目录>/upgrade-browser`；验证新实例门槛、刷新续查、成功自动刷新一次、失败保留页面及升级窗口 390px 深浅主题。设计见 [启动与刷新](../docs/design/2026-10-26-startup-ui.md)。

成员候选识别：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_discovery scripts.test_profile_create scripts.test_codex_driver_regressions scripts.test_member_identity -q`。使用隔离配置与 SQLite，覆盖浏览器配置误识别、空或无效配置、仅会话文件、独立认证 home、嵌套 home、根配置与外部命名 profile、删除后刷新过滤、读取失败隔离，以及历史 Bot 登记保留。两处选择列表的端到端验证复用 `scripts/test_profile_create_browser.py`。

后台重启诊断：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_service_restart scripts.test_service scripts.test_install -q`。隔离 Windows 程序目录、数据和端口，验证失败安装检查的 stdout/stderr 写入重启日志且旧实例保留，随后连续两次正常重启；配套覆盖安装就绪检查与依赖核验。手动更新依赖声明后，先运行 `tools/install.py repair` 并确认 `tools/install.py check` 通过，再手动重启生产实例。

Profile 模型凭证与执行异常：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_profile_credentials scripts.test_profile_create scripts.test_execution_errors scripts.test_profile_config scripts.test_task_retry scripts.test_wake_pid_watch scripts.test_hermes_transport scripts.test_stop_scope -q`。覆盖静态 provider 扫描且源码未执行、默认/指定来源、自定义 key_env、多个 key 与地址、渠道排除、不兼容回滚；真实子进程 stderr、分类、脱敏、持久化去重、话题父消息、主动停止及重试。补充回归运行 `scripts.test_codex_driver_regressions scripts.test_wmic_free_wake scripts.test_engine_shutdown`，覆盖 Codex 过期凭证、实际唤醒链路及停机。浏览器运行 `scripts/test_profile_create_browser.py --evidence <主目录报告目录>/browser`，验证默认来源创建、无认证勾选框、桌面异常消息刷新恢复，以及创建对话框 390px 深浅主题。使用隔离目录、数据库和合成凭证；Hermes 安装版本升级后先检查静态适配器兼容性。

正式 Release 验收：从干净提交构建六项发布资产，核对 Git 跟踪文件、包内外清单、VERSION 与摘要；运行 `.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_public_installer scripts.test_release_upgrade scripts.test_legacy_release_upgrade scripts.test_windows_bat.BatBytesTests -q`。公开发布后运行 `.venv\Scripts\python.exe -X utf8 -B scripts/verify_public_install.py --shell powershell --port <隔离空闲端口> --evidence <主目录报告目录>/public-install`，验证匿名下载、真实依赖安装、重复安装、BAT 启停、HTTP 页面及默认升级检查。此验收关闭真实桌面快捷方式生成，浏览器调用由记录器接收。报告记录发布提交、tag、六项资产摘要、退出码与实例清理结果；最新发布说明见 [v0.1.5](../docs/releases/v0.1.5.md)。

维护模块与 Hermes 共存：设置 `HERMES_TEST_SOURCE` 为已安装 Hermes 源码根，运行 `.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_maintenance_namespace scripts.test_app_updates scripts.test_app_upgrade scripts.test_slash_injection_review -q`。覆盖同一进程内两种导入顺序、真实 SessionDB 创建/恢复/新建、更新检查和重启模块共存，以及基础 Python CLI、发布清单和独立 bootstrap 资产。`test_app_upgrade_integration` 另从提交 `1ac85a2d4fccc751343c940c3312f660314bd67d` 构造旧 tools 包安装，验证旧 API/助手迁移至 engine.maintenance、就绪失败回滚及成功后重启；配套运行 `test_service_restart`、`test_engine_shutdown`、`test_legacy_release_upgrade`。安装/发布回归沿用 `test_install`、`test_bootstrap`、`test_mcp_lock`、`test_release_upgrade`、`test_upgrade`、`test_public_installer`、`test_windows_bat`、`test_service`、`test_identity_repair`。入口位于 `tools/`，实现由 `engine.maintenance` 导入。

Windows 进程查询：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_process_query scripts.test_wmic_free_wake scripts.test_is_chat_process scripts.test_codex_driver_regressions scripts.test_slash_injection_review scripts.test_mcp_lock -q`。覆盖 WMIC 缺失、进程退出与 PID 复用、候选权限失败、Unicode/空格参数、精确 profile/session 匹配、Codex 执行上下文、日志限频及恢复。`test_wmic_free_wake` 使用确定性 Hermes 替身及真实 Windows 启动器、队列、SQLite、stdio MCP、HTTP API，验证 inbox 门禁、显式 @user 回复和已读确认；设置 `PROCESS_QUERY_EVIDENCE_DIR` 为主目录报告路径可保存链路证据。配套回归使用 `test_topic_skip`、`test_topic_restart_recovery`、`test_stop_scope`、`test_engine_shutdown`、`test_wake_pid_watch`、`test_hermes_transport`；真实通用 MCP 契约运行 `.venv\Scripts\python.exe -X utf8 -B scripts/test_mcp_e2e.py`。所有运行资源均隔离，真实模型调用和目标电脑验收单独记录。

`test_process_tree` 的真实 Windows 用例需要测试进程具备终止其子进程树的权限。执行器沙箱禁止 taskkill 时，按执行器审批流程取得所需权限后运行原测试。`test_topic_skip` 的启动用例使用独立 Hermes home、managed/data 目录及绝对项目路径。设置 `HERMES_TEST_SOURCE` 为已安装 Hermes 源码根可启用 `test_slash_injection_review` 的真实 SessionDB 用例，其数据库仍位于临时目录。

项目成员移出：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_project_member_removal scripts.test_member_disabled scripts.test_access_contract scripts.test_queue_claim_race -q`。隔离 SQLite/HTTP 验证管理员权限、职责与运行阻塞、提交前再次校验、事务回滚、话题推进、收件箱及队列清理、跨项目隔离、历史与目录保留、重新加入授权重置。`python -X utf8 -B scripts/test_project_member_removal_browser.py --evidence <主目录报告目录>` 验证更多菜单、键盘、1280px/390px 深浅主题、取消、处理入口、冲突/失败重试、刷新和重新加入；profile 入口复用 `test_project_models_browser.py` 与 `test_codex_config_browser.py`。

项目详情角色删除提示：`python -X utf8 -B scripts/test_member_switch_browser.py --evidence <主目录报告目录>` 验证职责缺员与 AGENTS.md 协作流程调整提示、取消时保持角色且发送零次删除请求、确定后发送一次删除请求，并保存 390px 确认框截图；保留已有成员数限制、启停、改名和权限提示回归。

未入组候选过滤与成员卡片：复用 `test_profile_create_browser.py --evidence <主目录报告目录>`，验证选择列表隐藏当前项目成员、刷新后继续过滤并保留选择、批量加入部分成功后移除成功成员，以及创建 profile 和新项目向导选择。检查详情窗口与新建项目向导的圆角成员卡片、名字与宿主 / profile 的两行布局，覆盖 1280px / 390px 深浅主题并保存截图；同时验证向导移除角色的协作流程调整提示、取消保留及确认后移除。

网关探针（req#132 #250 隔离验证，非 unittest 套件）：`python -B scripts/gw_probe.py`。隔离布局：代码在 `.probe-worktrees/hermes-v0225`（v0.22.5 `bf0203a7` 检出），持久测试数据在引擎分配的 test-data 下（`<test-data>/task-250/gw-probe-home` 与 `hermes-pm-store`，可用 `GW_PROBE_DATA` 覆盖）。经 `ensure_gateway_runtime` 拉起真实网关，走 `/api/ws` 完成握手（subprotocol hermes-gateway-v1）、session ticket 认证（control socket，purpose=interactive）、`runtime.describe` 能力识别（判 authority 非 legacy -32601）、`session.create`（source=tool）、`session.detach` + `session.resume` 独立恢复回执、`prompt.submit` 拿 admission_id、`prompt.receipt` 轮询终态并带 `include_result` 取回真实执行结果。启动与运行门禁（任一不满足即非零，不得假绿）：preflight 校验子进程环境白名单、config 显式 `mcp_servers: {}`、先停残留网关；create/resume 后要求 `info` 为 dict 且显式含 `mcp_servers`、值为 list 且空；运行前后 `mcp-stderr` 的 `/heartbeat` 计数必须可读且增量为 0。退出码：0=全部门禁通过且 terminal/completed；2=authority 检查失败；3=轮询超时未达终态；4=terminal 但 outcome 非 completed（如 failed）；5=隔离未知或失守（info/mcp_servers 缺失、类型异常或非空）；6=preflight 拒绝启动；7=resume 的 session_id 与 create 不一致；8=心跳门禁失败（mcp-stderr 不可读或有新增心跳）。`--preflight-only` 只跑静态门禁后退出（负向测试用，不启动网关）。证据默认落 `reports/research/hermes-gateway/`，可用 `GW_EVIDENCE_DIR` 覆盖；禁止 mock 回执。**脚本不自动停止网关**：结束时须经 pm 环境 python 执行 gateway stop 子命令（`-m hermes_cli.main`，`HERMES_HOME`/`HERMES_RUNTIME_DIR` 指向探针目录；不用 `.hermes/bin/hermes.exe` shim，它内嵌生成时的绝对路径，迁移后失效），并核对目标端口无 LISTENING、`gateway.pid` 不存在。清理删 test-data 下 `task-250/` 子目录与 `.probe-worktrees/hermes-v0225`（先 `git worktree remove`）。全程不触碰生产配置/数据/进程 8180。门禁单测：`python -B -m unittest scripts.test_gw_probe_gates`（27 项，覆盖缺失/None/类型异常、config 五类非法形态、心跳门禁与证据不可读；探针运行时未安装时整模块跳过）。设计依据见 [Hermes 网关迁移](../docs/design/2026-10-27-hermes-gateway-migration.md) 与 [v0.22.5 调用表](../docs/design/2026-10-27-hermes-gateway-v0225-call-table.md)。

Web 升级：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_app_upgrade scripts.test_app_upgrade_integration scripts.test_app_updates scripts.test_service_restart scripts.test_release_upgrade scripts.test_upgrade -q`。覆盖管理员鉴权、固定版本、重复提交与重启互斥、虚拟环境外升级进程，以及隔离 Windows 实例的真实 API→下载校验→备份→升级→启动；就绪失败及新服务启动失败时恢复旧程序与数据并启动。安装依赖使用受控夹具，其余安装目录、SQLite、端口和进程独立于生产。`python -X utf8 -B scripts/test_app_upgrade_browser.py --evidence <主目录报告目录>` 覆盖关于手动检查、两个升级确认入口、取消、提交响应丢失、刷新续查、失败/成功与 390px 深浅主题；配套 `test_app_settings_browser.py` 回归检查周期和设置菜单。

旧版原入口升级兼容：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_legacy_release_upgrade scripts.test_release_upgrade scripts.test_upgrade -q`。从 Git 提交 `098867cdef323f41b2d5f030900aff92aa6b9b52` 读取 v0.1.1 的真实升级器与依赖，在独立 Python 进程中升级新构建源码包；覆盖旧路径安装/就绪子进程、回滚鉴权环境加载、依赖/迁移/就绪失败恢复、清单与用户数据保留。需本地保留该发布提交；下载使用 file URL，停机与数据库迁移边界隔离，安装夹具记录参数与退出码。

维护工具迁移与版本识别：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_app_updates scripts.test_install scripts.test_bootstrap scripts.test_service scripts.test_service_restart scripts.test_release_upgrade scripts.test_upgrade scripts.test_public_installer scripts.test_windows_bat scripts.test_mcp_lock scripts.test_identity_repair -q`。覆盖真实临时 Git 的 tag/提交距离/dirty/worktree、Release VERSION、源码父目录隔离、旧 scripts 布局到 tools 的升级及失败回滚、文件清单、启动入口和安装环境。运行工具位于 `tools/`，测试与测试支持保留在本目录。

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

资源配置布局与角色权限提示：`python scripts/test_project_settings_browser.py --permissions-only` 覆盖新增授权、取消未保存的新增授权、撤销已保存授权、保存冲突及 390px；`python scripts/test_project_setup_browser.py --permissions-only` 覆盖向导新增/减少权限、生效提示及角色删除/取消；省略参数继续完整设置和向导流程。`python scripts/test_member_switch_browser.py --evidence <报告目录>` 覆盖角色恢复勾选、删除确认和 390px 深浅主题。资源弹窗补查标签间距 7px、状态提示上方 12px、操作区上方 18px、按钮间距 8px，以及长路径、保存反馈、关闭重开与刷新。

场景模板执行请求入口：复用 `.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_project_templates scripts.test_template_role_rename scripts.test_product_prototype_template scripts.test_algorithm_template scripts.test_testing_template scripts.test_project_import_supplement scripts.test_project_setup_history -q`。覆盖六类模板生成、角色变量替换、预览与落盘一致、shared/worktree、角色授权和历史快照；人工核对执行请求、已有任务续做、接收成员转交、话题规划及 work 执行条款与各模板原流程一致。以上验证范围为模板与接口，模型实际遵循另行通过隔离行为场景验证。

调研、写作和产品原型 worktree 说明：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_project_templates scripts.test_template_role_rename scripts.test_product_prototype_template -q`。使用仓库外临时目录及隔离 Git/SQLite，覆盖三类项目的阶段分支、实际角色名称、审查与合并交接、预览到落盘及初始化快照，并回归软件模板和共享目录模式。

软件 UI 规范：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_project_templates scripts.test_template_role_rename scripts.test_project_import_supplement scripts.test_project_setup scripts.test_project_rules -q`。使用隔离数据库、项目目录与宿主配置，覆盖 `docs/ui-spec/UI-SPEC.md` 生成、角色替换、Git 初始化及 worktree 文件同步、导入补齐和已有规范保留、旧 `doc/` 复用，以及 work/chat 项目路径注入。

唤醒提示词与项目规则：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_project_rules scripts.test_prompt_entry_points scripts.test_workspace_resources scripts.test_codex_project_access -q`。验证共享目录和 worktree 的规则加载、主目录与当前目录及资源注入、项目隔离、静态规则组合，以及独立工作区的共享报告目录授权；目录分类和报告位置由项目规则维护。

提示词与角色参考文案：复用 `.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_prompt_entry_points scripts.test_flow scripts.test_project_templates scripts.test_template_role_rename scripts.test_product_prototype_template scripts.test_algorithm_template scripts.test_testing_template -q`，验证提示词组合、任务流转及六类模板生成；运行 `.venv\Scripts\python.exe -X utf8 -B scripts/test_mcp_handshake.py` 核验实际注册的工具说明与参数。人工检查直接提示、角色参考和启动阅读指令中的冗余规则来源声明，核对具体职责与参数要求；Bot 唤醒后的实际阅读行为单独验收。

真实规则加载：`.venv\Scripts\python.exe -X utf8 -B scripts/verify_prompt_rules_live.py --live --hermes-home <已授权的Hermes主目录> --evidence <主目录报告目录>/prompt-live.json`。复用明确选定的 Ollama Cloud 凭证与模型，使用临时数据库、项目、宿主 home 和随机 API 端口；保留文件读取工具，在同一话题连续唤醒三次，每次最长 240 秒。证据记录原始调用、规则令牌、角色与业务结果、已读状态及原配置指纹。输出路径须为新的绝对路径；模型或工具执行失败保留诊断，已有生产会话保持原状。

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
| 扫描、队列与进程 | `test_scan_order.py`、`test_scan_requirements.py`、`test_requirement_recovery.py`、`test_queue_claim_race.py`、`test_abnormal_exit.py`、`test_pids_alive.py`、`test_wake_pid_watch.py`、`test_wake_index_migration.py`、`test_is_chat_process.py`、`test_runtime_fork.py` |
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

`test_background_storage_browser.py` 同时检查设置菜单收起后，背景弹窗在加载、上传和恢复默认完成时保持位于右侧设置按钮下方。设置菜单图标调整后，复用 `test_app_settings_browser.py` 与 `test_service_restart_browser.py`，检查桌面和 390px 深浅主题截图：每项图标 16px、文字间距 10px，主题图标跟随切换，重启中及失败恢复后图标保留，键盘与禁用状态可辨识。

## 维护

独立 profile 与共用列表：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_profile_create scripts.test_discovery -q` 验证独立创建和重试、显式入组后的目录授权、旧 project 调用兼容、非法项目与鉴权。`python -X utf8 -B scripts/test_profile_create_browser.py --evidence <主目录报告路径>` 使用临时宿主、SQLite/Git 与真实 API，覆盖两处列表、选择保留、创建失败和丢响应重试、部分入组失败、取消保留、SOUL、最终确认及刷新；配套运行 `test_profile_soul_templates_browser.py` 与 `test_member_name_browser.py`。

应用设置与消息确认：`.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_app_updates scripts.test_message_execution scripts.test_inbox_seen_gate scripts.test_message_degrade_pagination scripts.test_read_inbox_batch scripts.test_group_send_thread_id -q`。覆盖正式版索引/未知版本/失败重试/管理员鉴权，以及 MCP→HTTP 读取重入、空分页 pending、发送失败、发送成功仍 pending、确认失败与成功清除。

`python -X utf8 -B scripts/test_app_settings_browser.py --evidence <主目录报告路径>` 验证打开/刷新、12 小时计时、休眠后补查、并发去重、安静重试、未知版本、菜单/主题/背景与 390px 深浅主题。`test_member_switch_browser.py --evidence <主目录报告路径>` 验证启停等待/缓存/失败恢复/键盘和角色删除、改名、权限提示。`test_project_setup_browser.py --role-notices-only` 验证向导角色提示、规则审阅、隔离 Git/SQLite 提交和刷新恢复；省略参数继续完整套件。复用 `test_user_display_name_browser.py` 与 `test_service_restart_browser.py` 验证称呼持久化与重启回执。浏览器解释器为 Python 3.10 时可在测试启动器中用 `sys.modules['tomllib']=tomli` 提供兼容，后端使用项目 Python 3.11。

用户显示称呼：`test_user_display_name.py` 使用隔离 SQLite 和真实 API 验证持久化、旧值保留、身份隔离和输入校验；运行 `.venv\Scripts\python.exe -X utf8 -B -m unittest scripts.test_user_display_name -q`。`test_user_display_name_browser.py` 使用已安装 Playwright 的 Python 和 Chrome，验证右上角设置、刷新及独立浏览器上下文、保存失败恢复和窄屏布局；运行 `python -X utf8 -B scripts/test_user_display_name_browser.py`，称呼读写转接隔离 API，其余页面接口使用测试数据。

新增或重命名脚本时更新所属分类、运行条件与命令。用 `rg --files scripts -g 'test_*' -g 'verify_*' -g 'spike_*'` 核对当前文件集合。完整 unittest discovery 会收集可选环境用例，按任务范围执行相关集合即可。测试报告记录被测提交、实际命令、结果、跳过原因和遗留问题。
