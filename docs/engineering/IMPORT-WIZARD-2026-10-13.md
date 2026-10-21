# 导入项目交互

导入入口位于 `frontend/project_import.js`，与新建项目共用 [七步向导](PROJECT-SETUP-WIZARD-2026-10-14.md)。现有目录预览、团队设置、规则编辑、资源分配及最终提交使用当前向导契约。

步骤进度固定在顶部，正文独立滚动，操作栏固定在底部。返回保留输入，改变配置后重新预览；已离开确认步骤的旧提交动作受步骤门禁拦截。权限控件沿用主题样式，支持桌面与窄屏。

显示名默认取目录最后一级名称，支持两种路径分隔符及末尾分隔符；手动改名后保留输入，清空后恢复自动填充。输入框自动填充颜色随深浅主题显示。

重复登记通过稳定项目 ID 打开原项目。已提交但打开失败时提供重试，避免重复创建。已有文件保护、规则备份和旧结构化配置迁移见 [向导清理与迁移](PROJECT-SETUP-CLEANUP-2026-10-15.md)；模板文件计划见 [补齐机制](IMPORT-TEMPLATE-SUPPLEMENT-2026-10-14.md)。

回归入口：`test_project_import_browser.py`、`test_project_import.py`、`test_directory_picker_browser.py` 和 `test_project_entry_browser.py`。截图及执行结果保存到主目录测试报告。
