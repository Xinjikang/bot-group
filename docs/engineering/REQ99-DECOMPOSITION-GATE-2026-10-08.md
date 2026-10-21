# 需求拆解门禁

`engine/requirement_gate.py` 提供共享只读校验。扫描和唤醒提示读取同一门禁，任务创建在事务内再次检查，覆盖扫描后来源撤回或需求变更。

## 字段与来源

- 新格式四个说明字段需完整；`undecided` 去除首尾空白后精确为 `无` 才允许拆解。
- `source_ref` 使用 `thread_id=<非负整数>; message_id=<正整数>`，thread_id=0 表示点对点。ID 限 SQLite 有符号 64 位范围。旧 confirm 后缀仅作兼容解析。
- 来源须为本项目 user 发出的有效消息，话题 ID 与消息一致；非零话题须属于该项目。创建/更新入口校验来源，来源真实与指令意图分别由引擎和协作流程负责。
- 旧记录的六个新增字段全部精确为空且 description 非空时走兼容路径；任一新字段有值即按新规则检查。

## 错误与事务

拒绝拆解返回 `code=requirement_decomposition_blocked`、`requirement_id`、`error` 和 `field_errors`（每项含 field/message）。未决事项回话题澄清后，由具备 req.update 权限的成员更新。

任务创建在 `BEGIN IMMEDIATE` 内校验权限、需求来源与字段，再写任务、节点及事件；拒绝和异常回滚。scanner/wake 提供只读预检查，最终写事务继续核验。拆解授权与占用详见 [来源及授权](../design/DECOMPOSE-AUTH-REDESIGN-2026-10-11.md)。

回归入口：`test_requirement_gate.py`、`test_requirement_fields.py`、`test_scan_requirements.py`、`test_flow.py`、`test_task_brief.py`、`test_project_permissions.py`、`test_message_injection.py`。
