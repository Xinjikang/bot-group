"""Hermes inference bridge executed with a disposable home and zero tools."""
import json
import os
from pathlib import Path
import sys


def main():
    sys.path.insert(0, sys.argv[1])
    from dotenv import load_dotenv
    load_dotenv(Path(os.environ['HERMES_HOME']) / '.env', override=True)
    from hermes_cli.config import load_config
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from run_agent import AIAgent
    config = load_config()
    model = config.get('model') or {}
    name = model.get('default') or model.get('model') or '' if isinstance(model, dict) else str(model)
    provider = model.get('provider', 'auto') if isinstance(model, dict) else 'auto'
    runtime = resolve_runtime_provider(requested=provider, target_model=name or None)
    allowed = ('api_key', 'base_url', 'provider', 'api_mode', 'command', 'args')
    agent = AIAgent(**{k: runtime[k] for k in allowed if k in runtime}, model=name,
                    enabled_toolsets=[], max_iterations=2, quiet_mode=True,
                    save_trajectories=False, checkpoints_enabled=False)
    if agent.tools:
        raise RuntimeError('Maintenance inference requires a tool-free runtime')
    try:
        result = agent.run_conversation(sys.stdin.read())
        if result.get('error') or result.get('completed') is False:
            raise RuntimeError('Maintenance model request failed')
        print('HUD_RESULT:' + result['final_response'])
    finally:
        if hasattr(agent, 'close'):
            agent.close()


if __name__ == '__main__':
    main()
