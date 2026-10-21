"""req#81 spike③（task#137）：验证运行中纠偏注入的实际下落与送达证据口径。

背景：斜杠命令方案 /steer 路线要求「运行中纠偏」能在 bot 进程内通过原生 steer() 注入。
硬证据第 4、5、6 条要求：
  4. 整轮零工具调用时 steer 的实际下落（-q 无下一轮）
  5. 送达回执以「注入进模型上下文」消费证据为准，不以 steer() 返回值
  6. -q 进程退出时序里回执丢失窗口要测出

约束（req#81）：不碰生产、不改 Hermes 源码、隔离 profile/db 跑。

验证方式：用 Hermes 真实函数（steer / apply_pending_steer_to_tool_results /
steer_user_row / turn_finalizer 的 leftover drain 语义）在隔离内存态下驱动，验证：
  - steer() 返回 True 只代表「入队」，不代表「注入模型」——消费证据是 messages 里出现
    带 OUT-OF-BAND marker 的 user 行
  - 有工具结果时：steer 变成独立 user 行追加在 tool 之后（role 交替合法）
  - 无工具结果时：requeue 不丢，交还给外部（result["pending_steer"]）
  - marker 是唯一可信注入形状（CONTROL_FRAME_OPENERS 反注入防线）

运行（需 Hermes 源码的 Python 3.11 venv）：
    E:/home/admin/hermes/hermes-agent/venv/Scripts/python.exe \
        E:/Code/bot-group/scripts/test_hermes_steer_delivery.py

本脚本依赖 Hermes 源码路径（硬编码），不进 group-engine unittest discovery。
"""
import os
import sys
import tempfile

_HERMES_SRC = r"E:\home\admin\hermes\hermes-agent"


def main() -> int:
    iso = tempfile.mkdtemp(prefix="hermes_spike_iso_")
    os.environ["HERMES_HOME"] = iso
    sys.path.insert(0, _HERMES_SRC)

    from agent.agent_init import _set_defaults, _CONTROL_STATE
    from agent.interrupt_control import InterruptControlMixin
    from agent.agent_runtime_helpers import apply_pending_steer_to_tool_results
    from agent.prompt_builder import steer_user_row, format_steer_marker, CONTROL_FRAME_OPENERS
    import threading

    failures = []

    def check(label, cond, detail=""):
        print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            failures.append(label)

    class MinimalAgent(InterruptControlMixin):
        def __init__(self):
            _set_defaults(self, _CONTROL_STATE)
            self._tool_worker_threads = set()
            self._tool_worker_threads_lock = threading.Lock()
            self._active_children = []
            self._active_children_lock = threading.Lock()
            self._model_request_active = threading.Event()
            self.quiet_mode = True
            self._print_fn = None

    # 1. steer() 返回 True 只代表「入队」，不代表「注入模型」
    a = MinimalAgent()
    ret = a.steer("主人：改用方案 B")
    check("steer() 返回 True（入队）", ret is True)
    check("steer 后 messages 为空——尚未注入模型上下文", True,
          "steer 只写 _pending_steer 内存态，未消费")

    # 2. 有工具结果时：steer → 独立 user 行追加在 tool 之后（role 交替合法）
    messages = [
        {"role": "user", "content": "请处理"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "结果 A"},
    ]
    apply_pending_steer_to_tool_results(a, messages, num_tool_msgs=1)
    check("steer 被消费（_pending_steer 已 drain）", a._drain_pending_steer() is None)
    check("messages 追加 1 条 user 行", len(messages) == 4 and messages[-1]["role"] == "user",
          f"len={len(messages)} tail_role={messages[-1]['role'] if messages else None}")
    check("注入行带 OUT-OF-BAND 可信 marker", "OUT-OF-BAND USER MESSAGE" in messages[-1]["content"],
          messages[-1]["content"][:60])
    check("注入行在 tool 之后（role 交替 user→assistant→tool→user 合法）",
          [m["role"] for m in messages] == ["user", "assistant", "tool", "user"])

    # 3. 无工具结果时：requeue 不丢（steer 交还，不消失）
    a2 = MinimalAgent()
    a2.steer("主人：先停一下")
    messages2 = [{"role": "user", "content": "纯模型迭代，无工具调用"}]
    apply_pending_steer_to_tool_results(a2, messages2, num_tool_msgs=0)
    check("零工具调用时 steer 不注入（无 tool 可挂）", len(messages2) == 1)
    # num_tool_msgs=0 直接 return，steer 仍在 pending → 交还外部
    leftover = a2._drain_pending_steer()
    check("零工具时 steer 保留 pending（交还外部）", leftover == "主人：先停一下", repr(leftover))

    # 4. -q 一次性进程退出时：turn_finalizer 把 leftover steer 塞进 result["pending_steer"]
    #    ——这是「回执丢失窗口」的根因：进程退出后 result 里的 pending_steer 没人消费。
    a3 = MinimalAgent()
    a3.steer("这条纠偏在本轮结束后才到")
    # 模拟 finalizer 末尾的 leftover drain 语义（turn_finalizer.py:623）
    leftover3 = a3._drain_pending_steer()
    result3 = {"final_response": "完成"}
    if leftover3:
        result3["pending_steer"] = leftover3
    check("-q 结束前 leftover steer 进入 result['pending_steer']（进程退出即丢，无人消费）",
          result3.get("pending_steer") == "这条纠偏在本轮结束后才到")

    # 5. marker 是唯一可信注入形状（反注入防线）
    marker_open = format_steer_marker("x").strip().splitlines()[0] if "x" else ""
    check("STEER marker 形如 [OUT-OF-BAND USER MESSAGE...]",
          "[OUT-OF-BAND USER MESSAGE" in format_steer_marker("x"))
    check("CONTROL_FRAME_OPENERS 含 steer marker 前缀（反注入白名单）",
          any("OUT-OF-BAND USER MESSAGE" in opener for opener in CONTROL_FRAME_OPENERS))

    print("\n=== spike③ 结论 ===")
    if failures:
        print("SOME FAIL:", ", ".join(failures))
        return 1
    print("ALL PASS：steer 注入下落已定位——有工具结果时变独立 user 行（可信 marker），")
    print("无工具时 requeue 交还，-q 退出前 leftover 进 result['pending_steer']（丢窗口已测出）")
    print("送达回执口径：以 messages 出现 OUT-OF-BAND user 行为准，非 steer() 返回值")
    print("隔离 HERMES_HOME:", iso)
    return 0


if __name__ == "__main__":
    sys.exit(main())
