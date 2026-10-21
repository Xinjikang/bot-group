# 斜杠命令与会话控制

命令入口为 `engine/commands.py`，通过项目、目标成员及当前话题确定执行范围。前端命令菜单与 @ 成员选择提供目标；管理员身份及目标有效性由后端检查，结果持久化到命令记录。

## 会话与停止

- `/new`：针对目标作用域切换会话。忙碌状态经过队列、运行记录和进程检查，空闲后才允许切换。Hermes 通过原生会话适配器管理真实 session ID，Codex 重置对应 thread 绑定；后续唤醒读取新绑定。
- `/stop`：按成员、项目及话题范围停止当前执行，停止结果与对应消息/队列状态关联。进程识别和保护规则由停止模块执行。
- `/help`：返回当前命令用法与能力说明，具体目标和参数以命令菜单及接口返回为准。

`session_bindings` 以项目、profile、kind、话题定位真实会话，`session_switch_ops` 保存切换操作。generation 用于并发核验和旧目标失效；逻辑名称保留显示与兼容用途。会话切换与唤醒共享作用域互斥，切换成功后批准续工使用当前会话和持久化任务信息。

## 纠偏状态

当前 `/steer` 传输适配能力不可用时返回明确错误。`steer_ledger.py` 保留具体运行的投递账本和回执模型，操作绑定 run/session/generation；目标结束、会话变更和 TTL 到期分别终结旧投递。

已入账、进程接收、原生消费和业务执行效果分别记录。原始消息保留所属项目/话题的正常可见性，新的主动投递需要绑定新的有效执行。

## 维护入口

核心文件：`engine/commands.py`、`engine/session_adapter.py`、`engine/stop.py`、`engine/steer_ledger.py`。测试使用 `test_commands_dispatch.py`、`test_session_binding.py`、`test_session_switch.py`、`test_member_session_reset.py`、`test_stop_scope.py`、`test_steer_ledger.py` 与前端命令回归。可选原生宿主探针的运行条件见 [测试索引](../../scripts/TEST_INDEX.md)。
