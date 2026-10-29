"""Hermes inference bridge executed with a disposable home and zero tools."""
import json
import os
from pathlib import Path
import sys
import traceback

_STAGE = '启动'


def report(kind, text):
    print('HUD_PROGRESS:' + json.dumps({'kind': kind, 'text': text}, ensure_ascii=True), flush=True)


def stage(text):
    global _STAGE
    _STAGE = text
    report('stage', text)


def main():
    global _STAGE
    # Direct script execution adds engine/ before the standard library's queue.
    sys.path[:] = [p for p in sys.path if Path(p).resolve() != Path(__file__).resolve().parent]
    sys.path.insert(0, sys.argv[1])
    from dotenv import dotenv_values
    values = dotenv_values(Path(os.environ['HERMES_HOME']) / '.env')
    for key, value in values.items():
        if value is not None and not key.startswith(('HERMES_', 'CODEX_', 'GROUP_', 'XDG_')):
            os.environ[key] = value
    stage('加载宿主配置')
    from hermes_cli.config import load_config
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from run_agent import AIAgent
    config = load_config()
    model = config.get('model') or {}
    name = model.get('default') or model.get('model') or '' if isinstance(model, dict) else str(model)
    provider = model.get('provider', 'auto') if isinstance(model, dict) else 'auto'
    stage('解析模型连接')
    runtime = resolve_runtime_provider(requested=provider, target_model=name or None)
    allowed = ('api_key', 'base_url', 'provider', 'api_mode', 'command', 'args')
    stage('初始化模型客户端')
    agent = AIAgent(**{k: runtime[k] for k in allowed if k in runtime}, model=name,
                    enabled_toolsets=[], max_iterations=2, quiet_mode=True,
                    reasoning_callback=lambda text: report('reasoning_delta', text),
                    stream_delta_callback=lambda text: None,
                    reasoning_config=config.get('reasoning'),
                    save_trajectories=False, checkpoints_enabled=False)
    try:
        if agent.tools:
            raise RuntimeError('Maintenance inference requires a tool-free runtime')
        stage('正在等待模型响应')
        result = agent.run_conversation(sys.stdin.read())
        if result.get('error') or result.get('completed') is False:
            raise RuntimeError('Maintenance model request failed')
        print('HUD_RESULT:' + json.dumps(json.loads(result['final_response'].strip().removeprefix('```json').removeprefix('```').removesuffix('```')), ensure_ascii=True), flush=True)
    finally:
        if hasattr(agent, 'close'):
            agent.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        frames = [{'file': Path(f.filename).name, 'line': f.lineno, 'function': f.name}
                  for f in traceback.extract_tb(exc.__traceback__)[-5:]]
        detail = {'stage': _STAGE, 'type': type(exc).__name__, 'frames': frames}
        if isinstance(exc, ModuleNotFoundError):
            detail['module'] = exc.name
        print('HUD_ERROR:' + json.dumps(detail, ensure_ascii=True), flush=True)
        sys.exit(1)
