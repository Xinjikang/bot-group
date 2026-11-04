"""One-shot DSH project assistant with native profile and scoped tools."""
import json
from pathlib import Path

from ruamel.yaml import YAML

from . import dsh_auth, dsh_auto, native_runtime


TOOL_ADAPTER = '''export const name = "hud-tool-scope";
export const inject = ["tools", "agents"];
export function apply(ctx) {
    const allowed = new Set(["read", "write", "edit", "glob", "grep", "read_image",
        "mcp__project_config__project_config", "mcp__project_config__open_project_page"]);
    ctx.tools.guard(exec => allowed.has(exec.name) ? undefined : "Tool outside project assistant scope");
    ctx.on("agent/created", ({agent}) => {
        const names = ctx.tools.schemas().map(tool => tool.name).filter(name => allowed.has(name));
        agent.ctx.tools.restrict({allow: names});
        console.error("[hud-tools] ready");
    });
}
'''


def respond(bot, prompt, system, scratch, workdir, server, run, env):
    credentials = dsh_auth.copy_credentials(bot['config_root'], str(scratch))
    auto_enabled = dsh_auto.plugin_available(bot)
    auto = None
    if auto_enabled:
        adapter = scratch / dsh_auto.ADAPTER_FILE
        adapter.write_text(dsh_auto.adapter_source(), encoding='utf-8')
        auto = {'adapter_path': str(adapter)}
    private = None
    if server:
        private = {'id': 'hud-project-config', 'name': '@deepseek-ai/dsh-mcp-client', 'config': {
            'serverName': 'project_config', 'transport': 'stdio',
            'command': server['command'], 'args': server.get('args', []),
            'env': server.get('env', {}), 'cwd': workdir,
            'failOnStartupError': True, 'reconnect': {'enabled': False},
        }}
    entries = native_runtime.dsh_overlay(bot, credentials_path=credentials,
                                         mcp_entry=private, auto_review=auto)
    path = Path(bot['config_root']) / 'profiles' / bot['profile'] / 'cordis.patch.yml'
    saved = YAML(typ='rt').load(path.read_text(encoding='utf-8-sig')) if path.is_file() else []
    config = {}
    for item in saved or []:
        if isinstance(item, dict) and item.get('id') == 'system-prompt':
            config = dict(item.get('config') or {})
    config['personaSuffix'] = system
    entries.append({'id': 'system-prompt', 'config': config})
    adapter = scratch / 'hud-tools.mjs'
    adapter.write_text(TOOL_ADAPTER, encoding='utf-8')
    entries.append({'insert': [{'id': 'hud-tool-scope', 'name': str(adapter)}]})
    overlay = scratch / 'overlay.yml'
    overlay.write_text(native_runtime.overlay_yaml(entries), encoding='utf-8')
    command, home = native_runtime.dsh(bot, overlay_path=str(overlay))
    env['DSH_HOME'] = home
    # The tool presentation remains a direct list of the scoped capabilities.
    env['DSH_TOOLS_MODE'] = 'native'
    state = {'tools': False, 'auto': []}

    def stderr(line):
        if '[hud-tools] ready' in line:
            state['tools'] = True
        if dsh_auto.STATE_MARKER in line:
            state['auto'][:] = [line]

    output = run(command, prompt, env, workdir, stderr_line=stderr)
    if not state['tools']:
        raise RuntimeError('DSH 项目助手工具范围未确认，请检查运行时插件')
    if auto_enabled and not dsh_auto.preset_evidence(state['auto'])['confirmed']:
        raise RuntimeError(dsh_auto.AUDIT_FAIL_TEXT)
    final = None
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            raise RuntimeError('DSH 项目助手返回损坏的事件流') from None
        if not isinstance(event, dict) or event.get('type') == 'error':
            raise RuntimeError('DSH 项目助手返回失败事件')
        if event.get('type') == 'final':
            final = event.get('text')
    if not isinstance(final, str) or not final.strip():
        raise RuntimeError('DSH 项目助手缺少完整回复')
    return final
