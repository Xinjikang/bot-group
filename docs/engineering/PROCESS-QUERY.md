# 进程查询

## 接口与调用方

`engine/process_query.py` 使用 psutil，运行期间直接访问进程信息。Windows 枚举先调用 `process_tree.snapshot()` 获取当前进程名称，仅为匹配候选创建新的 psutil 进程对象并复核名称。`command_line(pid, scope=...)` 返回参数列表，进程退出返回 `[]`，查询失败返回 `None`。`list_processes(names, scope=...)` 返回 `(pid, argv)` 列表，枚举或相关候选读取失败返回 `None`。

调用方必须保留 `None` 与 `[]` 的区别。`session_busy` 将未知状态视为暂缓启动，进程停止和生命周期检查沿用同一保护边界。活进程返回空命令行时，查询结果为未知。读取命令行后再次校验进程存活与身份，处理枚举期间退出及 PID 复用竞争。

- Hermes 查询 `python.exe` 和 `pythonw.exe`，按物理 profile、逻辑会话名称或持久化 runtime session ID 精确匹配。`-q`、`--query`、`--` 之后的参数退出身份匹配范围。
- Codex 优先使用引擎持有的进程句柄；恢复路径按 PID 查询，并解析 `mcp_servers.task_queue` 内的 `XDG_GROUP_ENGINE_EXECUTION`，核对 profile、项目、会话及精确启动时间。
- MCP 优先消费执行上下文；需要父进程回查时使用共享查询模块。stdio 直接启动会将程序源码根加入模块搜索路径，支持从其他工作目录启动。

## 诊断与安装

诊断日志包含 `scope`、`stage`、异常类、`winerror` 和 `errno`。同一范围、阶段和错误每 60 秒最多记录一次，错误类型变化立即记录；成功查询清除对应故障状态并输出恢复日志。命令行可能携带凭据，因此日志省略命令行和异常正文。

根 `requirements.txt` 声明 psutil，`requirements.lock` 固定验证版本，`tools/install.py` 自检导入。MCP 依赖文件引用根依赖文件，安装及源码升级沿用统一依赖流程。更新后由管理员手动重启引擎；持有旧 MCP 的宿主重新加载，Hermes 网关持有的 MCP 通过重启网关更新。

## 验证边界

`scripts.test_process_query` 覆盖权限失败、退出竞争、参数编码、日志限频及真实 Windows 子进程。`scripts.test_wmic_free_wake` 在隔离数据库、目录及端口中使用确定性 Hermes CLI 替身，验证真实队列、原生启动器、进程探测、stdio MCP 和 HTTP API：查询未知保留 queued，恢复后启动，读取 inbox，回复前已读受限，显式 `@user` 回复成功后标记已读。

替身测试验证基础链路；目标电脑的真实 Hermes、模型服务和宿主配置需在更新后进行现场验证。测试执行产物存入项目主目录的 `reports/tests/`。

方案依据见 [设计说明](../design/2026-10-25-process-query.md)。
