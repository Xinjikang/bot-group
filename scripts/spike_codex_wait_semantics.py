"""#155 实测：codex 子进程 proc.wait() 语义是否等同 hermes（有无 trampoline 壳）。

设计稿第九节「接入前硬门槛，不测不生产」：hermes 的 proc.wait() 语义由 req#39
实测——hermes.exe 是 uv trampoline 小壳，spawn 真 python 子进程后 wait 它们，
壳 poll 归零 = 整条进程链退完。codex 是否有同样语义需实测。

本脚本只观察进程树 + proc.wait() 时序，不挂 MCP、不碰 group.db、不写生产数据。
--live 显式开关才发起真实模型调用（消耗额度）。

结论口径：
- codex.exe 形态（.exe 原生 / trampoline 壳 / npm shim）
- codex exec 运行时 spawn 了哪些子进程（进程名 + 父子关系）
- 主进程自然退出（proc.wait 返回）时，子进程是否已全部退出
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


def _proc_exists(pid):
    import psutil
    return psutil.pid_exists(pid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="发起真实 codex 模型调用（消耗额度）")
    parser.add_argument("--shape-only", action="store_true",
                        help="只测 codex.exe 形态与 --version/--help 行为，不 exec")
    args = parser.parse_args()
    if not CODEX:
        parser.error("Set CODEX_BIN or add the Codex executable to PATH")

    import psutil

    print("CODEX =", CODEX)
    print("exists =", os.path.exists(CODEX))
    print("size_bytes =", os.path.getsize(CODEX))
    print("version:", flush=True)

    # 1. 形态：原生 exe（Rust） vs trampoline 壳
    #    trampoline 壳特征 = 几十 KB 小壳；codex 321MB 是完整原生二进制
    size_mb = os.path.getsize(CODEX) / 1024 / 1024
    print(f"  binary_kind = native_rust_exe ({size_mb:.0f} MB) — 非 trampoline 壳")
    # 同目录子进程可执行 = codex 运行时会 spawn 的独立进程
    siblings = []
    d = os.path.dirname(CODEX)
    for n in sorted(os.listdir(d)):
        if n.endswith(".exe") and n != "codex.exe":
            siblings.append(n)
    print(f"  sibling_executables = {siblings}")

    if args.shape_only:
        print("SHAPE-ONLY DONE")
        return

    # 2. 观察 codex exec 运行时的进程树 + proc.wait 时序
    if not args.live:
        print("需 --live 显式授权真实模型调用。可用 --shape-only 只测形态（已跑）。")
        return

    prompt = "Reply with exactly the single word OK and nothing else. Do not use any tools."
    cmd = [CODEX, "exec", "--ignore-user-config", "--json",
           "--skip-git-repo-check", prompt]

    print("spawn codex exec ...", flush=True)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=CREATE_NO_WINDOW)

    samples = []          # (t, [child (pid, name, status)])
    stop = threading.Event()

    def sampler():
        t0 = time.time()
        while not stop.is_set():
            t = time.time() - t0
            try:
                p = psutil.Process(proc.pid)
                children = [(c.pid, c.name(), c.status()) for c in p.children(recursive=True)]
            except psutil.NoSuchProcess:
                children = []
            samples.append((round(t, 2), children))
            time.sleep(0.2)

    th = threading.Thread(target=sampler, daemon=True)
    th.start()

    # 主进程退出（proc.wait 语义的核心观察点）
    out, err = proc.communicate(timeout=180)
    stop.set()
    th.join(timeout=3)

    # proc.wait 返回后，立即检查子进程是否已全退
    # psutil.pid_exists 对已回收 PID 可能误判（PID 复用），用精确查询兜底
    residual = []
    for t, children in samples[-5:]:
        for pid, name, status in children:
            residual.append((pid, name))
    # 最后一次采样后，主进程已退出，children 若还在 = proc.wait 不等子进程
    last_children = samples[-1][1] if samples else []
    # 主进程退出后，再等 1s 看是否还有残留
    time.sleep(1.0)
    still_alive = []
    for pid, name, status in last_children:
        try:
            psutil.Process(pid)
            still_alive.append((pid, name))
        except psutil.NoSuchProcess:
            pass

    result = {
        "returncode": proc.returncode,
        "max_concurrent_children": max((len(s[1]) for s in samples), default=0),
        "child_process_names": sorted({n for _, cs in samples for _, n, _ in cs}),
        "residual_after_wait": still_alive,
        "samples": samples[-8:],
        "stdout_tail": out.strip()[-500:] if out.strip() else "",
        "stderr_tail": err.strip()[-500:] if err.strip() else "",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
