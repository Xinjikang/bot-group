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

以下为接入目标和验收契约。当前 DSH 生产适配尚待实现，已完成的实验范围见上一节。

### 项目详情 Profile 编辑

复用项目详情的成员编辑入口、交互与权限。现有字段见 `frontend/index.html` 的 `profileFields`、`openProfileEditor` 和保存处理；配置落盘及并发保护见 `engine/profile_config.py`、`engine/codex_profile_config.py`；项目覆盖见 `engine/project_models.py`。DSH 的原生 profile 使用 `DSH_HOME/profiles/<name>/` 下的 cordis patch，需增加对应读写适配。

| 能力 | DSH 接入契约 |
| --- | --- |
| 配置定位 | 展示所编辑成员、宿主 home、原生 profile 及实际配置来源，校验读取和保存的目标一致。保留成员路由标识与宿主 profile 的既有含义。 |
| 描述、人格与指令 | 对齐现有描述和人格编辑用途；DSH 的描述存储、人格文件引用和加载优先级须核验后确定。验收包含保存回读与下一次执行实际采用指令。 |
| 环境变量与凭据 | `.env` 编辑指向所选 DSH home；界面保存凭据由 DSH 原生服务管理。认证来源与账号/API-key 选择保持一致，保存普通配置时保留既有凭据。 |
| 模型与提供商 | 显示实际生效值与来源；全局修改写入所选原生 profile，项目覆盖由本次执行 overlay 应用。有效 provider 决定本次认证路由，目标地址由原生配置解析。 |
| 推理强度 | 根据所选 provider/model 的实际能力显示可用值；DSH 字段与档位映射待核验，原有未知值保留并提示。 |
| 保存作用域 | 对齐“仅当前项目／应用到全局”：项目覆盖仅作用于当前项目和成员，清空覆盖后继承原生 profile；全局保存保留其他项目的独立覆盖。描述、人格和 `.env` 的作用域明确显示。 |
| 保存可靠性 | 对齐 revision 冲突检测、仅写变更字段、备份和失败恢复；保留未知插件配置及引用。动态表达式或歧义节点进入明确的只读/报错流程，阻止覆盖损坏。 |
| 生效时机 | 保存回执注明下一次启动生效；运行中执行保持本次快照。续跑会话采用新模型/认证的行为须实测，确需新会话时明确提示使用 `/new`。 |

全局配置和项目覆盖分两步保存时，沿用现有部分成功提示，明确已保存范围及失败范围。凭据错误和配置冲突消息应脱敏。

### 斜杠命令

当前引擎注册表为 `engine/commands.py` 的 `/new`、`/stop`、`/steer`、`/help`。群聊命令由引擎分发，DSH 交互终端中的其他命令单独核验能力；本期仅承诺下表。

| 命令 | DSH 首期行为与验收 |
| --- | --- |
| `/new @成员` | 新建会话绑定，保留旧会话记录；话题内作用于该话题 chat，话题外作用于本项目 work。忙碌时按现有策略拒绝；检查 generation、迟到事件及下一次唤醒新 ID。 |
| `/stop @成员` | 限定当前项目和当前 work/chat 执行，终止执行及 MCP 子进程树；按现有停止流程处理队列、任务状态和凭据清理，以实际退出证据反馈停止结果。 |
| `/help` | 命令菜单、说明和后端拒绝行为一致；明确标注运行中纠偏的宿主能力边界。 |
| `/steer @成员 内容` | 首期 DSH headless 返回明确的能力不支持提示，保留命令审计；在进入 Hermes 执行查询及投递前完成能力判断。现有实现的运行中纠偏通道为 Hermes gateway `session.steer`。 |

命令验收还包括缺失目标、多目标、未知命令、空闲与忙碌状态、跨项目和跨话题隔离。输入框补全和帮助内容应与后端能力判断保持一致。

### 成员与宿主生命周期盘点

对照 codex 子进程接入（`discovery`/`codex_profile_config`/`codex_auth`/`codex_reasoning`/`codex_sandbox_notice`/`stop`），dsh 需要覆盖的能力如下：

| 能力 | codex 参照实现 | DSH 接入契约 |
| --- | --- | --- |
| 宿主发现 | `discovery._scan_codex`（config.toml/auth.json/命名 config/独立 home） | `_scan_dsh` 扫 `DSH_HOME/profiles/<name>/`；`register_bot` 的 host_type 白名单加 `dsh` |
| 候选可用性 | `candidate_available` 按 host_type 分叉 | dsh 分支校验 profile 目录与有效 patch 存在 |
| Profile 读写 | `codex_profile_config`（TOML + revision 冲突 + 备份恢复） | `dsh_profile_config`（cordis patch yaml + 同款 revision/备份）；描述、人格、推理字段映射待实装 |
| 认证同步 | `codex_auth.sync`（auth.json 复制 + JWT 过期 + 账户一致） | dsh 无 auth.json 同步；账号路由走 credentials overlay，API-key 走 `.env`/界面凭据 |
| 认证失败识别 | `codex_auth.is_auth_error`（401/token_revoked） | dsh 识别 MISSING_CREDENTIAL/AUTH 事件，保持所选路由不切换 |
| 推理/思考读取 | `codex_reasoning`（--json 事件流） | dsh --json 事件流含 thinking/tool_call/tool_result，实现 `dsh_reasoning` |
| 沙箱提示 | `codex_sandbox_notice` | dsh headless 沙箱语义待验证 |
| 会话绑定 | `session_bindings` runtime='codex' 存 thread_id | runtime='dsh' 存 --session-id，generation 守卫一致 |
| 停止清理 | `stop_profile` 进程树 | 复用进程树 + MCP/overlay/临时数据清理 |
| 工作区授权 | codex cwd = 项目目录 | dsh cwd = 项目目录，复用工作区授权与目录报告 |
| 重试恢复 | task_retry 按 runtime | 续跑 --session-id 已验证，重试恢复复用 |
| 项目指令加载 | hermes 走 `--in` 原生加载 AGENTS.md；codex 走 `-c developer_instructions` 注入 | dsh 的系统提示词/AGENTS.md 等价注入机制待验证 |
| 成员启停/卸载 | active 0/1 与 home 清理通用 | 复用，不依赖 runtime 分叉；dsh home 清理按配置归属处理 |

### 关键映射核验结果

基于 dsh 0.2.1-rc.5 实装源码（`--dump-default-config` 完整 profile 树）核查：

| 核验项 | 实装证据 | 结论 |
| --- | --- | --- |
| Profile 继承 | `cordis.yml`（空 root）+ `cordis.patch.yml`（用户 patch 层）+ `package.json` 的 `dsh.profile.bundles` | 四层叠加 bundle→profile(cordis.patch.yml)→home→CLI(--patch)，config 整体替换不深合并；编辑点=用户层 `cordis.patch.yml` |
| 人格加载 | 内置 `dsh-persona` 的 prefix/suffix/complete/includeRuntimeContext；agent preset 的 plugins 声明 | 人格编辑用途对应 SOUL.md；内容由所选 preset 的 persona 插件配置持久化。插件在 agent 作用域覆盖部署人格，部署全局人格由 system-prompt 管理。编辑需定位具体 preset 声明及其 patch，保存回读与模型实际加载仍需验证 |
| 指令加载 | `dsh-agent-instructions`（maxBytes 65536）+ `dsh-skill`/`dsh-skill-filesystem`/`dsh-tool-skill` | 指令与 skill 入口存在；AGENTS.md 等价文件的加载路径与顺序待实测确认 |
| 推理参数 | `dsh-agent-default-model` 支持 `reasoningEffort`（desktop `cordis.patch.yml` 用 `max`）；headless 包内默认未显式设置 | reasoningEffort 字段真实生效，档位与默认值待确认，编辑映射按实装字段名对齐 |
| 沙箱/文件访问 | `dsh-sandbox-policy`：`mode = DSH_PERMISSION_MODE ?? 'workspace-write'`，`workspaceRoot = process.cwd()`；三级 read-only/workspace-write/danger-full-access | 工作区文件访问由 `DSH_PERMISSION_MODE` 环境变量 + cwd（workspaceRoot）控制，非固定 workspace-write |
| 智能审批 | 实装 `dsh-experimental-auto-review` 的工具执行前审查与 `permissionPresets.registerAuto`；`dsh-user-approval` 提供 ask/never 请求策略 | 接入要求启用 Auto review 插件并为执行会话选择 auto。ask 将请求交给人工或机器处理方；缺少处理方返回 unavailable。never 自动拒绝进入审批服务的请求。headless 的会话启用与拒绝后的处理链待隔离验证 |
| 模型/认证切换续跑 | 待实测 | 切换 provider/model/认证后 `--session-id` 续跑的行为需隔离实验确认 |

宿主候选发现需区分四个状态：目录存在、配置可解析、入口可执行、认证就绪，各保留独立诊断。

### 智能审批接入要求

智能审批为按宿主插件能力启用的可选能力。已安装并选择启用时，接入 Auto review；插件缺失时显示未安装状态，继续按 DSH 当前原生权限与审批策略运行。引擎保留宿主原生拒绝、不可用和超时结果。实装 `0.2.1-rc.5` 包 `@deepseek-ai/dsh-experimental-auto-review` 默认关闭；其 patch 插入 `auto-review` 服务，服务在 `tools/pre-execute` 检查当前会话的 `auto` 预设。仅加载插件仍需完成会话选择；`auto` 为专用会话模式，原生预设服务禁止将其配置为普通默认预设。

原生 Auto 使用当前 agent 的 provider/model 逐次审查工具调用，允许后按 Full access 执行；预设组合为 `sandbox=danger-full-access`、`approval=ask`。模型拒绝后进入审批请求链，处理方缺失或异常时动作失败关闭。该模式的文件访问边界与 workspace-write 预设分别记录。每次支持的工具调用增加一次审查模型请求。

选择启用 Auto 的执行必须在首个工具执行前确认插件与会话 Auto 状态，续跑恢复时再次核验。审查失败、拒绝或取消均保留明确结果和审计关联。原先依赖 Auto 的运行或持久化会话遇到插件缺失时，明确报告该会话能力失效；普通会话按插件缺失的可选能力规则处理。实装插件卸载会将存量 Auto 会话迁移至 Full access；引擎需阻止已启用审批的执行因能力失效而继续执行，验证重载和退出时序。

方案阶段完成 headless 的插件加载、会话 Auto 选择入口及拒绝处理路径验证。验收覆盖允许、拒绝、审查异常、无人响应、取消、续跑及插件失效，检查未获准动作的工具主体保持未执行。人工接管的通道、超时和取消契约在设计中明确后再实施。现场源码证据见项目主目录 `reports/research/dsh-integration/approval-review.md`。

### headless 隔离验证结果

现有实验以 `--patch` 插入 `auto-review` 后运行 headless，合成 MCP 工具执行成功，JSON 投影未观测到审查痕迹。脚本未调用会话 Auto 选择入口，且未记录生效 permission preset、审查请求计数或审查监听器状态；该结果仅覆盖插件插入配置下的工具调用。JSON 投影的事件词汇有限，判断审查是否发生须补充服务状态和审查调用证据。验证脚本与结果见 `reports/research/dsh-integration/probe-auto-review.py`。

实装 `dsh-permission-presets/lib/index.js` 提供 `set(session, name)`（344—348 行）；`pinInitialPermission` 对已保存 Auto 会话检查活动集成（370—375 行），活动集成缺失时拒绝恢复。插件生效时的会话切换与恢复入口已在源码确认。headless 可通过受控适配插件调用该服务的可行性、首个工具前的执行时序和持久化续跑仍待隔离验证；现有证据尚未排除该接入路径。

### headless agent 生命周期与 preset 边界

实测 `--patch` 注入 `dsh-agent-preset-registry` + 含 `dsh-persona` 的 preset 后运行 headless，模型回复未命中人格标记（`persona_hit=false`）。实装 `dsh-headless/lib/index.js` 的 one-shot runner 直接创建 agent；`currentPreset`（209—218 行）读取 header.agentPreset 和 `agent-preset/selected`，`assertAdoptable`（221—226 行）拒绝该 agent preset 存在的会话。人格配置需核验适用于 headless 的加载路径，例如将用户选定的 persona 正文适配到 system-prompt 的 personaPrefix，同时保留原生配置来源。验证脚本见 `reports/research/dsh-integration/probe-persona.py`。

Auto 的 permission preset 使用独立的 `permission/preset` 事件；上述 `assertAdoptable` 判定读取 agent preset。当前源码未证明 Auto 的持久化选择与该判定冲突。Auto 验证继续使用 `permissionPresets.set(session,'auto')` 原生写入路径，在首个工具前启用并验证恢复；保持权限状态的原生审计和持久化契约。

### 内置插件与安装状态

本机 desktop profile 的 `package.json` 中 `dsh.profile.bundles` 已包含 `@deepseek-ai/dsh-experimental-auto-review`。这证明该插件已加入桌面 profile 组合；headless 使用独立组合，须单独检查加载状态与会话模式。

内置 `@deepseek-ai/dsh-persona` 必须放入 agent preset 的 `plugins` 中。`prefix` 为必填人格正文，`suffix` 默认为空，`complete=false`、`includeRuntimeContext=true`。编辑器通过对应 preset 声明的 patch 持久化内容，保留其他插件；全局挂载 persona 会与 system-prompt 的注册冲突。`complete=true` 会将系统提示词收缩为 prefix，普通人格编辑应保留既有组合语义。宿主 profile（插件部署组合）与 agent preset（会话人格/工具组合）分别定位。

后续能力核验逐项记录包内插件、profile 声明、实际加载和会话选择四个层面的证据。插件已安装与具体会话能力生效分别验收。

### 插件缺失与配置提示

项目详情按实际加载能力展示配置。字段所依赖的插件缺失、禁用或加载失败时，在对应字段显示原因、插件名称和原生配置位置，并禁用该字段保存；其他可用字段继续编辑，原配置内容保留。插件存在但当前 preset 未启用时分别显示，避免把安装状态当成生效状态。

Auto review 缺失属于可选能力缺失，显示状态即可。执行沿用 DSH 原生策略；审批不可用、拒绝、初始化错误直接报告，达到执行时限后按既有超时流程结束并清理子进程。明确错误可立即呈现，无需等到超时。插件安装与权限放宽由用户在宿主中管理。选择启用 Auto 的会话遵循上一节的失效处理。

### HOME 未配置与目录发现

创建入口固定显示已支持的 Hermes、Codex、DSH 宿主。选择宿主后自动扫描可执行入口和配置目录；扫描为空或失败时保留入口，显示“未发现环境配置”及具体原因，提供“选择 HOME 目录”、路径手动填写和重新扫描。扫描器异常与目录缺失分别呈现，用户可通过手动指定目录继续校验。创建接口在写入前复核目标，防止扫描后目录状态变化。

现有 `frontend/project_settings.js::renderMemberAdd` 固定列出 Hermes/Codex，`loadMemberCreateSources` 另行扫描配置来源；接入沿用固定入口并增加 DSH。手动路径按引擎所在主机解析，目录选择采用引擎侧支持的选择机制，并保留文本输入。校验目录存在、可访问、宿主配置可解析、profile 可用和执行入口可用后允许继续创建；错误指明失败项并保留已填路径。

自动扫描找到的默认目录或其他位置、用户手动选择的目录，均作为实际配置目录保存到 `config_root` 和宿主 profile/执行入口绑定。HOME 环境变量为空时直接使用已确认目录，启动时显式传给子进程。已创建 Bot 的目录后续被移动、删除或失去权限时，在原 Bot 上显示绑定资源失效，保留成员和历史。

`DSH_HOME`、`HERMES_HOME`、`CODEX_HOME` 均为可选的定位来源。已注册成员使用已保存的 `config_root` 与 profile 绑定；自动发现沿用各宿主的解析规则。新增 DSH 扫描器按进程 `DSH_HOME`、Windows 当前运行账户的用户级 `DSH_HOME`（`HKCU\Environment`）、原生默认目录定位；显式选择目录后以用户选定并校验通过的路径为准。进程环境优先于用户级环境。发现、编辑与启动须使用同一解析结果，并显示实际目录及来源。

兼容现有扫描的约束：Hermes/Codex 保留既有扫描器、进程 HOME 到默认目录的解析顺序、命名 profile 识别和候选去重/绑定规则。用户级注册表发现先用于新增 DSH 分支；给既有宿主增加该来源会改变进程环境为空时的选择结果，属于独立行为扩展。手动选 HOME 在自动扫描失败或为空时提供，自动扫描成功后继续使用既有结果。`scan_candidates` 增加 `_scan_dsh` 的结果，前端固定选择器增加 DSH 项，保留 Hermes/Codex 既有交互。

实现前记录既有 Hermes/Codex 扫描基线，使用隔离夹具回归进程 HOME 有值/空值、原生默认目录、命名 profile、独立 home、已注册候选去重和成员绑定，以及扫描成功时的创建流程。相同输入下既有宿主的候选结果和默认选择应与基线一致；DSH 新增与手动目录恢复单独验证。测试数据、宿主 home、数据库和签名密钥均显式使用隔离资源。

| 宿主 | HOME 缺省时的发现位置 |
| --- | --- |
| DSH | 当前运行账户的 `~/.dsh`；自定义迁移目录通过显式配置绑定 |
| Hermes | Windows 为 `%LOCALAPPDATA%/hermes`，LOCALAPPDATA 缺省时使用用户目录下 `AppData/Local/hermes`；其他系统为 `~/.hermes` |
| Codex | 当前运行账户的 `~/.codex` |

引擎现有 `discovery._codex_root`、`db._hermes_root` 和 `profile_config._root` 已包含缺省回退；Hermes 命名 profile 的根目录归一处理存在不同路径，接入盘点需验证发现、编辑、启动一致性。DSH 分支按实装宿主目录规则实现。

默认目录不存在时显示“未发现配置”，提供显式目录选择/填写和重新扫描入口。显式指定或已绑定目录不存在、权限不足或配置损坏时保留该路径并报告具体问题，避免自动绑定到另一套账号或 profile。发现阶段只读，首次初始化由明确的新建操作完成。可执行入口与数据目录分别检查，桌面安装位置独立于 HOME。

迁移过的 home 以已保存绑定或用户确认的实际目录为准；原生默认目录仅为候选，须检查可解析的宿主配置和 profile。旧目录为空、残缺或访问失败时报告候选状态，阻止将其当作迁移目标。界面允许显式指定迁移目录并持久化绑定；经过验证的宿主原生定位信息可辅助发现，并显示来源。进程环境快照、Windows 用户/系统环境值和宿主内部设置分别记录，受限探测返回空值或 EPERM 时保留“当前环境不可见/不可访问”的诊断，目录有效性须用实际运行身份核验。

启动时向子进程显式传入已解析的宿主 home（主路径，非兜底）——引擎进程的环境快照可能尚未继承 Windows 用户级新设的 HOME，不能依赖引擎进程环境或原生默认目录；保留系统环境设置。服务账户与交互登录账户的默认目录可能不同，界面展示引擎运行账户下实际解析的结果。环境变量修改后的旧进程快照问题通过显式目录配置解决；跨项目同一 Bot 使用既有宿主绑定和独立执行上下文。

### 待完成的设计核验

以下仍缺实装证据，需在实现阶段以真实 dsh 验证后收敛：`dsh-agent-instructions` 的指令内容加载来源（AGENTS.md 等价物）、skill 目录约定、persona 持久化位置、模型/认证切换后的续跑语义。生产引擎适配完成后验证 UI 保存闭环、真实命令执行、工作区授权、状态展示、重试恢复及卸载清理。当前阶段为能力范围已盘点、关键映射已据实装源码核验、剩余行为待实测。

## 验收标准

| 项目 | 验收 |
| --- | --- |
| 入口与版本 | 升级后定位、版本不兼容提示 |
| 既有扫描兼容 | Hermes/Codex 相同输入的候选、默认选择、去重/绑定和成功创建流程与基线一致；DSH 分支及手动目录恢复独立验收 |
| HOME 与发现 | 宿主入口固定显示；扫描成功/为空/异常、手动选目录/填写路径、目录校验错误与重试、创建前复核并保存绑定；三种 HOME 未设置/空值、原生默认目录、自定义目录、无目录/无权限/损坏配置、已注册绑定、创建后目录失效、命名 profile 与发现/编辑/启动一致性 |
| 插件缺失 | 缺失/禁用/加载失败/已安装未启用分别提示；相关字段禁止保存，其他字段可用；Auto 缺失沿用原生策略，原生错误及超时正常报告 |
| Profile 编辑 | 字段映射、人格实际加载、项目/全局范围、清空覆盖、并发冲突、未知字段保留、备份恢复、部分保存提示及生效时机 |
| 命令与交互 | `/new`、`/stop`、`/help` 的实际闭环；`/steer` 明确能力提示；菜单/帮助一致、参数错误与作用域隔离 |
| MCP | 完整 headless overlay 与引擎 MCP 身份；必需工具失败终止、超时与 /stop |
| 智能审批 | 选择启用时 Auto 插件和会话预设均生效；允许/拒绝/异常/取消、无人响应、续跑及插件失效闭环；拒绝动作保持未执行，审计关联当前执行；插件缺失时适用可选能力规则 |
| 认证 | 账号登录、界面保存 API key、`.env` API key 分别调用成功；两种接入方式切换后实际路由一致；认证失败保持原选择；凭据归属、原配置完整性与日志脱敏 |
| 会话 | 两轮续跑、引擎重启恢复、/new 与迟到事件 |
| 业务闭环 | 合成项目 group_send、已读确认与 task 权限 |
| 隔离 | 双 Bot、双项目、work/chat 交叉与工作区资源 |
