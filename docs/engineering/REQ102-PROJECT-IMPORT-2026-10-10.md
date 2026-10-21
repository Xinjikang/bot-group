# 已有项目导入接口

新建与导入共用 [七步向导](PROJECT-SETUP-WIZARD-2026-10-14.md)。本文说明已有目录预览、团队配置和兼容纯登记接口；模板补齐、AGENTS 编辑、Git 基线与资源配置分别见对应专题。

## 目录与文件

预览规范化绝对路径，按 canonical 路径查询非归档项目。命中已有登记时返回稳定 ID 供直接打开；提交前重复检查，竞态返回 `directory_registered`。提交完成但列表刷新失败时保留新 ID，重试打开即可。

兼容的无模板导入保持现有文件字节。当前向导按确认计划补齐缺失文件、备份并修改规则；配置方案和文件摘要绑定到 plan_hash，详见 [补齐机制](IMPORT-TEMPLATE-SUPPLEMENT-2026-10-14.md) 与 [规则编辑](IMPORT-AGENTS-EDITOR-2026-10-14.md)。

文档预览读取根 AGENTS.md、README.md 和 doc/docs 内的 md/txt/rst/adoc，最多 64 份文档、1000 条目录项。AGENTS 正文最多 64KiB，其余最多 8KiB，超限显示截断提示。按 UTF-8/BOM 解码，未知编码显示替换字符；正文以文本渲染。链接目录和目录外链接目标跳过。内部目录保持本地。

## 基础 HTTP 契约

两接口要求管理员身份；Bot 返回 403。旧 `POST /projects` 保留纯登记与 `{project, root}` 返回。

`POST /projects/import/preview` 至少传 `root`，返回规范化 root、existing_projects、documents、warnings、path_hint、bots、permissions 和 file_action。文档包含 path/size/content/truncated；Bot 包含 bot_id、host_profile、display_name、runtime、member_profile。当前向导增加模板、文件计划、资源及 Git 方案，按返回 revision/plan_hash 确认。

`POST /projects/import` 的基础字段：

| 字段 | 约定 |
| --- | --- |
| root / display_name | 必填；已存在的本地绝对目录，显示名 1–200 字符 |
| description | 可选，默认空 |
| directory_mode / worktree_root | shared 或 worktree；独立模式需有效根目录与仓库 |
| roles | name/permissions 数组，名称唯一、单行、1–100 字符；权限由权限目录校验 |
| members | bot_id/role/permissions 数组；稳定 Bot ID 唯一，每成员一个所选角色或空角色 |
| owner_bot_id | 选定团队中的负责人，空值表示未配置；当前模板按负责人角色推导并校验 |

基础响应含 project、root、readiness_status、owner_profile、members、file_action。非法目录/字段返回 400，权限失败 403，重复目录或预览变化 409；错误包含原因及重试指引。

## 事务与平台边界

配置使用同一连接的 `BEGIN IMMEDIATE`：重查目录与工作区 → 项目 → 角色 → 成员 → 授权 → 负责人 → ready → 审计 → commit。任一步失败回滚；文件及宿主权限修改采用各自的备份、指纹与恢复机制。角色授权写 `role:<id>`，个人授权写实际 profile，取有效并集。

规范化采用 normcase/canonical/resolve，worktree 继续验证仓库根与 Git 注册。只读 Git 查询使用 `--no-optional-locks`，避免刷新 index。导入接受普通本地路径；UNC 和扩展前缀返回明确边界提示，已登记项目可按稳定 ID 打开。符号链接、junction 和文件发布的约束按具体预览/补齐流程校验。

回归入口：`test_project_import.py`、`test_project_import_supplement.py`、`test_project_import_browser.py` 和项目权限/工作区用例。结果与目录指纹证据输出到主目录测试报告。
