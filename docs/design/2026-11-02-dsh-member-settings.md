# DSH 成员设置与自动审批

## 成员编辑

DSH 成员编辑提供“描述、人格、模型与供应商”三个页签。描述继续写入 profile 的 `.group-engine-description`；人格继续写入 `cordis.patch.yml` 的 `system-prompt.config.personaPrefix`。模型、供应商和推理强度沿用项目覆盖与全局 profile 保存规则。

共享 HOME 的 `AGENTS.md` 属于原生全局指令，人格属于单个 profile。成员配置接口移除 `instructions`、`env`、`auto_review` 的读取、写入、修订计算和插件状态返回；旧请求显式携带这些字段时拒绝保存。Hermes 的 `.env` 编辑继续使用共用组件。原生 DSH HOME 中已有文件继续由宿主使用。

## 自动审批

成员页及设置菜单均无 DSH Auto 开关。每轮 DSH 启动前，通过绑定入口及 DSH HOME 检测 `@deepseek-ai/dsh-experimental-auto-review` 包：检查模块目录的 `package.json`，以及桌面安装 `resources/app.asar` 的 DSH 模块索引。

检测到插件时，执行 overlay 强制启用 `auto-review` 并插入会话适配插件，覆盖 profile 中的禁用项。旧 `.group-engine-auto-review` 文件退出控制逻辑。未检测到插件时省去 Auto overlay，沿用原生审批。安装索引损坏或检测发生读取错误时，中止本轮并提示检查安装。

插件包检测只确认安装存在性。运行时继续校验 `preset=auto` 证据；加载失败、会话预设失效等情况沿用阻断与审计机制。Auto 审查和工具主体执行分别计数。

## 验证

- `scripts.test_dsh_member_settings`：真实成员配置 HTTP 读写、删除字段拒绝、原生文件保留、旧文件变更与成员修订解耦、包存在/缺失、旧开关无效及安装索引损坏。
- `scripts.test_dsh_profile_config`、`scripts.test_dsh_auto`：配置保留、备份、修订冲突、执行 overlay、审批证据及审计。
- `scripts/test_dsh_member_config_browser.py`：三个页签、载荷字段、项目作用域、保存失败、插件状态与深浅主题窄屏。
- `scripts/verify_dsh_auto_config.py --entry <DSH入口> --evidence <报告目录>`：临时 HOME 中调用原生 `--dump-config`，检查禁用项被强制启用及适配插件存在，模型请求数为零。
