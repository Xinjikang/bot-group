"""One execution-scoped project configuration capability for native assistants."""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sys

from fastapi import HTTPException
from . import auth, config

PROJECT_PAGES = {'basic', 'agents', 'members', 'roles', 'permissions'}


def version(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def configuration(conn, item):
    from . import project_hud as hud
    if item['project']:
        from . import project_workspaces as ws
        source = ws.project(conn, item['project'])
        fields = hud.PROJECT_FIELDS
    else:
        source, fields = item['state'].get('draft', {}), hud.DRAFT_FIELDS
    return {key: source.get(key, '') for key in sorted(fields)}


def issue(item):
    generation, keys = auth.signing_keys()
    scope = {key: item[key] for key in ('id', 'generation', 'revision', 'bot_id')}
    payload = base64.urlsafe_b64encode(json.dumps(scope, sort_keys=True).encode()).decode()
    signature = hmac.new(keys[generation], ('hud-config:' + payload).encode(), hashlib.sha256).hexdigest()
    return 'hud.' + payload + '.' + signature


def authorize(token, sid, conn):
    from . import project_hud as hud
    try:
        prefix, payload, signature = token.split('.')
        if prefix != 'hud' or len(token) > 2048:
            raise ValueError()
        _, keys = auth.signing_keys()
        if not any(secrets.compare_digest(signature, hmac.new(key, ('hud-config:' + payload).encode(), hashlib.sha256).hexdigest())
                   for key in keys.values()):
            raise ValueError()
        scope = json.loads(base64.urlsafe_b64decode(payload))
        if scope.get('id') != sid:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(403, '助手配置凭据无效') from None
    item = hud.row(conn, sid)
    if (item['status'] != 'running' or any(scope.get(key) != item[key]
            for key in ('generation', 'revision', 'bot_id'))):
        raise HTTPException(409, '助手执行已结束或上下文已更新，请重新执行')
    if item['project'] and not conn.execute(
            'SELECT 1 FROM projects WHERE name=? AND removed=0 AND archived=0', (item['project'],)).fetchone():
        raise HTTPException(409, '项目已归档或移除')
    return item


def update(sid, token, changes, expected_version):
    from . import project_hud as hud, project_workspaces as ws
    if not isinstance(changes, dict) or not changes:
        raise HTTPException(400, '请提供要修改的通用配置字段')
    try:
        hud.validate_response({'text': '配置更新', 'proposal': {'changes': changes}})
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with hud._LOCK, ws.LOCK, hud.connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        item = authorize(token, sid, conn)
        if not item['state'].get('user_request'):
            raise HTTPException(409, '后台准备检查只提出建议，配置修改需要当前用户请求')
        before = configuration(conn, item)
        if set(changes) - set(before):
            raise HTTPException(400, '当前阶段不支持此配置字段')
        if version(before) != expected_version:
            raise HTTPException(409, {'message': '配置已经变化，请核对当前值后重试',
                                      'configuration': before, 'config_version': version(before)})
        changed = {key: value for key, value in changes.items() if value != before[key]}
        if not changed:
            return {'saved': True, 'changes': {}, 'configuration': before, 'config_version': version(before)}
        if item['project']:
            actual = ws.project(conn, item['project'])
            ws.validate_config(changed, actual)
            if 'display_name' in changed and not changed['display_name'].strip():
                raise HTTPException(400, '显示名不能为空')
            for key, value in changed.items():
                conn.execute(f'UPDATE projects SET {key}=? WHERE name=?', (value, item['project']))
            ws.audit(conn, item['project'], 'project-assistant', 'project.update',
                     {'before': actual, 'after': {**actual, **changed}})
        else:
            item['state'].setdefault('draft', {}).update(changed)
        item['state'].pop('proposal', None)
        item['state']['cards'] = [card for card in item['state'].get('cards', []) if not card.get('proposal')]
        record_update(item, changed, before, draft=not bool(item['project']))
        hud.add_entry(conn, item, 'system', {'text': '已更新创建草稿' if not item['project'] else '已更新项目设置',
                                           'changes': changed})
        conn.execute('UPDATE hud_sessions SET state=?,updated=? WHERE id=?',
                     (hud.encode(item['state']), hud.time.time(), sid))
        if item['project'] and any(key in changed for key in ('root', 'directory_mode', 'worktree_root')):
            from .codex_project_access import commit_directory_access
            members = [r[0] for r in conn.execute('SELECT profile FROM project_members WHERE project=?', (item['project'],))]
            commit_directory_access(conn, item['project'], members)
        else:
            conn.commit()
        after = configuration(conn, item)
        hud.events.bus.publish('hud.changed', {'session': sid, 'project': item['project']})
        hud.events.bus.publish('project.updated', {'project': item['project']})
        return {'saved': True, 'changes': changed, 'configuration': after, 'config_version': version(after)}


def record_update(item, changes, before, *, draft):
    previous = item['state'].get('config_update')
    steps = copy.deepcopy(previous.get('steps', [previous])) if previous else []
    step = {'id': secrets.token_hex(12), 'changes': copy.deepcopy(changes),
            'before': copy.deepcopy(before), 'draft': draft}
    item['state']['config_update'] = {**step, 'steps': [*steps, step]}


def open_page(sid, token, page):
    from . import project_hud as hud
    if page not in PROJECT_PAGES:
        raise HTTPException(400, '请选择项目配置中的有效标签')
    with hud._LOCK, hud.connection() as conn:
        item = authorize(token, sid, conn)
        if not item['project'] or not item['state'].get('user_request'):
            raise HTTPException(409, '打开项目配置需要当前项目及本轮用户请求')
        navigation = {'id': secrets.token_hex(12), 'project': item['project'], 'page': page, 'created': hud.time.time()}
        item['state']['navigation'] = navigation
        conn.execute('UPDATE hud_sessions SET state=?,updated=? WHERE id=?',
                     (hud.encode(item['state']), hud.time.time(), sid))
    hud.events.bus.publish('hud.changed', {'session': sid, 'project': item['project']})
    return {'requested': True, **navigation}


def server(item):
    python = Path(sys.executable)
    if python.name.lower() == 'pythonw.exe':
        python = python.with_name('python.exe')
    return {'command': str(python), 'args': [str(Path(__file__).resolve().parents[1] / 'mcp/hud_server.py')],
            'env': {'HUD_ENGINE_URL': config.engine_url(), 'HUD_SESSION_ID': item['id'], 'HUD_TOOL_TOKEN': issue(item)}}
