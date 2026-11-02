# 接入 DeepSeek Harness

新增 DeepSeek Harness（dsh）作为第三个 bot 运行时，与 Hermes、Codex 并列，完善成员配置、协作执行与 HOME 目录选择。

## 现状与问题

引擎按 `bots.runtime` 解析运行时并选择执行路径：hermes 走 gateway，codex 走 `codex exec --json`。主人已安装 dsh 桌面版（0.2.1-rc.5，自带 headless CLI），希望像 hermes/codex 一样接入群聊协作。

dsh 官方 headless 一次性执行支持 `--json` 事件流、`--session-id` 续跑、stdin 传任务、`--patch` 注入 MCP 与插件，与 codex exec 对应。首期采用 headless 子进程接入，复用通用队列、心跳、退出、重试、会话绑定与执行凭据。

## 范围

包含：

- 新增 dsh runtime 类型；宿主发现、HOME 目录选择与配置绑定；前端固定入口加 dsh 项。
- dsh profile 配置读写（描述、人格、指令、`.env`、模型、提供商、推理强度），persona 挂载。
- 认证接入：账号登录与 API key 两套方式，认证失败识别与路由保持。
- headless 子进程执行、`--json` 事件解析、会话绑定与结果判读。
- 斜杠命令 `/new`、`/stop`、`/help` 与 `/steer` 能力提示；停止清理、MCP 注入、项目指令加载、工作区授权。
- 智能审批（Auto review）接入：auto-review 插件 + `session/created` 事件切 auto，允许/拒绝/异常/续跑闭环。

不包含：

- 持续连接、执行中输入与交互审批（另列能力边界，不在首期）。
- Hermes/Codex 现有扫描、解析顺序、profile 识别、去重与绑定的行为改动（用户级注册表发现仅用于 dsh 分支）。

## 方案

见设计文档 `docs/design/2026-10-31-dsh-integration.md`（commit 834b815）。关键实测证据：

- persona 正确挂载点是 `system-prompt.personaPrefix`（实测 `persona_hit=true`）；agent preset 挂载在 headless 不生效（one-shot runner 直接建 agent，不消费 preset registry）。
- Auto 正确启用路径是 `session/created` 事件调用 `permissionPresets.set(session,'auto')`（实测 `preset=auto` 且 `read`/`glob`/`pwsh` 工具正常执行）；permission preset 与 agent preset 是两种独立状态。
- headless 续跑 `--session-id`、MCP `--patch` 注入、账号路由模型调用均已验证；KEY 路由成功调用与切换列入验收（此前 401 成因为 provider 选择差异，具体根因待核验）。

## 验收标准

1. 入口与版本：升级后定位、版本不兼容提示。
2. 既有扫描兼容：Hermes/Codex 相同输入的候选、默认选择、去重/绑定和成功创建流程与基线一致；DSH 分支及手动目录恢复独立验收。
3. HOME 与发现：宿主入口固定显示；扫描成功/为空/异常、手动选目录/填写路径、目录校验错误与重试、创建前复核并保存绑定；三种 HOME 未设置/空值、原生默认目录、自定义目录、无目录/无权限/损坏配置、已注册绑定、创建后目录失效、命名 profile 与发现/编辑/启动一致性。
4. 插件缺失：缺失/禁用/加载失败/已安装未启用分别提示；相关字段禁止保存，其他字段可用；Auto 缺失沿用原生策略，原生错误及超时正常报告。
5. Profile 编辑：字段映射、人格实际加载、项目/全局范围、清空覆盖、并发冲突、未知字段保留、备份恢复、部分保存提示及生效时机。
6. 命令与交互：`/new`、`/stop`、`/help` 的实际闭环；`/steer` 明确能力提示；菜单/帮助一致、参数错误与作用域隔离。
7. MCP：完整 headless overlay 与引擎 MCP 身份；必需工具失败终止、超时与 /stop。
8. 智能审批：选择启用时 Auto 插件和会话预设均生效；允许/拒绝/异常/取消、无人响应、续跑及插件失效闭环；拒绝动作保持未执行，审计关联当前执行；插件缺失时适用可选能力规则。
9. 认证：账号登录、界面保存 API key、`.env` API key 分别调用成功；两种接入方式切换后实际路由一致；认证失败保持原选择；凭据归属、原配置完整性与日志脱敏。
10. 会话：两轮续跑、引擎重启恢复、`/new` 与迟到事件。
11. 业务闭环：合成项目 group_send、已读确认与 task 权限。
12. 隔离：双 Bot、双项目、work/chat 交叉与工作区资源。

## 未决事项

无。技术核验已收敛（persona、Auto、续跑、MCP 均有实测证据），剩余为各任务实现阶段的验收项。KEY 路由 401 根因、指令加载来源（AGENTS.md 等价物）、skill 目录约定、模型/认证切换续跑语义列为实现阶段验证。
