"""req#81 spike①（task#135）：验证 Hermes 提供可嵌入、线程安全调用的 new_session()/steer() 入口。

背景：群聊引擎的斜杠命令方案（见 docs/design/SLASH-COMMAND-DESIGN-2026-09-27.md）提出由
group-engine 提供 subprocess 适配入口，在 bot 进程内通过受控本地 IPC 接收纠偏后调用
agent 的原生 steer()。前提是 Hermes 提供「可嵌入 + 线程安全」的入口——本脚本隔离验证
这个前提。

约束（req#81）：不碰生产、不改 Hermes 源码、隔离 profile/db 跑。

验证点（对齐 req#81 六条硬证据的第 1 条）：
  1. AIAgent.steer / interrupt / redirect / run_conversation 入口可从独立进程导入
  2. SessionManager / HermesACPAgent.new_session 入口存在
  3. steer() 是纯内存队列操作（不 IO、不依赖 LLM provider）
  4. steer() 线程安全：多线程并发调用不丢数据（真实 _pending_steer_lock）
  5. interrupt()/redirect() 无 provider、无活跃模型请求时不崩（fail-safe）

运行（需 Hermes 源码的 Python 3.11 venv）：
    E:/home/admin/hermes/hermes-agent/venv/Scripts/python.exe \
        E:/Code/bot-group/scripts/test_hermes_embed_entry.py

本脚本依赖 Hermes 源码路径（硬编码 E:\\home\\admin\\hermes\\hermes-agent），不进
group-engine 的 unittest discovery（import 零副作用，仅 __main__ 时执行验证）。
"""
import os
import sys
import tempfile
import threading

_HERMES_SRC = r"E:\home\admin\hermes\hermes-agent"


def _isolate_home() -> str:
    """隔离 HERMES_HOME，绝不触碰生产 state.db / config / profile。"""
    iso = tempfile.mkdtemp(prefix="hermes_spike_iso_")
    os.environ["HERMES_HOME"] = iso
    return iso


def _import_entries():
    """延迟导入 Hermes 入口，返回 (AIAgent, SessionManager, HermesACPAgent, InterruptControlMixin,
    _set_defaults, _CONTROL_STATE)。"""
    sys.path.insert(0, _HERMES_SRC)
    from run_agent import AIAgent  # noqa: F401
    from acp_adapter.session import SessionManager  # noqa: F401
    from acp_adapter.server import HermesACPAgent  # noqa: F401
    from agent.interrupt_control import InterruptControlMixin  # noqa: F401
    from agent.agent_init import _set_defaults, _CONTROL_STATE  # noqa: F401
    return AIAgent, SessionManager, HermesACPAgent, InterruptControlMixin, _set_defaults, _CONTROL_STATE


class MinimalAgentMixin:
    """最小可调用对象：仅继承 InterruptControlMixin，无 LLM provider、无 SessionDB。

    用 Hermes 真实的 _CONTROL_STATE 初始化（含真实 threading.Lock），证明 steer 的锁机制
    独立于 provider/CLI/SessionDB，可纯内存线程安全调用。
    """


def _make_agent(InterruptControlMixin, _set_defaults, _CONTROL_STATE):
    class MinimalAgent(InterruptControlMixin):
        def __init__(self):
            _set_defaults(self, _CONTROL_STATE)
            self._tool_worker_threads = set()
            self._tool_worker_threads_lock = threading.Lock()
            self._active_children = []
            self._active_children_lock = threading.Lock()
            self._model_request_active = threading.Event()
            self._model_request_active.set()
            self.quiet_mode = True
            self._print_fn = None
            self._pending_redirect = None

    return MinimalAgent()


def main() -> int:
    iso = _isolate_home()
    AIAgent, SessionManager, HermesACPAgent, InterruptControlMixin, _set_defaults, _CONTROL_STATE = _import_entries()

    failures = []

    def check(label, cond, detail=""):
        status = "PASS" if cond else "FAIL"
        print(f"[{status}] {label}" + (f"  ({detail})" if detail else ""))
        if not cond:
            failures.append(label)

    # 1. 入口存在性（不构造，仅 import + getattr）
    check("AIAgent.steer 存在", callable(getattr(AIAgent, "steer", None)))
    check("AIAgent.interrupt 存在", callable(getattr(AIAgent, "interrupt", None)))
    check("AIAgent.redirect 存在", callable(getattr(AIAgent, "redirect", None)))
    check("AIAgent.run_conversation 存在", callable(getattr(AIAgent, "run_conversation", None)))
    check("SessionManager.create_session 存在", callable(getattr(SessionManager, "create_session", None)))
    check("HermesACPAgent.new_session 存在", callable(getattr(HermesACPAgent, "new_session", None)))

    # 2. steer 纯内存 + 累积拼接
    a = _make_agent(InterruptControlMixin, _set_defaults, _CONTROL_STATE)
    check("steer 空文本返回 False", a.steer("   ") is False and a.steer("") is False)
    a.steer("第一句")
    a.steer("第二句")
    check("steer 多次调用换行拼接", a._drain_pending_steer() == "第一句\n第二句")

    # 3. 线程安全：200 线程并发 steer 不丢数据（真实锁）
    a2 = _make_agent(InterruptControlMixin, _set_defaults, _CONTROL_STATE)
    N = 200
    threads = [threading.Thread(target=lambda i=i: a2.steer(f"msg{i}")) for i in range(N)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    drained = a2._drain_pending_steer() or ""
    n_lines = len([x for x in drained.split("\n") if x])
    check(f"并发 steer 无丢失（期望 {N} 行）", n_lines == N, f"实际 {n_lines} 行")

    # 4. interrupt 无 provider / 无活跃请求不崩
    a3 = _make_agent(InterruptControlMixin, _set_defaults, _CONTROL_STATE)
    a3._model_request_active.clear()
    a3._execution_thread_id = None
    try:
        a3.interrupt("stop please")
        check("interrupt 无 provider 不崩", True)
    except Exception as e:  # noqa: BLE001
        check("interrupt 无 provider 不崩", False, f"{type(e).__name__}: {e}")

    # 5. redirect 无活跃请求返回 False；有活跃请求置位 pending + 请求中断
    a4 = _make_agent(InterruptControlMixin, _set_defaults, _CONTROL_STATE)
    a4._model_request_active.clear()
    check("redirect 无活跃请求返回 False", a4.redirect("correction") is False)

    a5 = _make_agent(InterruptControlMixin, _set_defaults, _CONTROL_STATE)  # model_request_active 已 set
    check("redirect 活跃请求返回 True", a5.redirect("turn around") is True)
    check("redirect 置位 pending", a5._pending_redirect == "turn around")

    print("\n=== spike① 结论 ===")
    if failures:
        print("SOME FAIL:", ", ".join(failures))
        return 1
    print("ALL PASS：Hermes 提供可嵌入、线程安全调用的 steer/interrupt/redirect 入口")
    print("隔离 HERMES_HOME:", iso)
    return 0


if __name__ == "__main__":
    sys.exit(main())
