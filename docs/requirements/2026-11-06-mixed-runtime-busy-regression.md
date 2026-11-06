# Codex/DSH busy 跳过与恢复回归

## 问题与范围

`scripts/test_native_busy_scheduling.py` 的 `NativeBusySchedulingTests.test_mixed_runtime_codex_dsh_busy_skip_and_resume` 在 Codex 与 DSH 被夹具标记为 busy 后，预期仅空闲 Hermes 成员获得投递，第二次 `queue.process_one(self.conn)` 返回 false。修复前第二次调用返回 true，第 408 行触发 `AssertionError: True is not false`。

受影响基线包含 `fc1a33d83db3d064dac43d1a5fbe8e7ef458c865` 和 `8b6f184dd1e516f0a268009fa3eac651a319f130`。

## 根因（2026-11-06 已定位并修复）

根因是测试夹具过时，不是调度器缺陷。夹具在 `test_native_busy_scheduling.py:401-403` 模拟 DSH 忙进程的命令行缺少生产命令必带的 `--json`（`engine/native_runtime.py:76`）；`wake._matches_dsh_execution`（`engine/wake.py:265`）要求 `--json` 才认可进程身份，因此模拟进程未被识别为忙碌，被当作空闲先投递了它自己的任务，随后 Hermes 空闲成员才获得投递，触发第二次 `process_one` 返回 true 的断言失败。补上 `--json` 后同一用例通过：仅 Hermes 空闲成员获得投递，Codex/DSH 任务保持 queued，解除占用后按原顺序恢复。

生产影响：生产 DSH 命令行始终携带 `--json`，忙碌识别链路不受影响，未发现生产调度缺陷。调度器文件在本问题引入前后无相关改动。

修复内容：测试夹具补 `--json`（一行）；本文档根因与验收同步更新。

## 复现方法

在对应提交的独立源码目录安装项目依赖，使用项目 Python 解释器，清空外来 `PYTHONPATH` 后执行：

```text
python -X utf8 -B -m scripts.run_isolated_tests --report <当前源码目录下 reports/tests 中的新绝对目录> scripts.test_native_busy_scheduling.NativeBusySchedulingTests.test_mixed_runtime_codex_dsh_busy_skip_and_resume
```

运行器创建隔离数据库与临时运行目录。分别保留两个基线的测试输出与 `isolation.json`。复现结果为同一断言失败。

## 定位与验收

1. 根因已确认：夹具缺少 `--json` 导致 DSH 忙碌识别失败（见「根因」）。保留忙碌成员跳过、空闲成员推进、解除占用后按既有顺序恢复的断言。
2. 单用例和 `scripts.test_native_busy_scheduling` 模块全部通过，并按根因跑相邻回归。
3. 保持 DFS 发言顺序、调度互斥、会话归属及保守处理未知进程状态的契约。
4. 记录根因、生产影响、修复提交与隔离测试证据；审查与验收结论按项目流程写入任务节点。
