# 兼容初始化接口与文件恢复

本文记录保留的初始化后端契约。当前页面使用 [七步向导](PROJECT-SETUP-WIZARD-2026-10-14.md)，旧初始化残留的清理及 discarded 门禁见 [清理与迁移](PROJECT-SETUP-CLEANUP-2026-10-15.md)。

新建入口提供目录、名称、简介与工作目录模式，预览三份基础文件后显式确认创建。文件包括 `AGENTS.md`、`README.md`、`docs/INDEX.md`，文档相互链接。空白成员、项目角色、Bot 授权及负责人均为空；页面显示文件就绪和后续团队配置说明。旧 `POST /projects` 继续纯登记，返回 `{project, root}`。

## 接口契约

全部初始化接口使用现有管理员身份校验。根目录为本机普通绝对路径；父目录须存在。新建支持既有空目录和父目录下的新一级目录。worktree 模式沿用现有 Git 仓库根、可解析 HEAD、工作树登记与独立 worktree 根目录校验；用户先准备含空提交的空白仓库，其 `.git` 元数据允许存在。

`POST /projects/initializations/preview`：

| 字段 | 类型 / 必填 | 示例与含义 |
|---|---|---|
| mode | string / 可省略，默认 new | `new` 或 `supplement` |
| root | string / new 必填 | `E:/Code/my-project`；supplement 从登记项目读取 |
| display_name | string / new 必填 | `我的项目`，1–200 字 |
| description | string / 可省略 | `协作试验`，默认空 |
| directory_mode | string / 可省略，默认 shared | `shared` / `worktree` |
| worktree_root | string / worktree 必填 | `E:/Worktrees/my-project`，须与项目主目录分开 |
| project | string / supplement 必填 | `p_…`，已有稳定 ID |
| replace_agents | boolean / 可省略，默认 false | `true` 明确备份后替换既有 AGENTS.md，仅 supplement 使用 |

预览读取目录和文件，数据库保存初始化意图、暂存文件内容与指纹。选定目录的文件保持原样。返回计划中的 create / preserve / replace 和全文；preserve 返回原文件摘要及 `rules_hint`。补文件服务从已登记项目读取配置，保持成员、角色、授权、负责人。

`GET /projects/initializations/{initialization_id}` 查询持久化进度；`POST …/{id}/submit` 和 `POST …/{id}/retry` 使用相同执行服务，body 仅包含必填 `revision: string`（预览返回的 SHA-256）。初始化 ID 为路径必填 string，例如 `d960…`。同 ID 完成后重复请求返回同一项目与进度；预览阶段已保留稳定的 planned_project，配置重试继续使用该 ID。

响应关键字段：

| 字段 | 类型 | 示例 |
|---|---|---|
| initialization_id / revision / planned_project / project | string | 初始化 ID、确认摘要、计划项目 ID、已提交项目 ID（提交前 project 为空） |
| root / mode / phase / status | string | `E:/Code/my-project` / `new` / `publishing` / `failed` |
| files | object[] | `{path:"AGENTS.md",action:"create",before_hash:null,sha256:"…",content:"…",backup:"",completed:false}`；before_hash 为 string/null，其余文本 string，completed 为 boolean |
| uncompleted | string[] | `["README.md","docs/INDEX.md"]` |
| error | object | `{code:"file_conflict",message:"文件与已确认计划冲突…"}`；无错误为 `{}` |
| files_ready / team_configured | boolean | `true` / `false`；team_configured 表示存在启用成员及授权，执行继续按具体权限校验 |
| team_hint / rules_hint | string | 后续团队配置说明、保留现有规则说明 |

格式/字段错误为 HTTP 400，权限 403，记录缺失 404，预览冲突与确认版本错误 409，detail 为文本或 `{code,message}`。提交中可恢复失败返回 HTTP 200 + `status:failed`，保留阶段、原因和清单；客户端按 status 展示重试。目录内容需导入时 code 为 `import_required`，页面提供导入入口并带入路径。持久化通道本身故障仍按 HTTP 服务错误处理。

## 持久化与恢复

新增 `project_initializations` 表保存 ID、稳定项目关联、根目录、phase、status、plan JSON、revision、completed JSON、error JSON、actor 和时间。计划在 SQLite 暂存三份 UTF-8 内容、前后 SHA-256、相对路径、动作与备份路径，以及目录身份（device/inode）。确认 revision 保持固定，发布期间仅补记目录身份和完成进度。

阶段依次为 `preview → configured → publishing → verifying → ready`。status 为 pending / initializing / failed / ready；失败保留发生阶段。配置事务使用同一 conn 的 `BEGIN IMMEDIATE`，SQLite 默认隔离（read_uncommitted=0 / SERIALIZABLE，WAL 读快照），整体提交项目、空授权、空负责人、审计与初始化阶段。复用 taskpool 的纯登记事务服务、permissions 的事务保存和既有 workspace 校验；角色种子一次性门栏保持原样。配置异常整笔回滚，独立的意图记录保存错误便于重试。

配置提交后 projects.readiness_status 为 initializing。逐文件在同目录创建确定性暂存文件并 fsync，以硬链接发布新文件（目标已存在时原子拒绝），替换使用原内容校验、独立备份及 os.replace。所有文件最终校验后，在一笔事务中将项目和初始化记录标记 ready。文件系统发布分阶段持久化，数据库负责记录可恢复进度。

重试重新校验登记配置、真实目录和原文件指纹。现有文件等于计划输出摘要时补记完成，覆盖“文件发布成功、完成日志尚未写入”的中断点；原内容匹配才执行计划动作，人工变化进入 file_conflict，目录身份变化进入 path_changed，运行配置变化进入 configuration_changed。暂存文件变化也保留并提示冲突。失败项目保持非 ready，已发布内容保留，清理只涉及初始化 ID 对应且完整摘要吻合的暂存文件。

当前引擎为单进程，initializer 使用既有 workspace LOCK 串行执行，并与实际 spawn/configuration 协调。多引擎实例同时处理同一初始化的分布式租约属于后续扩展。文件发布需要文件系统支持同目录硬链接；NTFS 实测通过，能力不足时返回 filesystem_error，保留清单供恢复。路径中的 `..`、符号链接、Windows junction / reparse point 在预览、恢复和每次文件操作前检查并拒绝；UNC / 扩展前缀初始化返回明确边界提示。

## 就绪检查

`project_readiness` 统一判断四态。需求创建、任务创建和领取在 BEGIN IMMEDIATE 内检查；扫描需求、任务、inbox、话题静默、轮次推进前检查；唤醒入队和队列领取的原子 SQL 再检查 ready；实际 spawn 在工作目录选择、规则同步和进程创建前检查。非 ready 返回 project_not_ready 或暂缓派发，存量 queued 任务保持等待。初始化期间话题扫描保留 pending 和发言位。

## 验证与生效

- [后端测试](../../scripts/test_project_initialization.py)：18 项隔离 HTTP/SQLite/文件系统测试，覆盖三文件与链接、空团队、同 ID 并发、DB 深层回滚、部分发布、发布后日志前中断、人工冲突、权限错误、暂存变更、补文件与备份、真实 Windows junction 两处逃逸、worktree Git 校验、旧接口、管理员及 revision、目录配置变化、各入口和队列领取竞态。
- 运行时证据记录文件 SHA-256、阶段与恢复结果，输出到主目录测试报告。中断通过 BaseException 故障注入，目标目录和数据库均为临时资源。
- 历史浏览器脚本 `test_project_initialization_browser.py` 已随向导演进移除。当时 Chromium 接真实隔离 API 验证全文展示、导入转接、返回、双击、重试及恢复；现行入口与覆盖范围见 [七步向导](PROJECT-SETUP-WIZARD-2026-10-14.md) 和 [test_project_setup_browser.py](../../scripts/test_project_setup_browser.py)。
- 异常退出和 PID 用例将 HERMES_HOME 指向测试临时目录，保证宿主隔离；具体运行结果及跳过条件按次记录。

部署引擎更新后，管理员手动重启引擎并刷新前端。
