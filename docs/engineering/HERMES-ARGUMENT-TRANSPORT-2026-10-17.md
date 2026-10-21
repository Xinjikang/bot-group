# Windows Hermes 多行参数传输

Hermes 初始输入包含多行消息及元数据。Windows 批处理 `%*` 转发可能截断多行正文并返回成功退出码，因此引擎调用可验证的原生 exe 入口。消息注入语义见 [注入与补读](../design/MESSAGE-INJECTION-DESIGN-2026-09-27.md)。

## 入口选择

1. 显式 `HERMES_BIN` 优先，可为路径或 PATH 命令名；原生 exe 直接调用，简单 cmd/bat 解析为实际 exe。
2. PATH 中优先查找 `hermes.exe`，随后查找通用 `hermes`。
3. cmd/bat 仅接受单条 `"<绝对exe>" %*` 或 `"%~dp0<相对exe>" %*` 转发。允许空行、`echo off`、REM/`::` 注释和命令前 `@`，支持 UTF-8 BOM、CRLF 和 Windows ANSI 编码。
4. 用户目录下 venv 原生入口作为兜底；Windows 无可用入口时返回明确错误。

包装含额外命令、控制流、未解析变量、裸相对目标或缺失目标时，提示配置真实 `HERMES_BIN`。需要包装提供环境初始化的场景，应先准备环境再指定等价原生入口。解析日志只记录入口路径。

命令形式为 `hermes.exe -p <profile> chat --in <目录> -c <会话> -Q -q <正文>`。正文通过原生 argv 传输，容量判断沿用 CreateProcessW 完整命令行预算；超预算使用整段补读信号。会话首次创建与恢复、work/chat/approved 入口保持各自契约。

## 验证入口

`scripts/test_hermes_transport.py` 使用 distlib PE launcher 创建只记录参数的原生替身，覆盖换行、引号、shell 字符、中文空格路径、入口优先级、复杂包装拒绝及真实 spawn 参数。使用临时 home/SQLite，阻断网络连接。`HERMES_TRANSPORT_EVIDENCE_DIR` 可指定主目录测试报告路径保存接收记录。

运行：`python -X utf8 -B -m unittest scripts.test_hermes_transport scripts.test_message_injection scripts.test_signal_prefix -q`。模型实际消费与会话持久化输入需另行验证并记录内部结果。引擎更新后由用户手动重启。
