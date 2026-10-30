# 全局 Hermes 文件编辑审批策略

## 范围与验收

管理员通过右上角设置菜单的“智能审批策略”选择文件编辑策略。配置全局持久化，所有项目和 Hermes ACP Bot 共用，默认 `workspace_session`。Codex 和旧版 Hermes CLI 保留各自的原生审批机制。命令智能审批继续由 Hermes 配置管理。

三档策略遵循 Hermes 原生语义：`ask` 逐次确认；`workspace_session` 自动允许工作区和系统临时目录中的普通文件；`session` 自动允许当前会话各目录的普通文件。自动允许策略保留 Hermes 敏感路径检查。

每轮执行前读取全局值，原生会话策略不同则调用 `session.mutate edit_policy` 并重新读取快照。相同策略沿用当前值。运行中和等待审批的轮次保持其原有策略，保存的新值从后续轮次应用。策略设置失败沿用现有配置失败处理，阻止本次提交。

## 存储与接口

复用 SQLite `settings` 表，键为 `hermes_edit_approval_policy`，`project=''` 表示全局作用域。未保存或存储值非法时使用 `workspace_session`。项目级同名键不参与读取。

`GET /system/edit-approval-policy` 返回 `{"policy":"workspace_session"}`；`PATCH` 接受唯一字段 `policy`，仅接受上述三值。请求使用现有管理员认证，Bot 凭据返回 403；响应使用 `Cache-Control: no-store`。页面每次打开重新读取，跨浏览器和跨项目共享服务端值。

## 交互与验证

设置入口使用盾牌线条图标。弹窗提供三张单选卡片、全局作用范围及下一轮生效说明，展示 Codex 使用自身审批配置。保存期间禁用选择与保存；失败保留选择，显示错误并允许重试或重新读取。关闭放弃尚未提交的选择；身份变更关闭弹窗并忽略旧响应。

隔离 SQLite/API 测试覆盖默认值、持久化、全局范围、输入校验和管理员鉴权；模拟 RPC 验证跨项目、跨 profile、连续轮次切换、运行中保护和设置失败。浏览器验证三档选择、重开/刷新、跨页面、失败重试、重复提交保护、键盘、深浅主题及 390px 布局。入口见 [测试索引](../../scripts/TEST_INDEX.md)。
