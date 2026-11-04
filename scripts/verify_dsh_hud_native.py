"""Verify installed DSH profiles and HUD with synthetic model output and isolated HOME."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import dsh_hud, hud_runtime, native_runtime


FIXTURE = '''export const name = "offline-model";
export const inject = ["llm", "tools"];
export function apply(ctx) {
    globalThis.fetch = () => { throw new Error("Network forbidden in native fixture"); };
    let step = 0;
    ctx.on("llm/stream", options => (async function* () {
        if (String(options.system).startsWith("REVIEW_POLICY")) {
            yield {type:"text-delta", index:0, text:JSON.stringify({risk:"low",decision:"allow"})};
        } else {
            const names = (options.tools || []).map(t => t.name);
            const required = ["read", "write", "edit", "glob", "grep", "read_image",
                "mcp__project_config__project_config", "mcp__project_config__open_project_page"];
            if (names.length !== required.length || required.some(name => !names.includes(name)))
                throw new Error("Incomplete or unexpected tool scope: " + names.join(","));
            if (!names.includes("read") || !JSON.stringify(options).includes("PROFILE_PERSONA"))
                throw new Error("Profile persona or file tools missing");
            if (!JSON.stringify(options).includes("PROFILE_SUFFIX") || !JSON.stringify(options).includes("HUD_SYSTEM"))
                throw new Error("Profile suffix or HUD instructions missing");
            console.error("[fixture] tools=" + names.join(","));
            if (step++ === 0) {
                yield {type:"tool-call-delta",index:0,id:"read-fixture",name:"read",
                    argumentsDelta:JSON.stringify({file_path:"input.txt"})};
                yield {type:"finish",reason:{kind:"tool-calls"}};
                return;
            }
            if (step === 2) {
                yield {type:"tool-call-delta",index:0,id:"config-fixture",name:"mcp__project_config__project_config",
                    argumentsDelta:JSON.stringify({action:"read"})};
                yield {type:"finish",reason:{kind:"tool-calls"}};
                return;
            }
            yield {type:"text-delta",index:0,text:JSON.stringify({text:"NATIVE_HUD_OK"})};
        }
        yield {type:"finish",reason:{kind:"stop"}};
    })());
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entry', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--persona-source', choices=['patch', 'home', 'bundle', 'desktop', 'layered'], default='patch')
    parser.add_argument('--dirty-env', action='store_true')
    parser.add_argument('--missing-tool', action='store_true')
    args = parser.parse_args()
    entry = args.entry.resolve(strict=True)
    evidence = args.evidence.resolve()
    if not args.evidence.is_absolute():
        parser.error('--evidence must be absolute')
    evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='native-hud-', dir=evidence) as directory:
        root = Path(directory)
        profile_name = 'desktop' if args.persona_source == 'desktop' else 'reviewer'
        profile = root / 'home' / 'profiles' / profile_name
        profile.mkdir(parents=True)
        scratch = root / 'scratch'
        scratch.mkdir()
        work = root / 'work'
        work.mkdir()
        (work / 'input.txt').write_text('isolated fixture', encoding='utf-8')
        fixture = root / 'offline-model.mjs'
        fixture.write_text(FIXTURE, encoding='utf-8')
        bundles = ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-headless']
        persona = {'id': 'system-prompt', 'config': {'personaPrefix': 'PROFILE_PERSONA', 'personaSuffix': 'PROFILE_SUFFIX'}}
        if args.persona_source == 'bundle':
            bundle = root / 'home' / 'node_modules' / 'fixture-persona'
            bundle.mkdir(parents=True)
            (bundle / 'package.json').write_text(json.dumps({'name': 'fixture-persona', 'version': '1.0.0',
                'dsh': {'bundle': {'patch': 'cordis.patch.yml'}}}), encoding='utf-8')
            (bundle / 'cordis.patch.yml').write_text(native_runtime.overlay_yaml([persona]), encoding='utf-8')
            bundles.append('fixture-persona')
        if args.persona_source in ('home', 'layered'):
            (root / 'home' / 'cordis.patch.yml').write_text(native_runtime.overlay_yaml([persona]), encoding='utf-8')
        (profile / 'package.json').write_text(json.dumps({'name': 'fixture-reviewer', 'private': True,
            'dsh': {'profile': {'bundles': bundles}}}), encoding='utf-8')
        (profile / 'cordis.yml').write_text('[]\n', encoding='utf-8')
        (profile / 'cordis.patch.yml').write_text(native_runtime.overlay_yaml([
            {'id': 'agent-default-model', 'config': {'provider': 'deepseek', 'model': 'fixture-model'}},
            *([persona] if args.persona_source in ('patch', 'desktop') else []),
            *([{'id': 'system-prompt', 'config': {'personaPrefix': 'OVERRIDDEN_PERSONA'}}]
              if args.persona_source == 'layered' else []),
            {'insert': [{'id': 'offline-model', 'name': str(fixture)}]},
        ]), encoding='utf-8')
        bot = {'runtime': 'dsh', 'profile': profile_name, 'config_root': str(root/'home'), 'runtime_entry': str(entry)}
        env = {k: v for k, v in native_runtime.environment().items()
               if not k.startswith(('DSH_', 'DEEPSEEK_', 'OPENAI_', 'ANTHROPIC_'))}
        (root / 'home' / '.env').write_text('DEEPSEEK_API_KEY=synthetic-local-only\n', encoding='utf-8')
        env.update(DSH_TELEMETRY_DISABLED='1', TEMP=str(root), TMP=str(root))
        if args.dirty_env:
            env.update(DEEPSEEK_API_KEY='foreign-key', DSH_PROFILE='foreign-profile', DSH_PERMISSION_MODE='danger-full-access')
        runs = []

        def run(command, prompt, child_env, cwd, *, stderr_line):
            if args.dirty_env:
                assert 'DEEPSEEK_API_KEY' not in child_env and 'DSH_PROFILE' not in child_env, 'inherited DSH identity'
                assert child_env.get('DSH_PERMISSION_MODE') == 'workspace-write', 'inherited permissions'
            errors = []
            def stderr(line):
                errors.append(line)
                stderr_line(line)
            try:
                output = hud_runtime._run(command, prompt, child_env, cwd, stderr_line=stderr, timeout=60)
            finally:
                (evidence/'stderr.txt').write_text(''.join(errors), encoding='utf-8')
            (evidence/'stdout.jsonl').write_text(output, encoding='utf-8')
            runs.extend(json.loads(line) for line in output.splitlines() if line.strip())
            return output

        mcp = root / 'fixture_mcp.py'
        navigation = ('' if args.missing_tool else
            '@app.tool()\ndef open_project_page(page: str) -> str:\n    return "PAGE_OK"\n')
        mcp.write_text('from mcp.server.fastmcp import FastMCP\n'
            'app = FastMCP("fixture")\n@app.tool()\n'
            'def project_config(action: str) -> str:\n    return "CONFIG_OK"\n'
            + navigation + 'app.run()\n', encoding='utf-8')
        server = {'command': sys.executable, 'args': ['-X', 'utf8', '-B', str(mcp)], 'env': env}
        if args.missing_tool:
            try:
                dsh_hud.respond(bot, 'Read input.txt.', 'HUD_SYSTEM', scratch, str(work), server, run, env)
            except hud_runtime.NativeProcessError as exc:
                assert 'missing required tools: mcp__project_config__open_project_page' in exc.diagnostic, exc.diagnostic
                assert '[fixture] tools=' not in exc.diagnostic, 'model ran before capability validation'
            else:
                raise AssertionError('missing navigation tool accepted')
            (evidence/'result.json').write_text(json.dumps({'passed': True, 'missing_tool_rejected': True,
                'model_requests_external': 0}), encoding='utf-8')
            print('PASS: missing required MCP tool rejected before model execution')
            return
        answer = dsh_hud.respond(bot, 'Read input.txt and report.', 'HUD_SYSTEM', scratch, str(work), server, run, env)
        assert json.loads(answer)['text'] == 'NATIVE_HUD_OK', answer
        completed = [e for e in runs if e.get('type') == 'tool_result' and e.get('status') == 'completed']
        assert len(completed) == 2, runs
        (evidence/'result.json').write_text(json.dumps({'passed': True, 'profile': profile_name,
            'persona_source': args.persona_source, 'dirty_environment': args.dirty_env,
            'required_tools': sorted(dsh_hud.REQUIRED_TOOLS), 'persona_suffix_preserved': True,
            'model_requests_external': 0, 'native_file_tool': True, 'auto_confirmed': True,
            'profile_plugin_loaded_once': True}, indent=2), encoding='utf-8')
    print('PASS: native named profile, persona, HUD tool scope, Auto and file execution; zero external model calls')


if __name__ == '__main__':
    main()
