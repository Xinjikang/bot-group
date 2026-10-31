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
_SESSION_MARKER = '[auto-enable] session/created'
_PREEXEC_MARKER = '[auto-enable] tools/pre-execute'
_PRESET_OK = 'auto'  # preset=(\S+) 捕获值须为 auto 才算会话预设生效

# 适配插件源码（probe-auto2 的 ADAPTER 实测版：session/created 调原生 set；证据走 stderr，
# 不与 stdout 的 --json 事件流混淆）。
ADAPTER_SOURCE = """export const name = "auto-enable-adapter";
export const inject = ["permissionPresets"];
export function apply(ctx) {
    ctx.on("session/created", (session) => {
        ctx.permissionPresets.set(session, "auto");
        console.error(`[auto-enable] session/created -> preset=${ctx.permissionPresets.current(session)}`);
    });
    ctx.on("tools/pre-execute", (exec, next) => {
        const preset = exec.agent ? ctx.permissionPresets.current(exec.agent.session) : "n/a";
        console.error(`[auto-enable] tools/pre-execute preset=${preset} name=${exec.name ?? "?"}`);
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
    """受控适配插件源码（同 probe-auto2 实测语义）。"""
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
    """从适配插件 stderr 证据行提取 Auto 会话状态与计数。

    返回 {session_preset, preexec_count, confirmed}：session/created 行报 preset=auto
    才算会话预设生效（fail-closed 判读）；tools/pre-execute 行数即审查尝试数
    （auto 预设下每个工具调用触发一次审查）。
    """
    session_preset = None
    preexec = 0
    for line in stderr_lines or ():
        if _SESSION_MARKER in line:
            match = re.search(r'preset=(\S+)', line)
            if match:
                session_preset = match.group(1)
        elif _PREEXEC_MARKER in line:
            preexec += 1
    return {'session_preset': session_preset,
            'preexec_count': preexec,
            'confirmed': session_preset == _PRESET_OK}


AUDIT_FAIL_TEXT = (
    'auto review capability unavailable: session preset not confirmed (preset=auto not '
    'observed on session/created); execution blocked for this session')


def audit(conn, *, session, wake_started_at, enabled, evidence, tool_calls,
          completed_results, ts) -> int:
    """每轮 Auto 审计落库：审查尝试（tools/pre-execute）与工具主体执行独立计数。

    blocked = 审查尝试数 - 工具主体执行成功数（allow 后执行才计 completed；deny/
    unavailable/cancel 的工具结果不 completed）。"""
    review_count = int(evidence.get('preexec_count') or 0)
    executed = int(completed_results or 0)
    blocked = max(0, int(tool_calls or 0) - executed)
    cur = conn.execute(
        'INSERT INTO dsh_auto_audit(session,wake_started_at,enabled,preset_confirmed,'
        'review_count,executed_count,blocked_count,ts) VALUES(?,?,?,?,?,?,?,?)',
        (session, wake_started_at, 1 if enabled else 0,
         1 if evidence.get('confirmed') else 0, review_count, executed, blocked, ts))
    return cur.lastrowid
