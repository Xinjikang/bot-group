"""DSH member settings backed by ``cordis.patch.yml``, with revision and backups.

``cordis.patch.yml`` 是 YAML 列表，每项形如 ``{id, disabled?, config?, insert?}``。
本模块只读写已知字段对应的条目 ``config``，其余条目与未知字段通过 round-trip 保留。

字段映射（persona 为实测命中项，其余按设计文档落盘，待实测收敛的已注明）：

    persona          -> system-prompt.config.personaPrefix      （实测命中 probe-persona2）
    model            -> agent-default-model.config.model         （实测 probe-desktop-model）
    provider         -> agent-default-model.config.provider
    reasoning_effort -> agent-default-model.config.reasoningEffort
    instructions     -> dsh-agent-instructions.config.instructions（字段名待实测收敛）
    description      -> profile.config.description               （引擎元数据，待实测收敛）

``.env`` 单独落在 DSH home 根（``DEEPSEEK_API_KEY=...``），与 patch 分开读写。
含动态表达式（``!!js``）或锚点/别名引用的 patch 无法安全 round-trip，保存前拒绝为只读，
避免覆盖损坏。凭据出现在错误消息时按脱敏规则替换。
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


# (entry_id, config_key) 映射，与 cordis.patch.yml 条目一一对应。
_FIELDS = {
    'description': ('profile', 'description'),
    'persona': ('system-prompt', 'personaPrefix'),
    'instructions': ('dsh-agent-instructions', 'instructions'),
    'model': ('agent-default-model', 'model'),
    'provider': ('agent-default-model', 'provider'),
    'reasoning_effort': ('agent-default-model', 'reasoningEffort'),
}
_SINGLE_LINE = ('model', 'provider', 'reasoning_effort')
# DSH reasoningEffort 档位（desktop 实测用 max，其余对齐 Codex 档位，待实测收敛）。
_REASONING_LEVELS = ('', 'minimal', 'low', 'medium', 'high', 'max', 'ultra')
# 动态表达式 / 锚点 / 别名：无法安全 round-trip，保存前拒绝。
_AMBIGUOUS = re.compile(r'!!js\b|&[A-Za-z0-9_-]+\b|\*\s*[A-Za-z0-9_-]+\b')
# 凭据脱敏：sk-* 密钥、bearer token、api_key=... 等。
_CREDENTIAL = re.compile(r'(?i)(sk-[A-Za-z0-9_-]{6,}|bearer\s+\S+|api[_-]?key\s*[:=]\s*\S+)')


def _sanitize(message: str) -> str:
    return _CREDENTIAL.sub('[REDACTED]', str(message))


def resolve(member: str):
    conn = db.get_connection()
    try:
        bot = bot_identity.global_bot(conn, member)
        return bot if bot and bot['runtime'] == 'dsh' else None
    finally:
        conn.close()


def _layout(bot) -> tuple[Path, Path, Path]:
    home = Path(bot['config_root']).resolve()
    patch = home / 'profiles' / bot['profile'] / 'cordis.patch.yml'
    env = home / '.env'
    return home, patch, env


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


def _view(patch: Path, env: Path, snapshot: dict[str, bytes | None]) -> dict:
    seq = _parse(files._text(snapshot[str(patch)]))
    view = {'runtime': 'dsh', 'revision': files._revision(snapshot), 'config_path': str(patch)}
    for field, (entry_id, key) in _FIELDS.items():
        entry = next((e for e in seq if isinstance(e, dict) and str(e.get('id') or '') == entry_id), None)
        config = entry.get('config') if entry is not None else None
        view[field] = str(config[key] or '') if isinstance(config, dict) and key in config else ''
    view['env'] = files._text(snapshot[str(env)])
    return view


def read(bot) -> dict:
    try:
        with files._LOCK:
            home, patch, env = _layout(bot)
            if not patch.is_file():
                raise HTTPException(404, 'DSH profile cordis.patch.yml does not exist')
            return _view(patch, env, _snapshot([patch, env]))
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
            home, patch, env = _layout(bot)
            if not patch.is_file():
                raise HTTPException(404, 'DSH profile cordis.patch.yml does not exist')
            before = _snapshot([patch, env])
            current = _view(patch, env, before)
            if values['revision'] != current['revision']:
                raise HTTPException(409, 'Configuration changed; reopen the editor')
            raw = files._text(before[str(patch)])
            if _AMBIGUOUS.search(raw):
                raise HTTPException(400, 'cordis.patch.yml 含动态表达式或锚点引用，为避免覆盖损坏请先在 DSH 宿主中解析')
            parser, seq = _yaml(before[str(patch)])
            for field, (entry_id, key) in _FIELDS.items():
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
            env_value = values['env'].replace('\r\n', '\n')
            if env_value != current['env'].replace('\r\n', '\n'):
                if before[str(env)] and b'\r\n' in before[str(env)]:
                    env_value = env_value.replace('\n', '\r\n')
                after[str(env)] = env_value.encode('utf-8')
            if any(raw is not None and len(raw) > files._MAX_BYTES for raw in after.values()):
                raise HTTPException(400, 'Configuration file exceeds 2 MB')
            return {'saved': True, 'backup': _commit(before, after)}
    except YAMLError as exc:
        raise HTTPException(400, _sanitize(f'cordis.patch.yml 解析失败：{exc}')) from None
    except UnicodeError:
        raise HTTPException(400, 'cordis.patch.yml 必须是有效 UTF-8') from None
    except OSError:
        raise HTTPException(500, 'Cannot save DSH configuration; check permissions and backups') from None
