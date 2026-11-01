"""req#138 task#271：DSH Auto 三层补验（隔离夹具 + 真实 DSH 直接运行）。

三层固定（docs/design/2026-10-31-dsh-integration.md「Auto 三层补验」）：
  1. 审批层：review deny 后进 approval/request 瀑布——无 answerer unavailable、answerer rejected。
  2. 审查层：隔离夹具拦截 llm/stream 瀑布，识别 Auto 审查请求（system 以 REVIEW_POLICY 开头）
     返回固定 allow/deny/异常，普通请求透传，记录命中次数。
  3. 取证层：marker MCP 工具在执行主体写文件 marker（工具主体进入的独立副作用证据），
     allow 阳性对照写入、deny/异常/阻断路径不写入。

分段（每段独立临时目录 + 真实 DSH + 账号凭据副本）：
  --segment review-allow       审查层 allow：工具执行 + marker 写入（阳性对照）
  --segment review-deny        审查层 deny + 无 answerer：工具不执行 + marker 缺失（unavailable）
  --segment review-rejected    审查层 deny + answerer rejected：工具不执行 + marker 缺失
  --segment review-exception   审查层异常（fixture throw）：工具不执行 + marker 缺失
  --segment review-abort       审查层 aborted finish：工具不执行 + marker 缺失
  --segment adapter-block      F2 失效阻断：卸载夹具把会话迁到 danger-full-access，适配插件
                               tools/pre-execute current≠auto 阻断（BLOCK 证据 + marker 缺失）

运行：python -X utf8 -B scripts/verify_dsh_auto_layers.py --entry <dsh.cmd绝对路径> \
  --source-home <源DSH_HOME> --test-directory <分配隔离目录> --evidence <绝对证据路径> \
  [--segment <name>|all]
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import dsh_auth, dsh_auto  # noqa: E402

SEGMENTS = ("review-allow", "review-deny", "review-rejected",
            "review-exception", "review-abort", "adapter-block")

# 审查层夹具：拦截 llm/stream，识别 Auto 审查请求（system 以 REVIEW_POLICY 开头），
# 返回固定决策流（text-delta + finish stop）；异常时 throw，abort 时 finish aborted。
REVIEW_FIXTURE = """export const name = "review-fixture";
export const inject = ["llm"];
export function apply(ctx) {
    let reviewCount = 0;
    ctx.on("llm/stream", (options, fallback) => {
        const sys = options && options.system;
        if (typeof sys === "string" && sys.startsWith("REVIEW_POLICY")) {
            reviewCount += 1;
            const decision = process.env.REVIEW_DECISION || "allow";
            console.error(`[review-fixture] review #${reviewCount} decision=${decision}`);
            if (decision === "throw") {
                throw new Error("review-fixture synthetic reviewer error");
            }
            return (async function* () {
                if (decision === "abort") {
                    yield { type: "finish", reason: { kind: "aborted",
                        failure: { code: "ABORTED", message: "fixture abort" } } };
                    return;
                }
                const text = decision === "deny"
                    ? JSON.stringify({ risk: "high", decision: "deny", reason: "fixture deny" })
                    : JSON.stringify({ risk: "low", decision: "allow" });
                yield { type: "text-delta", index: 0, text };
                yield { type: "finish", reason: { kind: "stop" } };
            })();
        }
        return fallback();
    });
}
"""

# 审批层夹具：拦截 approval/request 瀑布，按 APPROVAL_OUTCOME 返回固定结果。
APPROVAL_FIXTURE = """export const name = "approval-fixture";
export const inject = ["approval"];
export function apply(ctx) {
    ctx.on("approval/request", (req, fallback) => {
        const outcome = process.env.APPROVAL_OUTCOME || "unavailable";
        console.error(`[approval-fixture] request tool=${req && req.toolName} outcome=${outcome}`);
        if (outcome === "rejected") return "rejected";
        if (outcome === "allowed-once") return "allowed-once";
        return fallback();
    });
}
"""

# F2 失效夹具：session/created 把会话迁到 danger-full-access（模拟插件卸载迁移），
# 使适配插件 tools/pre-execute 观测到 current≠auto → 阻断。
UNLOAD_FIXTURE = """export const name = "unload-fixture";
export const inject = ["permissionPresets"];
export function apply(ctx) {
    ctx.on("session/created", (session) => {
        ctx.permissionPresets.set(session, "danger-full-access");
        console.error("[unload-fixture] session migrated to danger-full-access");
    });
}
"""

# 取证层：marker MCP 工具（执行主体写文件 marker）。
MARKER_PROBE = r"""const readline = require('node:readline');
const fs = require('node:fs');
const input = readline.createInterface({ input: process.stdin });
input.on('line', line => {
  const m = JSON.parse(line);
  if (m.id === undefined) return;
  let result;
  if (m.method === 'initialize') {
    result = { protocolVersion: '2024-11-05', capabilities: { tools: {} }, serverInfo: { name: 'marker-probe', version: '1' } };
  } else if (m.method === 'ping') {
    result = {};
  } else if (m.method === 'tools/list') {
    result = { tools: [{ name: 'probe', description: 'Write a side-effect marker on execution.', inputSchema: { type: 'object', properties: { text: { type: 'string' } }, required: ['text'] } }] };
  } else if (m.method === 'tools/call') {
    try {
      fs.writeFileSync(process.env.MARKER_FILE, JSON.stringify({ text: m.params.arguments.text, pid: process.pid, ts: Date.now() }));
      result = { content: [{ type: 'text', text: 'marker-written' }] };
    } catch (e) {
      result = { isError: true, content: [{ type: 'text', text: 'marker-failed: ' + e.message }] };
    }
  } else {
    process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: m.id, error: { code: -32601, message: 'Unknown method' } }) + '\n');
    return;
  }
  process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: m.id, result }) + '\n');
});
"""


def _free_env_keys(env):
    for k in ("DEEPSEEK_API_KEY", "DSH_PROFILE", "DSH_BIN", "REVIEW_DECISION",
              "APPROVAL_OUTCOME", "MARKER_FILE"):
        env.pop(k, None)


def _read_profile_selection(source_home: Path) -> list:
    import yaml
    class CR(yaml.SafeLoader):
        pass
    CR.add_constructor('tag:yaml.org,2002:js', lambda l, n: '[expression]')
    patch = source_home / "profiles" / "desktop" / "cordis.patch.yml"
    rows = yaml.load(patch.read_text(encoding="utf-8"), Loader=CR)
    return [r for r in rows if r.get("id") in
            ("agent-default-model", "llm-deepseek", "llm-pi-ai", "deepseek-account")]


def _run(entry: Path, source_home: Path, test_dir: Path, *, decision: str = "allow",
         outcome: str = None, unload: bool = False) -> dict:
    """一次真实 DSH 运行，返回结构化证据。"""
    node = r"D:\nodejs\node.exe"
    private = Path(tempfile.mkdtemp(prefix="layers_", dir=test_dir))
    home = Path(tempfile.mkdtemp(prefix="layers_home_", dir=test_dir))
    (home / "profiles" / "desktop").mkdir(parents=True)
    identity = f'{os.environ.get("USERDOMAIN", "")}\\{os.environ.get("USERNAME", "")}'
    subprocess.run(["icacls", str(private), "/inheritance:r", "/grant:r",
                    identity + ":(OI)(CI)F"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    cred = dsh_auth.copy_credentials(str(source_home), str(private))
    sel = _read_profile_selection(source_home)

    adapter = private / "auto-enable-adapter.mjs"
    adapter.write_text(dsh_auto.adapter_source(), encoding="utf-8")
    review = private / "review-fixture.mjs"
    review.write_text(REVIEW_FIXTURE, encoding="utf-8")
    approval = private / "approval-fixture.mjs"
    approval.write_text(APPROVAL_FIXTURE, encoding="utf-8")
    unload_fx = private / "unload-fixture.mjs"
    unload_fx.write_text(UNLOAD_FIXTURE, encoding="utf-8")
    marker_probe = private / "marker-probe.cjs"
    marker_probe.write_text(MARKER_PROBE, encoding="utf-8")
    marker_file = private / "body-marker.txt"

    insert_main = [
        {"id": "auto-review", "name": "@deepseek-ai/dsh-experimental-auto-review"},
        {"id": "auto-enable-adapter", "name": str(adapter)},
        {"id": "review-fixture", "name": str(review)},
    ]
    if outcome is not None:
        insert_main.append({"id": "approval-fixture", "name": str(approval)})
    if unload:
        insert_main.append({"id": "unload-fixture", "name": str(unload_fx)})

    overlay = [
        {"id": "credentials", "disabled": False, "config": {"path": str(cred), "watch": False}},
        {"id": "authorization", "disabled": False},
        {"id": "deepseek-account", "disabled": False},
        {"id": "session-log-deepseek", "disabled": True},
        {"id": "session-telemetry-otel", "disabled": True},
        *sel,
        {"insert": insert_main},
        {"insert": [{"id": "marker-mcp", "name": "@deepseek-ai/dsh-mcp-client", "config": {
            "serverName": "marker_probe", "transport": "stdio", "command": node,
            "args": [str(marker_probe)], "cwd": str(private),
            "env": {"MARKER_FILE": str(marker_file)},
            "failOnStartupError": True, "reconnect": {"enabled": False}, "toolCallTimeoutMs": 8000,
        }}]},
    ]
    op = private / "overlay.json"
    op.write_text(json.dumps(overlay, ensure_ascii=False), encoding="utf-8")

    env = os.environ.copy()
    _free_env_keys(env)
    env.update(DSH_HOME=str(home), REVIEW_DECISION=decision)
    if outcome is not None:
        env["APPROVAL_OUTCOME"] = outcome
    prompt = ("Call the marker_probe probe tool exactly once with text LAYER_EVIDENCE. "
              "After it, reply DSH_LAYERS_DONE. Do not use any other tool.")
    cmd = [str(entry), "--profile", "headless", "--patch", str(op), "--json", "-"]
    p = subprocess.Popen(cmd, cwd=str(private), env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", errors="replace",
                         creationflags=subprocess.CREATE_NO_WINDOW)
    timed_out = False
    try:
        out, err = p.communicate(input=prompt, timeout=180)
    except subprocess.TimeoutExpired:
        timed_out = True
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        out, err = p.communicate(timeout=10)
    events = []
    for line in out.splitlines():
        try:
            e = json.loads(line)
            if isinstance(e, dict):
                events.append(e)
        except ValueError:
            pass
    final = next((e for e in reversed(events) if e.get("type") == "final"), {})
    result = {
        "exit": p.returncode,
        "timed_out": timed_out,
        "marker_written": marker_file.is_file(),
        "tool_calls": [e.get("tool") for e in events if e.get("type") == "tool_call"],
        "tool_results": [(e.get("status"), bool(e.get("isError")))
                         for e in events if e.get("type") == "tool_result"],
        "review_count": sum(1 for l in err.splitlines() if "[review-fixture] review" in l),
        "review_decisions": [l.split("decision=")[-1] for l in err.splitlines()
                             if "[review-fixture] review" in l],
        "approval_lines": [l for l in err.splitlines() if "[approval-fixture]" in l],
        "adapter_block_lines": [l for l in err.splitlines() if "[auto-enable] BLOCK" in l],
        "adapter_state_tail": [l for l in err.splitlines() if "[auto-enable] state" in l][-1:],
        "final": final.get("text", "") if "DSH_LAYERS" in final.get("text", "") else "[omitted]",
        "stderr_tail": err[-600:] if err else "",
    }
    shutil.rmtree(private, ignore_errors=True)
    shutil.rmtree(home, ignore_errors=True)
    return result


def _pass(segment, r):
    completed = any(s == "completed" for s, _ in r["tool_results"])
    if segment == "review-allow":
        return (not r["timed_out"] and r["marker_written"] and completed
                and r["review_count"] >= 1 and r["review_decisions"] == ["allow"])
    if segment == "review-deny":
        # 工具主体未进入：marker 缺失 + 无 completed 结果；审查命中且 decision=deny。
        return (not r["timed_out"] and not r["marker_written"] and not completed
                and r["review_count"] >= 1 and r["review_decisions"] == ["deny"])
    if segment == "review-rejected":
        return (not r["timed_out"] and not r["marker_written"] and not completed
                and r["review_count"] >= 1 and r["review_decisions"] == ["deny"]
                and any("outcome=rejected" in l for l in r["approval_lines"]))
    if segment == "review-exception":
        return (not r["timed_out"] and not r["marker_written"] and not completed
                and r["review_count"] >= 1 and r["review_decisions"] == ["throw"])
    if segment == "review-abort":
        return (not r["timed_out"] and not r["marker_written"] and not completed
                and r["review_count"] >= 1 and r["review_decisions"] == ["abort"])
    if segment == "adapter-block":
        return (not r["timed_out"] and not r["marker_written"] and not completed
                and bool(r["adapter_block_lines"]))
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry", type=Path, required=True)
    ap.add_argument("--source-home", type=Path, required=True)
    ap.add_argument("--test-directory", type=Path, required=True)
    ap.add_argument("--evidence", type=Path, required=True)
    ap.add_argument("--segment", default="all", choices=[*SEGMENTS, "all"])
    args = ap.parse_args()
    if not args.entry.is_file():
        raise ValueError(f"--entry 不是文件：{args.entry}")
    if not args.source_home.is_dir():
        raise ValueError(f"--source-home 不是目录：{args.source_home}")

    cred = args.source_home / ".credentials.yaml"
    src_hash = hashlib.sha256(cred.read_bytes()).hexdigest() if cred.is_file() else None
    segs = SEGMENTS if args.segment == "all" else (args.segment,)
    outputs = {}
    all_passed = True
    for seg in segs:
        r = _run(args.entry, args.source_home, args.test_directory,
                 decision="allow" if seg == "review-allow" else
                          ("deny" if seg in ("review-deny", "review-rejected") else
                           ("abort" if seg == "review-abort" else "throw")),
                 outcome="rejected" if seg == "review-rejected" else None,
                 unload=(seg == "adapter-block"))
        outputs[seg] = r
        outputs[seg]["passed"] = _pass(seg, r)
        all_passed = all_passed and outputs[seg]["passed"]
    outputs["source_credentials_unchanged"] = (
        hashlib.sha256(cred.read_bytes()).hexdigest() if cred.is_file() else None) == src_hash
    outputs["passed"] = all_passed and outputs["source_credentials_unchanged"]
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(outputs, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(outputs, ensure_ascii=False, indent=2))
    return 0 if outputs["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
