# DSH 执行恢复、Profile 及项目助手

## 范围与依据

修复原生执行的 PID 缺失恢复、DSH 独立 profile 启动身份，并为项目助手接入 DSH。对应 GitHub #22、#23、#24。沿用 [进程查询三态](2026-10-25-process-query.md)、[DSH 独立 profile](2026-11-02-dsh-profile-create.md) 与 [项目助手契约](../engineering/PROJECT-HUD.md)。

## 执行恢复

`exit_code=-1` 包含重启恢复哨兵及 DSH 协议失败。恢复哨兵对应的子进程可能继续运行，查询保留运行、空闲、未知三态。`pid=NULL` 时枚举原生候选进程，以 DSH 独占 overlay 路径中的会话/启动时间标记或 Codex 内联执行上下文核验身份。确认无匹配进程后释放调度阻塞；枚举失败保持未知。历史运行记录保持可审计。

Popen 成功后立即登记进程句柄并持久化 PID。DSH stdout/stderr 读取启动后，独立线程提交 stdin，投递函数可及时返回，退出观察线程负责回收。该顺序覆盖大提示词和子进程启动输出造成的双向管道等待。

看门狗管理投递 job 的时间，原生进程另有退出观察与停止路径。本次保留既有超时设置；缩短 job 超时无法解决进程身份缺失或同步管道阻塞。

## Profile 加载

命名 CLI profile 使用登记的 profile 启动，由 DSH 原生加载 bundle、cordis.yml、用户 patch 与独立存储。执行 overlay 仅重复必要的认证控制配置及项目模型覆盖，避免用户 patch 的 insert 插件注册两次。动态表达式及控制项原配置继续保留，最终 provider 决定认证路由。

`desktop` 继续使用 `headless` 后台适配，并将 desktop patch 作为执行 overlay。实装 CLI 的 desktop 保留名称禁止直接启动；此路径沿用现有后台兼容方式。任意 GUI bundle 或 agent preset 的能力由其运行契约决定，后台独立成员应使用创建接口生成的 base/headless profile。其他命名 profile 的 CLI 不兼容错误明确上报。

原生会话归属随所选 profile 变化。旧版本在公共 headless 目录留下的会话 ID 可能无法在独立 profile 恢复；遇到原生会话缺失时由管理员使用 `/new` 开始新会话，历史记录保留。引擎持续报告原生错误，避免在未知执行状态下自动重复请求。

## 项目助手

DSH HUD 使用一次性独立原生会话，复用命名 profile、共享 HOME 凭据副本与 Auto 审批适配；每轮资料和历史由 HUD 提供。所选 profile 的人格保留，专用助手指令通过 system-prompt.personaSuffix 注入。

本轮私有 MCP 声明 project_config/open_project_page。原生工具通过 agent 范围 restrict 与执行 guard 限定为 read/write/edit/glob/grep/read_image 及两项私有 MCP。长期资料沿用所选 profile 的文件约定；当前 DSH 接入未声明独立 memory 工具。工具范围确认和已安装 Auto 的 preset 确认缺失时，本轮报告失败。

stdout 按 JSONL 协议解析，损坏行、错误事件和缺少最终正文均报告失败。stderr 独立排空并保留工具范围/Auto 状态证据；超时和取消复用 HUD 子进程树清理。临时 overlay、适配插件和凭据随轮次临时目录清理。项目配置写入继续受 HUD 会话、修订与本轮授权门禁约束。

## 验证

隔离回归覆盖残留记录缺席/存活/未知、命名 profile 与 desktop 兼容、插件单次加载、项目覆盖、长输入前的 PID/读取器登记及 HUD 协议与能力失败。

原生验证使用临时 HOME、命名 profile、合成模型输出和独立 MCP，外部模型网络请求被拒绝。验证原生文件读取、私有配置调用、人格、工具可见范围与 Auto 生效。测试入口见 [测试索引](../../scripts/TEST_INDEX.md)，现场结果存于项目主目录 reports/tests。
