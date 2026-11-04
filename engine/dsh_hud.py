"""One-shot DSH project assistant with native profile and scoped tools."""
import json
from . import dsh_auth, dsh_auto, native_runtime


REQUIRED_TOOLS = frozenset(('read', 'write', 'edit', 'glob', 'grep', 'read_image',
    'mcp__project_config__project_config', 'mcp__project_config__open_project_page'))
TOOL_MARKER = '[hud-tools] ready '

TOOL_ADAPTER = '''export const name = "hud-tool-scope";
export const inject = ["tools", "agents", "systemPrompt"];
export function apply(ctx, config) {
    ctx.systemPrompt.section({name: "hud:instructions",
        order: ctx.systemPrompt.getSectionOrder("DEPLOYMENT_PERSONA_SUFFIX") + 1,
        text: config.system});
    const allowed = new Set(["read", "write", "edit", "glob", "grep", "read_image",
        "mcp__project_config__project_config", "mcp__project_config__open_project_page"]);
    ctx.tools.guard(exec => allowed.has(exec.name) ? undefined : "Tool outside project assistant scope");
    ctx.on("agent/created", async ({agent, signal}) => {
        const deadline = Date.now() + 10000;
        for (;;) {
            signal?.throwIfAborted();
            const names = new Set(ctx.tools.schemas(agent).map(tool => tool.name));
            const missing = [...allowed].filter(name => !names.has(name));
            if (!missing.length) break;
            if (Date.now() >= deadline)
                throw new Error("DSH HUD missing required tools: " + missing.join(", "));
            await new Promise(resolve => setTimeout(resolve, 50));
        }
        agent.ctx.tools.restrict({allow: [...allowed]});
        const names = ctx.tools.schemas(agent).map(tool => tool.name);
        if (names.length !== allowed.size || names.some(name => !allowed.has(name)))
            throw new Error("DSH HUD tool scope mismatch");
        console.error("[hud-tools] ready " + JSON.stringify(names));
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
    adapter = scratch / 'hud-tools.mjs'
    adapter.write_text(TOOL_ADAPTER, encoding='utf-8')
    entries.append({'insert': [{'id': 'hud-tool-scope', 'name': str(adapter),
                                'config': {'system': system}}]})
    overlay = scratch / 'overlay.yml'
    overlay.write_text(native_runtime.overlay_yaml(entries), encoding='utf-8')
    command, home = native_runtime.dsh(bot, overlay_path=str(overlay))
    env = dict(env)
    for key in list(env):
        if key.upper() in {'DEEPSEEK_API_KEY', 'DSH_PROFILE', 'DSH_HOME',
                           'DSH_PERMISSION_MODE', 'DSH_TOOLS_MODE'}:
            env.pop(key)
    env['DSH_HOME'] = home
    env['DSH_PERMISSION_MODE'] = 'workspace-write'
    # The tool presentation remains a direct list of the scoped capabilities.
    env['DSH_TOOLS_MODE'] = 'native'
    state = {'tools': False, 'auto': []}

    def stderr(line):
        if TOOL_MARKER in line:
            try:
                names = json.loads(line.split(TOOL_MARKER, 1)[1])
                state['tools'] = (isinstance(names, list) and len(names) == len(REQUIRED_TOOLS)
                                  and all(isinstance(name, str) for name in names)
                                  and set(names) == REQUIRED_TOOLS)
            except ValueError:
                state['tools'] = False
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
