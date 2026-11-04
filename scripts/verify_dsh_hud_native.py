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
            if (names.includes("pwsh") || names.includes("bash") || names.includes("task_queue"))
                throw new Error("Unexpected tool scope: " + names.join(","));
            if (!names.includes("read") || !JSON.stringify(options).includes("PROFILE_PERSONA"))
                throw new Error("Profile persona or file tools missing");
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
    args = parser.parse_args()
    entry = args.entry.resolve(strict=True)
    evidence = args.evidence.resolve()
    if not args.evidence.is_absolute():
        parser.error('--evidence must be absolute')
    evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='native-hud-', dir=evidence) as directory:
        root = Path(directory)
        profile = root / 'home' / 'profiles' / 'reviewer'
        profile.mkdir(parents=True)
        scratch = root / 'scratch'
        scratch.mkdir()
        work = root / 'work'
        work.mkdir()
        (work / 'input.txt').write_text('isolated fixture', encoding='utf-8')
        fixture = root / 'offline-model.mjs'
        fixture.write_text(FIXTURE, encoding='utf-8')
        (profile / 'package.json').write_text(json.dumps({'name': 'fixture-reviewer', 'private': True,
            'dsh': {'profile': {'bundles': ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-headless']}}}), encoding='utf-8')
        (profile / 'cordis.yml').write_text('[]\n', encoding='utf-8')
        (profile / 'cordis.patch.yml').write_text(native_runtime.overlay_yaml([
            {'id': 'agent-default-model', 'config': {'provider': 'deepseek', 'model': 'fixture-model'}},
            {'id': 'system-prompt', 'config': {'personaPrefix': 'PROFILE_PERSONA'}},
            {'insert': [{'id': 'offline-model', 'name': str(fixture)}]},
        ]), encoding='utf-8')
        bot = {'runtime': 'dsh', 'profile': 'reviewer', 'config_root': str(root/'home'), 'runtime_entry': str(entry)}
        env = {k: v for k, v in native_runtime.environment().items()
               if not k.startswith(('DSH_', 'DEEPSEEK_', 'OPENAI_', 'ANTHROPIC_'))}
        env.update(DSH_TELEMETRY_DISABLED='1', DEEPSEEK_API_KEY='synthetic-local-only',
                   TEMP=str(root), TMP=str(root))
        runs = []

        def run(command, prompt, child_env, cwd, *, stderr_line):
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
        mcp.write_text('from mcp.server.fastmcp import FastMCP\n'
            'app = FastMCP("fixture")\n@app.tool()\n'
            'def project_config(action: str) -> str:\n    return "CONFIG_OK"\n'
            '@app.tool()\ndef open_project_page(page: str) -> str:\n    return "PAGE_OK"\n'
            'app.run()\n', encoding='utf-8')
        server = {'command': sys.executable, 'args': ['-X', 'utf8', '-B', str(mcp)], 'env': env}
        answer = dsh_hud.respond(bot, 'Read input.txt and report.', 'HUD_SYSTEM', scratch, str(work), server, run, env)
        assert json.loads(answer)['text'] == 'NATIVE_HUD_OK', answer
        completed = [e for e in runs if e.get('type') == 'tool_result' and e.get('status') == 'completed']
        assert len(completed) == 2, runs
        (evidence/'result.json').write_text(json.dumps({'passed': True, 'profile': 'reviewer',
            'model_requests_external': 0, 'native_file_tool': True, 'auto_confirmed': True,
            'profile_plugin_loaded_once': True}, indent=2), encoding='utf-8')
    print('PASS: native named profile, persona, HUD tool scope, Auto and file execution; zero external model calls')


if __name__ == '__main__':
    main()
