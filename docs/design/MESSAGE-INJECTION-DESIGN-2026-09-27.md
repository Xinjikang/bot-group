# 唤醒消息注入与补读

`engine/wake.py` 在取得执行权、确定项目与会话后，读取当前执行范围的 pending 快照。work 限定项目/成员的点对点未读，chat 限定项目/成员/话题。输入携带正文、消息 ID、发送者、有效回复要求及附件索引，控制信息与原始消息内容分别组织。

## 输入模式

- 快照能放入命令行预算时全量注入初始输入，成员直接处理已提供的正文。
- 超预算时整段退化为补读信号，携带固定 `upper_id`。通过 `group_read_inbox` 按 `after_id < message_id <= upper_id` 分页读取当前仍有效的 pending，直到 `has_more=false`。
- 批准续工使用独立入口，携带任务 ID；成员读取该任务与批准结果后继续。会话切换后按当前绑定重建任务上下文。

分页过滤撤回、失效和已读消息，后到且超过上界的消息由后续投递处理。读取期间目标执行、会话或成员资格失效时返回错误。附件仅注入元数据，需要内容时调用 `group_read_image`。

## 回复、确认与重复派发

每条消息保留当前接收者的有效 `need_reply`；触发消息单独标识。进程启动和正文注入保留 pending 状态，处理完成按现有回复与 `mark_seen` 契约确认。需要实质回复的点对点消息受已读门禁约束，话题发言位继续按话题规则推进。

活跃队列按 chat 的 `(profile, thread_id)`、work 的 `(profile, project)` 去重。退出、启动恢复和扫描还会核对执行存活、任务状态与话题轮次。消息落库、入队、进程启动、目标接收、业务完成分别取证；投递进程成功退出仅提供该层结果。

work 未读按同一接收者/项目的最新有效 pending 时间判断整组陈旧。整组超过 30 分钟且缺少相关活动执行保护时，扫描器将该组转为 `failed` 并通知；新有效消息保留整组的后续处理机会。失败按接收者隔离，原消息保留，已终结记录退出 pending 投递集合。

## 参数与宿主

Windows 命令预算按最终 argv 转义后的整条命令行及 UTF-16 码元计算，包含固定参数和安全余量。单条群聊消息长度上限为 2000 UTF-16 码元。Hermes 使用可验证的原生入口，具体包装解析规则见 [参数传输](../engineering/HERMES-ARGUMENT-TRANSPORT-2026-10-17.md)。Codex 使用自己的原生 exec 参数，并通过 developer instructions 接收协作与项目信息。

运行中的纠偏按具体执行记录管理，入口与能力见 [命令与会话](SLASH-COMMAND-DESIGN-2026-09-27.md)。当前执行的项目、工作目录和主目录报告路径见 [项目配置](../engineering/PROJECT-CONFIGURATION.md)。

## 回归入口

`test_message_injection.py`、`test_message_degrade_pagination.py`、`test_signal_prefix.py`、`test_read_inbox_batch.py`、`test_inbox_seen_gate.py`、`test_prompt_entry_points.py` 覆盖注入、预算、分页与确认边界；传输层另运行 `test_hermes_transport.py`。条件与命令见 [测试索引](../../scripts/TEST_INDEX.md)。
