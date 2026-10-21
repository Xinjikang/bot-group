# Codex 内联协作指令

`engine/wake.py` 通过 `-c developer_instructions=<JSON quoted string>` 传递 `wake_system_prompt(kind, project, workdir)`，包含协作规则与项目 JSON。参数位于 `resume` 子命令前，首次与续会话共用构造路径；末位 user 参数携带本轮唤醒事件及任务上下文。

JSON 编码保留中文并转义换行、引号和反斜杠，整个参数纳入命令行预算。项目 JSON 包括主目录、当前工作区及主目录报告路径；审批、网络和模型配置继续由所选 profile 管理。

引擎更新后由用户手动重启，需要新上下文的成员通过 `/new` 加载最新规则。历史会话记录保留原内容。

回归入口：`test_runtime_fork.py`、`test_prompt_entry_points.py`、`test_is_chat_process.py`、`test_codex_session_mapping.py`、`test_member_session_reset.py`、`test_message_injection.py`。真实模型验证使用 `verify_codex_driver_live.py --live`，需先明确数据与认证范围，结果保存到测试报告。
