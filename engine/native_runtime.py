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


def dsh_execution_profile(bot):
    """Desktop uses the background adapter; named CLI profiles retain their identity."""
    profile = bot.get('profile') or 'headless'
    return 'headless' if profile == 'desktop' else profile


def dsh(bot, *, overlay_path='', resume=None, native_auto=False):
    """DSH headless 命令（req#137 task#266：spawn 分叉的 dsh 侧入口）。

    用已绑定的 ``bots.runtime_entry``（dsh.cmd，自带 ELECTRON_RUN_AS_NODE 与 exe/cli 定位），
    使用成员的 CLI profile；desktop 使用 headless 适配。提示词经 stdin 提交。
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
    command = [entry, '--profile', dsh_execution_profile(bot), '--patch', str(overlay_path), '--json']
    if native_auto:
        command += ['--permission-mode', 'auto', '--approval-policy', 'never']
    if resume:
        command += ['--session-id', resume]
    command += ['-']  # task 读 stdin
    return command, str(Path(bot['config_root']).resolve())


# req#138 F5：项目覆盖字段名映射——项目模型统一用下划线 reasoning_effort，DSH 原生
# agent-default-model 用驼峰 reasoningEffort。
_DASH_PROJECT_FIELD = {'model': 'model', 'provider': 'provider', 'reasoning_effort': 'reasoningEffort'}
# req#138 F4：认证 overlay 会把非控制的 provider 条目（经 tolerant parse 降级 !!js）重发；
# ruamel 已原样载入这些条目，跳过重发以保留 !!js 动态表达式。
_AUTH_NON_CONTROL = frozenset({'agent-default-model', 'llm-deepseek', 'llm-pi-ai'})


def dsh_overlay(bot, *, credentials_path=None, mcp_entry=None, project_overrides=None,
                auto_review=None) -> list:
    """构建 DSH spawn 的完整 ``--patch`` overlay（req#137 task#266/#268，req#138 F3/F4/F5）。

    = bot profile ``cordis.patch.yml`` 全部条目（264 保存的 persona/model/provider/
    reasoning_effort 等）∪ dsh_auth 认证控制（同名条目覆盖启停与凭据路径）∪
    项目级 model/provider/reasoningEffort 覆盖 ∪ MCP 客户端 insert ∪ Auto 审批
    （auto_review={adapter_path} 时插入 auto-review 插件 + 受控适配插件）。

    req#138 F4：用 ruamel round-trip 读取，保留无 id 的 ``insert`` 插件条目与 ``!!js``
    动态表达式（原样透传，DSH 原生求值），解析失败显式报错而非回空丢失 persona。
    req#138 F3：认证路由按含项目覆盖的最终 provider 统一解析（见 overlay_entries 的
    final_provider）。req#138 F5：项目覆盖 reasoning_effort 映射为原生 reasoningEffort。
    返回值含 ruamel 类型（``!!js`` 为带标签标量），须用 :func:`overlay_yaml` 序列化。
    """
    from pathlib import Path
    from . import dsh_auth
    home = Path(bot['config_root']).resolve()
    patch = home / 'profiles' / bot['profile'] / 'cordis.patch.yml'
    entries: list = []
    if patch.is_file():
        from ruamel.yaml import YAML
        parser = YAML(typ='rt')
        try:
            loaded = parser.load(patch.read_text(encoding='utf-8-sig'))
        except Exception as exc:  # 解析失败显式报错，不回空丢失 persona
            from fastapi import HTTPException
            raise HTTPException(400, f'cordis.patch.yml 解析失败：{exc}') from exc
        entries = [e for e in (loaded or []) if isinstance(e, dict)]
    if dsh_execution_profile(bot) == bot['profile']:
        # The native loader applies this profile itself. Replaying inserts would
        # register plugins twice; only the model's merged project override needs
        # its original config in the execution layer.
        controlled = {'credentials', 'authorization', 'deepseek-account',
                      'session-log-deepseek', 'session-telemetry-otel'}
        if project_overrides:
            controlled.add('agent-default-model')
        entries = [e for e in entries if e.get('id') in controlled]
    # 认证控制优先（credentials 路径/启停），同 id 覆盖 profile 原条目；按最终 provider 路由。
    final_provider = (project_overrides or {}).get('provider') or None
    for control in dsh_auth.overlay_entries(
            bot, credentials_path=str(credentials_path) if credentials_path else None,
            final_provider=final_provider):
        if str(control.get('id') or '') in _AUTH_NON_CONTROL:
            # req#138 F4：跳过非控制 provider 条目重发，保留 ruamel 载入的 !!js 原值。
            continue
        _replace_or_append(entries, control)
    # 项目级覆盖：只改 agent-default-model 的 config 字段，保留其余字段；字段名映射原生。
    if project_overrides:
        target = _find_entry(entries, 'agent-default-model')
        if target is None:
            target = {'id': 'agent-default-model'}
            entries.append(target)
        config = target.get('config')
        if not isinstance(config, dict):
            config = {}
            target['config'] = config
        for field, value in project_overrides.items():
            if value:
                config[_DASH_PROJECT_FIELD.get(field, field)] = value
    if mcp_entry:
        entries.append({'insert': [mcp_entry]})
    if auto_review:
        # req#137 task#268：Auto 审批 insert（插件加载 + session/created 切 auto 的
        # 受控适配插件）。插入层优先级最高，覆盖 profile patch 的 auto-review 条目。
        from . import dsh_auto
        entries.extend(dsh_auto.overlay_inserts(str(auto_review['adapter_path'])))
    return entries


def _replace_or_append(entries: list, control: dict) -> None:
    """同 id 就地合并控制字段（disabled 等非 config 标量键），保留原条目 config。

    req#138 返修：ruamel 载入的条目 config 含 !!js 动态表达式与未知字段，控制项经
    dsh_auth 的 tolerant parse 已把 !!js 降级为 '[expression]'——整项替换会丢原生标签。
    故只合并 disabled 等标量键，config 保留 ruamel 原值；唯 credentials 的 config 是本次
    执行的凭据副本路径（spawn 专用，须覆盖）。新 id 追加尾部（保持 cordis patch 顺序）。

    认证控制（deepseek-account/credentials/authorization/telemetry 等）是最终有效状态：
    须应用到**所有**同 id 条目（而非首个），否则重复 id 的末项仍保留旧启停状态，导致
    认证路由与最终 provider 分裂（如两个 deepseek-account 只禁首项，末项仍启用）。
    """
    cid = str(control.get('id') or '')
    matched = False
    for entry in entries:
        if not isinstance(entry, dict) or str(entry.get('id') or '') != cid:
            continue
        matched = True
        for key, value in control.items():
            if key == 'id':
                continue
            if key == 'config':
                if cid == 'credentials' or 'config' not in entry or entry.get('config') is None:
                    entry[key] = value
                # 其余情况保留 ruamel 原 config（含 !!js/未知字段），不覆盖。
                continue
            entry[key] = value
    if not matched:
        entries.append(control)


def _find_entry(entries: list, entry_id: str):
    """返回 id 匹配的最后一个条目（cordis patch 顺序：后者覆盖前者，末位为有效条目）。

    req#138 返修：重复 agent-default-model 时项目覆盖须落在末位（原生生效项），否则
    认证路由（按最终 provider）与有效 provider 分裂。
    """
    found = None
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get('id') or '') == entry_id:
            found = entry
    return found


def overlay_yaml(overlay: list) -> str:
    """把 dsh_overlay 的返回值序列化为 YAML（保留 ``!!js`` 动态表达式与 insert 条目）。

    ``--patch`` 接受 YAML；JSON 无法表示 ``!!js`` 标签，故改用 YAML 输出（req#138 F4）。
    """
    import io
    from ruamel.yaml import YAML
    parser = YAML(typ='rt')
    parser.width = 4096
    buf = io.StringIO()
    parser.dump(overlay, buf)
    return buf.getvalue()


# 已验证事件契约的 DSH 包版本（0.2.1-rc.5；其它版本以启动证据为准，见 design）。
_DSH_VERIFIED_VERSION = '0.2.1-rc.5'
_dsh_version_cache: dict[str, tuple] = {}


def dsh_version(entry: str) -> str:
    """Read the bound CLI package or desktop app.asar version.

    CLI wrappers resolve through adjacent package manifests. Desktop archive
    versions are cached by mtime. An unreadable installation returns an empty value.
    """
    try:
        from pathlib import Path
        if not entry:
            return ''
        # CLI packages and unpacked desktop bundles expose their own manifest.
        parents = list(Path(entry).resolve().parents)[:5]
        manifests = [base / 'package.json' for parent in parents for base in (
            parent, parent / 'node_modules' / '@deepseek-ai' / 'dsh',
            parent / '@deepseek-ai' / 'dsh')]
        for manifest in manifests:
            if manifest.is_file():
                info = json.loads(manifest.read_text(encoding='utf-8'))
                if info.get('name') == '@deepseek-ai/dsh':
                    return str(info.get('version') or '')
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
