# ECS 安装 bot-group relay 插件（req#168）

ECS 上的 Hermes gateway 通过 bot-group relay 插件接入 bot-group 引擎：插件随 gateway
加载，持一条主动出站 WSS 连引擎 relay 入口，把引擎的 JSON-RPC 帧转发到本机
gateway `/api/ws`，并把本机事件帧回传引擎。插件职责收窄为「传令」——不解析会话语义、
不复制引擎逻辑。

## 1. 前置

- ECS 上 Hermes gateway 已安装且常驻运行（systemd 托管），`$HERMES_HOME` 已确定；
- 办公机引擎与云端插件均支持首次配对，管理员可打开引擎设置中的“远程节点”。
- 分发包 `bot-group-relay-<version>.tar.gz` 与主包同 tag 构建（`engine/maintenance/make_release.py`
  产出，SHA256 见同目录 `SHA256SUMS`）。

## 2. 安装

```bash
mkdir -p "$HERMES_HOME/plugins/bot-group-relay"
cd "$HERMES_HOME/plugins"
curl -O <资产URL>/bot-group-relay-<version>.tar.gz
tar -xzf bot-group-relay-<version>.tar.gz -C bot-group-relay   # 解到插件目录
cd bot-group-relay
./install.sh    # 建独立 venv（python3 -m venv venv）并按 requirements.lock 装依赖
hermes plugins enable bot-group-relay
```

安装脚本只建 venv + 按锁文件安装随包 MCP 依赖——不 clone 仓库、不手动 pip。
插件本体（转发代码）跑在 gateway 进程内，只用标准库与 gateway 已带的 `websockets`，
无额外安装。不用 Hermes 自带 venv（Hermes venv 无 mcp 包）。

## 3. 配置

首次接入在办公机设置中打开“远程节点”，选择“添加远程 Hermes”。

- 云端主动连接办公机时，选择“云端连本机”。选取云端可达的办公机网卡并启用监听。使用 Tailscale 时选择办公机的 Tailscale 地址。在目标 Hermes profile 下运行页面生成的邀请命令，保持命令运行，回到页面核对成员和来源 IP，再点“确认添加”。插件读取该 profile 的实际身份。邀请只用于首次配对，后续连接使用插件在本机保存的凭据。
- 办公机主动连接云端时，在目标 profile 运行 `hermes bot-group-relay listen --address <云端网卡地址> --port <监听端口>`。将输出的配对链接粘贴到“本机连云端”，核对经验证的成员与短证书指纹并确认。主窗口显示该节点的实际连接状态。

CLI 的连接随前台命令运行。常驻 gateway 插件使用该 profile 的配置，首次主动连接办公机可配置 `BOT_GROUP_ENGINE_URL` 和一次性 `BOT_GROUP_RELAY_INVITE`。项目成员资格单独管理，节点卡中的在线状态表示连接已建立。

使用已有节点连接凭据的部署可保留以下配置：

| 配置项 | 说明 |
| --- | --- |
| `BOT_GROUP_ENGINE_URL` | 引擎地址（`http(s)://host:port`，不带路径） |
| `BOT_GROUP_RELAY_MEMBER` | 引擎节点注册的成员名（通常即 profile 名） |
| `BOT_GROUP_RELAY_CREDENTIAL` | 节点连接凭据（引擎注册时下发，只存本机） |
| `BOT_GROUP_MCP_PYTHON` | 可选：随包 MCP 解释器绝对路径（默认自动探测 `插件目录/venv/bin/python`） |

配置后重启 gateway 生效。插件目录为 `$HERMES_HOME/plugins/bot-group-relay/`，
解包后目录结构（gateway 插件发现要求直接子目录含 `__init__.py`）：

```
bot-group-relay/
├── plugin.yaml  __init__.py  adapter.py  config.py  relay.py
├── streams.py  tickets.py  mcp_digest.py
├── VERSION              # 发布版本标识（打包时注入，插件版本单一事实源）
├── mcp/                 # 随包 MCP（server.py + requirements.txt）
├── engine/              # mcp 运行时最小子集（mcp_http / process_query）
├── requirements.lock    # 随包 MCP 依赖锁
└── install.sh
```

## 4. 版本配套

- 归档内 `VERSION` 文件由发布工具打包时注入**实际发布版本**（与主包同 tag），
  插件握手上报的 `plugin_version`/`code_version` 读它（`config.plugin_version()`：
  VERSION 文件 > `BOT_GROUP_RELAY_VERSION` 环境变量 > 源码常量兜底）。
- 引擎按此值与自身发布版本同 tag 配套校验，不符 4003。**只允许同 tag 的
  引擎发布包与插件分发包配套**；源码开发模式（无 VERSION 文件）只报源码常量，
  仅用于本地调试，不与正式发布引擎互连。

## 5. MCP 随包自动注入

- 引擎按节点配置渲染 MCP 启动命令与 env，经 relay 帧下发、插件原样转发到本机
  gateway；用户装一次插件、填一次配置，**不单独配 MCP、不复制成员 token**。
- 插件握手时上报随包 MCP 的**解释器绝对路径、入口绝对路径、版本与内容摘要**
  （摘要 = `mcp/server.py` + `mcp/requirements.txt` + `requirements.lock` 的 sha256
  组合，只覆盖 MCP 包与依赖清单，不含 env 或令牌）；引擎按版本不匹配即拒
  （插件与引擎同 tag 配套）。
- 升级：正常升级=重启 gateway/插件（WS 断开重来）；「连接不断、磁盘 MCP 包原地
  替换」场景，引擎在 configure 前经 `mcp_snapshot_request` 同步问询，插件实时
  重算摘要回复——系统重核摘要再配会话，缓存不失效。

## 6. 权限与共存

- **chat/work 权限**：由引擎会话执行范围收窄（execution token 绑定 project/member/
  chat-work），插件不参与鉴权，只传令。
- **本地直连共存**：同一台机器上的本机 spawn/网关直连路径与 relay 互不影响；
  引擎按成员是否注册 relay 节点分流（注册走 relay、未注册走本机直连）。
- **多成员同机隔离**：同一 ECS 两个 profile 各配一条出站 WSS（各填自己的
  member/credential），引擎按 member 派发、插件按 home 维护本机连接，
  回复与事件各回各话题。

## 7. 排障

| 现象 | 处置 |
| --- | --- |
| 握手被拒 4001 | 凭据错或已吊销：引擎 `POST /relay/nodes/{member}` 重新注册/下发 |
| 握手被拒 4002 | 随包 MCP 摘要与引擎节点配置 `_package_sha` 不符：升级插件包或更新节点配置 |
| 握手被拒 4003 | 插件包版本与引擎不配套：使用与引擎同 tag 的分发包 |
| 连接建立但无派发 | 引擎节点 offline（心跳 90s TTL）：查 gateway 日志与出站网络 |
| 首装 MCP 拉起失败 | 检查 `install.sh` 输出与 `venv/bin/python` 是否存在，MCP 日志在 gateway 会话日志 |
