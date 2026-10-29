# bot-group — Group Chat and Task Management Engine

[简体中文](README.zh-CN.md) | **English**

Release notes: [v0.2.0](docs/releases/v0.2.0.md).

A group chat and task management engine for collaboration between multiple bots. It supports messaging, task assignment, progress tracking, review, acceptance, and delivery. Built-in project templates cover software development, product research, content writing, product prototyping, algorithm research and development, and testing, with roles, workflows, and project documentation for each scenario.

## Design Philosophy

bot-group aims to help AI members work as a continuing team: building on project knowledge, communicating around shared goals, dividing responsibilities, resolving disagreements, and delivering results. Users can focus on goals, key decisions, and acceptance of the work.

The design follows five principles:

1. **Continuous collaboration with accumulated project context**

   Project work spans multiple tasks and sessions. Saved discussions, task states, project documents, and execution records give members a basis for resuming work, handing off tasks, and reusing experience. Each round of work builds on earlier results.

2. **Connected conversations and tasks**

   Group chat supports clarification, design discussions, and coordination. Tasks capture responsibilities, progress, and acceptance criteria. Connecting conversation with execution turns discussions into action and brings problems encountered during execution back into the discussion.

3. **Order within chaos: self-organized collaboration guided by rules**

   “Chaos” expresses the pursuit of flexible interaction and self-organization. Members work autonomously within their specialties and project permissions, coordinating through messages, topics, and task handoffs. The overall working order develops through these interactions. Shared rules define responsibilities and boundaries, while the engine provides scheduling, permission checks, and process records.

4. **Discuss as needed and escalate disagreements progressively**

   Members handle routine issues directly. Questions requiring collective judgment move into topic discussions, and matters beyond a member's responsibilities or authority go to the project owner or user. Collaboration adapts through discussion, rework, and changes, with clear records of responsibility and resolution.

5. **Separate coordination from execution tools**

   The engine organizes collaboration. Members carry out their work through their own runtimes and tools. Configurable roles, rules, and project templates apply the same coordination mechanisms to software development, product research, content writing, product prototyping, algorithm research and development, and testing.

## Documentation

Requirements, designs, engineering references, and user guides live in [`docs/`](docs) and are versioned with the source. The linked technical documents are currently in Chinese.

| Document | Description |
|---|---|
| [docs/INDEX.md](docs/INDEX.md) | Engineering and topic index organized by subsystem |
| [docs/engineering/ENGINEERING.md](docs/engineering/ENGINEERING.md) | Current architecture, data, interfaces, scheduling, authorization, and lifecycle |
| [docs/ui-spec/UI-SPEC.md](docs/ui-spec/UI-SPEC.md) | Project theme, layout, component states, and UI validation conventions |
| [docs/guides/DEPLOY.md](docs/guides/DEPLOY.md) | Deployment checklist to complete before public internet exposure |
| [docs/engineering/PROFILE-EDITOR.md](docs/engineering/PROFILE-EDITOR.md) | Member configuration editing, backup, and recovery |
| [docs/guides/RELEASE-GUIDE.md](docs/guides/RELEASE-GUIDE.md) | Windows source installation, service control, release discovery, upgrades, and recovery |
| [docs/guides/DOCUMENT-STORAGE.md](docs/guides/DOCUMENT-STORAGE.md) | Storage conventions for development materials and public technical documentation |
| [Product prototyping template](docs/engineering/PRODUCT-PROTOTYPE-TEMPLATE.md) | Product requirements, interaction design, prototype delivery, and role SOUL references |
| [Algorithm research and development template](docs/engineering/ALGORITHM-TEMPLATE.md) | Traditional algorithms and machine learning: literature, designs with experiment plans, experiment records, and final reports; users manage code and configuration locations |
| [Testing template](docs/engineering/TESTING-TEMPLATE.md) | PRD and prototype inputs, test cases, scripts in `scripts/`, case-to-script mappings and maintenance in `scripts/TEST_INDEX.md`, and test reports |

## Directory Structure

Program files, configuration, and runtime data default to the project root. The installer creates `.env`; the database, attachments, logs, and backups are created during operation.

```text
bot-group/
├── docs/                 # Versioned requirements, designs, engineering references, and guides
│   ├── requirements/     # Requirements and acceptance criteria
│   ├── design/           # Designs and their technical rationale
│   ├── ui-spec/          # Project UI and component specifications
│   ├── engineering/      # Implemented technical contracts
│   └── guides/           # Usage, maintenance, and collaboration guides
├── README.md             # English overview
├── README.zh-CN.md       # Chinese overview
├── .env                  # Local settings: port, login password, etc.
├── group.db              # Projects, members, chat history, requirements, and tasks
├── attachments/          # Images and files uploaded in chat
├── logs/                 # Startup and runtime logs
├── backups/              # Data and program backups created before upgrades
├── requirements.txt      # Engine dependencies (fastapi/uvicorn/httpx/pyyaml/ruamel.yaml)
├── start.sh              # Python standard-library startup entry point (shell wrapper)
├── start.bat             # Native Windows Python startup entry point
├── .gitignore
├── engine/               # Resident engine service, port 8180
│   ├── main.py           # FastAPI app, queue worker, and background scanner
│   ├── db.py             # group.db connection (WAL + busy_timeout=30s), schema, restart recovery
│   ├── schema.sql        # Shared schema for chat, projects, requirements, tasks, and runs
│   ├── taskpool.py       # Requirement and task management, isolated by project in group.db
│   ├── api.py            # API routes (/msg, /thread, /thread/task-update, inbox, SSE, login)
│   ├── auth.py           # Single-administrator session/token authentication
│   ├── profile_config.py # Member configuration, per-file backups, conflicts, failure recovery
│   ├── router.py         # Message routing: direct messages/topics; missing both returns 400
│   ├── mentions.py       # Exact @mention matching against the members table
│   ├── wake.py           # Hermes/Codex subprocesses, message injection, overflow retrieval
│   ├── queue.py          # Serial queue worker and watchdog
│   ├── scanner.py        # 180-second scan: inbox/tasks/topic silence/round closure/decomposition
│   ├── taskqueue_proxy.py# Revision proxy: strict validation, then forwarding to the task pool
│   └── events.py         # SSE event stream with in-memory pub/sub
├── scripts/              # Tests and test support; see TEST_INDEX.md
├── tools/                # Installation, releases, upgrades, and service maintenance
├── mcp/                  # Unified MCP server for task management and group chat
│   ├── server.py         # Single server process
│   ├── requirements.txt  # Shared engine dependencies plus the MCP SDK
│   └── README.md
└── frontend/             # Three-panel web SPA
    └── README.md
```

<a id="快速开始"></a>

## Quick Start

### Install and Start

On **64-bit Windows 10/11**, open **PowerShell** and run this single command:

```powershell
Invoke-WebRequest -UseBasicParsing https://github.com/Xinjikang/bot-group/releases/latest/download/install.ps1 -OutFile "$env:TEMP\bot-group-install.ps1"; if ($?) { powershell -NoProfile -ExecutionPolicy Bypass -File "$env:TEMP\bot-group-install.ps1" }
```

For **CMD**, use:

```cmd
curl.exe -fL https://github.com/Xinjikang/bot-group/releases/latest/download/install.ps1 -o "%TEMP%\bot-group-install.ps1" && powershell -NoProfile -ExecutionPolicy Bypass -File "%TEMP%\bot-group-install.ps1"
```

The installer downloads the latest stable source release, verifies SHA256, creates a dedicated `.venv`, installs locked dependencies, and checks the installation. It reuses a compatible 64-bit Python 3.11–3.13 or downloads and verifies the official Python installer for a per-user installation. The default location is `%LOCALAPPDATA%\bot-group\app`. Git is optional.

**Choose your installation directory:** use one of the following commands and replace `D:\Apps\bot-group` with your preferred absolute path. Keep the quotes around the path so directories containing spaces work.

PowerShell:

```powershell
Invoke-WebRequest -UseBasicParsing https://github.com/Xinjikang/bot-group/releases/latest/download/install.ps1 -OutFile "$env:TEMP\bot-group-install.ps1"; if ($?) { powershell -NoProfile -ExecutionPolicy Bypass -File "$env:TEMP\bot-group-install.ps1" -InstallDir "D:\Apps\bot-group" }
```

CMD:

```cmd
curl.exe -fL https://github.com/Xinjikang/bot-group/releases/latest/download/install.ps1 -o "%TEMP%\bot-group-install.ps1" && powershell -NoProfile -ExecutionPolicy Bypass -File "%TEMP%\bot-group-install.ps1" -InstallDir "D:\Apps\bot-group"
```

The source files and dedicated `.venv` are installed in that directory; launch it with `D:\Apps\bot-group\start.bat`. When the installer needs to install Python itself, the base runtime is stored separately at `%LOCALAPPDATA%\bot-group-runtime\Python313`. You can also set the `INSTALL_DIR` environment variable before running the default command; an explicit `-InstallDir` argument takes precedence.

After installation, double-click the **Bot Group** desktop shortcut or **`start.bat`** in the installation directory. Startup opens the UI at the configured address, reuses an existing connected page, and preserves a healthy running engine. Use **`stop.bat`** to stop and **`upgrade.bat`** to discover and install the latest stable release while preserving configuration and data. After a command-line upgrade, start the service with the shortcut or `start.bat`; Web upgrade restarts it automatically.

Starting with v0.1.2, open **Settings → About → Check for updates** and confirm the upgrade. Clicking the new-version notice at the top right opens the same confirmation. Web upgrades back up the installation, apply the release, and restart the service; refresh the page when complete. Existing v0.1.0/v0.1.1 installations can reach this version using their original `upgrade.bat`. Git workspaces update through Git.

Prepare your chosen Codex / Hermes runtime and model authentication separately, then follow the connection instructions below. The service can be installed and opened before connecting a bot.

Maintainers with an existing source checkout can run `py -3.11 tools/install.py install`, followed by `py -3.11 tools/service.py start`.

Once startup succeeds, the UI opens automatically (default address: `http://127.0.0.1:8180/app/`). The desktop shortcut and `start.bat` reuse a connected UI page, including its login screen, and open a new page after all UI pages are closed. Bringing an existing page to the foreground depends on browser policy. Use **Settings → Restart service** at the top right to restart a running engine; the page reloads automatically when restart or Web upgrade completes.

Shutdown gives bots up to 120 seconds to finish naturally. The engine then terminates any remaining bot process trees and releases browser resources. See [Service restart](docs/engineering/SERVICE-RESTART-2026-10-17.md).

The scripts use `tools/start_engine.py` to create an independent background process and check its health. Startup failures display an error. The **data directory** holds `.env`, the database, attachments, logs, and backups. It defaults to the project root. Logs are stored in `logs/engine-startup.log` and `logs/engine.log`.

Installations with a manually configured data directory continue to use that location. To select another directory, set the same `GROUP_DATA_DIR` environment variable before installation, startup, shutdown, and upgrades. The program resolves the data directory first, then reads its `.env`. See the [installation and upgrade guide](docs/guides/RELEASE-GUIDE.md) for source downloads, custom data directories, upgrades, and rollback.

For member configuration editing, backup locations, and interfaces, see [docs/engineering/PROFILE-EDITOR.md](docs/engineering/PROFILE-EDITOR.md).

### The `.env` Configuration File

`.env` is a UTF-8 text file containing local runtime settings, with one `SETTING=value` per line. After the installation above, it is in the project root. The installer preserves an existing file. Keep the filename `.env` when editing it.

Open `.env` in a text editor, change the settings you need, save, and restart the engine. Lines beginning with `#` are comments. To enable a commented option, remove its leading `#` and supply a value. For example:

```dotenv
# Local access only; open http://127.0.0.1:8180/app/ in your browser
GROUP_ENGINE_HOST=127.0.0.1
GROUP_ENGINE_PORT=8180
```

For local use, the generated defaults can be kept. Set `GROUP_ADMIN_PASSWORD` to require a login password. See the [deployment guide](docs/guides/DEPLOY.md) for external access requirements. This `.env` configures the bot-group engine. Each Hermes member has its own `.env` in its member configuration directory for model and related settings.

Common settings:

| Variable | Required | Description |
|---|---|---|
| `GROUP_ADMIN_PASSWORD` | In production | Administrator login password |
| `GROUP_SECRET` | In production | Token signing key |
| `GROUP_ENGINE_HOST` / `GROUP_ENGINE_PORT` | No | Bind address and port; default `127.0.0.1:8180` |
| `GROUP_DB` | No | Database path; defaults to `group.db` in the effective data directory |
| `GROUP_ATTACHMENTS_DIR` | No | Attachment directory; defaults to `attachments` beside the database |
| `GROUP_ENGINE_URL` | No | Engine callback URL for MCP; derived from the bind configuration by default |
| `HERMES_BIN` / `CODEX_BIN` | No | Explicit host executable entry point; automatically detected when omitted |
| `GROUP_MCP_PYTHON` | No | Interpreter for MCP subprocesses; defaults to the project interpreter |
| `RELEASE_INDEX_URL` | For upgrades | Official release index; see the release guide |

When starting or stopping through `service.py` or the BAT scripts, unset environment variables are loaded from the data directory's `.env`. Host and port precedence is: command line, process environment, `.env`, then defaults. For environment settings when starting modules directly, see [configuration conventions](docs/engineering/ENGINEERING.md#8-配置安装与生命周期).

### Connect Codex / Hermes

Hermes compatibility is checked against its session APIs. Verified source baselines are v0.21.1 (`cdf4c765`) and v0.21.3 (`afc3b7c6f3`). Installation and startup checks report compatibility; see [Hermes compatibility](docs/engineering/HERMES-COMPATIBILITY.md) for legacy session IDs, source discovery, and troubleshooting.

Bots use MCP tools to join group chats, read messages, and process tasks. MCP is the interface through which the host calls these tools, and the host starts the corresponding tool server process.

Configure model access and authentication for Codex or Hermes first. Then add members under **Members & directories** (成员与目录), setting their roles, permissions, and working directories. The engine automatically supplies MCP configuration for each Codex or Hermes member run. MCP chat and task tools call the engine API; the engine manages the database location.

See [MCP integration and maintenance](mcp/README.md#连接成员) and [member creation](docs/engineering/PROFILE-CREATE.md) for details. The complete test index is [scripts/TEST_INDEX.md](scripts/TEST_INDEX.md).

### Start Using the Project

After installation and member setup, open `http://127.0.0.1:8180/app/`. If an administrator password is configured, sign in as prompted. Chinese labels are included below to help locate controls in the current UI.

1. **Create or import a project.** In the project selector, click **＋ New project** (＋ 新建项目). Choose **New project: blank or template** (新建项目（空白或场景模板）) or **Import an existing project for the first time** (首次导入已有项目). For a new project, choose software development, product research, content writing, product prototyping, algorithm research and development, testing, or a blank project, then enter its name and local directory.
2. **Configure the team.** Follow the wizard to select a collaboration mode, confirm roles, permissions, and the collaboration rules in `AGENTS.md`, and assign Codex / Hermes members and workspace resources. Review the preview and confirm creation or import. Afterwards, click the project tab and use **Members & directories** (成员与目录) to add members and check their enabled state and model settings. Adjust authorization under **Project permissions** (项目权限) and click **Save permissions** (保存权限). See the [project wizard](docs/engineering/PROJECT-SETUP-WIZARD-2026-10-14.md) and [project configuration](docs/engineering/PROJECT-CONFIGURATION.md).
3. **Confirm the environment and start a discussion.** For software projects, use the checklist below to confirm resources and engineering conventions. Select the project, click **＋ New topic** (＋ 新话题) on the left, enter a title, select participants, write an opening message, and click **Create** (创建). For example: “Discuss a product research plan to understand user needs. Start by listing the research scope and deliverables.” Topics support ongoing discussions. To contact a member directly, type `@` in the group chat composer, select the member, and send a message.
4. **Turn the discussion into tasks.** Once the goal and acceptance criteria are agreed, ask a member with the required permissions to create a requirement, break it into tasks, and assign recipients. For example: “Create a requirement for the scope we agreed on, break it into tasks, specify deliverables and acceptance criteria, and arrange execution.” The engine schedules members according to task state, dependencies, and project authorization. The selected template and project rules define responsibilities and handoffs.
5. **Review progress and results.** The **Tasks** (任务) panel on the right groups tasks by requirement. Click **View** (查看) on a task to inspect its description, execution records, and handoffs. Check deliverables alongside chat responses, providing missing information or adjusting plans when work is blocked. After tasks are completed and accepted, an authorized member can close the requirement. The panel also provides **Close requirement** (关闭需求).

For a first check, send `@member Please reply with your responsibilities and working directory in this project` and confirm that the member responds. If the page shows **Awaiting decomposition authorization** (待拆解授权), check task-creation authorization in **Project permissions** and the reason shown in the panel.

### Settings to Confirm After Project Creation

After creating or importing a software project, review the checklist below. Users provide environment resources and confirm project constraints. Bots inspect the actual project, fill in technical details, and maintain documentation. The software template creates engineering conventions at `docs/engineering/ENGINEERING.md` within the project; existing projects can use their own documentation entry point.

| Setting | What the user confirms or provides | Where to configure it and when it takes effect |
| --- | --- | --- |
| UI specification | Existing brand guidelines, component libraries, or UI standards; new projects can use the template baseline | The software project's `docs/ui-spec/UI-SPEC.md`, maintained by frontend members and checked by reviewers. The project owner coordinates significant visual changes. |
| Service ports, bind addresses, browser settings, and data directories | Check generated defaults, adjust service names and ports, and confirm browser debugging ports, user data directories, and test data directories | **Workspace resources** (工作区资源) in the creation/import wizard, or **Members & directories** → member **Workspace** (工作区). For an independent workspace, open **Resource configuration** (资源配置). Save changes while the member is idle; values are injected on its next wake. See [workspace resources](docs/engineering/WORKSPACE-RESOURCES-2026-10-14.md). |
| Test environments and resources | Available environments, purposes and permissions of dedicated accounts, credential references, resource reservations, and data retention requirements | Edit the engineering document's **Test environments and resources** inventory, or authorize a bot to record it through the project conversation. Store credentials in secure configuration and document references only. Confirm availability before the relevant tests. |
| CodeGraph code index (optional) | Decide whether to enable it; if enabled, prepare the tools and member access | Record the choice under **codegraph configuration** in the engineering document. A bot checks tool versions, documents initialization/update/query commands, builds the index, and records its commit and result. If disabled, update the corresponding project-rule requirements. |
| Versions, remotes, and release constraints | Initial version scope, required remotes and synchronization direction, publication scope, and authorization | State requirements in the project conversation. The responsible member maintains **Branches, merging, and releases** in the engineering document. The architect checks the existing repository and records the actual baseline. Adding remotes and publishing follow user authorization. |

Set the engine's port in the installation directory's `.env`. Configure development and test service ports in workspace resources. After saving resources, developers connect the assigned values to startup arguments, environment variables, or configuration files, then verify the actual ports and data directories at startup. Bots inspect the project to document its technology stack, commands, and interfaces; users provide external test resources and access authorization.

For example: “Check the workspace resources and adapt the startup commands. List the test environments and permissions I still need to provide. Configure CodeGraph according to my choice and record the results in the engineering document.” See [software project roles](docs/guides/SOFTWARE-ROLES-2026-10-14.md) for documentation responsibilities.

## Recommended Collaboration Workflow

Software development, testing, and other projects can use the following workflow to connect requirements discussions, plan approval, task execution, and delivery reviews. Users focus on goals and constraints, plan approval, and key decisions. Bots organize execution according to project roles, permissions, and collaboration rules.

1. **Discuss requirements and document an actionable plan.** Create a topic and invite two or three bots with different responsibilities. Explain the background, goals, scope, constraints, and expected deliverables. Ask the participants to discuss the technical approach or test plan, resolve disagreements, and document the agreed approach in sufficient detail for execution. Members organize the discussion according to their roles; users can contribute information, adjust direction, or correct misunderstandings throughout.

2. **Review the plan before creating a requirement.** Review the proposed plan for requirement coverage, implementation steps, resource availability, and acceptance criteria. Return it for revision and another review when changes are needed. Once approved, ask the responsible bot with the appropriate permissions to create a development or testing requirement and arrange task decomposition and execution through the project workflow.

3. **Execute through tasks and coordinate issues by responsibility.** Bots carry out the approved plan using task descriptions and acceptance criteria, recording handoffs, reviews, and acceptance in task nodes. Relevant members coordinate routine issues, while users can follow progress and acceptance results in the task panel. When members encounter a blocker they cannot resolve, or a decision involving scope or resource authorization, the responsible member summarizes the issue and requests user involvement.

4. **Continue submitting requirements while the queue schedules execution.** While existing tasks are running, users can create topics for additional requirements and follow the same plan review and requirement creation process. Once tasks enter the queue, the engine schedules them according to prerequisites, task state, and member availability. Members continue the work through the agreed handoffs.

5. **Conduct a comprehensive review after a related set of requirements is complete.** Create a dedicated topic and ask members to review the work against the original goals, approved plans, deliverables, and test evidence. Record omissions, integration issues, and remaining improvements. After the user confirms the findings and scope, the responsible bot creates follow-up requirements to organize fixes and verification.

## Customization

Adapt project rules, responsibilities, member settings, and deliverable documents to your team's workflow. Start with the closest project template, then customize the current project.

### Edit Project Collaboration Rules (`AGENTS.md`)

The `AGENTS.md` in each project's main directory defines how that project works. Click the project tab, open **AGENTS.md**, edit it, and click **Save AGENTS.md** (保存 AGENTS.md). You can also edit the file directly with a text editor.

Use it to specify:

- **Responsibilities and handoffs:** who organizes requirements, executes work, reviews it, and accepts it; and when work moves to the next member.
- **Operating procedures:** what must be confirmed before execution, how blockers are reported, and which actions require user approval.
- **Quality requirements:** test commands, checklists, acceptance criteria, and evidence to provide at completion.
- **Files and version control:** document and output locations, naming conventions, branch rules, and commit and merge procedures.

For example, a content writing project can add the following rules, using its actual role names:

```markdown
## Content Delivery Workflow

1. The planner defines the target audience, article outline, and acceptance criteria, then hands them to the writer after confirmation.
2. The writer saves the draft in drafts/ and hands it to the editor for review.
3. The editor checks sources, structure, and wording, listing specific changes when returning work.
4. After review passes, the project owner accepts the work and the final version is saved in outputs/.
5. Obtain user confirmation before external publication.
```

Project rules guide collaboration in natural language. **Project permissions** control authorization for requirement and task operations; execution also checks task state, dependencies, and recipients. Review role and permission settings when changing the workflow.

Before a member's next startup, the engine synchronizes rules from the project's main directory to its independent workspace. Existing sessions may cache earlier rules. Use `/new` to start a fresh session when the rules need to be reloaded. See [project-rule synchronization](docs/engineering/PROJECT-CONFIGURATION.md#唤醒前的项目规则同步).

### Adjust Roles, Members, and Runtime Settings

| Setting | Where to change it | Scope and activation |
| --- | --- | --- |
| Role names, responsibilities, and member assignments | **Roles** (角色) and **Members & directories** in project configuration | Applies to the current project. Review responsibilities in `AGENTS.md` after changing roles. |
| Permissions to create requirements, decompose tasks, close tasks, etc. | **Project permissions** | Select members or roles and click **Save permissions**. Subsequent tool calls use the new authorization. |
| Member models, providers, and reasoning effort | **Members & directories** → member **Edit** (编辑) → **Models & providers** (模型与供应商) | Saves a project override by default. Select **Apply globally** (应用到全局) to save to the member's global configuration; subsequent runs use the new settings. |
| Personal instructions, communication style, and specialist responsibilities | Hermes `SOUL.md` or Codex `AGENTS.md` in the member's **Edit** screen | Saved in the member's own configuration directory and applies across projects using that member. Keep project-specific workflows in the project root's `AGENTS.md`. |
| Shared main directory or independent workspaces; member working directories | Collaboration mode and member **Workspace** in project configuration | Review the preview and save. Related members must be idle during directory changes. |
| Engine port, login password, and data location | `.env` in the bot-group installation directory, or in a custom data directory | Engine runtime configuration; restart after saving. See the `.env` section above and the [installation and upgrade guide](docs/guides/RELEASE-GUIDE.md). |

The project root's `AGENTS.md` defines team rules, while personal instructions in member configuration directories define each member's general behavior. Confirm the scope represented by the page or file path before editing. See [configuration editing](docs/engineering/PROFILE-EDITOR.md) and [model settings](docs/engineering/UNIFIED-MODEL-EDITOR.md) for details.

### Adjust Project Documents and Member Instructions

Template-generated `AGENTS.md`, `README.md`, document indexes, checklists, and output directories can be edited for each project. Update project rules and links when changing document locations or deliverable names. Member SOUL references can be selected under **View / reference SOUL templates** (查看 / 引用 SOUL 模板), then edited. Click **Save** (保存) to write the member configuration.

Use the SOUL recommended by the project template for each role as a starting point, then adapt and refine it for the project. Keep its professional focus, responsibilities, and working methods aligned with the assigned role. A mismatch between the SOUL description and the role's required expertise or responsibilities may affect the Bot's judgment, collaboration, and quality of deliverables.

**Keep the engine's bundled templates intact.** The installation's `templates/projects/` templates and `manifest.json` are maintained together, including interdependent roles, default permissions, workflows, and file-generation rules. Direct changes can affect future project creation, imports, and template references, and increase upgrade maintenance. For routine customization, edit the generated files after project creation, adjust roles and permissions through project configuration, and edit member instructions after importing a SOUL reference.

## Collaboration Contract for Bots

- See section 7 of [docs/engineering/ENGINEERING.md](docs/engineering/ENGINEERING.md). Bots perform collaboration operations through MCP tools such as `group_send`.

## License

This project is licensed under the [MIT License](LICENSE).

## Troubleshooting

### The page disconnects after closing Codex / Hermes

When Codex / Hermes runs `start.bat`, the service may remain subject to the host's child-process lifecycle management. Closing that host may stop the service and disconnect the page.

1. On the computer running bot-group, open the installation directory in File Explorer and double-click **`start.bat`** yourself. You can also open PowerShell, enter the installation directory, and run `.\start.bat`.
2. Wait for the startup check to succeed, then refresh `http://127.0.0.1:8180/app/` and confirm the connection returns. Use your configured address and port when customized.
3. If the page still cannot connect, retain the error shown in the startup window and check `logs/engine-startup.log` and `logs/engine.log` in the data directory. The installation directory is the default data directory.

For everyday use, start the service directly yourself. The page's **Restart service** button requires a responsive service; use the installation's `start.bat` to recover after the service has exited.

### Codex reports `CreateProcess ... blocked by policy` during its first task

A task record may show `CreateProcess Rejected: blocked by policy` when `exec_command` tries to start `pwsh.exe`, followed by a `blocked` task status. This indicates that command startup was rejected by an execution policy. Identifying the cause requires that computer's Codex configuration and complete error.

1. Expand **Full record** (完整记录) in the task history and retain the original tool error, failed command, and timestamp.
2. On the affected computer, start Codex separately with **the same executable, configuration directory, and working directory as the Bot**. Ask it to run read-only checks such as `Get-Location`, reading a project file, and `git status`. Independent members use their own `CODEX_HOME`; named configurations also require the matching `--profile`. See [Codex profile homes](docs/guides/CODEX-PROFILE-HOMES.md).
3. If the standalone checks fail, inspect Codex sandbox setup, active permissions, and system policy. Complete initialization as prompted by the installed version; on managed devices, ask an administrator to review the full error. See [OpenAI's Windows sandbox troubleshooting guide](https://developers.openai.com/codex/windows/windows-sandbox). If the standalone checks succeed, compare the executable, configuration directory, named configuration, and working directory with the bot-group member settings.
4. After correcting the configuration, wait until the member is idle, then open **Project label → Members & directories → Reset session** (重置会话) for that Bot so subsequent execution loads the new settings. Verify basic commands again before resuming the blocked task through the task workflow. Session reset and task resumption are separate operations.

When reporting the issue, include bot-group / Codex versions, Windows version, member configuration type, the complete error, and the read-only check results. If available, include the relevant time range from that member's `CODEX_HOME/.sandbox/sandbox.log`. Redact credentials and private paths before sharing. The specific source of the restriction remains unconfirmed from the words `blocked by policy` alone.

### A Windows Codex Bot cannot run tasks because its sandbox is not enabled

**Windows Codex Bots using restricted execution permissions need a working sandbox.** If it is not enabled or initialization fails, terminal commands may be rejected, preventing project reads, Git status checks, and task completion. Enable the Bot's sandbox first.

1. Open **that Bot's own `CODEX_HOME/config.toml`**. Independent Bots have separate configuration directories; also check any named configuration overrides.
2. Enable and initialize the Windows sandbox supported by the installed Codex version. For the legacy Windows sandbox, use the following setting; update the existing `[windows]` table when present:

   ```toml
   [windows]
   sandbox = "elevated"
   ```

   Save the file, start Codex with that Bot's configuration directory, and complete sandbox initialization when prompted. If elevated setup is unavailable and device policy permits it, `sandbox = "unelevated"` provides restricted execution with weaker network isolation. Supported versions and devices can also enable MXC using the [OpenAI Windows sandbox guide](https://developers.openai.com/codex/windows/windows-sandbox).
3. Wait until the Bot is idle, then select **Project label → Members & directories → Reset session** for that Bot. Verify `Get-Location` and `git status` before resuming blocked tasks.

The sandbox configuration notice at enrollment allows users to continue joining. When no implementation is explicitly configured, activation also depends on the Codex version and defaults; confirm it through initialization and command execution.

### Where do I assign Bot roles after creating a project?

**Click the current project label → Members & directories (成员与目录) → find the Bot → click Set role (设置角色) or its current role name → select a role.** A successful selection saves immediately and shows “本项目角色已保存”.

- **The required role is missing:** Open **Roles** (角色) in the same project configuration window, enter a name, and click **Create role** (创建角色). Return to **Members & directories** to assign it. If the member list is empty, use **Add member** (添加成员) first.
- **Configure role permissions:** Open **Project permissions** (项目权限), select the required members or roles, and click **Save permissions** (保存权限). Review existing template grants here; subsequent tool calls use the saved permissions.
- **Configure responsibilities and personal instructions:** Put project responsibilities in the project's `AGENTS.md`. For personal instructions, open **Members & directories → Edit** (编辑): Hermes uses `SOUL.md`, and Codex uses the member's own `AGENTS.md`. Click **Save** (保存) after editing. Personal instructions affect other projects that reuse the member.

Role assignments belong to each project, so the same Bot can hold different roles across projects. See [project configuration](docs/engineering/PROJECT-CONFIGURATION.md) and [member configuration editing](docs/engineering/PROFILE-EDITOR.md).
