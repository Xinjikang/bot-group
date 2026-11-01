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
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import dsh_auth, dsh_auto  # noqa: E402

SEGMENTS = ("review-allow", "review-deny", "review-rejected",
            "review-exception", "review-abort", "adapter-block", "policy-never",
            "cancel-probe", "approval-cancelled", "entry-fail", "review-passthrough")

# 审查层夹具：拦截 llm/stream，识别 Auto 审查请求（system 以 REVIEW_POLICY 开头），
# 返回固定决策流（text-delta + finish stop）；异常时 throw，abort 时 finish aborted。
REVIEW_FIXTURE = """import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
// 与生产适配插件同模块加载（manager node1647）：以 DSH CLI 入口（process.argv[1]）为 createRequire
// 解析锚点动态 import 原生 isAgentLoopRequest（同一模块 WeakSet 检查），联合 REVIEW_POLICY 准确
// 特征 + temperature 0 识别——与生产识别一致，避免夹具误拦 agent-loop 普通请求。
const _rfRequire = createRequire(process.argv[1]);
const _rfLlmPath = _rfRequire.resolve("@deepseek-ai/dsh-llm");
const _rfLlmMod = await import(pathToFileURL(_rfLlmPath).href);
const isAgentLoopRequest = _rfLlmMod.isAgentLoopRequest;
console.error(`[review-fixture] llm-load isAgentLoopRequest=${typeof isAgentLoopRequest}`);
export const name = "review-fixture";
export const inject = ["llm"];
export function apply(ctx) {
    let reviewCount = 0;
    let passCount = 0;
    ctx.on("llm/stream", (options, fallback) => {
        const sys = options && options.system;
        const temp = options && options.temperature;
        // 联合识别（与生产适配插件一致）：isAgentLoopRequest===true → agent-loop 普通请求直接透传；
        // ===false 联合 REVIEW_POLICY 原文精确前缀 + temperature 0 才判定审查（REVIEW_POLICY_EXTRA
        // 同前缀非审查无准确特征 → 透传）。
        if (isAgentLoopRequest(options) === true) {
            passCount += 1;
            return fallback();
        }
        if (typeof sys === "string"
                && sys.startsWith("REVIEW_POLICY" + String.fromCharCode(10) + "You are the final authorization reviewer")
                && temp === 0) {
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
        // 普通请求透传（不拦截）：累计 pass-through 计数证明不误拦截。
        passCount += 1;
        console.error(`[review-fixture] pass-through #${passCount} sys=${String(sys).slice(0, 24)} temp=${temp}`);
        return fallback();
    });
}
"""

# 非审查探针：合成一个「REVIEW_POLICY 前缀 + temperature 0 但非审查原文」的普通请求，
# 断言 review-fixture 不误拦（精确前缀「REVIEW_POLICY\nYou are the final authorization
# reviewer」不匹配 REVIEW_POLICY_EXTRA）→ 透传（pass-through 日志 + review_count 不变）。
# options 形状对齐 classifyRisk（provider/model/system/messages/temperature），否则 llm
# 服务把缺 provider/model 的裸 options 归一化为 system=undefined、到不了审查夹具。
NON_REVIEW_PROBE = """export const name = "non-review-probe";
export const inject = ["llm"];
export function apply(ctx) {
    ctx.on("session/created", () => {
        const options = {
            provider: "deepseek", model: "deepseek-chat",
            system: "REVIEW_POLICY_EXTRA", temperature: 0,
            messages: [{ role: "user", content: [{ type: "text", text: "reply exactly ok" }] }],
        };
        const stream = ctx.llm.stream(options);
        (async () => {
            try {
                for await (const _ of stream) {}
            } catch (e) {}
            console.error("[non-review-probe] fired");
        })();
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
        if (outcome === "cancelled") return "cancelled";
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

# 审批层 policy=never 夹具：tools/pre-execute 时把会话 approval policy 切 never
# （auto-review 门 overrideOf==="never"→denied 或 approval decide effectivePolicy==="never"→
# rejected，dispatch 前拒绝，不派发 approval/request）。
POLICY_FIXTURE = """export const name = "approval-policy-fixture";
export const inject = ["approval"];
export function apply(ctx) {
    ctx.on("tools/pre-execute", (exec, next) => {
        if (process.env.APPROVAL_POLICY === "never" && exec && exec.agent) {
            try {
                ctx.approval.setPolicy(exec.agent, "never");
                console.error("[policy-fixture] approval policy set to never");
            } catch (e) {
                console.error("[policy-fixture] setPolicy failed: " + (e && e.message));
            }
        }
        return next();
    });
}
"""

# 审批层 cancelled 两时序主动夹具：在合法 open turn（tools/pre-execute）内**主动调用**
# 原生 ctx.approval.request，传自有 AbortController 的 signal——请求前 abort（decide 首行
# signal?.aborted→cancelled）与 answerer 已进入等待后 abort（decide abort 事件→cancelled），
# 保留原生 asked/decided 审计。answerer（probe-wait）返回永不 resolve 的 promise + 就绪屏障
# + 超时清理；禁止仅返回 cancelled 伪造信号测试。
CANCEL_PROBE_FIXTURE = """export const name = "cancel-probe-fixture";
export const inject = ["approval"];
export function apply(ctx) {
    let entered = false;
    let resolveEntered;
    const enteredPromise = new Promise(r => { resolveEntered = r; });

    // answerer：probe-wait 进入时置就绪屏障并返回永不 resolve 的 promise（等 abort 触发 cancelled）。
    ctx.on("approval/request", (req, fallback) => {
        if (req && req.toolName === "probe-wait") {
            if (!entered) { entered = true; resolveEntered(); }
            return new Promise(() => {});
        }
        return fallback();
    });

    let ran = false;
    ctx.on("tools/pre-execute", async (exec, next) => {
        if (!ran && exec && exec.agent) {
            ran = true;
            try {
                // ① 请求前 cancelled：先 abort 再 request → decide signal?.aborted → cancelled。
                const c1 = new AbortController();
                c1.abort(new Error("cancel-probe-pre"));
                const o1 = await ctx.approval.request({
                    agent: exec.agent, toolName: "probe-pre", callId: exec.callId,
                    signal: c1.signal,
                });
                console.error("[cancel-probe] pre-abort outcome=" + o1);

                // ② 等待中 cancelled：answerer 进入后 abort → decide abort 事件 → cancelled。
                const c2 = new AbortController();
                const pending = ctx.approval.request({
                    agent: exec.agent, toolName: "probe-wait", callId: exec.callId,
                    signal: c2.signal,
                });
                const barrier = await Promise.race([
                    enteredPromise.then(() => "entered"),
                    new Promise(r => setTimeout(() => r("timeout"), 5000)),
                ]);
                console.error("[cancel-probe] wait-barrier=" + barrier);
                c2.abort(new Error("cancel-probe-wait"));
                const o2 = await pending;
                console.error("[cancel-probe] wait-abort outcome=" + o2);
            } catch (e) {
                console.error("[cancel-probe] error: " + (e && e.message));
            }
        }
        return next();
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
    // 真实函数入口：进入 body 即记录（独立于副作用 marker 写入 + completed）。
    try {
      if (process.env.ENTRY_FILE) fs.writeFileSync(process.env.ENTRY_FILE, JSON.stringify({ pid: process.pid, ts: Date.now() }));
    } catch (e) {}
    console.error('[marker-probe] ENTRY text=' + (m.params.arguments && m.params.arguments.text));
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
              "APPROVAL_OUTCOME", "APPROVAL_POLICY", "MARKER_FILE"):
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


def _cleanup_dirs(dirs) -> dict:
    """统一回收临时目录并核验清理结果（rmtree 后目录确实移除，失败明确报错不静默）。"""
    report = {}
    for d in dirs:
        if not d.exists():
            report[str(d)] = True  # 已不存在
            continue
        try:
            shutil.rmtree(d)
            report[str(d)] = not d.exists()
        except OSError as e:
            report[str(d)] = f"rmtree error: {e}"
    return report


def _run(entry: Path, source_home: Path, test_dir: Path, node: str, *, decision: str = "allow",
         outcome: str = None, unload: bool = False, policy_never: bool = False,
         cancel_probe: bool = False, passthrough: bool = False,
         fail_marker: bool = False) -> dict:
    """一次真实 DSH 运行，返回结构化证据。"""
    private = None
    home = None
    result = None
    try:
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
        policy_fx = private / "approval-policy-fixture.mjs"
        policy_fx.write_text(POLICY_FIXTURE, encoding="utf-8")
        cancel_fx = private / "cancel-probe-fixture.mjs"
        cancel_fx.write_text(CANCEL_PROBE_FIXTURE, encoding="utf-8")
        passthrough_fx = private / "non-review-probe.mjs"
        passthrough_fx.write_text(NON_REVIEW_PROBE, encoding="utf-8")
        marker_probe = private / "marker-probe.cjs"
        marker_probe.write_text(MARKER_PROBE, encoding="utf-8")
        marker_file = private / "body-marker.txt"
        entry_file = private / "body-entry.txt"
    
        insert_main = [
            {"id": "auto-review", "name": "@deepseek-ai/dsh-experimental-auto-review"},
            {"id": "auto-enable-adapter", "name": str(adapter)},
            {"id": "review-fixture", "name": str(review)},
        ]
        if outcome is not None:
            insert_main.append({"id": "approval-fixture", "name": str(approval)})
        if unload:
            insert_main.append({"id": "unload-fixture", "name": str(unload_fx)})
        if policy_never:
            insert_main.append({"id": "approval-policy-fixture", "name": str(policy_fx)})
        if cancel_probe:
            insert_main.append({"id": "cancel-probe-fixture", "name": str(cancel_fx)})
        if passthrough:
            insert_main.append({"id": "non-review-probe", "name": str(passthrough_fx)})
    
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
                "env": {"MARKER_FILE": str(marker_file) if not fail_marker
                        else str(private / "missing-dir" / "body-marker.txt"),
                        "ENTRY_FILE": str(entry_file)},
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
        if policy_never:
            env["APPROVAL_POLICY"] = "never"
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
            "entry_written": entry_file.is_file(),
            "entry_count": sum(1 for l in err.splitlines() if "[marker-probe] ENTRY" in l),
            "prod_entry_count": _last_state_entry(err),
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
            "adapter_llm_load": [l for l in err.splitlines() if "[auto-enable] llm-load" in l],
            "cancel_probe_lines": [l for l in err.splitlines() if "[cancel-probe]" in l],
            "final": final.get("text", "") if "DSH_LAYERS" in final.get("text", "") else "[omitted]",
            "stderr_tail": err[-600:] if err else "",
        }
        return result
    finally:
        # 统一回收自启资源/临时目录 + 核验清理结果（覆盖初始化/Popen/通信异常，不静默）。
        cleanup = _cleanup_dirs([d for d in (private, home) if d is not None])
        if result is not None:
            result["cleanup"] = cleanup
            result["cleanup_ok"] = bool(cleanup) and all(v is True for v in cleanup.values())


def _last_state_entry(err: str) -> int:
    """生产入口计数：adapter 末位 state 行的 entry=（tools/execute 主体入口），独立于 marker 日志 entry_count。

    manager node1647：verify 的 entry_count 来自 marker 日志，须另明确生产入口计数字段核验三态——
    成功 entry=1、进入失败 entry=1、取消 entry=0；缺计数/错误计数必须失败。"""
    for line in reversed((err or "").splitlines()):
        if "[auto-enable] state" in line:
            m = re.search(r"entry=(\d+)", line)
            return int(m.group(1)) if m else 0
    return 0


def _pass(segment, r):
    completed = any(s == "completed" for s, _ in r["tool_results"])
    # 门禁：非崩溃（exit 0、未超时）+ 目标工具确实被请求（tool_call 非空）。
    # 排除「进程崩溃 / 无调用 / marker 写失败」被误判为审批阻断。
    gated = (not r["timed_out"] and r["exit"] == 0 and bool(r["tool_calls"]))
    if not gated:
        return False
    if segment == "review-allow":
        # 成功入口 1：body 进入（entry_written + [marker-probe] ENTRY）+ 副作用 marker + completed
        # + 生产入口计数 entry=1。
        return (r["entry_written"] and r["marker_written"] and completed
                and r["prod_entry_count"] == 1
                and r["review_count"] >= 1 and r["review_decisions"] == ["allow"])
    if segment == "review-deny":
        # 工具主体未进入（取消入口 0）：无 entry + marker 缺失 + 无 completed + 生产入口 entry=0；
        # 审查命中且 decision=deny。
        return (not r["entry_written"] and not r["marker_written"] and not completed
                and r["prod_entry_count"] == 0
                and r["review_count"] >= 1 and r["review_decisions"] == ["deny"])
    if segment == "review-rejected":
        return (not r["marker_written"] and not completed
                and r["review_count"] >= 1 and r["review_decisions"] == ["deny"]
                and any("outcome=rejected" in l for l in r["approval_lines"]))
    if segment == "review-exception":
        return (not r["marker_written"] and not completed
                and r["review_count"] >= 1 and r["review_decisions"] == ["throw"])
    if segment == "review-abort":
        return (not r["marker_written"] and not completed
                and r["review_count"] >= 1 and r["review_decisions"] == ["abort"])
    if segment == "adapter-block":
        return (not r["marker_written"] and not completed
                and bool(r["adapter_block_lines"]))
    if segment == "policy-never":
        # review deny + approval policy=never → 拒绝（dispatch 前，无 approval-fixture 行，
        # 工具主体未进入）；policy-fixture 落 setPolicy 证据。
        return (not r["marker_written"] and not completed
                and r["review_count"] >= 1 and r["review_decisions"] == ["deny"]
                and not r["approval_lines"]
                and any("approval policy set to never" in l for l in r["stderr_tail"].splitlines()))
    if segment == "cancel-probe":
        # 主动调用原生 approval.request 两时序 cancelled：请求前 abort + answerer 进入后 abort
        # 均 outcome=cancelled，就绪屏障 entered（非 timeout/伪造），工具仍正常执行（marker 写入
        # + completed，cancel-probe 不阻断工具）。
        lines = r["cancel_probe_lines"]
        return (r["marker_written"] and completed
                and any("pre-abort outcome=cancelled" in l for l in lines)
                and any("wait-barrier=entered" in l for l in lines)
                and any("wait-abort outcome=cancelled" in l for l in lines))
    if segment == "approval-cancelled":
        # 审批 cancelled → serviceAsk 映射 deny（approvalCancelled）→ 工具主体不执行（取消入口 0）：
        # 无 entry + marker 缺失 + 无 completed + 生产入口 entry=0；审批夹具落 outcome=cancelled 证据。
        return (not r["entry_written"] and not r["marker_written"] and not completed
                and r["prod_entry_count"] == 0
                and r["review_count"] >= 1 and r["review_decisions"] == ["deny"]
                and any("outcome=cancelled" in l for l in r["approval_lines"]))
    if segment == "entry-fail":
        # 进入后失败：body 进入（entry_written + [marker-probe] ENTRY 落行）+ 生产入口 entry=1 +
        # 副作用 marker 写失败（marker_written=false，tool_result error，completed=0）——证明 entry
        # 独立于 completed（进入主体后失败 entry=1 completed=0）。
        return (r["entry_written"] and not r["marker_written"] and not completed
                and r["prod_entry_count"] == 1
                and r["review_count"] >= 1 and r["review_decisions"] == ["allow"])
    if segment == "review-passthrough":
        # 「同前缀非审查」反例：review-fixture 精确前缀（REVIEW_POLICY 原文）不匹配非审查请求 →
        # 透传（pass-through 日志），review_count 保持 1（仅 marker 工具的真实审查被拦截，非审查
        # 请求未计）。精确前缀识别（原文前缀不匹配 REVIEW_POLICY_EXTRA）由单测
        # test_review_fixture_exact_policy_prefix_not_bare 断言；真机段验证非审查透传不误增计数。
        return (r["entry_written"] and r["marker_written"] and completed
                and r["review_count"] == 1)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry", type=Path, required=True)
    ap.add_argument("--source-home", type=Path, required=True)
    ap.add_argument("--test-directory", type=Path, required=True)
    ap.add_argument("--evidence", type=Path, required=True)
    ap.add_argument("--segment", default="all", choices=[*SEGMENTS, "all"])
    ap.add_argument("--node", default=shutil.which("node") or "node",
                    help="node 可执行文件（MCP 夹具 stdio 服务器）；默认 PATH 发现")
    args = ap.parse_args()
    if not args.entry.is_file():
        raise ValueError(f"--entry 不是文件：{args.entry}")
    if not args.source_home.is_dir():
        raise ValueError(f"--source-home 不是目录：{args.source_home}")
    if shutil.which(args.node) is None and not Path(args.node).is_file():
        raise ValueError(f"--node 不可执行：{args.node}（需 PATH 发现或显式绝对路径）")

    cred = args.source_home / ".credentials.yaml"
    src_hash = hashlib.sha256(cred.read_bytes()).hexdigest() if cred.is_file() else None
    segs = SEGMENTS if args.segment == "all" else (args.segment,)
    outputs = {}
    all_passed = True
    for seg in segs:
        r = _run(args.entry, args.source_home, args.test_directory, args.node,
                 decision="allow" if seg in ("review-allow", "cancel-probe", "review-passthrough",
                                             "entry-fail")
                          else
                          ("deny" if seg in ("review-deny", "review-rejected", "policy-never",
                                             "approval-cancelled")
                           else ("abort" if seg == "review-abort" else "throw")),
                 outcome=("rejected" if seg == "review-rejected"
                          else ("cancelled" if seg == "approval-cancelled" else None)),
                 unload=(seg == "adapter-block"),
                 policy_never=(seg == "policy-never"),
                 cancel_probe=(seg == "cancel-probe"),
                 passthrough=(seg == "review-passthrough"),
                 fail_marker=(seg == "entry-fail"))
        outputs[seg] = r
        # cleanup_ok 进入总 passed：清理失败（rmtree 后目录仍存在）不通过。
        outputs[seg]["passed"] = _pass(seg, r) and bool(r.get("cleanup_ok", False))
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
