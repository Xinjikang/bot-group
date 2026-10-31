"""DSH member settings backed by ``cordis.patch.yml``, with revision and backups.

``cordis.patch.yml`` 是 YAML 列表，每项形如 ``{id, disabled?, config?, insert?}``。
本模块只读写已知字段对应的条目 ``config``，其余条目与未知字段通过 round-trip 保留。

字段映射（按 dsh 0.2.1-rc.5 实装源码收敛）：

    persona          -> system-prompt.config.personaPrefix      （dsh-system-prompt 实装
                       ``personaPrefix: z.string().default("")``，probe-persona2 实测命中）
    model            -> agent-default-model.config.model
    provider         -> agent-default-model.config.provider
    reasoning_effort -> agent-default-model.config.reasoningEffort
    instructions     -> DSH_HOME/AGENTS.md（dsh-agent-instructions 加载用户全局指令链
                       ``$DSH_HOME/AGENTS.md``；其 config 仅 maxBytes 等预算字段，无指令正文）
    description      -> profiles/<name>/.group-engine-description（DSH 无原生描述字段，
                       存引擎侧边文件，不写入 cordis.patch.yml）
    env              -> DSH_HOME/.env（``DEEPSEEK_API_KEY=...``）
    auto_review      -> profiles/<name>/.group-engine-auto-review（'on'/'off' 引擎侧边文件，
                       Auto 审批启用开关；插件条目由 spawn overlay insert，不写入 patch）

含动态表达式（``!!js``）的 patch 无法安全 round-trip，保存前拒绝为只读。锚点/别名由
ruamel 原生 round-trip 保留，不再拒绝（避免误拒 ``A&B`` / ``*重点*`` 等合法字符串内容）。
凭据出现在错误消息时按脱敏规则替换。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi import HTTPException
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import YAMLError
import yaml as _pyyaml

from . import bot_identity, db, profile_config as files


class _TolerantLoader(_pyyaml.SafeLoader):
    """容忍 cordis 动态表达式（``!!js``），只读解析不执行。"""


def _js_expression(loader, node):
    return '[expression]'


_TolerantLoader.add_constructor('tag:yaml.org,2002:js', _js_expression)


# cordis.patch.yml 条目映射（entry id -> config key）。
_PATCH_FIELDS = {
    'persona': ('system-prompt', 'personaPrefix'),
    'model': ('agent-default-model', 'model'),
    'provider': ('agent-default-model', 'provider'),
    'reasoning_effort': ('agent-default-model', 'reasoningEffort'),
}
_SINGLE_LINE = ('model', 'provider', 'reasoning_effort')
# DSH reasoningEffort 档位（desktop 实测用 max，其余对齐 Codex 档位）。
_REASONING_LEVELS = ('', 'minimal', 'low', 'medium', 'high', 'max', 'ultra')
# 动态表达式（!!js）无法安全 round-trip；锚点/别名由 ruamel 原生保留，不再拒绝。
_AMBIGUOUS = re.compile(r'!!js\b')
# 凭据脱敏：sk-* 密钥、bearer token、api_key=... 等。
_CREDENTIAL = re.compile(r'(?i)(sk-[A-Za-z0-9_-]{6,}|bearer\s+\S+|api[_-]?key\s*[:=]\s*\S+)')
# 字段 -> (依赖插件 id, 包名)；description/env 无插件依赖（引擎侧边文件 / DSH 原生 .env）。
_PLUGIN_BACKING = {
    'persona': ('system-prompt', '@deepseek-ai/dsh-system-prompt'),
    'model': ('agent-default-model', '@deepseek-ai/dsh-agent-default-model'),
    'provider': ('agent-default-model', '@deepseek-ai/dsh-agent-default-model'),
    'reasoning_effort': ('agent-default-model', '@deepseek-ai/dsh-agent-default-model'),
    'instructions': ('dsh-agent-instructions', '@deepseek-ai/dsh-agent-instructions'),
    'description': (None, None),
    'env': (None, None),
    'auto_review': ('auto-review', '@deepseek-ai/dsh-experimental-auto-review'),
}


def _pkg_installed(home: Path, profile: str, pkg_name: str) -> bool:
    """插件包目录存在性（profile/home 的 node_modules）。

    scoped 包名保留 ``@``（``@scope/name`` → ``node_modules/@scope/name``），bundle 内
    插件（asar 内、node_modules 不可见）不在此判定范围，由调用方按继承语义处理。
    """
    parts = pkg_name.split('/')
    for base in (home / 'profiles' / profile / 'node_modules', home / 'node_modules'):
        if base.joinpath(*parts).is_dir():
            return True
    return False


def _plugin_status(home: Path, profile: str, seq: list, patch: Path) -> dict:
    """按字段返回插件状态（state/plugin/reason/path）。

    依据有效继承（bundle→profile→home→CLI，编辑点为用户 patch 层）与包解析结果判定：
    - 用户 patch 未覆盖该条目 → 继承 bundle 默认插件，视为可用（ok），不再误判 missing；
    - 用户覆盖 disabled → disabled；
    - 用户显式指定非 bundle 包名且该包未安装 → load_failed；
    - bundle 默认包（asar 内 node_modules 不可见）或包已安装 → ok。
    真实运行时加载失败（bundle 插件在 asar 内）由 266 启动探测补足。
    """
    out: dict[str, dict] = {}
    for field, (plugin_id, pkg_name) in _PLUGIN_BACKING.items():
        base = {'plugin': plugin_id, 'path': str(patch)}
        if plugin_id is None:
            out[field] = dict(base, state='ok', reason='')
            continue
        entry = next((e for e in seq if isinstance(e, dict) and str(e.get('id') or '') == plugin_id), None)
        if entry is None:
            out[field] = dict(base, state='ok', reason='')
        elif entry.get('disabled'):
            out[field] = dict(base, state='disabled', reason=f'{plugin_id} 插件已禁用')
        elif entry.get('name') and entry['name'] != pkg_name and not _pkg_installed(home, profile, entry['name']):
            out[field] = dict(base, state='load_failed', reason=f'{entry["name"]} 未安装或加载失败')
        else:
            out[field] = dict(base, state='ok', reason='')
    return out


def _sanitize(message: str) -> str:
    return _CREDENTIAL.sub('[REDACTED]', str(message))


def resolve(member: str):
    conn = db.get_connection()
    try:
        bot = bot_identity.global_bot(conn, member)
        return bot if bot and bot['runtime'] == 'dsh' else None
    finally:
        conn.close()


def _layout(bot) -> tuple[Path, Path, Path, Path, Path, Path]:
    """返回 (home, patch, env, agents_md, description_file, auto_review_file)。"""
    home = Path(bot['config_root']).resolve()
    profile = bot['profile']
    patch = home / 'profiles' / profile / 'cordis.patch.yml'
    env = home / '.env'
    agents = home / 'AGENTS.md'
    desc = home / 'profiles' / profile / '.group-engine-description'
    auto = home / 'profiles' / profile / '.group-engine-auto-review'
    return home, patch, env, agents, desc, auto


def _snapshot(paths) -> dict[str, bytes | None]:
    return {str(p): files._snapshot(p.parent, [p.name])[p.name] for p in dict.fromkeys(paths)}


def _yaml(content: bytes | None):
    parser = YAML()
    parser.preserve_quotes = True
    parser.width = 4096
    parsed = parser.load(files._text(content))
    if parsed is None:
        parsed = CommentedSeq()
    if not isinstance(parsed, CommentedSeq):
        raise HTTPException(400, 'cordis.patch.yml 顶层必须是 YAML 列表')
    return parser, parsed


def _entry(seq: CommentedSeq, entry_id: str):
    for item in seq:
        if isinstance(item, CommentedMap) and str(item.get('id') or '') == entry_id:
            return item
    return None


def _parse(raw_text: str) -> list:
    """只读解析 cordis.patch.yml 顶层列表，容忍 ``!!js`` 动态表达式。"""
    parsed = _pyyaml.load(raw_text, Loader=_TolerantLoader)
    if parsed is None:
        return []
    if not isinstance(parsed, list):
        raise HTTPException(400, 'cordis.patch.yml 顶层必须是 YAML 列表')
    return parsed


def _view(home: Path, profile: str, patch: Path, file_paths: dict[str, Path],
          snapshot: dict[str, bytes | None]) -> dict:
    seq = _parse(files._text(snapshot[str(patch)]))
    view = {'runtime': 'dsh', 'revision': files._revision(snapshot), 'config_path': str(patch)}
    for field, (entry_id, key) in _PATCH_FIELDS.items():
        entry = next((e for e in seq if isinstance(e, dict) and str(e.get('id') or '') == entry_id), None)
        config = entry.get('config') if entry is not None else None
        view[field] = str(config[key] or '') if isinstance(config, dict) and key in config else ''
    for field, path in file_paths.items():
        view[field] = files._text(snapshot.get(str(path)))
    # auto_review 侧边文件归一化为 'on'/'off'（缺失/其他内容一律 off，fail-closed）。
    view['auto_review'] = 'on' if (view.get('auto_review') or '').strip() == 'on' else 'off'
    view['plugins'] = _plugin_status(home, profile, seq, patch)
    return view


def read(bot) -> dict:
    try:
        with files._LOCK:
            home, patch, env, agents, desc, auto = _layout(bot)
            if not patch.is_file():
                raise HTTPException(404, 'DSH profile cordis.patch.yml does not exist')
            paths = [patch, env, agents, desc, auto]
            file_paths = {'env': env, 'instructions': agents, 'description': desc,
                          'auto_review': auto}
            return _view(home, bot['profile'], patch, file_paths, _snapshot(paths))
    except YAMLError as exc:
        raise HTTPException(400, _sanitize(f'cordis.patch.yml 解析失败：{exc}')) from None
    except UnicodeError:
        raise HTTPException(400, 'cordis.patch.yml 必须是有效 UTF-8') from None
    except OSError:
        raise HTTPException(500, 'Cannot read DSH member configuration') from None


def _commit(before: dict, after: dict) -> list[str]:
    """多目录原子写：先备份、复核未变、再替换，失败回滚。"""
    staged: dict[str, Path] = {}
    replaced: list[str] = []
    backups: list[str] = []
    try:
        for path, content in after.items():
            if content != before[path]:
                staged[path] = files._stage(Path(path).parent, content)
        if _snapshot(map(Path, before)) != before:
            raise HTTPException(409, 'Configuration changed; reopen the editor')
        grouped: dict[Path, dict[str, bytes | None]] = {}
        for path in staged:
            p = Path(path)
            grouped.setdefault(p.parent, {})[p.name] = before[path]
        for folder, originals in grouped.items():
            backups.append(str(files._backup(folder, originals)))
        if _snapshot(map(Path, before)) != before:
            raise HTTPException(409, 'Configuration changed; reopen the editor')
        for path, temp in staged.items():
            os.replace(temp, path)
            replaced.append(path)
    except Exception:
        recovery_failed = False
        for path in reversed(replaced):
            try:
                if before[path] is None:
                    Path(path).unlink(missing_ok=True)
                else:
                    temp = files._stage(Path(path).parent, before[path])
                    try:
                        os.replace(temp, path)
                    finally:
                        temp.unlink(missing_ok=True)
            except OSError:
                recovery_failed = True
        if recovery_failed:
            raise HTTPException(500, 'Save and rollback failed; originals remain in .group-engine-backups') from None
        raise
    finally:
        for temp in staged.values():
            temp.unlink(missing_ok=True)
    return backups


def save(bot, values: dict) -> dict:
    try:
        with files._LOCK:
            home, patch, env, agents, desc, auto = _layout(bot)
            if not patch.is_file():
                raise HTTPException(404, 'DSH profile cordis.patch.yml does not exist')
            paths = [patch, env, agents, desc, auto]
            file_paths = {'env': env, 'instructions': agents, 'description': desc}
            before = _snapshot(paths)
            current = _view(home, bot['profile'], patch,
                            dict(file_paths, auto_review=auto), before)
            if values['revision'] != current['revision']:
                raise HTTPException(409, 'Configuration changed; reopen the editor')
            raw = files._text(before[str(patch)])
            if _AMBIGUOUS.search(raw):
                raise HTTPException(400, 'cordis.patch.yml 含动态表达式，为避免覆盖损坏请先在 DSH 宿主中解析')
            parser, seq = _yaml(before[str(patch)])
            # req#137 task#268：Auto 审批开关。启用前校验插件状态（可选能力规则）：
            # 插件禁用/未安装时拒绝保存，避免「已启用审批」在运行时静默失效。
            auto_value = str(values.get('auto_review') or '').strip()
            if auto_value not in ('', 'on', 'off'):
                raise HTTPException(400, 'auto_review 只接受 on/off')
            if auto_value == 'on' and current['auto_review'] != 'on':
                from . import dsh_auto
                state = dsh_auto.plugin_state(bot, _parse(raw))
                if state['state'] != 'ok':
                    raise HTTPException(400, f'无法启用 Auto 审批：{state["reason"]}')
            for field, (entry_id, key) in _PATCH_FIELDS.items():
                value = values.get(field)
                if value is None or value == current[field]:
                    continue
                value = value.strip()
                if field in _SINGLE_LINE and any(c in value for c in '\n\r\0'):
                    raise HTTPException(400, '模型、提供商和推理强度必须是单行文本')
                if field == 'reasoning_effort' and value not in _REASONING_LEVELS:
                    raise HTTPException(400, 'Unsupported reasoning effort')
                entry = _entry(seq, entry_id)
                if entry is None:
                    entry = CommentedMap()
                    entry['id'] = entry_id
                    seq.append(entry)
                config = entry.get('config')
                if config is None:
                    config = CommentedMap()
                    entry['config'] = config
                if not isinstance(config, CommentedMap):
                    raise HTTPException(400, f'{entry_id} 的 config 必须是键值映射')
                if value:
                    config[key] = value
                else:
                    config.pop(key, None)
            after = dict(before)
            after[str(patch)] = files._dump(parser, seq, before[str(patch)])
            # 独立文件字段（env/instructions/description）：仅写变更，保留原 EOL 风格。
            for field, path in file_paths.items():
                value = values[field].replace('\r\n', '\n')
                if value != current[field].replace('\r\n', '\n'):
                    if before[str(path)] and b'\r\n' in before[str(path)]:
                        value = value.replace('\n', '\r\n')
                    after[str(path)] = value.encode('utf-8')
            # Auto 审批开关（'on'/'off'，缺失视为不变更）；仅写变更。
            if auto_value in ('on', 'off') and auto_value != current['auto_review']:
                after[str(auto)] = auto_value.encode('utf-8')
            if any(raw is not None and len(raw) > files._MAX_BYTES for raw in after.values()):
                raise HTTPException(400, 'Configuration file exceeds 2 MB')
            return {'saved': True, 'backup': _commit(before, after)}
    except YAMLError as exc:
        raise HTTPException(400, _sanitize(f'cordis.patch.yml 解析失败：{exc}')) from None
    except UnicodeError:
        raise HTTPException(400, 'cordis.patch.yml 必须是有效 UTF-8') from None
    except OSError:
        raise HTTPException(500, 'Cannot save DSH configuration; check permissions and backups') from None
