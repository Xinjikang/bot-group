"""Native Bot launch configuration shared by group members and project assistants."""
import json
import os

from . import bot_identity


def environment():
    inherited_python = {'__PYVENV_LAUNCHER__', 'PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV'}
    execution = {'HERMES_EPHEMERAL_SYSTEM_PROMPT', 'HERMES_CHAT_MODE', 'HERMES_PROFILE'}
    env = {key: value for key, value in os.environ.items()
           if key not in inherited_python | execution and not key.startswith(('GROUP_', 'XDG_GROUP_'))}
    env.update(PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
    return env


def toml_value(value):
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(key) + '=' + toml_value(val) for key, val in value.items()) + '}'
    return json.dumps(value, ensure_ascii=False)


def codex(bot, *, config_args=(), model_args=(), system_prompt='', resume=None, excluded_mcp=()):
    from . import codex_auth
    from .wake import _codex_bin
    home, profile = bot_identity.codex_settings(bot)
    codex_auth.sync(home)
    command = [_codex_bin(), 'exec', '--json', '--skip-git-repo-check', *config_args]
    if excluded_mcp:
        from pathlib import Path
        import tomlkit
        root = Path(home)
        path = root / (f'{profile}.config.toml' if profile and (root/f'{profile}.config.toml').is_file() else 'config.toml')
        saved = tomlkit.parse(path.read_text(encoding='utf-8')) if path.is_file() else {}
        selected = {**saved, **saved.get('profiles', {}).get(profile, {})} if profile else saved
        for name in excluded_mcp:
            if name in selected.get('mcp_servers', {}):
                command += ['-c', f'mcp_servers.{name}.enabled=false']
    if profile:
        command += ['--profile', profile]
    command += list(model_args)
    if system_prompt:
        command += ['-c', 'developer_instructions=' + json.dumps(system_prompt, ensure_ascii=False)]
    if resume:
        command += ['resume', resume]
    return command, home


def hermes_chat(profile, *, entry=None):
    from . import hermes_runtime
    return hermes_runtime.command(entry) + ['-p', profile, 'chat']
