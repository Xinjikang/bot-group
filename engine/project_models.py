"""Project/member model overrides; host configuration files remain untouched."""
import hashlib
import json
from pathlib import Path
from ruamel.yaml import YAML

from fastapi import HTTPException
from . import bot_identity, db


FIELDS = ('model', 'provider', 'reasoning_effort')


def read(conn, project, member):
    bot = bot_identity.resolve(conn, project, member)
    if not bot or bot['runtime'] not in ('codex', 'hermes', 'dsh'):
        raise HTTPException(404, 'Project runtime member not found')
    key = f'member_model:{bot["id"]}'
    row = conn.execute('SELECT value FROM settings WHERE project=? AND key=?', (project, key)).fetchone()
    raw = row['value'] if row else '{}'
    values = dict.fromkeys(FIELDS, '')
    values.update(json.loads(raw))
    return dict(values, runtime=bot['runtime'], member_profile=member, project=project,
                revision=hashlib.sha256(raw.encode()).hexdigest()), key


def save(conn, project, member, values):
    conn.execute('BEGIN IMMEDIATE')
    current, key = read(conn, project, member)
    if values['revision'] != current['revision']:
        raise HTTPException(409, 'Project model settings changed; reopen the editor')
    clean = {field: values[field].strip() for field in FIELDS}
    if any(any(c in value for c in '\r\n\0') for value in clean.values()):
        raise HTTPException(400, 'Model settings must be single-line text')
    efforts = ('', 'low', 'medium', 'high', 'xhigh', 'ultra')
    if current['runtime'] == 'hermes':
        efforts += ('none', 'minimal', 'max')
    elif current['runtime'] == 'dsh':
        efforts = ('', 'minimal', 'low', 'medium', 'high', 'max', 'ultra')
    if clean['reasoning_effort'] not in efforts:
        raise HTTPException(400, 'Unsupported reasoning effort')
    conn.execute('INSERT INTO settings(key,project,value,updated_at) VALUES(?,?,?,?) '
                 'ON CONFLICT(key,project) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                 (key, project, json.dumps(clean, sort_keys=True), db.now_iso()))
    conn.commit()
    return read(conn, project, member)[0]


def _hermes_defaults(bot):
    home = Path(bot['config_root'])
    if bot['profile'] != 'default':
        home = home / 'profiles' / bot['profile']
    try:
        with (home / 'config.yaml').open(encoding='utf-8') as stream:
            config = YAML(typ='safe').load(stream) or {}
    except FileNotFoundError:
        config = {}
    if not isinstance(config, dict):
        raise ValueError('Hermes config.yaml must be a mapping')
    model = config.get('model') or {}
    default = (model.get('default') or model.get('model') or model.get('name') or '') if isinstance(model, dict) else model
    if isinstance(default, dict):
        default = default.get('model') or default.get('default') or ''
    agent = config.get('agent') or {}
    if not isinstance(agent, dict):
        raise ValueError('Hermes config.yaml agent must be a mapping')
    effort = agent.get('reasoning_effort')
    return dict(model=str(default).strip(),
                provider=str(model.get('provider') or '').strip() if isinstance(model, dict) else '',
                reasoning_effort='none' if effort is False else str(effort or '').strip())


def arguments(conn, project, member):
    # Legacy/unregistered members continue using their host defaults.
    bot = bot_identity.resolve(conn, project, member)
    if not bot:
        return []
    values, _ = read(conn, project, member)
    if values['runtime'] == 'hermes':
        # Explicit inherited fields prevent resume from restoring stale policy.
        if any(not values[field] for field in FIELDS):
            defaults = _hermes_defaults(bot)
            for field in FIELDS:
                values[field] = values[field] or defaults[field]
        if not values['model']:
            raise ValueError('Hermes project model fallback requires a configured default model')
    args = []
    if values['runtime'] == 'dsh':
        # dsh 的模型/提供商/推理覆盖经 spawn overlay（--patch JSON）应用，由 task 266 实现，
        # 不走 CLI 参数。此处仅完成项目级覆盖的读写闭环。
        return args
    for field in FIELDS:
        value = values[field]
        if not value:
            continue
        if values['runtime'] == 'hermes':
            args += [{'model': '--model', 'provider': '--provider', 'reasoning_effort': '--reasoning'}[field], value]
        elif field == 'model':
            args += ['--model', value]
        else:
            key = {'provider': 'model_provider', 'reasoning_effort': 'model_reasoning_effort'}[field]
            args += ['-c', key + '=' + json.dumps(value)]
    return args
