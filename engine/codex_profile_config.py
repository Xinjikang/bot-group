"""Codex member settings, with explicit agent declarations and backed-up writes."""
import os
from pathlib import Path

from fastapi import HTTPException
import tomlkit
from tomlkit.exceptions import TOMLKitError

from . import bot_identity, db, discovery, profile_config as files


def resolve(member):
    conn = db.get_connection()
    try:
        bot = bot_identity.global_bot(conn, member)
        return bot if bot and bot['runtime'] == 'codex' else None
    finally:
        conn.close()


def _layout(bot):
    home, selector = bot_identity.codex_settings(bot)
    home = Path(home).resolve()
    if not home.is_dir():
        raise HTTPException(404, 'Codex home directory does not exist')
    config = home / (f'{selector}.config.toml' if selector and (home / f'{selector}.config.toml').is_file() else 'config.toml')
    root = discovery._codex_root().resolve()
    owner = root if home == root or home.parent == root or home.parent == root / 'profiles' else home
    return home, config, owner / 'config.toml', selector if config.name == 'config.toml' else None


def _snapshot(paths):
    return {str(p): files._snapshot(p.parent, [p.name])[p.name] for p in dict.fromkeys(paths)}


def _parse(raw):
    return tomlkit.parse(files._text(raw))


def _agent_name(owner, config, profile):
    matches = []
    agents = owner.get('agents', {})
    if not isinstance(agents, dict):
        raise HTTPException(400, 'agents must be a TOML table')
    for name, value in agents.items():
        if isinstance(value, dict) and isinstance(value.get('config_file'), str):
            if (config[0].parent / value['config_file']).resolve() == config[1]:
                matches.append(name)
    if len(matches) > 1:
        raise HTTPException(409, 'Multiple agent declarations reference this config; resolve the ambiguity first')
    name = matches[0] if matches else profile
    value = agents.get(name, {})
    if not isinstance(value, dict):
        raise HTTPException(409, 'Agent name conflicts with a reserved setting')
    target = value.get('config_file')
    if target and (config[0].parent / target).resolve() != config[1]:
        raise HTTPException(409, 'Agent name already references a different member config')
    return name


def _state(bot):
    home, config, owner, selector = _layout(bot)
    before = _snapshot([config, owner, home / 'AGENTS.md', home / 'AGENTS.override.md'])
    doc, parent = _parse(before[str(config)]), _parse(before[str(owner)])
    name = _agent_name(parent, (owner, config), bot['profile'])
    model = doc.get('profiles', {}).get(selector, {}) if selector else doc
    if not isinstance(model, dict):
        raise HTTPException(400, 'Codex profile must be a TOML table')
    view = {'runtime': 'codex', 'description': str(parent.get('agents', {}).get(name, {}).get('description', '')),
            'soul': files._text(before[str(home / 'AGENTS.md')]), 'env': '',
            'model': str(model.get('model', '')), 'provider': str(model.get('model_provider', '')),
            'reasoning_effort': str(model.get('model_reasoning_effort', '')),
            'revision': files._revision(before), 'config_path': str(config),
            'description_path': str(owner), 'agent_name': name,
            'instructions_overridden': bool(before[str(home / 'AGENTS.override.md')])}
    return home, config, owner, selector, before, doc, parent, view


def read(bot):
    try:
        with files._LOCK:
            return _state(bot)[-1]
    except (TOMLKitError, UnicodeError, TypeError, AttributeError):
        raise HTTPException(400, 'Codex configuration must contain valid UTF-8 TOML') from None
    except OSError:
        raise HTTPException(500, 'Cannot read Codex member configuration') from None


def _commit(before, after):
    staged, replaced, backups = {}, [], []
    try:
        for path, content in after.items():
            if content != before[path]:
                staged[path] = files._stage(Path(path).parent, content)
        if _snapshot(map(Path, before)) != before:
            raise HTTPException(409, 'Configuration changed; reopen the editor')
        grouped = {}
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


def save(bot, values):
    try:
        with files._LOCK:
            home, config, owner, selector, before, doc, parent, current = _state(bot)
            target = values.get('config_path')
            if target is not None and Path(target).resolve() != config:
                raise HTTPException(409, 'Member configuration path changed; reopen the editor')
            if values['revision'] != current['revision']:
                raise HTTPException(409, 'Configuration changed; reopen the editor')
            if values.get('env'):
                raise HTTPException(400, 'Codex editor does not support .env')
            if owner == config:
                parent = doc
            model = doc.setdefault('profiles', tomlkit.table()).setdefault(selector, tomlkit.table()) if selector else doc
            for field, key in (('model', 'model'), ('provider', 'model_provider'), ('reasoning_effort', 'model_reasoning_effort')):
                value = values.get(field)
                if value is None or value == current[field]:
                    continue
                value = value.strip()
                if any(c in value for c in '\n\r\0'):
                    raise HTTPException(400, 'Model settings must be single-line text')
                if field == 'reasoning_effort' and value not in ('', 'low', 'medium', 'high', 'xhigh', 'ultra'):
                    raise HTTPException(400, 'Unsupported reasoning effort')
                if value:
                    model[key] = value
                else:
                    model.pop(key, None)
            if values['description'] != current['description']:
                if selector:
                    raise HTTPException(400, 'Agent descriptions require an independent home or a separate config file')
                agent = parent.setdefault('agents', tomlkit.table()).setdefault(current['agent_name'], tomlkit.table())
                agent['description'] = values['description']
                if owner != config:
                    agent['config_file'] = str(config)
            after = dict(before)
            for path, data in ((config, doc), (owner, parent)):
                # TOMLDocument inherits dict.__ne__, which can miss replaced keys.
                # Its __eq__ compares the actual TOML values.
                if data == _parse(before[str(path)]):
                    continue
                rendered = tomlkit.dumps(data)
                if before[str(path)] and b'\r\n' in before[str(path)]:
                    rendered = rendered.replace('\r\n', '\n').replace('\n', '\r\n')
                after[str(path)] = rendered.encode('utf-8')
                _parse(after[str(path)])
            soul = values['soul'].replace('\r\n', '\n')
            if soul != current['soul'].replace('\r\n', '\n'):
                if current['instructions_overridden']:
                    raise HTTPException(409, 'AGENTS.override.md takes precedence; resolve it before editing AGENTS.md')
                if before[str(home / 'AGENTS.md')] and b'\r\n' in before[str(home / 'AGENTS.md')]:
                    soul = soul.replace('\n', '\r\n')
                after[str(home / 'AGENTS.md')] = soul.encode('utf-8')
            if any(raw is not None and len(raw) > files._MAX_BYTES for raw in after.values()):
                raise HTTPException(400, 'Configuration file exceeds 2 MB')
            return {'saved': True, 'backup': _commit(before, after)}
    except (TOMLKitError, UnicodeError, TypeError, AttributeError):
        raise HTTPException(400, 'Codex configuration must contain valid UTF-8 TOML') from None
    except OSError:
        raise HTTPException(500, 'Cannot save Codex configuration; check permissions and backups') from None
