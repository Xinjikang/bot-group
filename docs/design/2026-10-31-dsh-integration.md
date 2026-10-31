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

- 认证来源沿用 DSH 原生配置：API-key 路由支持 `DSH_HOME/.env` 中的 `DEEPSEEK_API_KEY` 和界面保存的原生凭据；账号路由使用 DSH 保存的账号授权。两类路由分别核验。引擎设置中保存宿主定位及配置引用，凭据继续由 DSH 管理。
- 继承所选原生 profile 的 provider、model 及相关服务配置，由 DSH 原生适配器处理目标地址与认证。`deepseek-account / deepseek-flash` 为本次通过的验证组合，具体值从配置读取。
- 使用已保存认证时，通过启动 overlay 恢复 `credentials`、`authorization`、`deepseek-account` 三项。headless 默认关闭这些服务，仅提供凭据而遗漏提供商选择会改变实际路由。
- 启动器显式选择已确认的 home；Bot 会话、工作目录和 MCP 执行上下文分别隔离。凭据和原生配置的归属保持明确，验证产生的临时副本限制为当前用户可访问并在退出时清理；原配置保持原值。

## 已验证能力与实施边界

基线为桌面自带 DSH `0.2.1-rc.5`。完整 headless 子进程已验证以下行为：

- 使用所选桌面账号提供商成功完成模型回复，退出码为 0。
- 第二次启动通过 `--session-id` 续跑同一会话，正确召回第一轮合成标记。
- 通过 `--patch` 注入官方 stdio MCP 客户端，真实模型调用本地合成工具；事件包含 tool_call 和 status=completed 的 tool_result，最终回复符合约定。
- 原配置和凭据文件校验一致，临时凭据副本已删除，现场检查确认实验进程结束。

客户端级实验同时覆盖工具错误、初始化失败以及工具注销和子进程清理。生产引擎的注册、执行凭据、权限、消息已读和任务状态链属于后续实现与验收范围。

方法与现场证据保存在项目主目录 `reports/research/dsh-integration/`。技术依据为 [headless](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/headless/README.md) 和 [MCP 客户端](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md)；上游文档更新时以实装版本验证结果约束解析契约。

## 模块改动范围

涉及 `engine/discovery.py`、`engine/api.py`、`engine/access.py`、`engine/bot_identity.py`、`engine/profile_create.py`、`engine/native_runtime.py`、`engine/wake.py`、`engine/session_adapter.py`、`engine/commands.py`、`engine/process_tree.py`、`engine/project_models.py`、相关模型/配置/HUD/runtime 白名单与前端 `project_settings.js`。实施前完成全部双宿主分支盘点。

## 验收标准

| 项目 | 验收 |
| --- | --- |
| 入口与版本 | 升级后定位、版本不兼容提示 |
| MCP | 完整 headless overlay 与引擎 MCP 身份；必需工具失败终止、超时与 /stop |
| 认证 | 实际迁移目录、凭据归属、登录有效性与模型路由 |
| 会话 | 两轮续跑、引擎重启恢复、/new 与迟到事件 |
| 业务闭环 | 合成项目 group_send、已读确认与 task 权限 |
| 隔离 | 双 Bot、双项目、work/chat 交叉与工作区资源 |
