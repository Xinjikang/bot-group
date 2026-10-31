# DeepSeek Harness 接入设计

日期：2026-10-31。项目：bot-group。范围：新增 DeepSeek Harness（dsh）作为第三个 bot 运行时。

## 目标与范围

- 新增 `dsh` 运行时类型，与现有 `hermes`、`codex` 并列，按 `bots.runtime` 分叉 spawn。
- 首期采用 headless 子进程接入，复用通用队列、心跳、退出、重试、会话绑定与执行凭据。
- 持续连接、执行中输入与交互审批另列能力边界，不在首期范围。

## 现状与接入模式

引擎按 `bots.runtime` 解析运行时并选择执行路径：`hermes` 走 hermes gateway 或原生 CLI；`codex` 走 `codex exec --json`。通用队列负责项目隔离、忙闲、心跳、退出与重试，会话绑定按 runtime 判读 `runtime_session_id`。DSH 接入复用这些设施，补齐宿主注册、原生启动、事件解析、会话和停止清理的协议适配。

dsh 官方提供 headless 一次性执行：`dsh --profile headless "任务"`，支持 `--json` 事件流、`--session-id` 续跑、stdin 传任务，与 codex exec 对应。

## 技术方案

1. **宿主接入**：新增 `dsh` 类型；发现/注册、配置根、模型选择与前端展示；记录入口、支持版本、Bot home 与认证方式。
2. **执行**：headless 子进程，在子进程环境显式传 `DSH_HOME`（不依赖系统环境变量传播），在实际工作区 cwd 执行，通过 stdin 提交完整提示词，通过本次执行 overlay 注入 MCP。
3. **MCP**：复用引擎执行凭据上下文与 chat/work 工具注册边界，显式提供 command、args、cwd、env；必需工具初始化失败阻止任务进入模型执行，保持可诊断终态。
4. **会话**：从 `--json` 的 session 事件取真实 ID，按 project/bot/work-chat/thread 绑定；续跑使用 `--session-id` 与相同目录，保留 generation 守卫覆盖旧执行迟到回填与 `/new`。
5. **结果**：按实装版本解析事件。已验证的 `0.2.1-rc.5` 在开头 session 事件提供会话 ID，final 仅含 type/text；以进程退出码、final 完整性和脱敏错误诊断判读执行状态。支持 `final.reason` 的后续版本经能力验证后使用该字段。缺少 final、损坏 JSON、初始化失败、模型拒绝、取消与超时保持可区分；任务业务成果另行验收。
6. **生命周期**：`/stop` 处理整棵执行进程树，等待退出后恢复原会话；清理 MCP、overlay 与临时数据，保留脱敏诊断记录。

## 认证与配置

- 接入方式以用户在所选 DSH 原生 profile 中的配置为依据，支持账号登录和 API key。引擎保存宿主定位及 profile/config 引用，启动时解析本次提供商选择；凭据继续由 DSH 管理。账号路由使用 DSH 保存的账号授权；API-key 路由支持 `DSH_HOME/.env` 的 `DEEPSEEK_API_KEY` 和界面保存的原生凭据。两类路由分别核验，认证失败保持所选路由并明确报错，禁止自动切换认证方式。
- 继承所选原生 profile 的 provider、model 及相关服务配置，由 DSH 原生适配器处理目标地址与认证。`deepseek-account / deepseek-flash` 为本次通过的验证组合，具体值从配置读取。
- 账号路由需通过启动 overlay 恢复 `credentials`、`authorization`、`deepseek-account` 三项（headless 默认关闭这些服务）。API-key 路由通过 DSH 原生加载规则读取 `.env`；采用界面保存的 API key 时显式恢复 `credentials` 服务。API-key 路由保持 `deepseek-account` 关闭。提供商、模型及相关服务配置和凭据来源必须一起解析。
- 启动器显式选择已确认的 home；Bot 会话、工作目录和 MCP 执行上下文分别隔离。凭据和原生配置的归属保持明确，验证产生的临时副本限制为当前用户可访问并在退出时清理；原配置保持原值。

## 已验证能力与实施边界

基线为桌面自带 DSH `0.2.1-rc.5`。完整 headless 子进程已验证以下行为：

- 使用所选桌面账号提供商成功完成模型回复，退出码为 0。
- 第二次启动通过 `--session-id` 续跑同一会话，正确召回第一轮合成标记。
- 通过 `--patch` 注入官方 stdio MCP 客户端，真实模型调用本地合成工具；事件包含 tool_call 和 status=completed 的 tool_result，最终回复符合约定。
- 原配置和凭据文件校验一致，临时凭据副本已删除，现场检查确认实验进程结束。

客户端级实验同时覆盖工具错误、初始化失败以及工具注销和子进程清理。生产引擎的注册、执行凭据、权限、消息已读和任务状态链属于后续实现与验收范围。

API-key 路由曾向其官方接口发起请求并收到 HTTP 401，账号路由的模型调用与续跑已通过。提供商与桌面选择存在差异是测试配置事实；401 的具体成因仍待核验。API-key 路由的成功调用和两种接入方式之间的切换均列入验收。

方法与现场证据保存在项目主目录 `reports/research/dsh-integration/`。技术依据为 [headless](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/README.md) 和 [MCP 客户端](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md)；上游文档更新时以实装版本验证结果约束解析契约。

## 模块改动范围

涉及 `engine/discovery.py`、`engine/api.py`、`engine/access.py`、`engine/bot_identity.py`、`engine/profile_create.py`、`engine/native_runtime.py`、`engine/wake.py`、`engine/session_adapter.py`、`engine/commands.py`、`engine/process_tree.py`、`engine/project_models.py`、相关模型/配置/HUD/runtime 白名单与前端 `project_settings.js`。实施前完成全部双宿主分支盘点。

## 宿主能力对齐

- **Profile 编辑**：现有编辑入口按宿主分叉（hermes 走 profile.yaml/SOUL.md/.env/config.yaml 四文件，codex 走独立配置）。dsh 新增独立配置读写分支，对齐编辑入口与模型、提供商、认证方式字段；dsh 的 profile 为 `DSH_HOME/profiles/<name>/` 下的 cordis patch 层，不复用 hermes 文件格式。
- **斜杠命令**：命令分发在引擎层（commands.py），按目标 bot runtime 分叉。/new、/stop、/help 对 dsh 有效（/new 重置会话绑定、/stop 走进程树清理、/help 通用）；/steer 不支持——运行中纠偏依赖 hermes gateway 的 session.steer，dsh headless 与 codex 子进程均无此通道。

## 验收标准

| 项目 | 验收 |
| --- | --- |
| 入口与版本 | 升级后定位、版本不兼容提示 |
| MCP | 完整 headless overlay 与引擎 MCP 身份；必需工具失败终止、超时与 /stop |
| 认证 | 账号登录、界面保存 API key、`.env` API key 分别调用成功；两种接入方式切换后实际路由一致；认证失败保持原选择；凭据归属、原配置完整性与日志脱敏 |
| 会话 | 两轮续跑、引擎重启恢复、/new 与迟到事件 |
| 业务闭环 | 合成项目 group_send、已读确认与 task 权限 |
| 隔离 | 双 Bot、双项目、work/chat 交叉与工作区资源 |
