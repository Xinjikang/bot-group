# 需求来源校验与拆解授权

需求来源使用 `thread_id=<话题ID>; message_id=<消息ID>`。`engine/requirement_gate.py` 校验来源消息存在、由 user 发出、属于当前项目及指定话题，且未撤回。需求字段与来源校验由创建/更新入口和扫描器共用，错误返回具体字段及原因。

该规则验证来源引用的真实性。引用者仍需遵守用户指令，准确判断创建与执行授权；自然语言意图由协作流程处理。

拆解还要求项目就绪、需求开放及字段完整。扫描器按项目内有效 `task.create` 授权选择成员，结合占用凭据、续期和幂等键避免重复拆解。`task_create(..., complete=True)` 提交拆解完成，允许以 `tasks=[]` 结束已有批次。

需求面板展示门禁状态和原因，权限及负责人配置见 [项目配置](../engineering/PROJECT-CONFIGURATION.md)。测试入口为 `test_requirement_gate.py`、`test_requirement_gate_reason.py`、`test_decompose_state_machine.py`、`test_decompose_finalize.py`；运行条件见 [测试索引](../../scripts/TEST_INDEX.md)。
