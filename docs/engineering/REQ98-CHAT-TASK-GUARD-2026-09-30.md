# chat/work 任务工具边界

当前 MCP 将任务执行工具 `task_claim`、`task_log`、`task_complete`、`task_block` 限定为 work 模式，工具注册与运行时守卫共同校验。chat 可按项目授权创建需求、拆解与修订任务；执行操作由 work 调度处理。完整接口以 [MCP 契约](../../mcp/README.md) 为准。

运行时模式拒绝返回 `error=PERMISSION_DENIED`、`reason=CHAT_MODE_RESTRICTED`、`action`、`message` 和 `next_step`。调用者按错误指引结束该动作，并通过项目协作流程交接。

work 继续检查项目授权、具体执行身份、任务归属、说明和依赖；工具可见性与业务操作权限分别判断。测试入口为 `test_is_chat_process.py`、`test_prompt_entry_points.py`、`test_mcp_handshake.py`、`test_mcp_e2e.py`。

更新引擎提示词后手动重启引擎；更新 MCP 后由宿主重新加载，Hermes 网关持有的进程需重启网关。
