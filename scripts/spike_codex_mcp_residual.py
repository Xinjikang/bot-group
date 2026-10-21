"""#155 补充验证：codex 挂 MCP 自然退出后，MCP 子进程（Python server.py）是否残留。

第九节 reaper 路径完整性 = 自然退出时「退出即清心跳」依赖 proc.wait() 返回后子进程全退。
spike 只验证了强杀（stop_tree 杀 6 进程），未验证自然退出时 MCP 子进程残留。本脚本补这块：
codex exec 挂一个最小 MCP（复用 isolated_database），自然跑完，观察 codex 退出后
MCP server.py 的 Python 进程是否残留。

--live 显式授权真实模型调用（消耗额度）。不碰生产 group.db。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time

CODEX = os.environ.get("CODEX_BIN") or shutil.which("codex.exe") or shutil.which("codex")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not CODEX:
        parser.error("Set CODEX_BIN or add the Codex executable to PATH")
    if not args.live:
        parser.error("--live explicitly authorizes real model calls")

    import psutil
    from scripts.unified_test_support import isolated_database

    mcp_python = sys.executable  # 系统 Python 3.10（装 mcp 依赖）

    with isolated_database() as path:
        mcp_env = {
            "GROUP_DB": path,
            "GROUP_ENGINE_URL": "http://127.0.0.1:1",  # 不真调 API，仅让 MCP 起得来
            "TASK_QUEUE_ROLE": "default",
            "HERMES_PROFILE": "default",
            "HERMES_CHAT_MODE": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        server = {
            "command": mcp_python,
            "args": [os.path.join(ROOT, "mcp", "server.py")],
            "env": mcp_env,
            "required": True,
            "startup_timeout_sec": 30,
            "tools": {"group_read_inbox": {"approval_mode": "approve"}},
            "enabled_tools": ["group_read_inbox"],
        }

        def toml(v):
            if isinstance(v, dict):
                return "{" + ",".join(json.dumps(k) + "=" + toml(x) for k, x in v.items()) + "}"
            return json.dumps(v)

        cmd = [CODEX, "exec", "--ignore-user-config", "--json",
               "--skip-git-repo-check",
               "-c", "mcp_servers.probe=" + toml(server),
               "Reply with exactly the single word OK. Do not use any tools."]

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace",
                                creationflags=CREATE_NO_WINDOW)

        # 记录 codex spawn 出来的 MCP Python 子进程（server.py）
        mcp_pids_seen = set()
        stop = threading.Event()

        def sampler():
            t0 = time.time()
            while not stop.is_set():
                try:
                    p = psutil.Process(proc.pid)
                    for c in p.children(recursive=True):
                        cmdline = ""
                        try:
                            cmdline = " ".join(c.cmdline())
                        except Exception:
                            pass
                        if "server.py" in cmdline or "python" in c.name().lower():
                            mcp_pids_seen.add((c.pid, c.name()))
                except psutil.NoSuchProcess:
                    pass
                time.sleep(0.1)

        th = threading.Thread(target=sampler, daemon=True)
        th.start()
        out, err = proc.communicate(timeout=180)
        stop.set()
        th.join(timeout=3)
        time.sleep(1.0)  # 主进程退出后留 1s 观察残留

        residual = []
        for pid, name in sorted(mcp_pids_seen):
            try:
                psutil.Process(pid)
                residual.append((pid, name))
            except psutil.NoSuchProcess:
                pass

        result = {
            "returncode": proc.returncode,
            "mcp_python_children_seen": sorted(mcp_pids_seen),
            "residual_after_codex_exit": residual,
            "thread_id": None,
        }
        for line in out.splitlines():
            if '"thread.started"' in line:
                try:
                    result["thread_id"] = json.loads(line).get("thread_id")
                except ValueError:
                    pass
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
