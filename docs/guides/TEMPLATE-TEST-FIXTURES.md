# 模板测试配置夹具

2026-10-16：`scripts/test_project_templates.py` 的宿主 `config.toml` 原先使用普通占位文字。入组流程开始解析 Codex 配置后，初始化在配置阶段提前失败，导致 13 项失败；文件发布故障用例继续读取不存在的项目记录，又产生 1 项 `NoneType` 错误。

夹具改为合法的 `workspace-write` TOML。模板测试按真实项目成员及其分配目录核对新增的 `writable_roots`，完整断言审批、网络与其他配置字段，并保留其他宿主文件的指纹检查；仅允许对应 Codex 配置与备份目录变化。全部数据位于临时目录和隔离数据库。

文件发布故障用例先确认发布钩子实际被调用、项目记录存在且状态为 `failed`，再验证手工编辑冲突与恢复门禁，便于直接定位提前失败的原因。

验证入口：`python -X utf8 -B -m unittest scripts.test_project_templates scripts.test_template_role_rename -q`，具体结果保存到主目录测试报告。
