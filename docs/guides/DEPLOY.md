# 部署与访问边界

更新：2026-10-20。安装、升级和回滚操作见 [发布指南](RELEASE-GUIDE.md)，配置与组件关系见 [工程说明](../engineering/ENGINEERING.md)。当前实现以本机协作服务为基础，默认监听 `127.0.0.1:8180`。

## 数据与运行拓扑

数据库由引擎统一访问，MCP 的群聊和任务工具通过 HTTP API 调用引擎。每个引擎实例应明确有效数据目录、数据库、附件、监听地址及端口。`/health` 公开存活状态、PID 和 instance_id；管理员请求同时返回程序根与数据库路径，管理脚本据此核对停机目标。

前端由引擎 `/app/` 托管。外部访问可经 HTTPS 反向代理和隧道接入；SSE 关闭代理缓冲并保持长连接。业务时间戳存在本地时间与运行 epoch 两种用途，多时区部署前需另行核对时间语义及数据迁移。

## 外部访问检查

- 设置 `GROUP_ADMIN_PASSWORD` 和 `GROUP_SECRET`。密码为空时仅接受回环客户端的免密管理；回环反向代理对外提供服务时同样需要设置密码。
- 成员请求同时验证 `X-Group-Profile` 与绑定该成员的 `X-Group-Key`，引擎启动执行时自动注入凭据。可选 `GROUP_ENGINE_KEY` 作为引擎侧持久派生密钥，默认采用随进程重启轮换的随机密钥。
- 业务路由统一执行认证，成员允许访问的接口及项目/话题范围见 [访问控制契约](../engineering/ACCESS-CONTROL.md)。对外访问使用 HTTPS，并按部署需求配置网络范围与代理限制。
- 管理、文件、目录列表、配置及重启接口保持受限访问；配置文件、宿主认证、数据库和日志由文件系统权限保护。

## MCP 与宿主配置

`TASK_QUEUE_ROLE` 保存具体引擎成员 profile，`HERMES_PROFILE` 用于群聊 header，缺失时回退到前者。角色由项目数据库管理。引擎提供 `XDG_GROUP_ENGINE_EXECUTION`，将群聊调用绑定到准确项目、话题与执行；stdio 手动接线说明见 [MCP README](../../mcp/README.md)。

MCP 需要引擎 API 地址、成员身份和 `GROUP_ENGINE_TOKEN` 凭据；数据目录与数据库路径由引擎独立管理。`GROUP_ENGINE_URL` / `XDG_GROUP_ENGINE_URL` 是回调覆盖项，管理脚本要求其目标与本实例 host/port 一致；隔离测试应清理继承的生产回调配置。Hermes 与 Codex 采用子进程驱动，实际可执行入口由安装探测及环境配置确定。

Codex 使用启动参数注入 MCP。Hermes 需要支持 `HERMES_MANAGED_DIR` 配置层，启动时读取引擎为该次执行生成的覆盖文件；临时目录位于数据目录 `runtime/mcp/`，正常退出及启动失败后清理。已有管理配置中的其他设置和 `.env` 随覆盖层保留；成员原配置保持原样。异常终止引擎可能留下临时文件，应在相关进程停止后清理。配置合并与同名冲突说明见 [MCP 接入](../../mcp/README.md#运行时配置)。

内置 user 始终作为人侧身份处理，扫描与唤醒排除 user。当前项目已归档、移除、尚未就绪或成员停用时，派发按相应门禁处理。话题项目字段和按话题聚合的未读接口均已实现。

## 更新生效与故障定位

引擎代码更新由用户手动重启，随后刷新页面；MCP 由承载宿主重新加载，Hermes 网关持有的 MCP 需重启网关。页面重启入口和脚本共用引擎退出流程：120 秒自然结束等待，超时终止 Bot 进程树，再清理专属浏览器资源。细节及失败恢复见 [服务重启](../engineering/SERVICE-RESTART-2026-10-17.md)。

启动检查数据目录的 `logs/engine-startup.log` 和 `logs/engine.log`；页面重启还记录 `logs/service-restart.json`、`logs/service-restart.log`。升级恢复证据位于对应备份目录。验收应分别确认新实例身份、页面刷新、调用结果与资源释放。
