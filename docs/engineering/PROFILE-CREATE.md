# 在项目中创建 profile

入口为 **项目详情 → 成员与目录 → 添加成员**。选择 Hermes / Codex 后，可切换“选择已有 profile”和“创建新 profile”。项目创建、导入向导维持现有流程。

## 使用

- 填写显示名称和 profile 标识；标识使用 1–64 位小写字母、数字、短横线、下划线。默认选择该宿主的 default 作为配置来源，否则选择第一个可用来源；也可选择“使用默认设置”。已加入当前项目的 profile 也可作为来源。
- “更多设置”可填写模型和个人指令。空模型沿用来源或宿主默认设置；空个人指令生成基础工作约定。
- Codex 自动复制引擎主账户目录（`CODEX_HOME`，默认 `~/.codex`）的 `auth.json`。配置来源用于继承模型与运行设置，创建界面省去认证选择。使用默认设置或选择其他 profile 作为配置来源时，文件认证仍来自主账户。
- Hermes 保留“同时复用来源的模型认证”选项，勾选后复制 `.env` 中常用模型服务的 API Key/endpoint，以及模型配置中的直接凭据；OAuth 登录需在新 home 中完成。
- 创建后自动注册 Bot 并加入当前项目。角色、项目权限、工作目录继续通过现有成员设置配置。缺少可复制认证时显示宿主对应的登录/配置命令，并明确目标 home；Codex 指向主账户目录，并提示提供文件认证。系统钥匙串中的凭据保持原生管理方式。
- “认证已复制”仅说明本地配置准备完成。实际账号权限、模型可用性和认证有效期以运行结果为准；创建过程不发起付费模型调用。

## 当前项目的 SOUL 模板

“创建新 profile”和“编辑成员 → SOUL.md / AGENTS.md”均提供“查看 / 引用 SOUL 模板”。创建项目向导中的原参考入口保持现状。

成员编辑页的模板按钮位于 SOUL.md / AGENTS.md 标题说明区右侧，提示文字排列在按钮下方，窄屏沿用左右布局并自动换行。

- 模板选择窗只调用当前项目的 `/projects/{project}/template-reference`。服务端从项目创建/导入记录或早期初始化记录中确定模板类型，按类型读取当前最新的角色名称、职责及 SOUL 全文；创建时间和当时的模板版本均不限制参考内容。空白项目或未关联模板的项目显示空态；读取失败可重试，项目不匹配的响应会被拒绝。
- 创建入口使用当前项目详情的项目 ID；成员编辑入口使用打开该编辑窗口时的项目上下文。全局花名册有当前选中项目时使用该项目，未选中项目时禁用模板入口。
- 查看保持正文原样；“引用模板”填入个人指令或 SOUL 编辑框，展开创建表单的更多设置，允许继续修改。替换已有正文前确认，取消确认保留原文。
- 引用仅更改草稿，实际文件写入仍通过原有“创建并加入项目”或“保存”。Hermes 对应 SOUL.md，Codex 对应 AGENTS.md；既有 profile 的保存范围保持原有全局配置语义。
- 选择窗跟随所属编辑窗口，处理关闭、重新打开和跨项目响应竞态；预览以纯文本呈现。已有向导的复制、下载、预览功能保持现状。

项目详情的“模板参考”同样展示该类型的最新参考。读取过程保持项目角色、授权、创建记录和已保存的 profile 正文原样；用户引用并保存后才更新 profile。此接口修复需要手动重启引擎并刷新页面。

## 创建接口与文件布局

`POST /profiles/create` 仅供管理员。请求包含 `project`、`host_type`、`profile`、`display_name`、可选 `source: {profile, config_root}`、`reuse_auth`、`model`、`instructions` 和 `request_id`。来源必须匹配当前宿主扫描结果。

`reuse_auth` 仅控制 Hermes。Codex 为兼容旧客户端接受此字段，其创建行为统一为继承主账户文件认证。运行期间的按需同步见 [Codex profile 认证](../guides/CODEX-PROFILE-HOMES.md#file-authentication-synchronization)。

- Codex 使用 `$CODEX_HOME/profiles/<name>/`，生成 `config.toml`、`AGENTS.md`、空 `memories/` 与 `skills/`。按白名单继承模型、provider、推理、审批、沙箱及权限设置；支持来源为独立 home、原生命名配置及旧式命名配置。协作 MCP 继续由现有唤醒入口注入。
- Hermes 使用 `$HERMES_HOME/profiles/<name>/`，生成 `config.yaml`、`profile.yaml`、`SOUL.md`、`.env`、空记忆与技能目录。协作 MCP 在每次运行时由引擎提供成员身份和 API 地址，新成员配置文件只保存成员自身设置；消息平台配置、频道凭据、OAuth 缓存、历史、任务和源人格均保留在原 profile。
- 现有源配置保持原样。新 profile 的审批设置在创建时确定，后续由用户维护。
- 同名目录和系统保留名拒绝创建。生成文件先写入随机暂存目录；发布时独占目标目录，文件就绪后写入请求指纹。数据库操作在事务内完成，普通失败回滚数据库和本次写入的文件。相同请求重试复用已创建的 profile，避免网络响应丢失造成重复成员。
- 弹窗提交期间锁定按钮并阻止 Escape 关闭；失败保留草稿。扫描响应绑定弹窗实例和请求版本，避免关闭、切换宿主后收到过期结果。

## 验证与生效

使用项目 Python 3.11 环境执行 `scripts.test_codex_auth`、`scripts.test_profile_create`、`scripts.test_codex_profile_config`、`scripts.test_discovery`、`scripts.test_codex_driver_regressions`。浏览器脚本 `scripts/test_profile_create_browser.py` 使用真实 Chrome 和合成接口，覆盖新建/已有、配置来源、Codex 默认认证、Hermes 认证选择、重试、重复提交、关闭重开、登录提示及 390px 深浅主题截图。

部署引擎更新后，管理员手动重启引擎并刷新页面。

专项浏览器回归同时覆盖“选择已有 profile”加入流程；执行结果及遗留问题保存到主目录测试报告。
