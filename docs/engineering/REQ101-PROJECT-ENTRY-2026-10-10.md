# 项目接入与就绪状态

`projects.readiness_status` 为非空 TEXT，默认 `ready`，枚举为 `pending`、`initializing`、`ready`、`failed`。初始化迁移保留既有业务字段和状态，纯登记项目直接进入 ready；实际执行仍需通过成员、权限及任务门禁。

`GET /projects` 与项目详情返回就绪状态。兼容 `POST /projects` 只登记已有目录，返回 `{project, root}`；支持 `name`（兼容稳定 ID）、`display_name`、`root`、`description`、`owner_profile`、`directory_mode` 和 `worktree_root`。省略 name 时由引擎生成稳定 ID，display_name 为可编辑展示名。

登记在 `BEGIN IMMEDIATE` 中提交项目、负责人及审计，初始成员和授权为空。现有规则、文档与源码保持原始字节。新建、模板补齐及团队配置使用 [七步向导](PROJECT-SETUP-WIZARD-2026-10-14.md)。

接入导航提供打开已登记项目、导入已有项目和新建项目，已登记项目按稳定 ID 打开。列表空态、失败重试和异步过期响应单独处理。

测试入口为 `test_project_readiness.py`、`test_project_entry_browser.py` 与项目生命周期/工作区用例。迁移核对脚本 `verify_project_readiness_migration.py` 从只读源库建立临时副本，证据输出到主目录测试报告。数据库备份使用 SQLite backup API 获取一致快照。
