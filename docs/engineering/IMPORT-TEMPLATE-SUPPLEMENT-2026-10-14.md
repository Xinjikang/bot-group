# 导入模板、默认负责人和缺失文件补齐

## 文件补齐流程

当前交互使用 [七步向导](PROJECT-SETUP-WIZARD-2026-10-14.md)：选择目录和模板、目录模式、角色、规则、成员、资源后确认。模板角色预填权限，负责人从模板指定的唯一角色成员推导。确认页展示目录和文件的新增/保留状态及正文，规则修改另有差异和备份。

所有导入模板均提供负责人默认角色。模板补齐作用于已有项目导入流程；历史项目的角色配置继续沿用。

## 文件规则

- 复用 project_templates 的模板加载、变量校验、角色流程及 shared/worktree 规则渲染。项目 ID 由运行时引擎注入，文档明确这一来源。
- 补齐服务保留已有目标文件；AGENTS.md 在当前向导中的显式编辑使用独立的指纹、备份和替换流程。README 与已有技术文档保持原始字节。
- 现有 doc/ 且没有 docs/ 时沿用 doc/ 并调整模板文档链接；其他情况采用模板 docs/。两者同时存在时使用 docs/。
- 按模板补齐父目录及文件：软件模板包括 docs 分类文档、工程约定、踩坑文档、测试索引和测试模板，并创建 reports 忽略规则与测试、技术调研分类；其他场景使用对应模板清单。详见 [资料边界](../guides/DOCUMENT-STORAGE.md)。
- SOUL 参考和 Bot 宿主配置保持原状。所有验证使用临时目录、临时 SQLite 与合成身份。

## 接口与失败处理

沿用 POST /projects/import/preview：目录阶段传 template.id，返回模板版本与 default_roles；确认阶段传完整配置、template.id/version、supplement，返回 file_plan（文件、目录、根目录标识和 plan_hash）。预览全程只读。

POST /projects/import 接收同一配置和 plan_hash。提交在 BEGIN IMMEDIATE 中重新校验模板、配置、目录标识及目标文件哈希；变化返回 409 并要求重新预览。旧版不传 template 的调用保持只读文件登记契约。

新增文件先写临时文件并 fsync，再通过同目录硬链接独占发布完整文件，目标已存在时失败。失败后回滚数据库并清理本次新增且仍保持原哈希/文件身份的文件，保留其他写入者的修改；空的新增目录一并清理。文件系统需要支持硬链接，失败会给出目录权限/空间/并发修改提示。

进程强制终止可能留下已经发布的基础文件或临时文件；重新导入时按当前磁盘状态重新预览，已存在文件保留。该流程不承诺跨 SQLite 与文件系统的崩溃原子事务。

## 验证

- `python -X utf8 -B -m unittest scripts.test_project_import scripts.test_project_import_supplement -q`：覆盖四类模板、权限、规则文件保护、doc 复用、版本与配置变化、失败回滚、并发文件保护及 worktree 渲染。
- `python -X utf8 -B -m unittest scripts.test_project_templates scripts.test_project_initialization -q`：相关模板与初始化回归。
- `python -X utf8 -B scripts/test_project_import_browser.py --screenshot-dir cache/import-template-verification`：真实 Chrome、隔离 API；先选负责人、自动加入与默认角色/权限摘要、取消成员清空负责人、角色权限保留、模板切换、文件预览与实际生成、深浅主题、390px、提交去重及失败重试。

前后端同时更新后，由用户手动重启引擎，再刷新浏览器加载新版本资源。

## 软件项目本地 Git 补齐

目录预览检测 Git 命令可用性、当前目录仓库、上级仓库及初始提交。软件模板且尚无仓库时，默认勾选“初始化本地 Git”；用户可关闭。已有仓库、上级仓库、损坏元数据及裸仓库均保留现状。补齐基础文件或选择 Git 初始化时，缺失的 `.gitignore` 会列入文件预览，已有内容原样保留。

确认页展示 Git 初始化动作。提交重新检查仓库状态，使用临时独立目录执行 `git init`（空模板，隔离 GIT_* 环境变量），随后独占创建目标 `.git` 并发布完整元数据。初始化不暂存文件、不创建提交、不添加远端、不推送；分支使用 Git 配置的初始分支。数据库失败时沿用文件身份/哈希校验回滚，只清理本次未被修改的新增元数据。

worktree 模式要求项目目录为仓库根目录且已有初始提交；缺少时提示先共用目录导入，再由用户手动提交后切换。

补充验证：`scripts.test_project_import_supplement` 新增 5 项真实 Git 测试，覆盖只读预览、空仓库初始化且无提交/远端/暂存、已有仓库与 gitignore 保留、初始化及数据库失败回滚、上级仓库与预览后竞态、worktree 初始提交提示。浏览器回归覆盖默认勾选、取消后全文件保留以及确认导入后的 `.git/HEAD`、`.gitignore` 实际生成。
