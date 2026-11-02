# DSH 独立 profile 创建

创建窗口统一使用显示名称、Profile 标识、配置来源和更多设置。标识由用户输入，初始为空；扫描、切换宿主和切换来源保留输入。来源仅供继承配置，成功扫描静默完成，失败沿用通用错误提示。已有 profile 在成员候选列表选择。 模型与供应商位于默认收起的更多设置，均可选；默认设置继承 desktop 主 profile，显式选择来源时继承所选 profile。

DSH 使用 `POST /profiles/create`，在解析后的 DSH HOME 下创建 `profiles/<新标识>/`，沿用独占目录发布、同请求幂等恢复和数据库事务回滚。同名目录返回冲突，来源目录保持原值。新 profile 使用原生 base/headless bundle，以支持后台启动；来源仅继承模型、供应商及认证路由的 patch 条目。自定义模型覆盖 `agent-default-model.config.model`，个人指令写入该 profile 的 `system-prompt.config.personaPrefix`，共享 HOME 的 AGENTS.md 保持原值。账号凭据继续由同一 DSH HOME 提供。

新目录包含 package.json、cordis.yml、cordis.patch.yml、创建指纹和独立记忆/技能目录；注册在发布完成后执行，以便现有 DSH 目录校验读取真实文件。创建结果返回实际 HOME、profile、Bot ID；加入项目继续由调用方确认。

验证覆盖手动输入及来源切换、同名冲突、源配置保持、继承与覆盖、失败回滚、幂等重试、延迟入组、默认设置、Hermes/Codex 回归和桌面/390px 深浅主题。运行环境未提供模型调用授权时使用合成凭据及隔离资源。
