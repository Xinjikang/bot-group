# 项目文档索引

当前发布说明：[v0.1.2](releases/v0.1.2.md)。

项目成员更多菜单、移出与状态保留契约见 [项目成员移除](engineering/PROJECT-MEMBER-REMOVAL.md)。

访问认证、成员凭据及需求终态门禁见 [访问控制契约](engineering/ACCESS-CONTROL.md)。

当前系统说明从 [ENGINEERING.md](engineering/ENGINEERING.md) 开始，源码安装从 [RELEASE-GUIDE.md](guides/RELEASE-GUIDE.md) 开始。本索引收录版本化需求、设计、工程和使用文档，文件名中的日期用于定位专题。

## 需求与设计

| 文档 | 内容 |
| --- | --- |
| [文档目录要求](requirements/DOCUMENT-LAYOUT.md) | 分类、Git、报告与存量资料整理 |
| [协作基础需求](requirements/REQUIREMENTS-V2.md) | 项目中心、话题与上下文 |
| [脚手架与模板](requirements/PROJECT-SCAFFOLD-DESIGN.md) | 创建导入、身份角色、文件与初始化 |
| [安装与升级需求](requirements/RELEASE-INSTALL-DESIGN-2026-10-16.md) | 安装、启停、配置与文件保护 |
| [产品原型模板需求](requirements/2026-10-20-product-prototype.md) | 四角色、UX、交互原型和成果目录 |
| [算法研发模板需求](requirements/2026-10-21-algorithm-template.md) | 四角色、算法方案、实验复现和报告交付 |
| [测试项目模板需求](requirements/2026-10-21-testing-template.md) | PRD 与原型、三角色、用例脚本联动及报告交付 |
| [MCP API 需求](requirements/2026-10-20-mcp-api-runtime.md) / [配置设计](design/2026-10-20-mcp-config-scope.md) | 运行时配置及身份隔离 |
| [协作设计依据](design/DESIGN-NOTES.md) / [配置职责](design/PROJECT-CONFIG-INVENTORY-2026-09-23.md) | 会话、持久知识和配置边界 |
| [Codex 驱动设计](design/CODEX-DRIVER-DESIGN-2026-09-29.md) | 宿主、Bot 与项目成员分层 |
| [进程查询兼容性](design/2026-10-25-process-query.md) | Windows 进程探测、三态保护与依赖交付 |
| [常驻执行后端方案](design/SERVE-RESIDENT-SOLUTION-2026-10-08.md) | 方案与实施前验证条件 |
| [规则维护边界](design/bot-group-copy-edit-checklist.md) | 提示词、AGENTS 和角色参考 |
| [维护经验](PITFALLS.md) | 调度、身份、前端与生命周期边界 |

## 工程与运行

| 文档 | 内容 |
| --- | --- |
| [工程说明](engineering/ENGINEERING.md) | 子系统、身份、数据、接口、调度、授权和生命周期 |
| [进程查询](engineering/PROCESS-QUERY.md) | Hermes、Codex、MCP 共享查询接口及部署验证 |
| [发布指南](guides/RELEASE-GUIDE.md) | 安装、配置、正式 Release 升级与恢复 |
| [Web 升级](engineering/WEB-UPGRADE.md) | 关于检查、升级确认、独立进程与失败恢复 |
| [部署边界](guides/DEPLOY.md) | 本机身份信任、反代与外部访问 |
| [服务重启](engineering/SERVICE-RESTART-2026-10-17.md) | 页面入口、120 秒排空、进程树收尾、脚本与回执 |
| [浏览器生命周期](engineering/BROWSER-LIFECYCLE-2026-10-16.md) | 资源登记、归属、IPC 回收及验证 |
| [停机隔离](engineering/SHUTDOWN-ISOLATION-FIX-2026-10-17.md) | 实例绑定、升级回滚隔离与停机失败处理 |
| [Windows BAT](engineering/WINDOWS-BAT-COMPATIBILITY-2026-10-17.md) | 编码、换行、参数和退出码 |
| [测试索引](../scripts/TEST_INDEX.md) | Python、Node、Chromium、MCP 及现场探针入口 |
| [维护工具](../tools/README.md) | 安装、发布、升级、服务启停与身份修复 |
| [升级清单](engineering/UPGRADE-MANIFEST-SEMANTICS-2026-10-17.md) | 文件归属、冲突保护及恢复 |
| [临时目录污染](engineering/HERMES-TMP-PATH-INVESTIGATION-2026-10-17.md) | Windows 环境变量边界与处理方法 |
| [重复拆解边界](engineering/REQ97-DUPLICATE-DECOMPOSE-ROOTCAUSE-2026-09-30.md) | 并发控制及现行授权依据 |

## 消息、调度与宿主

| 文档 | 内容 |
| --- | --- |
| [MCP 接入与契约](../mcp/README.md) | Codex/Hermes 成员接入、配置示例、群聊工具与任务权限 |
| [消息注入](design/MESSAGE-INJECTION-DESIGN-2026-09-27.md) | pending 快照、正文注入、预算降级与分页 |
| [多行参数传输](engineering/HERMES-ARGUMENT-TRANSPORT-2026-10-17.md) | Windows Hermes 原生入口 |
| [消息执行归属](engineering/BOT-MESSAGE-EXECUTION.md) | 消息与具体运行的关联 |
| [斜杠命令](design/SLASH-COMMAND-DESIGN-2026-09-27.md) | 会话重置、停止、纠偏与分层回执 |
| [Codex 配置目录](guides/CODEX-PROFILE-HOMES.md) | 独立 home、发现、入组与目录授权 |
| [Codex 内联指令](engineering/REQ100-INLINE-INSTRUCTIONS-IMPLEMENTATION-2026-10-08.md) | 首次及续会话的协作规则、项目信息与命令预算 |
| [任务说明契约](engineering/TASK-BRIEF-CONTRACT.md) | 发布、依赖、交接与领取门禁 |
| [需求拆解](design/DECOMPOSE-AUTH-REDESIGN-2026-10-11.md) | 项目授权、拆解占用、幂等及完成提交 |
| [需求字段](engineering/REQ99-REQUIREMENT-FIELDS-2026-09-30.md) / [拆解门禁](engineering/REQ99-DECOMPOSITION-GATE-2026-10-08.md) | 字段、来源校验、未决事项和事务 |
| [chat/work 工具边界](engineering/REQ98-CHAT-TASK-GUARD-2026-09-30.md) | 讨论、拆解与执行工具权限 |

## 项目、团队与工作区

| 文档 | 内容 |
| --- | --- |
| [项目配置](engineering/PROJECT-CONFIGURATION.md) | 项目身份、角色、权限、目录、规则同步 |
| [接入与就绪](engineering/REQ101-PROJECT-ENTRY-2026-10-10.md) / [导入接口](engineering/REQ102-PROJECT-IMPORT-2026-10-10.md) | 稳定身份、就绪状态、目录与团队登记 |
| [新建与导入向导](engineering/PROJECT-SETUP-WIZARD-2026-10-14.md) | 七步交互、预览与提交 |
| [向导清理与迁移](engineering/PROJECT-SETUP-CLEANUP-2026-10-15.md) | 普通目录、历史结构化配置和恢复边界 |
| [导入基础交互](engineering/IMPORT-WIZARD-2026-10-13.md) | 导入与团队配置 |
| [导入模板补充](engineering/IMPORT-TEMPLATE-SUPPLEMENT-2026-10-14.md) | 模板补文件 |
| [规则编辑](engineering/IMPORT-AGENTS-EDITOR-2026-10-14.md) | 向导内 AGENTS 编辑 |
| [场景模板](engineering/REQ104-SCENARIO-TEMPLATES-2026-10-10.md) | 软件研发、产品调研、内容写作、产品原型设计、算法研发和测试项目模板 |
| [产品原型模板](engineering/PRODUCT-PROTOTYPE-TEMPLATE.md) | 四角色分工、研究与设计、HTML 原型、评审返修和用户验收 |
| [算法研发模板](engineering/ALGORITHM-TEMPLATE.md) | 文献、算法方案与实验计划、用户管理工程目录、实验记录和最终报告 |
| [测试项目模板](engineering/TESTING-TEMPLATE.md) | 产品依据、用例脚本映射与维护、独立测试报告及角色参考 |
| [模板工程基线](guides/TEMPLATE-ENGINEERING-BASELINE-2026-10-13.md) | 生成文件及工程约定 |
| [文档与测试报告](guides/DOCUMENT-STORAGE.md) | docs 分类、Git 版本管理、主目录共享报告与工作区交接 |
| [模板测试夹具](guides/TEMPLATE-TEST-FIXTURES.md) | 隔离测试和模板验证入口 |
| [软件角色](guides/SOFTWARE-ROLES-2026-10-14.md) / [模板负责人](engineering/TEMPLATE-OWNER-ROLES-2026-10-16.md) / [调研负责人](engineering/RESEARCH-PROJECT-OWNER-2026-10-16.md) | 模板分工和负责人边界；版本以模板 manifest 为准 |
| [工作区资源](engineering/WORKSPACE-RESOURCES-2026-10-14.md) / [资源入口](engineering/WORKSPACE-RESOURCE-ENTRY-2026-10-15.md) | 服务端口、浏览器与测试数据目录 |
| [项目移除](engineering/PROJECT-REMOVAL-2026-10-14.md) | 逻辑移除、占用检查和重新导入 |
| [目录选择](engineering/DIRECTORY-PICKER.md) | 网页目录导航及权限 |
| [Profile 编辑](engineering/PROFILE-EDITOR.md) / [Profile 创建](engineering/PROFILE-CREATE.md) | 双宿主配置、备份、冲突与入组 |
| [统一模型编辑](engineering/UNIFIED-MODEL-EDITOR.md) / [项目模型](engineering/PROJECT-MEMBER-MODELS-2026-10-11.md) | 项目覆盖与全局配置 |

## 前端交互与内容

应用设置、自动更新检查、角色影响提示与成员启停见 [应用设置](engineering/APP-SETTINGS.md)。

| 文档 | 内容 |
| --- | --- |
| [前端模块](../frontend/README.md) | SPA 模块与验证入口 |
| [UI 规范](ui-spec/UI-SPEC.md) | 本项目主题变量、布局、组件状态与验证要求 |
| [刷新范围](engineering/FRONTEND-REFRESH.md) / [固定控件](engineering/FIXED-PANEL-CONTROLS.md) | 增量渲染、选区、草稿与滚动 |
| [消息导航](engineering/MESSAGE-NAVIGATION.md) | @ 提醒、跳转与已读持久化 |
| [缓存与背景](engineering/BROWSER-CACHE-LIFECYCLE-2026-10-17.md) | 推理缓存期限/预算、服务端背景 |
| [连接灯](engineering/CONNECTION-INDICATOR-2026-10-17.md) / [气泡颜色](engineering/BUBBLE-COLOR-2026-10-17.md) | 连接状态与主题 |
| [图片附件](engineering/IMAGE-ATTACHMENTS.md) | 上传、保存、读取和预览 |
| [话题创建幂等](engineering/THREAD-CREATION-IDEMPOTENCY-2026-10-14.md) | 重复请求、刷新和恢复 |
| [需求详情](engineering/REQ99-REQUIREMENT-DETAILS-2026-10-08.md) | 需求展示与操作 |
| [成员卡片](engineering/MEMBER-CARD-LAYOUT-2026-10-15.md) / [角色下拉](engineering/MEMBER-ROLE-DROPDOWN-2026-10-15.md) / [负责人展示](engineering/PROJECT-OWNER-ROLE-UI-2026-10-15.md) | 项目成员界面 |
| [空消息状态](engineering/MESSAGE-EMPTY-STATE-2026-10-15.md) / [错误弹窗](engineering/ALERT-DIALOG.md) | 空态与失败反馈 |

## 维护约定

新专题登记到本索引，调整行为时同步更新对应技术契约。需求在 `requirements/`，方案在 `design/`，UI 规范在 `ui-spec/`，工程契约在 `engineering/`，使用维护说明在 `guides/`。维护经验见 [PITFALLS.md](PITFALLS.md)。审查与验收结论写入任务节点，执行证据写入项目主目录测试报告，具体位置见 [存放约定](guides/DOCUMENT-STORAGE.md)。
