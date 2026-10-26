# bot-group 群聊与任务管理引擎

**简体中文** | [English](README.md)

发布说明：[v0.1.5](docs/releases/v0.1.5.md)。

面向多 Bot 协作的群聊与任务管理引擎，支持消息交流、任务分派、进度跟踪、审查验收与协作交付。内置软件研发、产品调研、内容写作、产品原型设计、算法研发、测试项目六类模板，提供对应的角色分工、协作流程和项目文档。

## 设计理念

bot-group 希望让多个 AI 成员像一支持续运作的团队，带着项目积累，围绕共同目标交流、分工、协商和交付，让用户把精力集中在目标、关键决策与成果验收上。

其设计围绕以下理念展开：

1. **持续协作，积累项目上下文**

   项目工作跨越多次任务与会话。通过保存讨论、任务状态、项目文档和执行记录，为成员继续工作、交接任务和复用经验提供依据，让每次工作都能承接已有成果。

2. **群聊与任务相互衔接**

   群聊承载问题澄清、方案讨论和成员协调，任务承载责任、进度与验收要求。将交流与执行连接起来，使讨论形成行动，让执行中的问题及时回到协商过程。

3. **混沌中的秩序：规则驱动的自组织协作**

   “混沌”体现了对灵活互动与自组织的追求：成员依据专业分工和项目授权自主开展工作，通过消息、话题和任务交接相互协调，让整体协作秩序从具体互动中逐步形成。共同规则明确职责与边界，引擎提供调度、权限校验和过程记录，使灵活协作有章可循。

4. **按需协商，逐级处理分歧**

   日常问题由相关成员直接沟通，需要共同判断的问题进入话题讨论，超出职责或授权范围的事项交由项目负责人或用户决策。协作随实际情况展开、返工和调整，并保留清晰的责任与处理记录。

5. **协作机制与执行工具分层**

   引擎负责组织协作，成员通过各自的运行环境和工具完成具体工作。结合可配置的角色、规则与项目模板，将同一套协作机制应用于软件研发、产品调研、内容写作、产品原型设计、算法研发和测试项目等场景。

## 文档

需求、设计、工程和使用说明位于 [`docs/`](docs)，随源码版本管理：

| 文档 | 说明 |
|---|---|
| [docs/INDEX.md](docs/INDEX.md) | 按子系统组织的工程与专题文档索引 |
| [docs/engineering/ENGINEERING.md](docs/engineering/ENGINEERING.md) | 当前架构、数据、接口、调度、授权与生命周期 |
| [docs/ui-spec/UI-SPEC.md](docs/ui-spec/UI-SPEC.md) | 本项目主题、布局、组件状态与界面验证约定 |
| [docs/guides/DEPLOY.md](docs/guides/DEPLOY.md) | 部署清单（公网上线前必做） |
| [docs/engineering/PROFILE-EDITOR.md](docs/engineering/PROFILE-EDITOR.md) | 成员配置编辑、备份与恢复 |
| [docs/guides/RELEASE-GUIDE.md](docs/guides/RELEASE-GUIDE.md) | Windows 源码安装、独立启停、自动发现正式版本升级与恢复 |
| [docs/guides/DOCUMENT-STORAGE.md](docs/guides/DOCUMENT-STORAGE.md) | 开发资料与公开技术文档的存放约定 |
| [产品原型设计模板](docs/engineering/PRODUCT-PROTOTYPE-TEMPLATE.md) | 产品需求、交互设计、原型交付及角色 SOUL 参考 |
| [算法研发模板](docs/engineering/ALGORITHM-TEMPLATE.md) | 兼顾传统算法与机器学习，分类管理调研文献、含实验计划的算法方案、实验记录及最终报告；代码与配置位置由用户管理 |
| [测试项目模板](docs/engineering/TESTING-TEMPLATE.md) | PRD 与产品原型输入、测试用例、`scripts/` 测试脚本、`scripts/TEST_INDEX.md` 用例与脚本映射及维护约定、测试报告 |

## 目录结构

程序文件、配置和运行数据默认都放在项目根目录。`.env` 由安装程序创建，数据库、附件、日志和备份随运行生成。

```
bot-group/
├── docs/                 # 需求、设计、工程和指南，纳入 Git
│   ├── requirements/     # 需求与验收标准
│   ├── design/           # 设计与方案依据
│   ├── ui-spec/          # 本项目 UI 视觉与组件规范
│   ├── engineering/      # 已实现的技术契约
│   └── guides/           # 使用、维护与协作指南
├── README.md             # 英文首页
├── README.zh-CN.md       # 中文说明
├── .env                  # 本机配置：端口、登录密码等，按需编辑
├── group.db              # 数据库：项目、成员、聊天记录、需求和任务
├── attachments/          # 聊天中上传的图片和文件
├── logs/                 # 启动与运行日志
├── backups/              # 升级前的数据和程序备份
├── requirements.txt      # 引擎依赖（fastapi/uvicorn/httpx/pyyaml/ruamel.yaml）
├── start.sh              # Python 标准库启动入口（shell 包装）
├── start.bat             # Windows 原生 Python 启动入口
├── .gitignore
├── engine/               # 引擎本体（常驻服务，端口 8180）
│   ├── main.py           # 入口：FastAPI app + 队列 worker + 扫描器后台线程
│   ├── db.py             # group.db 连接（WAL+busy_timeout=30s）、建表、重启恢复
│   ├── schema.sql        # 统一数据库：群聊、项目、需求、任务和运行记录
│   ├── taskpool.py       # 需求与任务管理，共用 group.db，按 project 隔离
│   ├── api.py            # API 路由（/msg、/thread、/thread/task-update、inbox、SSE、登录）
│   ├── auth.py           # 管理员单账号 session/token
│   ├── profile_config.py # 成员配置编辑、按变更文件备份、冲突检测和失败恢复
│   ├── router.py         # 消息路由分发（点对点/话题/无@无话题400）
│   ├── mentions.py       # @解析（精确匹配 members 表）
│   ├── wake.py           # Hermes/Codex 子进程、正文注入及超预算补读
│   ├── queue.py          # 串行队列 worker + 看门狗
│   ├── scanner.py        # 180s 扫描：inbox/任务/话题静默/轮次收口/需求拆解
│   ├── taskqueue_proxy.py# 修订代理（硬校验→转发任务池）
│   └── events.py         # SSE 事件流（内存 pub/sub）
├── scripts/              # 测试与测试支持，测试清单见 TEST_INDEX.md
├── tools/                # 安装、发布、升级及服务维护工具
├── mcp/                  # 统一 MCP server（任务管理 + 群聊）
│   ├── server.py         # 一个进程对外
│   ├── requirements.txt  # MCP 与引擎共用依赖，加 MCP SDK
│   └── README.md
└── frontend/             # Web 前端（三栏 SPA）
    └── README.md
```

## 快速开始

### 安装与启动

Windows 10/11（64 位）用户打开 **PowerShell**，复制执行这一条命令：

```powershell
Invoke-WebRequest -UseBasicParsing https://github.com/Xinjikang/bot-group/releases/latest/download/install.ps1 -OutFile "$env:TEMP\bot-group-install.ps1"; if ($?) { powershell -NoProfile -ExecutionPolicy Bypass -File "$env:TEMP\bot-group-install.ps1" }
```

使用 **CMD** 时，执行：

```cmd
curl.exe -fL https://github.com/Xinjikang/bot-group/releases/latest/download/install.ps1 -o "%TEMP%\bot-group-install.ps1" && powershell -NoProfile -ExecutionPolicy Bypass -File "%TEMP%\bot-group-install.ps1"
```

安装器自动下载最新正式版本源码、校验 SHA256、创建专用 `.venv`、安装锁定依赖并自检。它优先复用 64 位 Python 3.11–3.13，缺失时自动下载并校验 Python 官方安装程序，安装到当前用户目录。默认安装位置为 `%LOCALAPPDATA%\bot-group\app`，完成后显示实际路径；整个流程无需 Git。

**自定义安装目录：**选择下面对应终端的一条命令，把 `D:\Apps\bot-group` 换成你想要的绝对路径。保留路径两侧的双引号，以支持含空格的目录。

PowerShell：

```powershell
Invoke-WebRequest -UseBasicParsing https://github.com/Xinjikang/bot-group/releases/latest/download/install.ps1 -OutFile "$env:TEMP\bot-group-install.ps1"; if ($?) { powershell -NoProfile -ExecutionPolicy Bypass -File "$env:TEMP\bot-group-install.ps1" -InstallDir "D:\Apps\bot-group" }
```

CMD：

```cmd
curl.exe -fL https://github.com/Xinjikang/bot-group/releases/latest/download/install.ps1 -o "%TEMP%\bot-group-install.ps1" && powershell -NoProfile -ExecutionPolicy Bypass -File "%TEMP%\bot-group-install.ps1" -InstallDir "D:\Apps\bot-group"
```

源码和专属 `.venv` 都放在指定目录，安装后运行 `D:\Apps\bot-group\start.bat` 启动。需要自动补装 Python 时，基础运行时单独存放在 `%LOCALAPPDATA%\bot-group-runtime\Python313`。也可以在运行默认命令前设置 `INSTALL_DIR` 环境变量；显式传入的 `-InstallDir` 参数优先。

安装完成后，双击桌面的 **Bot Group** 快捷方式或安装目录里的 **`start.bat`**。启动后自动打开配置地址的 UI，已有连接页面时直接复用，健康运行的引擎保持原实例。日常停止使用 **`stop.bat`**，升级使用 **`upgrade.bat`**；升级会自动查询本仓库最新正式版本并保留配置与数据。命令行升级成功后通过快捷方式或 `start.bat` 启动服务；网页升级会自动启动服务。

从 v0.1.2 起，也可在页面右上角 **设置 → 关于 → 检查新版本**，点击升级按钮并确认。右上角出现“新版本 vX.X.X”时，点击同样打开升级确认框。网页升级自动备份、更新并启动服务，完成后刷新界面；v0.1.0/v0.1.1 用户通过原目录 `upgrade.bat` 升级到本版本。Git 源码工作区通过 Git 更新。

Codex / Hermes 的运行环境与模型认证由使用者准备，接入步骤见下方“连接 Codex / Hermes”。服务本身可以先安装并打开。

维护者从已有源码目录安装时，可执行 `py -3.11 tools/install.py install`，随后执行 `py -3.11 tools/service.py start`。

启动成功后自动打开 UI，默认地址为 `http://127.0.0.1:8180/app/`。快捷方式与 `start.bat` 会复用已经连接的页面，登录页同样参与复用；全部 UI 页面关闭后再次启动会打开新页面。已有页面切到前台的实际效果由浏览器策略决定。运行中的服务通过页面右上角“设置 → 重启服务”操作；重启与网页升级完成后自动刷新当前页面。
退出先等待 Bot 自然结束最多 120 秒，超时由引擎终止剩余 Bot 进程树，再回收浏览器资源。详见 [服务重启](docs/engineering/SERVICE-RESTART-2026-10-17.md)。
脚本通过 `tools/start_engine.py` 创建独立后台进程并进行健康检查；启动失败会显示错误。
“数据目录”指保存 `.env`、数据库、附件、日志和备份的文件夹，默认就是项目根目录。日志位于 `logs/engine-startup.log` 和 `logs/engine.log`。

手动配置过数据目录的安装继续使用指定位置。选择其他目录时，在安装、启动、停止和升级前设置同一个环境变量 `GROUP_DATA_DIR`；程序先确定数据目录，再读取其中的 `.env`。源码下载、自定义数据目录、升级与回滚见[安装升级说明](docs/guides/RELEASE-GUIDE.md)。

成员配置编辑的使用方式、备份位置和接口见 [docs/engineering/PROFILE-EDITOR.md](docs/engineering/PROFILE-EDITOR.md)。

### 配置文件 `.env`

`.env` 是一个普通的 UTF-8 文本文件，用来保存本机的运行设置，每行采用 `配置名=值` 的格式。按上面的步骤安装后，它位于项目根目录；安装程序会保留已经存在的文件。文件名就是 `.env`，编辑时保留这一名称。

用记事本打开 `.env`，按需修改，保存后重启引擎。以 `#` 开头的整行是注释；启用某个选项时，去掉该行开头的 `#` 并填写值。例如：

```dotenv
# 仅供本机访问，浏览器使用 http://127.0.0.1:8180/app/
GROUP_ENGINE_HOST=127.0.0.1
GROUP_ENGINE_PORT=8180
```

默认本机使用可保留安装时生成的配置。需要登录密码时，填写 `GROUP_ADMIN_PASSWORD`；外部访问的部署要求见[部署说明](docs/guides/DEPLOY.md)。此处的 `.env` 管理 bot-group 引擎；Hermes 成员自己的 `.env` 位于对应成员配置目录，用于该成员的模型等设置。

可填写的常用配置：

| 变量 | 必填 | 说明 |
|---|---|---|
| `GROUP_ADMIN_PASSWORD` | 生产必填 | 管理员登录密码 |
| `GROUP_SECRET` | 生产必填 | token 签名密钥 |
| `GROUP_ENGINE_HOST` / `GROUP_ENGINE_PORT` | 否 | 监听地址与端口，默认 `127.0.0.1:8180` |
| `GROUP_DB` | 否 | 数据库路径，默认有效数据目录内 `group.db` |
| `GROUP_ATTACHMENTS_DIR` | 否 | 附件目录，默认数据库所在目录内 `attachments` |
| `GROUP_ENGINE_URL` | 否 | MCP 回调引擎 URL；默认由监听配置派生 |
| `HERMES_BIN` / `CODEX_BIN` | 否 | 显式指定宿主可执行入口，省略时自动探测 |
| `GROUP_MCP_PYTHON` | 否 | 子进程 MCP 解释器，默认项目解释器 |
| `RELEASE_INDEX_URL` | 升级时需要 | 正式发布索引，见发布指南 |

使用 `service.py` 或 BAT 启停时，会导入数据目录 `.env` 中尚未定义的环境变量。host/port 优先级为命令行、进程环境、`.env`、默认值；直接启动模块的环境项见 [配置约定](docs/engineering/ENGINEERING.md#8-配置安装与生命周期)。

### 连接 Codex / Hermes

Bot 通过 MCP 工具参与群聊、读取消息和处理任务。MCP 是宿主调用这些工具的接口；由宿主启动对应的工具服务进程。

先准备好 Codex 或 Hermes 的模型与认证，再在项目的“成员与目录”中添加成员，配置角色、权限和工作目录。引擎在启动 Codex、Hermes 成员时自动提供本次运行的 MCP 配置。MCP 的群聊和任务工具统一调用引擎 API，数据库位置由引擎管理。

具体步骤见 [MCP 接入与维护说明](mcp/README.md#连接成员)，新建成员操作见[成员创建说明](docs/engineering/PROFILE-CREATE.md)。完整测试清单见 [scripts/TEST_INDEX.md](scripts/TEST_INDEX.md)。

### 开始使用

完成安装和成员准备后，在浏览器打开 `http://127.0.0.1:8180/app/`；设置了管理员密码时，按页面提示登录。

1. **创建或导入项目。** 在项目选择菜单中点击“＋ 新建项目”，选择“新建项目（空白或场景模板）”或“首次导入已有项目”。新建时可选软件研发、产品调研、内容写作、产品原型设计、算法研发、测试项目或空白项目，填写项目名称和本机目录。
2. **配置协作团队。** 按向导选择协作方式，确认角色、权限和 `AGENTS.md` 中的协作规则，分配 Codex / Hermes 成员及工作区资源，检查预览后确认创建或导入。创建后可点击项目标签，在“成员与目录”中补充成员、检查启用状态和模型配置，在“项目权限”中调整授权并点击“保存权限”。详细步骤见[项目向导](docs/engineering/PROJECT-SETUP-WIZARD-2026-10-14.md)和[项目配置](docs/engineering/PROJECT-CONFIGURATION.md)。
3. **确认环境并发起讨论。** 软件项目先按下方“项目创建后需要确认的配置”核对资源和工程约定。选中项目后，点击左侧“＋ 新话题”，填写标题、选择参与成员并写下开场白，点击“创建”。例如：“请讨论一个产品调研计划，目标是了解用户需求，先列出调研范围和交付物。”话题用于持续讨论；需要直接找某个成员时，可在群聊输入框输入 `@`，从列表选择成员后发送消息。
4. **把讨论转成任务。** 目标和验收标准确认后，要求具备相应权限的成员建立需求、拆分任务并分配接手人。例如：“请按刚才确认的范围建立需求，拆分任务，写清交付物和验收标准并安排执行。”引擎按任务状态、依赖和项目授权调度成员；具体分工与交接流程由所选模板和项目规则约定。
5. **查看进度和结果。** 右侧“任务”面板按需求展示任务，点击任务行的“查看”可查看任务说明及执行、交接记录。结合群聊回复检查交付物；遇到阻塞时补充信息或调整安排。任务完成并验收后，由具备权限的成员关闭需求，也可通过面板中的“关闭需求”操作完成收尾。

首次使用可先发一条 `@成员 请回复你在本项目中的职责和工作目录`，确认成员能够响应，再开展具体工作。页面显示“待拆解授权”时，检查“项目权限”中的任务创建授权及面板提示的原因。

### 项目创建后需要确认的配置

软件项目创建或导入后，先核对以下清单。用户负责提供环境资源和确认项目约束，Bot 负责检查实际工程、补齐技术信息并持续维护文档。软件模板生成的工程约定位于项目内 `docs/engineering/ENGINEERING.md`；已有项目按自己的文档入口维护。

| 配置项 | 用户需要确认或提供 | 操作入口与生效方式 |
| --- | --- | --- |
| UI 规范 | 提供已有品牌、组件库或界面规范；新项目可采用模板基线 | 软件项目的 `docs/ui-spec/UI-SPEC.md`，由前端成员维护、审查员核验；重要视觉变更由项目负责人协调确认 |
| 服务端口、监听地址、浏览器及数据目录 | 核对引擎生成的默认值，按实际服务调整名称、端口，确认浏览器调试端口、用户数据目录及测试数据目录 | 新建/导入向导的“工作区资源”，或项目“成员与目录”→成员“工作区”；独立工作区再进入“资源配置”。成员空闲时修改并保存，下次唤醒注入新值；见[工作区资源](docs/engineering/WORKSPACE-RESOURCES-2026-10-14.md) |
| 测试环境与资源 | 提供可用测试环境、专用账号的用途和权限、凭据引用、占用安排及数据保留要求 | 编辑项目工程文档的“测试环境与资源清单”，或通过项目对话授权 Bot 登记；凭据保存在安全配置中，文档只登记引用。开展相应测试前确认资源可用 |
| CodeGraph 代码索引（按需启用） | 明确启用或停用；启用时准备工具安装和成员访问条件 | 在项目工程文档的“codegraph 配置”登记选择。由 Bot 核对实际工具版本、填写初始化/更新/查询命令并建立索引，记录索引对应的提交与结果；选择停用时同步项目规则中的索引要求 |
| 版本、远端与发布约束 | 确认首轮版本范围、需要使用的远端及同步方向、发布范围与授权 | 在项目对话中明确要求，由责任成员维护工程文档的“分支、合并与发布”；架构师核验已有仓库并登记实际基线，新增远端和发布按用户授权执行 |

引擎端口在安装目录 `.env` 中设置，项目开发和测试服务端口在工作区资源中设置。保存资源后，由开发成员将分配值接入项目启动参数、环境变量或配置文件，启动时核验实际端口和数据目录。技术栈、运行命令和接口约定由 Bot 检查工程后补齐；外部测试资源及访问授权由用户提供。

可以在项目中交代：“请核对工作区资源并适配启动命令，列出仍需我提供的测试环境和权限；CodeGraph 按我的选择配置，将结果更新到工程文档。”工程约定的维护分工见[软件项目角色说明](docs/guides/SOFTWARE-ROLES-2026-10-14.md)。

## 自定义

可以根据团队的实际流程调整项目规则、角色分工、成员设置和交付文档。建议先选用接近需求的项目模板，再针对当前项目修改。

### 修改项目协作规则（AGENTS.md）

每个项目主目录中的 `AGENTS.md` 用来约定该项目的工作方式。点击项目标签，打开“AGENTS.md”页签，编辑后点击“保存 AGENTS.md”；也可以直接用文本编辑器修改项目主目录中的同名文件。

可在其中写清：

- **分工与交接**：谁整理需求、谁执行、谁审查、谁验收，什么条件下交给下一位成员。
- **操作流程**：执行前需要确认哪些内容，遇到阻塞如何汇报，哪些操作需要用户批准。
- **质量要求**：测试命令、检查清单、验收标准，以及完成任务时应提供的证据。
- **文件与版本管理**：文档和成果的存放位置、命名方式、分支规则、提交与合并步骤。

例如，内容写作项目可以增加以下约定，角色名称按项目实际设置填写：

```markdown
## 内容交付流程

1. 策划整理目标读者、文章大纲和验收标准，确认后交给作者。
2. 作者将初稿保存到 drafts/，完成后交给编辑审查。
3. 编辑核对事实来源、结构和表达，退回时列出具体修改项。
4. 审查通过后，由项目负责人验收，将定稿保存到 outputs/。
5. 对外发布前取得用户确认。
```

项目规则按自然语言指导成员协作；需求和任务操作的实际授权由“项目权限”控制，执行时还会检查任务状态、依赖及接手人。调整流程后，应同步核对角色和权限配置。

引擎在成员下次启动前同步项目主目录的规则到独立工作区。已有会话可能缓存旧规则，需要重新读取时可通过 `/new` 建立新会话。具体行为见[项目规则同步](docs/engineering/PROJECT-CONFIGURATION.md#唤醒前的项目规则同步)。

### 调整角色、成员和运行设置

| 可修改内容 | 修改位置 | 作用范围与生效方式 |
| --- | --- | --- |
| 角色名称、分工和成员归属 | 项目配置的“角色”及“成员与目录” | 属于当前项目；角色调整后同步检查 `AGENTS.md` 中的分工 |
| 创建需求、拆分任务、关闭任务等权限 | 项目配置的“项目权限” | 选择成员或角色后点击“保存权限”；后续工具调用按新授权检查 |
| 成员模型、供应商、推理强度 | “成员与目录”→ 成员“编辑”→“模型与供应商” | 默认保存当前项目的覆盖设置；勾选“应用到全局”后保存到该成员全局配置，后续启动使用新设置 |
| 成员个人指令、表达风格和专业职责 | 成员“编辑”中的 Hermes `SOUL.md` 或 Codex `AGENTS.md` | 保存到成员自身的配置目录，作用于复用该成员的项目；项目专属流程放在项目主目录的 `AGENTS.md` |
| 共用主目录或独立工作区、成员工作目录 | 项目配置的协作方式及成员“工作区” | 按页面预览确认并保存；目录调整要求相关成员处于空闲状态 |
| 引擎端口、登录密码和数据位置 | bot-group 安装目录中的 `.env`；自定义数据目录时使用该目录的 `.env` | 属于引擎运行配置，保存后重启；详见上面的“配置文件 `.env`”及[安装升级说明](docs/guides/RELEASE-GUIDE.md) |

项目主目录的 `AGENTS.md` 管理团队规则，成员配置目录里的个人指令管理该成员的通用行为。修改时先确认页面和文件路径对应的范围。成员配置的详细说明见[配置编辑](docs/engineering/PROFILE-EDITOR.md)和[模型设置](docs/engineering/UNIFIED-MODEL-EDITOR.md)。

### 调整项目文档与成员指令

模板生成的 `AGENTS.md`、`README.md`、文档索引、检查清单及成果目录都可以按项目需要继续编辑。修改文档目录或交付物名称时，同步更新项目规则和相关链接。成员的 SOUL 参考也可在“查看 / 引用 SOUL 模板”中引用后修改，点击“保存”才写入成员配置。

建议采用项目模板为相应角色推荐的 SOUL，并在此基础上根据项目情况调整优化，保持专业定位、职责和工作方式与实际角色一致。SOUL 中的描述与角色所需专业能力或职责不匹配时，可能影响 Bot 的判断、协作和交付效果。

**建议保持引擎自带模板原样。** 安装目录 `templates/projects/` 下的模板及 `manifest.json` 由引擎统一维护，包含相互关联的角色、默认权限、流程和文件生成规则。直接修改可能影响后续项目的创建、导入和模板参考，增加升级维护的复杂度。日常定制请在项目创建后修改该项目生成的文件，通过项目配置调整角色与权限，并在引用 SOUL 参考后编辑成员指令。

## 协作契约（给 bot）

- 见 [docs/engineering/ENGINEERING.md](docs/engineering/ENGINEERING.md) 第 7 节；bot 侧工具 = MCP（group_send 等），不组装命令。

## 许可证

本项目采用 [MIT License](LICENSE)。

## 故障排查（Troubleshooting）

### 关闭 Codex / Hermes 后，页面显示“断开连接”

通过 Codex / Hermes 执行 `start.bat` 时，启动的服务可能受到宿主的子进程生命周期管理影响。关闭宿主后，服务可能随之退出，页面会显示断开连接。

1. 在运行 bot-group 的电脑上，用文件资源管理器打开安装目录，手动双击 **`start.bat`**；也可以自行打开 PowerShell，进入安装目录执行 `.\start.bat`。
2. 等待启动检查成功，刷新 `http://127.0.0.1:8180/app/`，确认页面恢复连接。使用自定义地址或端口时，访问对应地址。
3. 仍然无法连接时，保留启动窗口中的错误，并查看数据目录下的 `logs/engine-startup.log` 和 `logs/engine.log`。默认数据目录就是安装目录。

建议日常由用户直接启动服务。页面右上角的“重启服务”适用于服务仍可响应的情况；服务已退出时，通过安装目录中的 `start.bat` 恢复。

### Codex 首次执行任务时出现 `CreateProcess ... blocked by policy`

任务记录可能显示 `exec_command` 启动 `pwsh.exe` 时返回 `CreateProcess Rejected: blocked by policy`，随后任务进入 `blocked`。这表明命令启动被执行策略拒绝；具体原因需要结合该电脑上的 Codex 配置和完整错误判断。

1. 展开任务记录的“完整记录”，保存原始工具错误、失败命令及发生时间。
2. 在发生问题的电脑上，使用**该 Bot 相同的 Codex 可执行程序、配置目录和工作目录**单独启动 Codex，让它执行 `Get-Location`、读取项目中的一个文件、运行 `git status` 等只读检查。独立成员使用自己的 `CODEX_HOME`，命名配置还需匹配 `--profile`；配置位置见 [Codex 成员配置目录](docs/guides/CODEX-PROFILE-HOMES.md)。
3. 单独运行也失败时，检查 Codex 的沙箱初始化、当前权限配置和系统策略。按所用版本提示完成初始化；设备受组织管理时，将完整错误交给管理员核查。操作参考 [OpenAI Windows 沙箱排查文档](https://developers.openai.com/codex/windows/windows-sandbox)。单独运行成功时，对照 bot-group 成员的可执行程序、配置目录、命名配置和工作目录，查明差异。
4. 配置修正后，等待成员空闲，在“项目标签 → 成员与目录”中对该 Bot 点击 **“重置会话”**，让后续执行加载新配置。重新验证基本命令通过后，再通过任务流程恢复受阻任务；重置会话和恢复任务分别执行。

反馈问题时附上 bot-group / Codex 版本、Windows 版本、成员配置类型、完整错误和上述只读检查结果；存在沙箱日志时，可附该成员 `CODEX_HOME/.sandbox/sandbox.log` 中对应时段的内容。分享前遮去凭据和私人路径。仅凭 `blocked by policy` 这段文字，具体拦截来源仍待确认。

### Windows Codex Bot 未开启沙箱，无法正常执行任务

**使用受限执行权限的 Windows Codex Bot 需要可用的沙箱。** 沙箱未启用或初始化失败时，终端命令可能被拒绝，Bot 无法正常读取项目、检查 Git 状态或完成任务。请先为该 Bot 开启沙箱。

1. 打开**该 Bot 自己的 `CODEX_HOME/config.toml`**。独立 Bot 使用自己的配置目录；命名配置还需核对对应覆盖项。
2. 按所用 Codex 版本启用并初始化 Windows 沙箱。传统 Windows 沙箱可配置如下；已有 `[windows]` 表时，在原表内修改该项：

   ```toml
   [windows]
   sandbox = "elevated"
   ```

   保存后，在该 Bot 的配置目录环境下启动 Codex，按提示完成沙箱初始化。`elevated` 初始化不可用且设备策略允许时，可以选择 `sandbox = "unelevated"`；它仍提供受限执行，网络隔离能力较弱。支持 MXC 的版本和设备也可按 [OpenAI Windows 沙箱指南](https://developers.openai.com/codex/windows/windows-sandbox) 启用。
3. 等待 Bot 空闲，在“项目标签 → 成员与目录”中点击该 Bot 的 **“重置会话”**，再验证 `Get-Location` 和 `git status`，通过后恢复受阻任务。

入组时的“沙箱配置待确认”提醒允许用户继续加入。配置未显式填写沙箱实现时，实际启用状态还取决于 Codex 版本和默认设置；以初始化及命令执行结果为准。

### 项目建好后，在哪里给 Bot 配置角色？

**分配角色的入口：点击当前项目标签 →“成员与目录”→ 找到 Bot → 点击“设置角色”或已有角色名称 → 选择角色。** 选择成功后自动保存，页面显示“本项目角色已保存”。

- **列表缺少需要的角色**：在同一项目配置窗口切换到“角色”，输入名称并点击“创建角色”，再回到“成员与目录”分配。成员列表为空时，先通过“添加成员”加入 Bot。
- **配置角色权限**：切换到“项目权限”，按需勾选成员或角色，点击 **“保存权限”**。已有模板授权可在此核对，后续工具调用使用保存后的权限。
- **配置职责与个人指令**：项目分工写在项目的 `AGENTS.md`；Bot 的个人指令从“成员与目录 → 编辑”进入，Hermes 对应 `SOUL.md`，Codex 对应成员自己的 `AGENTS.md`，编辑后点击“保存”。个人指令会影响复用该成员的其他项目。

角色归属按项目保存，同一 Bot 在不同项目中可以承担不同角色。详细说明见 [项目配置](docs/engineering/PROJECT-CONFIGURATION.md) 和 [成员配置编辑](docs/engineering/PROFILE-EDITOR.md)。
