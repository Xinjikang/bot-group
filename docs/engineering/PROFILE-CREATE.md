# 创建 profile 与选择项目成员

项目创建/导入第 5 步“选择成员与负责人”和 **项目详情 → 成员与目录 → 添加成员** 使用同一套勾选列表，顶部均提供“创建 profile”。创建窗口独立于项目详情窗口，内部选择 Hermes / Codex 并填写配置。

`frontend/member_picker.js` 共用候选展示、选择、刷新及创建回调。`GET /profiles/candidates` 合并宿主扫描结果与已注册 Bot，标记已入组成员；选择列表仅展示尚未加入当前项目的 Bot。同名 profile 通过宿主、配置根目录与 Bot ID 区分。选中尚未注册的宿主 profile 时只注册 Bot。新建成功返回原列表并自动勾选，其他选择与向导角色草稿保留。详情点击“加入项目”才入组；向导最终确认时统一入组和授权。批量加入部分成功后，成功成员从候选列表移除，失败及未处理成员仍可重试；没有可加入成员时保留创建和刷新入口。

## 使用

- 填写显示名称和 profile 标识；标识使用 1–64 位小写字母、数字、短横线、下划线。默认选择该宿主的 default 作为配置来源，否则选择第一个可用来源；也可选择“使用默认设置”。已加入当前项目的 profile 也可作为来源。
- “更多设置”可填写模型和个人指令。空模型沿用来源或宿主默认设置；空个人指令生成基础工作约定。
- Codex 自动复制引擎主账户目录（`CODEX_HOME`，默认 `~/.codex`）的 `auth.json`。配置来源用于继承模型与运行设置，创建界面省去认证选择。使用默认设置或选择其他 profile 作为配置来源时，文件认证仍来自主账户。
- 新建 Codex Bot 统一使用智能审批（“帮我审批”）及工作区写入沙箱，加入项目时获得项目工作目录授权；创建规则独立于配置来源。创建表单显示该规则。
- Hermes 保留“同时复用来源的模型认证”选项，勾选后复制 `.env` 中常用模型服务的 API Key/endpoint，以及模型配置中的直接凭据；OAuth 登录需在新 home 中完成。
- 创建后注册 Bot，待显式确认选择后加入项目。取消成员选择或整个项目向导时，已创建 profile 与 Bot 保留；窗口在创建前说明此行为。缺少可复制认证时显示宿主对应的登录/配置命令，并明确目标 home；Codex 指向主账户目录，并提示提供文件认证。系统钥匙串中的凭据保持原生管理方式。
- “认证已复制”仅说明本地配置准备完成。实际账号权限、模型可用性和认证有效期以运行结果为准；创建过程不发起付费模型调用。

## 当前项目的 SOUL 模板

“创建 profile”和“编辑成员 → SOUL.md / AGENTS.md”均提供“查看 / 引用 SOUL 模板”。

成员编辑页的模板按钮位于 SOUL.md / AGENTS.md 标题说明区右侧，提示文字排列在按钮下方，窄屏沿用左右布局并自动换行。

- 已登记项目使用 `/projects/{project}/template-reference`，按项目模板类型读取最新角色参考。创建项目向导使用当前选定的 `/projects/templates/{id}`，核对版本与向导快照一致。空白项目或未关联模板显示空态；读取失败可重试，项目或版本不匹配的响应会被拒绝。
- 创建入口使用当前项目详情的项目 ID；成员编辑入口使用打开该编辑窗口时的项目上下文。全局花名册有当前选中项目时使用该项目，未选中项目时禁用模板入口。
- 查看保持正文原样；“引用模板”填入个人指令或 SOUL 编辑框，展开创建表单的更多设置，允许继续修改。替换已有正文前确认，取消确认保留原文。
- 引用仅更改草稿，实际文件写入通过“创建 profile”或“保存”。Hermes 对应 SOUL.md，Codex 对应 AGENTS.md；既有 profile 的保存范围保持原有全局配置语义。
- 选择窗跟随所属编辑窗口，处理关闭、重新打开和跨项目响应竞态；预览以纯文本呈现。已有向导的复制、下载、预览功能保持现状。

项目详情的“模板参考”同样展示该类型的最新参考。读取过程保持项目角色、授权、创建记录和已保存的 profile 正文原样；用户引用并保存后才更新 profile。此接口修复需要手动重启引擎并刷新页面。

## 新建 Codex 成员的执行配置

创建接口生成 `approval_policy = "on-request"`、`approvals_reviewer = "auto_review"`、`sandbox_mode = "workspace-write"`。来源的审批策略、审批者、旧式沙箱权限及命名权限策略均由这组创建规则替代。来源的模型、供应商、推理及 shell 环境设置仍按白名单继承；来源的额外可写目录和网络放行规则保留在源配置中，新 home 的目录授权按当前项目生成。

Windows 新 home 保留来源中有效的 `windows.sandbox` 选择（`elevated`、`unelevated`、`mxc`）及 `features.prefer_mxc`。旧 `elevated_windows_sandbox = true` 转为显式 `elevated`；其余缺省或无效选择生成 `unelevated`，使用受限令牌以支持后台创建，无需在引擎内发起管理员初始化。旧式沙箱功能开关由显式实现选择替代。来源的显式实现及 MXC 仍受安装版本、设备能力和管理策略约束。

来源解析支持独立 home、命名文件、内联 profile 和配置中的默认 `profile`。来源文件保持原样。新建完成后的同请求重试复用既有结果；已有 Bot 入组、项目导入、普通唤醒和会话恢复继续使用成员自身的审批与沙箱配置。

新建和已有成员入组均调用项目目录授权逻辑：shared 成员获得项目根目录，worktree 成员获得分配的工作目录及主目录的 `reports/`。详见 [项目目录授权](../guides/CODEX-PROFILE-HOMES.md#project-directory-access-on-enrollment-2026-10-16)。

## 已有 Codex 成员的沙箱与智能审批提示

`GET /discovery/scan` 的 Codex 候选附带可空的 `sandbox_notice: {code, message}` 和 `approval_notice: {code, message}`。Windows 检查沙箱实现，所有平台检查智能审批。每个候选只读取一次合并配置，按成员实际 `CODEX_HOME` 解析本地 `config.toml`、命名配置文件或内联 profile 覆盖；独立 home 使用自身配置。读取过程保持配置原样，扫描结果中的提示仅供用户参考。

- 未显式选择 Windows 沙箱实现时返回 `unspecified`，文案说明实际启用情况取决于 Codex 版本和默认设置；缺省字段标记为待确认。
- 显式完全访问、无法读取/解析配置及未知实现值分别返回 `disabled`、`unreadable`、`unrecognized`。错误文案保留排查方向，原始配置内容与底层异常文本留在本机。
- 智能审批检查同时核对审批者和请求策略：`auto_review` 配合 `on-request`，或允许沙箱审批的 granular 策略时通过。其他配置返回 `not_enabled`；读取失败返回 `unreadable`。仅设置审批者、同时使用 `never` 时仍显示提醒。
- `windows.sandbox` 为 `elevated`、`unelevated`、`mxc`，或启用对应已识别功能开关时，该项配置检查通过。此结果只表示存在配置选择，运行时默认值、托管策略、项目覆盖和沙箱初始化状态仍以 Codex 实际执行为准。
- 选择已有 Codex profile 后，在现有选择区域下方分别显示两项提示，加入按钮保持可用。用户可以直接继续加入，也可以使用同页“创建新 profile”。
- 提醒沿用现有入组请求与目录授权流程，保留已有 Bot 的沙箱实现及审批策略。Hermes 沿用原流程。

## 创建接口与文件布局

`POST /profiles/create` 仅供管理员。请求包含 `host_type`、`profile`、`display_name`、可选 `source: {profile, config_root}`、`reuse_auth`、`model`、`instructions` 和 `request_id`。来源必须匹配当前宿主扫描结果。`project` 可省略或为 null/空字符串，此时只创建并注册 Bot，响应 `member_profile` 和 `project` 为 null。提供有效 `project` 的旧调用继续创建并入组；非空无效、归档或已移除项目返回错误，纯空白标识返回 400。响应补充 `host_type`、`config_root` 供列表选择复用。

`reuse_auth` 仅控制 Hermes。Codex 为兼容旧客户端接受此字段，其创建行为统一为继承主账户文件认证。运行期间的按需同步见 [Codex profile 认证](../guides/CODEX-PROFILE-HOMES.md#file-authentication-synchronization)。

- Codex 使用 `$CODEX_HOME/profiles/<name>/`，生成 `config.toml`、`AGENTS.md`、空 `memories/` 与 `skills/`。按上述创建规则生成执行配置，协作 MCP 继续由现有唤醒入口注入。
- Hermes 使用 `$HERMES_HOME/profiles/<name>/`，生成 `config.yaml`、`profile.yaml`、`SOUL.md`、`.env`、空记忆与技能目录。协作 MCP 在每次运行时由引擎提供成员身份和 API 地址，新成员配置文件只保存成员自身设置；消息平台配置、频道凭据、OAuth 缓存、历史、任务和源人格均保留在原 profile。
- 现有源配置保持原样。新 profile 统一开启智能审批，后续由用户维护。
- 同名目录和系统保留名拒绝创建。生成文件先写入随机暂存目录；发布时独占目标目录，文件就绪后写入请求指纹。数据库操作在事务内完成，普通失败回滚数据库和本次写入的文件。相同请求重试复用已创建的 profile，避免网络响应丢失造成重复成员。
- 弹窗提交期间锁定按钮并阻止 Escape 关闭；失败保留草稿。扫描响应绑定弹窗实例和请求版本，避免关闭、切换宿主后收到过期结果。
- Profile 标识输入时校验 1–64 位、小写英文字母/数字开头及允许字符，并校验系统保留名称。错误以字段下方红色文字说明，修正后清除，显示名称继续支持中文。创建失败的服务端信息以红色显示并保留草稿。

## 验证与生效

使用项目 Python 3.11 环境执行 `scripts.test_profile_create`、`scripts.test_discovery`，验证独立创建、旧调用兼容、目录授权和认证默认。`scripts/test_profile_create_browser.py --evidence <主目录报告路径>` 使用真实 Chrome 及隔离 HTTP/SQLite/Git，覆盖两处共用列表、已有/未注册候选、创建失败与丢响应重试、取消保留、选择保留、SOUL 引用、最终提交和 390px 深浅主题。配套运行 `test_profile_soul_templates_browser.py`、`test_member_name_browser.py`。

部署引擎更新后，管理员手动重启引擎并刷新页面。

专项浏览器回归同时覆盖“选择已有 profile”加入流程；执行结果及遗留问题保存到主目录测试报告。
