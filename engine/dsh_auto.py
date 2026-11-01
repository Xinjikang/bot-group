"""req#137 task#268：DSH 智能审批（Auto review）接入。

实装依据（0.2.1-rc.5，reports/research/dsh-integration/approval-review.md + probe-auto2.py）：
- ``@deepseek-ai/dsh-experimental-auto-review`` 默认关闭；其 patch 插入 ``auto-review``
  服务，服务在 ``tools/pre-execute`` 仅当 ``permissionPresets.current(agent.session)``
  等于 auto 预设时逐次审查工具调用（当前 provider/model 各发起一次审查请求），allow 后
  按 danger-full-access 执行，deny 进审批请求链（headless 无应答方 → unavailable → 工具失败）。
- auto 是专用会话预设，原生 ``permissionPresets.set(session,'auto')`` 是唯一写入路径；
  在 ``session/created`` 事件调用即启用（probe-auto2 实测 preset=auto 命中）。
- 插件卸载会把存量 Auto 会话迁移至 danger-full-access——引擎必须 fail-closed：
  已启用 Auto 的执行若拿不到「preset=auto」运行时证据，判定能力失效并阻止继续。

本模块职责：受控适配插件（session/created 切 auto + stderr 证据行）、overlay insert、
启用状态读写（引擎侧边文件，不写 cordis.patch.yml）、插件状态判定（保存校验用）、
每轮审计落库（审查尝试 vs 工具主体执行独立计数）。
"""
from __future__ import annotations

import re
from pathlib import Path

AUTO_PLUGIN_ID = 'auto-review'
AUTO_PLUGIN_PACKAGE = '@deepseek-ai/dsh-experimental-auto-review'
# 引擎侧边文件（与 description 同约定：不改 cordis.patch.yml，避免 round-trip 风险）。
SIDE_FILE = '.group-engine-auto-review'
ADAPTER_ID = 'auto-enable-adapter'
ADAPTER_FILE = 'auto-enable-adapter.mjs'
# 适配插件 stderr 证据行（引擎审计 + fail-closed 判读的唯一事实源）。
# req#138 F6：改用「累积状态行」——每次事件输出一条 `[auto-enable] state preset=.. preexec=.. review=.. blocked=..`，
# 末位状态行携带完整累计值。引擎在 stderr 读取线程内**流式维护状态行**（独立于 40 行诊断尾部滚动），
# 40 行截断不会丢 session/created 与早期 pre-execute 证据。
_STATE_MARKER = '[auto-enable] state'
STATE_MARKER = _STATE_MARKER  # wake.py 流式维护状态行用（公开别名）
_BLOCK_MARKER = '[auto-enable] BLOCK'
BLOCK_MARKER = _BLOCK_MARKER  # 公开别名（callId 证据行）
ENTRY_MARKER = '[auto-enable] ENTRY'  # 工具主体入口 callId 证据行（tools/execute）
_PRESET_OK = 'auto'  # state 行 preset= 捕获值须为 auto 才算会话预设生效

# 适配插件源码（probe-auto2 的 ADAPTER 实测版 + req#138 F2/F6/F7）：
# - session/created 调原生 set 并记录 preset 确认（evidence 唯一事实源）；
# - llm/stream 拦截识别 Auto 审查请求（system 以 REVIEW_POLICY 开头）累积 review 计数（F7 实际审查请求）；
# - tools/pre-execute 累积 preexec 计数；current≠auto 时 fail-closed **阻断**（不 next，
#   工具不执行，副作用不发生），并累加 blocked；current==auto 时 next() 放行交由原生
#   auto-review 审查。证据走 stderr（累积状态行），不与 stdout --json 事件流混淆。
ADAPTER_SOURCE = """export const name = "auto-enable-adapter";
export const inject = ["permissionPresets", "llm"];
export function apply(ctx) {
    let presetConfirmed = false;
    let preexec = 0;
    let review = 0;
    let entry = 0;
    let blocked = 0;
    const state = () => console.error(
        `[auto-enable] state preset=${presetConfirmed ? "auto" : "n/a"} preexec=${preexec} review=${review} entry=${entry} blocked=${blocked}`);
    ctx.on("session/created", (session) => {
        ctx.permissionPresets.set(session, "auto");
        presetConfirmed = ctx.permissionPresets.current(session) === "auto";
        state();
    });
    ctx.on("llm/stream", (options, fallback) => {
        // 审查识别：REVIEW_POLICY 准确特征 + temperature===0 复合识别（classifyRisk 固定 temp0、
        // 审查原文首行「REVIEW_POLICY」后接「You are the final authorization reviewer」）。
        // isAgentLoopRequest 是 dsh-llm 模块级导出，本地 .mjs 裸说明符 @deepseek-ai/dsh-llm
        // 解析失败（实测 module resolution error，同 review-fixture），故以准确特征 + temp0
        // 等价区分：agent-loop 普通请求无 REVIEW_POLICY 准确特征 → 透传；REVIEW_POLICY_EXTRA
        // 同前缀非审查（无准确特征）→ 透传，均不误计审查。
        const sys = options && options.system;
        const temp = options && options.temperature;
        if (typeof sys === "string"
                && sys.startsWith("REVIEW_POLICY" + String.fromCharCode(10) + "You are the final authorization reviewer")
                && temp === 0) {
            review += 1;
        }
        return fallback();
    });
    ctx.on("tools/pre-execute", (exec, next) => {
        preexec += 1;
        const preset = exec.agent ? ctx.permissionPresets.current(exec.agent.session) : "n/a";
        const callId = exec.callId ?? "?";
        if (preset !== "auto") {
            blocked += 1;
            console.error(`[auto-enable] BLOCK preset=${preset} name=${exec.name ?? "?"} callId=${callId}`);
            state();
            return;  // fail-closed：能力失效即阻断，不 next，工具不执行
        }
        state();
        return next();
    });
    ctx.on("tools/execute", (exec, next) => {
        // 主体入口（真实函数进入处）：审批 deny 在 tools/execute 之前 → 未到此处 entry=0；
        // 进入后失败/成功均到此 entry=1（独立于 completed）。exec.callId 关联工具调用。
        entry += 1;
        console.error(`[auto-enable] ENTRY name=${exec.name ?? "?"} callId=${exec.callId ?? "?"}`);
        state();
        return next();
    });
}

"""


def side_path(bot) -> Path:
    """引擎侧启用标记文件：<DSH_HOME>/profiles/<profile>/.group-engine-auto-review。"""
    return Path(bot['config_root']).resolve() / 'profiles' / bot['profile'] / SIDE_FILE


def read_enabled(bot) -> bool:
    """Auto 是否启用（'on'；文件缺失/其他内容一律视为 off，fail-closed）。"""
    try:
        return side_path(bot).read_text(encoding='utf-8-sig').strip().lower() == 'on'
    except OSError:
        return False


def write_enabled(bot, enabled: bool) -> None:
    path = side_path(bot)
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('on', encoding='utf-8')
    else:
        try:
            path.unlink()
        except OSError:
            pass


def adapter_source() -> str:
    """受控适配插件源码（tools/execute 主体入口 + REVIEW_POLICY 准确特征 + temp0 审查识别）。"""
    return ADAPTER_SOURCE


def overlay_inserts(adapter_path: str) -> list:
    """Auto 启用时的 overlay insert 条目：插件加载 + 适配插件（切 auto）。"""
    return [{
        'insert': [
            {'id': AUTO_PLUGIN_ID, 'name': AUTO_PLUGIN_PACKAGE, 'disabled': False},
            {'id': ADAPTER_ID, 'name': str(adapter_path)},
        ],
    }]


def plugin_state(bot, patch_entries: list) -> dict:
    """auto-review 插件状态（保存校验 + 视图展示）。

    沿用 dsh_profile_config 的继承语义：用户 patch 无该条目 → 继承 bundle 默认
    （bundle 内含该插件，desktop 实测）→ ok；显式 disabled → disabled；显式指定
    非 bundle 包名且未安装 → load_failed。真实加载失败由启动证据补足（wake fail-closed）。
    """
    home = Path(bot['config_root']).resolve()
    profile = bot['profile']
    entry = next((e for e in patch_entries if isinstance(e, dict)
                  and str(e.get('id') or '') == AUTO_PLUGIN_ID), None)
    if entry is None:
        return {'plugin': AUTO_PLUGIN_ID, 'state': 'ok', 'reason': ''}
    if entry.get('disabled'):
        return {'plugin': AUTO_PLUGIN_ID, 'state': 'disabled',
                'reason': f'{AUTO_PLUGIN_ID} 插件已禁用'}
    name = entry.get('name')
    if name and name != AUTO_PLUGIN_PACKAGE and not _pkg_installed(home, profile, name):
        return {'plugin': AUTO_PLUGIN_ID, 'state': 'load_failed',
                'reason': f'{name} 未安装或加载失败'}
    return {'plugin': AUTO_PLUGIN_ID, 'state': 'ok', 'reason': ''}


def _pkg_installed(home: Path, profile: str, pkg_name: str) -> bool:
    parts = pkg_name.split('/')
    for base in (home / 'profiles' / profile / 'node_modules', home / 'node_modules'):
        if base.joinpath(*parts).is_dir():
            return True
    return False


def preset_evidence(stderr_lines) -> dict:
    """从适配插件 stderr 累积状态行提取 Auto 会话状态与计数（req#138 F6/F7）。

    读**末位** `[auto-enable] state` 行（每次事件都输出累计值，末位即最终状态），而非逐行
    计数——这样 40 行滚动尾部截断不会丢 session/created 与早期 pre-execute 证据。
    返回 {session_preset, preexec_count, review_count, entry_count, blocked_count, confirmed}：
    - preexec_count = tools/pre-execute 总触发次数（审查尝试近似）；
    - review_count = llm/stream 拦截到的 Auto 审查请求数（isAgentLoopRequest===false +
      REVIEW_POLICY 准确特征 + temperature 0，实际审查请求，F7 独立取证）；
    - entry_count = tools/execute 主体入口数（真实函数进入处，独立于 completed；审批 deny
      在 execute 前 → entry=0，进入后失败/成功 → entry=1）；
    - blocked_count = 适配插件在 current≠auto 时阻断的工具数（F2 fail-closed，副作用未发生）；
    - confirmed = 末位状态 preset==auto 才算会话预设生效。
    """
    session_preset = None
    preexec = 0
    review = 0
    blocked = 0
    entry = 0
    call_ids: list[str] = []
    for line in stderr_lines or ():
        if _STATE_MARKER in line:
            m_preset = re.search(r'preset=(\S+)', line)
            m_preexec = re.search(r'preexec=(\d+)', line)
            m_review = re.search(r'review=(\d+)', line)
            m_entry = re.search(r'entry=(\d+)', line)
            m_blocked = re.search(r'blocked=(\d+)', line)
            if m_preset:
                session_preset = m_preset.group(1)
            if m_preexec:
                preexec = int(m_preexec.group(1))
            if m_review:
                review = int(m_review.group(1))
            if m_entry:
                entry = int(m_entry.group(1))
            if m_blocked:
                blocked = int(m_blocked.group(1))
        elif ENTRY_MARKER in line or BLOCK_MARKER in line:
            # 工具入口/阻断的 callId（入口计数按 callId 关联，非仅全局计数）。
            m_cid = re.search(r'callId=(\S+)', line)
            if m_cid:
                call_ids.append(m_cid.group(1))
    return {'session_preset': session_preset,
            'preexec_count': preexec,
            'review_count': review,
            'entry_count': entry,
            'blocked_count': blocked,
            'call_ids': call_ids,
            'confirmed': session_preset == _PRESET_OK}


AUDIT_FAIL_TEXT = (
    'auto review capability unavailable: session preset not confirmed (preset=auto not '
    'observed on session/created); execution blocked for this session')


def audit(conn, *, session, wake_started_at, enabled, evidence, tool_calls,
          completed_results, ts) -> int:
    """每轮 Auto 审计落库：审查请求与工具主体执行独立计数（req#138 F7）。

    review_count = 适配插件 llm/stream 拦截到的实际审查请求数（system 以 REVIEW_POLICY
    开头，非 tools/pre-execute 日志行近似）；executed_count = 主体入口数（entry_count，
    合成工具 body 真实函数进入处落行，独立于副作用 marker 与 completed）——审批取消/拒绝
    body 未进入 entry=0，进入后失败/成功 entry=1；blocked_count = 适配插件在 current≠auto
    时阻断的工具数（F2 fail-closed，副作用未发生）。三者独立取证，不用 tool_calls-completed
    反推 blocked。executed 取 entry（主体入口），不取 completed（会漏「进入主体后失败」）。"""
    # review_count 只在 review 字段缺失（旧证据格式无 llm/stream 计数）时才回退 preexec；
    # review=0 是合法值（审查异常/跳过时实际审查数为 0），不得被 or 短路成 preexec。
    review = evidence.get('review_count')
    if review is None:
        review = evidence.get('preexec_count') or 0
    review_count = int(review)
    # executed_count = 主体入口（entry_count，真实函数进入处），非 pre-execute 也非
    # completed（completed 会漏掉「进入主体后失败 entry=1 completed=0」）。
    executed = int(evidence.get('entry_count') or 0)
    blocked = int(evidence.get('blocked_count') or 0)
    cur = conn.execute(
        'INSERT INTO dsh_auto_audit(session,wake_started_at,enabled,preset_confirmed,'
        'review_count,executed_count,blocked_count,ts) VALUES(?,?,?,?,?,?,?,?)',
        (session, wake_started_at, 1 if enabled else 0,
         1 if evidence.get('confirmed') else 0, review_count, executed, blocked, ts))
    return cur.lastrowid
