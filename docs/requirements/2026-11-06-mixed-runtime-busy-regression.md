# Codex/DSH busy 跳过与恢复回归

## 问题与范围

`scripts/test_native_busy_scheduling.py` 的 `NativeBusySchedulingTests.test_mixed_runtime_codex_dsh_busy_skip_and_resume` 在 Codex 与 DSH 被夹具标记为 busy 后，预期仅空闲 Hermes 成员获得投递，第二次 `queue.process_one(self.conn)` 返回 false。当前第二次调用返回 true，第 408 行触发 `AssertionError: True is not false`。

受影响基线包含 `fc1a33d83db3d064dac43d1a5fbe8e7ef458c865` 和 `8b6f184dd1e516f0a268009fa3eac651a319f130`。问题独立于话题等待气泡与 HUD 幂等的交付范围，修复范围由测试夹具与运行时进程识别的根因调查确定。生产影响待定位。

## 复现方法

在对应提交的独立源码目录安装项目依赖，使用项目 Python 解释器，清空外来 `PYTHONPATH` 后执行：

```text
python -X utf8 -B -m scripts.run_isolated_tests --report <当前源码目录下 reports/tests 中的新绝对目录> scripts.test_native_busy_scheduling.NativeBusySchedulingTests.test_mixed_runtime_codex_dsh_busy_skip_and_resume
```

运行器创建隔离数据库与临时运行目录。分别保留两个基线的测试输出与 `isolation.json`。复现结果为同一断言失败。

## 定位与验收

1. 确认第二次调度实际放行的成员及 busy 判断链路，核对夹具是否准确模拟当前 Codex/DSH 进程识别契约。
2. 按根因修复夹具或实现，保留忙碌成员跳过、空闲成员推进、解除占用后按既有顺序恢复的断言。
3. 单用例和 `scripts.test_native_busy_scheduling` 模块全部通过，按实际根因补充必要的相邻回归。
4. 保持 DFS 发言顺序、调度互斥、会话归属及保守处理未知进程状态的契约。
5. 记录根因、生产影响、修复提交与隔离测试证据；审查与验收结论按项目流程写入任务节点。
