# 本机源码安装、启停与正式版本升级

采用源码安装，协作与交付流程见项目 [AGENTS.md](../../AGENTS.md)。

## 支持环境与版本

发布锁定依赖要求 Python 3.11+。Windows 使用 Python 标准库入口和程序专用 `.venv`；下载源码 ZIP 后可直接安装。Hermes/Codex 使用各自运行环境；安装时探测并报告缺项，配置完整后才可执行相应模型任务。其他平台发布前需完成对应环境的安装验证。

`VERSION` 是安装版本，正式版本格式为 `v1.2.3`，由发布负责人分配版本号和发布地址。

## 发布负责人：生成完整资产

从已通过审查与验收的源码目录执行：

```powershell
py -3.11 scripts\make_release.py --formal --version v1.2.3 --out dist\v1.2.3
```

输出目录包含：`bot-group-v1.2.3.zip`、`release_manifest.json`、`SHA256SUMS`、`releases.json`，以及独立标准库安装入口 `bootstrap.py`。ZIP 包含引擎、MCP、前端静态资源、scripts、templates、依赖锁定清单、文档与入口。`VERSION` 纳入文件级受管清单，随源码更新与恢复。数据、凭据、`.venv`、运行缓存及单元测试保持在安装环境或仓库中。

每个正式版本使用独立且固定的资产目录。将这些文件放到配置的可信 HTTPS/内网发布源；将索引中相对地址转换为相对于稳定索引 URL 的正确地址。稳定索引可以包含多条 Release；客户端选择数值最大的正式三段版本，跳过 `draft=true` 和 `prerelease=true`。发布前核对资产 SHA256，再更新稳定索引。命令生成本地资产，发布操作由发布负责人执行。

`releases.json` 契约：顶层 `releases` 为必填数组，单项字段如下。

| 字段 | 类型 | 必填 | 示例 |
| --- | --- | --- | --- |
| version | string | 是 | `v1.2.3` |
| source | string，ZIP URL，可相对索引 | 是 | `v1.2.3/bot-group-v1.2.3.zip` |
| sha256 | string，64位十六进制 | 是 | 源码 ZIP 的 SHA256 |
| manifest | string，清单 URL，可相对索引 | 是 | `v1.2.3/release_manifest.json` |
| manifest_sha256 | string，64位十六进制 | 是 | 外置清单的 SHA256 |
| prerelease | boolean | 否，默认 false | `false` |
| draft | boolean | 否，默认 false | `false` |

外置清单和 ZIP 内清单字节内容对应相同 JSON 对象，包含必填 `version: string` 和 `program_files: string[]` 文件级相对路径。索引地址及摘要应从同一可信发布渠道获得。摘要校验用于验证内容完整性。

## 首次安装

安装 Python 3.11+ 后，从可信发布页下载独立 `bootstrap.py`，核对发布页提供的入口摘要。发布页也可提供已解压源码包；解压后执行 `install.bat`。

以下例子假设已经将经校验的 `bootstrap.py` 下载到 `C:\Downloads\bootstrap.py`：

```powershell
$env:INSTALL_DIR = 'D:\Tools\bot-group'
$env:BOOTSTRAP_URL = 'https://releases.example.org/v1.2.3/bot-group-v1.2.3.zip'
$env:BOOTSTRAP_SHA256 = '<发布页给出的 ZIP SHA256>'
py -3.11 C:\Downloads\bootstrap.py
```

入口自动下载、校验、解压、创建专用 venv、安装 `requirements.lock`、检查关键导入并生成就绪记录。重复安装同一源码可补齐中断文件，同时保留用户文件；安装位置出现内容冲突时提示使用升级流程。依赖安装失败后可重新运行 `py -3.11 scripts\install.py install`；首次数据目录配置 `.env` 保留原内容。

## 配置与独立启停

数据目录是保存本机配置和运行数据的文件夹，默认就是程序根目录；`.env` 是其中的文本配置文件，每行使用 `配置名=值`，首次安装自动生成，编辑保存后重启引擎生效。上面的示例将程序和数据放在 `D:\Tools\bot-group`，配置文件为 `D:\Tools\bot-group\.env`。源码安装步骤见 [README 快速开始](../../README.md#快速开始)。

数据库、附件、`.env`、备份、日志与 `install.json` 默认保存在程序根目录。手动设置过 `GROUP_DATA_DIR` 或 `DATA_DIR` 的安装继续使用指定位置；管理自定义实例时，在安装、启动、停止和升级前设置相同的环境变量。数据目录选择顺序为命令行 > 进程环境 > 程序根目录；监听地址和端口的优先级为命令行 > 进程环境 > 数据目录 `.env` > 默认值。

```dotenv
# 数据目录 .env，按本机实例填写
GROUP_ENGINE_HOST=127.0.0.1
GROUP_ENGINE_PORT=8180
RELEASE_INDEX_URL=https://releases.example.org/releases.json
```

在安装目录运行 `start.bat` / `stop.bat`；也可用 `py -3.11 scripts\service.py start` / `stop`。启动执行轻量就绪检查和同实例优雅停机，随后创建独立后台进程并核验 `/health`。默认浏览器地址 `http://127.0.0.1:8180/app/`，未配置密码时直接进入应用；已有密码配置沿用。前端由同一后端进程静态托管。

运行中的服务可使用页面主题按钮右侧的“重启服务”。后台助手复用 `start.bat`，旧实例停止及浏览器资源回收由引擎负责：最多等待 Bot 自然结束 120 秒，超时终止剩余进程树后继续收尾。停机客户端默认等待 180 秒，页面每次轮询等待 300 秒。成功以新 instance_id 和相同程序根/数据库为依据；失败查看 `logs/service-restart.json`、`logs/service-restart.log`。详见 [服务重启](../engineering/SERVICE-RESTART-2026-10-17.md)。

Windows 四个 BAT 入口使用 ASCII 注释/提示与 CRLF，兼容默认及 UTF-8 CMD 代码页。Git 属性保留 BAT 原始 CRLF 字节，发布构建原样写入 ZIP；维护入口后执行 `python -X utf8 -B -m unittest scripts.test_windows_bat -q` 核对检出、归档及真实 CMD 参数/退出码。详见 [批处理兼容验证](../engineering/WINDOWS-BAT-COMPATIBILITY-2026-10-17.md)。

`GROUP_ENGINE_URL` / `XDG_GROUP_ENGINE_URL` 是 bot 回调覆盖项。管理入口检查它们与当前 host/port 一致，并在停机前核对服务报告的程序目录、数据库和实例身份；配置不一致时先修正实例配置。日志位于数据目录 `logs/engine-startup.log` 和 `logs/engine.log`。

## 默认升级与恢复

在安装目录使用系统 Python 执行：

```powershell
py -3.11 scripts\upgrade.py upgrade
```

配置发布索引后，每次升级自动显示当前与最新正式 Release；用户直接执行同一条命令。当前版本等于或高于索引最新版本时显示“无需更新”。`--index-url <URL>` 可覆盖本次索引地址，数据目录 `.env` 与环境变量同样支持 `RELEASE_INDEX_URL`。`--url <归档> --sha256 <摘要>` 保留指定归档的维护入口。

升级先解析并固定版本与全部资产，再下载校验源码、外置/包内清单、文件集合和 VERSION。查询、下载、缺资产、校验失败时旧实例继续可用。校验通过后：确认旧实例及写入方安全退出 → SQLite backup API 数据快照与程序快照 → 文件级更新 → 依赖同步 → 独立副本迁移验证 → 就绪检查。成功后运行 `start.bat` 恢复服务；真实数据库迁移在新引擎启动时执行。

备份位于数据目录 `backups/upgrade-<时间>/`，包含完整性清单、绑定目录、固定升级目标 `upgrade_target.json`、旧版本程序和数据库/附件/配置快照。受管文件按旧文件级清单更新；受管目录内用户文件原样保留。缺旧清单的环境先走可核验迁移，建立真实旧文件归属后升级。

落地后失败自动恢复匹配的旧程序、VERSION、数据库/附件/配置，并用旧基础解释器重建依赖。恢复结果见备份的 `rollback_evidence.json`，随后启动旧服务。手动回滚最近一次完整快照：

```powershell
py -3.11 scripts\upgrade.py rollback
start.bat
```

升级与回滚外层使用系统 Python，可在恢复时安全重建安装目录的 `.venv`。损坏快照或绑定路径变化会中止恢复，保留现场供核对。SQLite 备份在写入方退出后获取一致快照；业务流程测试走真实任务池事务（`BEGIN IMMEDIATE`）和 SQLite WAL，写锁在每次提交后释放。

## 发布范围与验证

Git 跟踪源码、`docs/` 下的需求、设计、技术文档和指南，以及可复用测试。主目录 `reports/` 保存测试结果和现场证据，通过忽略规则保持本地。发布源码前核对暂存区和 `git ls-files`；源码归档使用受管顶层白名单，并排除测试报告、隐藏目录及隐藏临时文件，清单与 ZIP 文件集合均需核验。模板中用于生成报告目录的静态资源属于运行资源，继续随源码交付。

现有仓库中迁移文件只改变新提交的目录树，旧提交仍保存历史内容。采用全新发布历史时，从经过审核的源码快照建立发布仓库，再检查发布分支、标签及资产。

测试使用隔离数据库、随机端口和临时宿主目录，命令见 [测试索引](../../scripts/TEST_INDEX.md)。结果保存到主目录测试报告。引擎由用户手动重启，MCP 由对应宿主重新加载。
