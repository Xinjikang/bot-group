# 清晨首条消息卡顿探针方案（2026-11-12）

## 背景与目标

2026-11-12 08:29:57-08:30:20 引擎首条消息 POST /msg 总耗时 22.4s，分解为取连接 19.3s、消息路由 3.1s、写锁 0.5s。慢窗口与 Codex 自更新会话（RestartManager 10000/10001/10002，08:29:53-08:30:32）逐秒重叠，且为 11-10/11-11/11-12 三天中唯一一次（其余两早晨首条消息 56/112ms）。同盘 IO 吞吐饱和受控实验未复现（取连接 p50=1.1ms，详见文末实验记录）。机制未证，候选：文件打开被过滤驱动阻塞、CPU 饥饿、系统级锁、随机 IO 风暴或队列深度异常。

目标：在早晨窗口采集现场证据，定位 19.3s 落在取连接路径的哪一步及同期系统状态；不改变首条消息的发生条件。

## 观测对象与数据源

### 真实发送请求的观测（引擎现有日志，零改动）

engine.log 已包含真实请求的分段计时，可直接作为探针的「真实侧」数据源：

- message.send 行：connect_ms（取连接总耗时）/ route_ms / publish_ms / total_ms；
- message.persist 行：lock_ms / prepare_ms / enqueue_ms / commit_ms / total_ms；
- 当日首条消息判据：当天第一条 message.persist 的 total_ms 与时间戳；
- 墙钟对齐：日志时间戳与探测进程采样记录使用同一机器时钟。

真实请求取连接内部四段的分解（.env 读 / makedirs / connect / PRAGMA）现有日志没有，取得方式见下节三档方案。

### 探测进程的观测（独立进程，不改引擎代码）

- 磁盘指标：typeperf 采集 `\LogicalDisk(E:)\Current Disk Queue Length` 与 `\LogicalDisk(E:)\Avg. Disk sec/Transfer`，每 10s 一次；
- 进程快照：每 30s 一次，采集 CPU 占用与 IO 字节率（`\Process(*)\% Processor Time`、`\Process(*)\IO Data Bytes/sec`；权限不足时降级为仅对引擎 PID 做 Win32_Process 的 UserModeTime/KernelModeTime 增量采样——进程级 CPU 卡顿的直接观测）；
- 事件日志增量：每 300s 拉 Application 日志新增事件（RestartManager、AppX 603/400/613、Windows Defender、WER），只记时间戳与 EventID，不落正文。

### 取连接四段计时的取得方式（三档，按需升级）

1. 首选（零预热）：不改引擎。探测进程第一轮不触碰 .env 与 group.db，只采系统指标与事件日志。若现场复现时引擎 connect_ms 异常且系统指标可归因（队列长度持续高位、某进程 IO 突增、RM/AppX 事件同步出现），即完成归因，无需四段分解。
2. 次选（有预热）：探测进程复刻 db_path 同款代码路径（engine/config.py 的 db_path → data_dir → _dotenv_data_dir → sqlite3.connect → 两条 PRAGMA），对四段分别 perf_counter 计时，每周期一次。注意：这会预热 .env 与 group.db 文件头的系统页缓存，可能改变首条消息条件，只能作为第一轮拿到现场后的补充取证。
3. 末选（改代码）：在 engine/db.py get_connection 内加四段 perf_counter 并打进 message.send 日志。需改引擎代码并重启；重启会重置 .env 解析缓存、连接池与页缓存状态，观测条件被污染，仅在无法归因时采用，且报告必须注明重启时间点。

## 采样参数

- 采样窗口：每日 08:00-09:30（覆盖 08:30 现场及其前后）；手动单次运行时由调用方指定窗口。
- 采样周期：磁盘计数器 10s、进程快照 30s、事件日志增量 300s。
- 权限要求：LogicalDisk/PhysicalDisk 性能计数器普通用户可读；Process 计数器部分实例需管理员权限，不足时按上节降级方案处理，并在输出中注明实际权限档位。
- 输出目录：`E:\Code\bot-group\reports\research\2026-11-12-morning-probe\`，按日期分文件；目录创建被系统拒绝时退回 TMPDIR（E:\home\admin\hermes\profiles\architect\cache\scratch）并在报告注明实际路径。
- 容量上限：单文件 10MB 轮转，每日最多 3 个文件；目录总上限 100MB，超限删最旧文件。
- 失败与清理：脚本启动即写运行标记与启动时刻；正常结束或异常退出均落 exit/error 日志；退出后不留常驻进程与临时文件（临时文件用完即删）。

## 低干扰对照设计

- 对照组：engine.log 中 11-10/11-11 早晨首条消息耗时（56/112ms）作为正常基线。
- 第一轮探测进程零触碰业务文件（.env、group.db 均不读），确保不预热、不改变首条消息发生条件；探测进程只写自己的输出文件。
- 每次运行记录「探测进程启动时刻」与「当日首条消息时刻」的先后关系，用于事后排除预热干扰。
- 探测进程以只读方式访问 group.db（若进入次选档）时使用 `file:...?mode=ro`，不写 WAL。

## 部署方式

- 手动运行：单次或临时窗口运行探测脚本，无持久配置，随时可做。
- 定时任务：每日 08:00 自动启动属持久系统配置，按 AGENTS.md 修改前同意要求，在方案经主人确认后单独落，不与探测脚本本身捆绑。

## 本轮负载实验记录（2026-11-12，architect 执行）

- 命令：execute_code 会话内 Python 脚本（sqlite3 标准库）：先 30 次基线取连接（`file:E:/Code/bot-group/data/group.db?mode=ro` + 2 PRAGMA + SELECT sqlite_master）；再在线程中向 E 盘 scratch 连续写 480MB（8MB×60 块），期间循环取连接；最后在线程中循环读 E:\home\admin\codex\.sandbox-bin\codex.exe（373MB，4MB 块），持续 20s 期间循环取连接。
- 原始结果：baseline n=30 min=0.9ms p50=1.0ms p90=1.2ms max=3.3ms；写饱和 n=122 min=0.9ms p50=1.1ms p90=1.5ms max=26.2ms；读饱和 n=16885 min=0.9ms p50=1.1ms p90=1.5ms max=8.4ms。
- 清理：临时文件 .io-probe-w.tmp 写入完成后已删除（leftover 检查为 False），实验线程随脚本结束，无残留进程。
- 结论：同盘顺序吞吐饱和不产生 19s 级取连接延迟；未覆盖随机 IO 风暴与队列深度场景，为实验缺口。
