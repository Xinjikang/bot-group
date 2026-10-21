# 需求七字段数据契约

来源格式与就绪条件见 [拆解门禁](REQ99-DECOMPOSITION-GATE-2026-10-08.md)，本文说明字段、接口及事务。

## 数据结构与接口

requirements 复用 title，增加 background_goal、scope、approach_basis、acceptance、undecided、source_ref 六个独立 TEXT NOT NULL DEFAULT '' 列。历史 description 原文保留；旧记录新增列为空字符串，读取接口继续返回 description。新建需求的七项内容独立存储。

MCP req_create 提供逐项参数，req_update 支持按字段更新。req_list、req_status、task_get/claim 上下文及 HTTP 需求详情沿用整行读取，返回新增数据库字段。客户端按数据库列名读取，以 background/approach 参数名提交。

## 参数

| 字段 | 类型 | 创建必填 | 更新必填 | 示例 |
| --- | --- | --- | --- | --- |
| project | string | 是 | 是 | bot-group |
| requirement_id | integer | 不适用 | 是 | 99 |
| title | string | 是 | 按需 | 需求字段规范化 |
| background | string | 是 | 按需 | 需求上下文缺失，目标为完整交接 |
| scope | string | 是 | 按需 | 需求字段存储、接口校验与权限控制 |
| approach | string | 是 | 按需 | 独立列，依据本设计文档 |
| acceptance | string | 是 | 按需 | 缺字段逐项返回；旧需求读取成功 |
| undecided | string | 是 | 按需 | 无 |
| source_ref | string | 是 | 按需 | thread_id=10082; message_id=确认消息编号 |
| version | string | 否，默认空 | 不适用 | v1 |
| priority | integer | 否，默认 0 | 不适用 | 0 |

七项内容的业务校验为非空字符串，保存时裁剪首尾空白。工具为七项设置空字符串默认值，使漏传时进入逐项补全反馈。background 写入 background_goal；approach 写入 approach_basis；其余参数与列同名。

更新至少提供一个内容字段；省略或 null 保留原值，空字符串或非字符串拒绝。旧需求可逐项补全；任何一个已提供字段非法时整次更新回滚。历史 description 仅保留兼容读取，MCP 创建/更新以七项字段为入口。Python 内部 req_create 保留旧 version/description/priority 位置参数，创建同样强制校验七项。

source_ref 保存来源引用，创建/更新检查消息归属及有效性；拆解时继续检查完整字段与未决事项。填写示例中的消息编号应替换为实际来源消息 ID。

## 响应与权限

非法字段响应包含 error（string，合并指引）、code（string，requirement_fields_invalid）和 field_errors（array，每项含 field:string、message:string）。例如漏 background 返回该参数名称及“填写现状问题、使用场景和预期结果”。空更新返回相同 code 及至少提供一个字段的提示。

创建成功返回 requirement_id:integer、project:string、project_root:string、priority:integer 及七项数据库字段（string）；更新成功返回 updated:boolean=true、requirement_id:integer、project:string 及实际修改的数据库字段（string）。

创建检查 req.create，更新检查 req.update 项目权限。权限遵循显式授权规则，现有授权保持原值，由管理员在项目权限中按需授予更新权限；角色授权及停用成员约束沿用现有判定。更新查询和写入同时限定 project 与 requirement_id。

## 迁移、事务和回滚

新增六列在独立 BEGIN IMMEDIATE 事务中迁移，全部成功后提交，异常回滚；重复初始化通过列存在判断保持幂等。SQLite WAL，默认串行化隔离（read_uncommitted=0），busy_timeout=30000ms。迁移期间占用写锁，读者可继续读取原有快照；字段为正文，不新增索引。

创建和更新均以 BEGIN IMMEDIATE 包住授权检查、业务校验、内容写入及事件记录，成功后一次提交；拒绝与异常通过关闭连接回滚。更新仅修改请求中的字段，串行化写入避免并发修改不同字段时相互覆盖；同一字段按提交顺序保留最后一次成功写入。

应用回滚：恢复升级前的代码版本，由管理员手动重启引擎及 Hermes 网关，保留新增列和已写数据；旧版本使用具名列读取/写入，兼容额外列。恢复完整旧数据库需要在维护窗口备份当前库后恢复升级前备份，会丢弃备份之后的数据，须单独审批。推荐采用保留新增列的应用回滚。

## 验证

- `test_requirement_fields.py`、`test_flow.py`、`test_task_brief.py`、`test_project_permissions.py`、`test_permission_roles.py`、`test_project_workspaces.py` 覆盖字段、权限及工作流。
- 字段专项覆盖独立列往返、缺失/非法类型、原子更新拒绝、旧记录部分更新、跨项目边界、事件失败回滚与重复迁移。
- MCP handshake 和 e2e `--tasks-only` 检查工具 schema、完整上下文及任务链，使用临时 SQLite；结果保存到测试报告。

部署更新后，管理员手动重启引擎和 Hermes 网关，使引擎及 MCP 服务加载新代码。
