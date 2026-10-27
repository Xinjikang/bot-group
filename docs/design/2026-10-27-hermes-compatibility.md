# Hermes 会话兼容与安装定位

## 范围

适配 Hermes Agent v0.21.1 的 `cdf4c765` 源码及包含 `hermes_state_ids` 的新版源码。版本用于诊断与验证范围记录，执行条件由实际会话接口决定。已有项目、profile、数据库和会话绑定保持原有归属。

## 设计

- 统一解析 Hermes 启动入口和源码目录，覆盖源码虚拟环境、根目录 bin 入口及 Windows 纯转发包装。显式入口优先，源码定位与执行入口对应；配置冲突明确失败。
- 安装检查通过源码静态检查报告会话接口能力；执行前检查实际导入对象和模块来源。缺失接口、导入依赖或已加载其他安装的模块均提供明确诊断，禁止混用不同源码。
- 新版使用 `hermes_state_ids.new_session_id`；该模块确实缺失的旧版使用 `YYYYMMDD_HHMMSS_<6hex>`，与旧版 `hermes_cli.main._create_titled_session` 的生成规则一致。模块存在但导入失败时保留错误并报告兼容性异常。
- 会话创建和标题仍由原生 SessionDB 处理，恢复继续使用真实 session_id。保留标题迁移、失效绑定报错、`/new` generation 和压缩续接行为。
- 兼容性异常按 configuration 分类，公开提示分别指向 Hermes 更新或入口配置；完整模块与源码路径仅写入诊断日志。Codex 执行跳过 Hermes 检查。

## 验收

单元测试覆盖入口布局、配置冲突、静态能力检查、新旧 ID 路径、内部依赖错误、导入来源及公开提示。新旧真实 Hermes 源码分别在独立进程、临时 home 和数据库中验证创建、标题迁移、恢复、`/new`、压缩续接及重启恢复，生产数据库与模型调用保持隔离。

旧版依据：https://github.com/NousResearch/hermes-agent/blob/cdf4c765/hermes_cli/main.py 。
