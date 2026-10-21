# 场景模板资源与接口

模板位于 `templates/projects/<id>/`，内置 software、research、writing、product-prototype、algorithm、testing。`manifest.json` 声明版本、目录模式、流程、角色 ID/名称/职责/默认授权、文件源与目标、SOUL 参考及负责人角色。版本以当前 manifest 为准。

## 生成内容

各模板提供 AGENTS.md、README.md、文档索引及专业资料入口。软件模板生成 docs 下的需求、设计、工程和指南目录，以及踩坑文档、可复用测试索引和测试模板；主目录 `reports/.gitignore` 与报告索引用于保存本地执行证据。调研和写作模板提供来源、草稿、成果及检查清单。

shared 渲染串行开发与交接约定，worktree 渲染任务分支、合并和验证约定。需求、设计与技术文档随当前工作区分支提交，测试报告由主目录共用。默认角色及权限见 [软件角色](../guides/SOFTWARE-ROLES-2026-10-14.md) 和 [负责人规则](TEMPLATE-OWNER-ROLES-2026-10-16.md)。

产品原型设计模板提供产品经理、UX 设计师、原型工程师、原型评审员四角色。docs 保存项目说明、体验设计和验证依据，materials 保存研究及 references 素材，ui-spec 保存默认 UI 规范，outputs 保存 PRD 和 prototype 原型；审查、复审及验收结论写入 task node。流程与目录约定见 [产品原型模板](PRODUCT-PROTOTYPE-TEMPLATE.md)。

算法研发模板提供研发负责人、算法研究员、算法工程师和评审员。docs 按需求、文献、算法方案和工程说明分类，实验计划包含在算法方案中；项目主目录 reports 保存最终报告，reports/experiments 保存逐次记录，工程代码与配置目录由用户管理。流程与目录约定见[算法研发模板](ALGORITHM-TEMPLATE.md)。

SOUL 作为参考供查看、复制、下载和引用，实际 profile 保存由成员配置流程执行。项目详情的 template-reference 按项目关联类型返回当前模板参考；历史初始化快照继续保存在原记录中。

测试项目提供测试负责人、测试工程师、测试评审员。docs 保存产品依据、计划、用例及环境，scripts 保存测试脚本与映射索引，主目录 reports 按轮次保存报告及证据。产品质量和报告交付分别判断，产品修复及下一轮测试按用户指令安排。维护契约见[测试项目模板](TESTING-TEMPLATE.md)。

## 接口与校验

- `GET /projects/templates` 返回模板列表及 id/version/name/description/directory_modes。
- `GET /projects/templates/{id}` 返回 manifest、角色参考、已登记 Bot 和权限目录。
- 新建/导入使用共用 [七步向导](PROJECT-SETUP-WIZARD-2026-10-14.md)。模板配置携带 id/version，角色使用模板局部 ID，成员引用稳定 Bot ID 与单个角色，个人授权单列；负责人从指定角色成员推导。
- 兼容初始化接口见 [初始化契约](REQ103-PROJECT-INITIALIZATION-2026-10-10.md)。预览保存生成内容、团队方案及 revision；提交校验确认摘要。非法模板返回 `invalid_template`，冲突要求重新预览。

变量只做一次文本替换，用户输入保持字面内容。未知变量、权限、重复角色/成员、必需角色缺失、失效宿主身份和版本差异在暂存前拒绝。资源与目标必须为限定相对路径，检查 Windows 保留名、符号链接/reparse point、文件/目录冲突及容量限制；隐藏路径允许精确的 `reports/.gitignore`，并兼容旧快照中的 `internal/.gitignore`。

配置事务使用同一连接的 `BEGIN IMMEDIATE`，复核身份绑定后原子提交项目、角色、成员、授权、负责人及审计。每成员一个 role_id，直接授权与角色授权取并集。文件系统使用同目录暂存、fsync、独占发布和逐文件进度记录；跨数据库与文件系统通过指纹及恢复流程协调。确认后的历史计划按已保存内容恢复。

## 维护与验证

改变资源时更新 manifest 版本及相关引用，保持预览与实际生成一致。软件资料边界见 [文档存放约定](../guides/DOCUMENT-STORAGE.md)。已有项目补齐只新增缺失文件，规则修改在向导确认并备份后执行。

运行 `test_project_templates.py`、`test_template_role_rename.py`、`test_project_templates_live_dispatch.py` 和 `test_project_templates_browser.py`。合成用例检查文件/链接、权限、恢复、字段解析与派发范围；原生模型探针需单独授权，运行结果保存到主目录测试报告。
