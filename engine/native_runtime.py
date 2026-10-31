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


def dsh(bot, *, overlay_path='', resume=None):
    """DSH headless 命令（req#137 task#266：spawn 分叉的 dsh 侧入口）。

    用已绑定的 ``bots.runtime_entry``（dsh.cmd，自带 ELECTRON_RUN_AS_NODE 与 exe/cli 定位），
    ``--profile headless`` 启动；提示词经 stdin 提交（task 省略时读 stdin，``-`` 显式）。
    ``--session-id`` 续跑；``--patch`` 注入本次执行 overlay（认证/MCP/配置）。
    返回 ``(command, home)``。
    """
    from pathlib import Path
    entry = (bot.get('runtime_entry') or '').strip()
    if not entry:
        raise FileNotFoundError('DSH 入口未绑定：请重新扫描并选择 DSH 宿主')
    if not Path(entry).is_file():
        raise FileNotFoundError(f'DSH 入口不可用：{entry}')
    # 注意：--patch 必须位于 --json 之前（headless 子命令的 --json 之后不再识别顶层
    # --patch，实测 0.2.1-rc.5：--profile headless --json --patch X 报 unknown option）。
    command = [entry, '--profile', 'headless', '--patch', str(overlay_path), '--json']
    if resume:
        command += ['--session-id', resume]
    command += ['-']  # task 读 stdin
    return command, str(Path(bot['config_root']).resolve())


def dsh_overlay(bot, *, credentials_path=None, mcp_entry=None, project_overrides=None,
                auto_review=None) -> list:
    """构建 DSH spawn 的完整 ``--patch`` overlay（req#137 task#266/#268）。

    = bot profile ``cordis.patch.yml`` 全部条目（264 保存的 persona/model/provider/
    reasoning_effort 等）∪ dsh_auth 认证控制（同名条目覆盖启停与凭据路径）∪
    项目级 model/provider/reasoning_effort 覆盖 ∪ MCP 客户端 insert ∪ Auto 审批
    （auto_review={adapter_path} 时插入 auto-review 插件 + 受控适配插件）。动态表达式
    （``!!js``）条目原样透传（DSH 原生求值），只做静态拼装。
    """
    import yaml as _yaml
    from pathlib import Path
    from . import dsh_auth
    home = Path(bot['config_root']).resolve()
    patch = home / 'profiles' / bot['profile'] / 'cordis.patch.yml'
    entries: list = []
    if patch.is_file():
        try:
            entries = _yaml.safe_load(patch.read_text(encoding='utf-8-sig')) or []
        except Exception:
            entries = []
    merged: dict[str, dict] = {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get('id'):
            merged[str(entry['id'])] = dict(entry)
    # 认证控制优先（credentials 路径/启停），同 id 覆盖 profile 原条目。
    for control in dsh_auth.overlay_entries(
            bot, credentials_path=str(credentials_path) if credentials_path else None):
        merged[str(control['id'])] = dict(control)
    # 项目级覆盖：只改 agent-default-model 的 config 字段，保留其余字段。
    if project_overrides:
        target = merged.setdefault('agent-default-model', {'id': 'agent-default-model'})
        config = target.setdefault('config', {})
        if not isinstance(config, dict):
            config = {}
            target['config'] = config
        for field, value in project_overrides.items():
            if value:
                config[field] = value
    out = list(merged.values())
    if mcp_entry:
        out.append({'insert': [mcp_entry]})
    if auto_review:
        # req#137 task#268：Auto 审批 insert（插件加载 + session/created 切 auto 的
        # 受控适配插件）。插入层优先级最高，覆盖 profile patch 的 auto-review 条目。
        from . import dsh_auto
        out.extend(dsh_auto.overlay_inserts(str(auto_review['adapter_path'])))
    return out


# 已验证事件契约的 DSH 包版本（0.2.1-rc.5；其它版本以启动证据为准，见 design）。
_DSH_VERIFIED_VERSION = '0.2.1-rc.5'
_dsh_version_cache: dict[str, tuple] = {}


def dsh_version(entry: str) -> str:
    """读 DSH 安装的包版本（app.asar 头里 package.json 的 version，按 mtime 缓存）。

    ``entry`` 为 dsh.cmd；asar 位于其上级三级目录 ``resources/app.asar``。返回 '' 表示
    不可读（入口失效/布局变化），调用方提示重新扫描或按未知版本处理。
    """
    try:
        from pathlib import Path
        asar = Path(entry).resolve().parent.parent.parent.parent / 'app.asar'
        if not asar.is_file():
            return ''
        key = str(asar)
        mtime = asar.stat().st_mtime_ns
        cached = _dsh_version_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
        import json as _json
        import struct as _struct
        with open(asar, 'rb') as stream:
            head = stream.read(16)
            if len(head) < 16:
                return ''
            json_size = _struct.unpack('<I', head[12:16])[0]
            header = _json.loads(stream.read(json_size))
        base = (16 + json_size + 3) & ~3  # 4 字节对齐 base
        pkg = (header.get('files') or {}).get('package.json') or {}
        if not pkg.get('size'):
            return ''
        with open(asar, 'rb') as stream:
            stream.seek(int(pkg['offset']) + base)
            info = _json.loads(stream.read(int(pkg['size'])))
        version = str(info.get('version') or '')
        _dsh_version_cache[key] = (mtime, version)
        return version
    except Exception:
        return ''
