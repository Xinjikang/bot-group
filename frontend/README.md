# frontend/ — Web 前端

三栏 SPA：话题卡片流（含未读成员标记）｜消息时间线（修订事件/@高亮/标红）｜任务池投影。
SSE 实时上屏（反代关 proxy_buffering），登录页，人侧写入（发消息/建话题）。
契约 = [../doc/ENGINEERING.md](../docs/engineering/ENGINEERING.md) 第 6 节。
刷新触发、DOM 更新范围和选区约定见 [前端刷新范围](../docs/engineering/FRONTEND-REFRESH.md)。

## 已实现交互

- **我的称呼**：右上角入口修改全局用户显示名，保存到引擎 SQLite 数据库；刷新、独立浏览器和不同项目共用。身份与路由始终使用 `user`，默认称呼为“用户”，升级保留已有名称。
- **任务池栏**：可拖拽调宽（200–800px）+ 可往右收起/展开（`»`/`«` 按钮，收起态 24px）。
- **任务池表头**：`# / 任务 / 状态 / 领取 / 下一步` 五列，sticky 置顶。
- **新建话题成员选择**：多选下拉（数据源 `GET /members`，排除 user 自己），替代原逗号分隔输入。
- **成员配置**：项目详情的“成员与目录”管理成员，编辑支持 Hermes/Codex。模型默认保存项目覆盖，“应用到全局”更新全局并清空当前项目覆盖。见 [成员配置编辑](../docs/engineering/PROFILE-EDITOR.md) 和 [模型编辑](../docs/engineering/UNIFIED-MODEL-EDITOR.md)。
- **新建/导入项目**：共用七步向导，预览与最终提交分离，使用 revision 校验；见 [向导](../docs/engineering/PROJECT-SETUP-WIZARD-2026-10-14.md)。
- **观察与缓存**：SSE 状态灯显示连接状态；推理按具体执行读取，浏览器快照有期限、数量与体积限制；背景由服务端保存。见 [缓存生命周期](../docs/engineering/BROWSER-CACHE-LIFECYCLE-2026-10-17.md)。
- **设置菜单**：集中提供称呼、背景、主题、重启与关于。更新提示位于用户称呼之前，网页打开及每 12 小时检查，恢复可见时到期补查。见 [应用设置](../docs/engineering/APP-SETTINGS.md)。
- **重启服务**：设置菜单入口调用管理员重启接口，连接状态显示进度，刷新可续查同一操作。见 [重启流程](../docs/engineering/SERVICE-RESTART-2026-10-17.md)。
- **网页升级**：关于窗口支持手动检查和升级，右上角新版本提示打开同一确认框；独立进程执行升级与失败恢复，界面刷新后继续核对回执。见 [Web 升级](../docs/engineering/WEB-UPGRADE.md)。

## 模块与验证

`user_settings.js` 通过 `GET/PATCH /user/display-name` 读取和保存称呼，只在当前页面内存保留展示值；持久值由服务端数据库提供。SSE 名称变更事件与轮询同步其他页面，失败时保留输入并支持重试。

`member_picker.js` 为项目向导与项目详情提供共用勾选列表，仅展示尚未加入当前项目的 Bot；`project_settings.js` 中的独立 profile 创建窗口复用配置来源、认证规则和 SOUL 草稿。创建、注册与项目入组的边界见 [profile 创建](../docs/engineering/PROFILE-CREATE.md)。

`index.html` 维护应用状态、消息/任务渲染及 SSE；`appearance.css` 维护主题。`project_import.js`、`project_settings.js`、`project_removal.js`、`workspace_resources.js` 分别负责向导、配置、移除和资源表单。`member_identity.js`、`profile_soul_templates.js`、`project_template_references.js` 管理身份及模板参考；`message_navigation.js`、`attachments.js`、`commands.js` 负责导航、附件和命令；`background_settings.js`、`service_restart.js` 提供背景与重启入口。目录选择、需求详情和弹窗使用各自模块。

按改动选择 [测试索引](../scripts/TEST_INDEX.md) 中的 Node VM 和隔离 Chromium 用例；静态资源修改验证刷新后的行为，后端接口更新需先加载新服务。完整工程专题见 [文档索引](../docs/INDEX.md)。
