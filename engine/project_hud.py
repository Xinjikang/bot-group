"""Durable project-maintenance conversations, separate from group execution."""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from contextlib import contextmanager

from fastapi import HTTPException
from . import db, events, project_templates, hud_inspection, hud_knowledge, hud_documents

_LOCK = threading.RLock()
DRAFT_FIELDS = {'display_name', 'description', 'root', 'template', 'directory_mode', 'worktree_root'}
PROJECT_FIELDS = DRAFT_FIELDS - {'template'}
STAGES = ('purpose', 'collaboration', 'roles', 'rules', 'members', 'resources', 'review', 'maintenance')
BACKGROUND_CHECK_SECONDS = 3600


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def schema(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS hud_sessions (
        id TEXT PRIMARY KEY, project TEXT UNIQUE, bot_id INTEGER,
        revision INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'idle', state TEXT NOT NULL DEFAULT '{}',
        stage TEXT NOT NULL DEFAULT 'purpose', digest TEXT NOT NULL DEFAULT '',
        error TEXT NOT NULL DEFAULT '', idle_seconds INTEGER NOT NULL DEFAULT 1800,
        activity REAL NOT NULL, updated REAL NOT NULL, checked REAL NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS hud_entries (
        id INTEGER PRIMARY KEY, session TEXT NOT NULL REFERENCES hud_sessions(id),
        generation INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS hud_entries_session ON hud_entries(session,id);
    ''')


@contextmanager
def connection():
    conn = db.get_connection()
    try:
        schema(conn)
        with conn:
            yield conn
    finally:
        conn.close()


def row(conn, sid):
    value = conn.execute('SELECT * FROM hud_sessions WHERE id=?', (sid,)).fetchone()
    if not value:
        raise HTTPException(404, '维护会话不存在')
    result = dict(value)
    result['state'] = json.loads(result['state'])
    return result


def default_bot(conn):
    value = conn.execute("SELECT value FROM settings WHERE project='' AND key='hud_bot' ").fetchone()
    return int(value[0]) if value and value[0] else None


def valid_bot(conn, bot_id):
    if bot_id is None:
        return None
    bot = conn.execute("SELECT * FROM bots WHERE id=? AND runtime IN ('hermes','codex')", (bot_id,)).fetchone()
    if not bot:
        raise HTTPException(400, '请选择可用的 Hermes 或 Codex Bot')
    return dict(bot)


def add_entry(conn, item, role, content):
    conn.execute('INSERT INTO hud_entries(session,generation,role,content,created) VALUES(?,?,?,?,?)',
                 (item['id'], item['generation'], role, encode(content), time.time()))


def advance_generation(item):
    if (item['status'] != 'running' and not item['state'].get('proposal')
            and not any(c.get('proposal') for c in item['state'].get('cards', []))
            and time.time() - item['activity'] > item['idle_seconds']):
        item['generation'] += 1
        item['activity'] = time.time()


def create(project=None):
    with _LOCK, connection() as conn:
        if project:
            if not conn.execute('SELECT 1 FROM projects WHERE name=? AND removed=0 AND archived=0', (project,)).fetchone():
                raise HTTPException(404, '项目不存在或已归档')
            old = conn.execute('SELECT id FROM hud_sessions WHERE project=?', (project,)).fetchone()
            if old:
                return view(old[0], conn)
        sid, now = uuid.uuid4().hex, time.time()
        conn.execute("INSERT INTO hud_sessions(id,project,bot_id,stage,activity,updated,status) VALUES(?,?,?,?,?,?,'paused')",
                     (sid, project, default_bot(conn), 'maintenance' if project else 'purpose', now, now))
        return view(sid, conn)


def view(sid, conn=None):
    if conn is None:
        with connection() as conn:
            return view(sid, conn)
    item = row(conn, sid)
    bot = conn.execute("SELECT id,display_name,runtime FROM bots WHERE id=? AND runtime IN ('hermes','codex')", (item['bot_id'],)).fetchone()
    item['bot'] = {k: bot[k] for k in ('id', 'display_name', 'runtime')} if bot else None
    item['entries'] = [dict(r, content=json.loads(r['content'])) for r in reversed(conn.execute(
        'SELECT id,role,content,created FROM hud_entries WHERE session=? ORDER BY id DESC LIMIT 80', (sid,)).fetchall())]
    return item


def checked(conn, sid, revision):
    conn.execute('BEGIN IMMEDIATE')
    item = row(conn, sid)
    if item['revision'] != revision:
        raise HTTPException(409, '维护配置已更新，请刷新后继续；输入仍保留')
    if item['project'] and not conn.execute('SELECT 1 FROM projects WHERE name=? AND removed=0 AND archived=0', (item['project'],)).fetchone():
        raise HTTPException(409, '项目已归档或移除')
    return item


def store(conn, item, *, queue=False, activity=True, commit=True):
    now = time.time()
    if activity:
        advance_generation(item)
    if queue:
        item['status'] = 'queued' if item['bot_id'] else 'idle'
        item['error'] = ''
        item['state'].pop('progress', None)
        item['state'].pop('notification', None)
    conn.execute('UPDATE hud_sessions SET state=?,bot_id=?,stage=?,generation=?,status=?,error=?, '
                 'activity=?,updated=?,revision=revision+1,idle_seconds=? WHERE id=?',
                 (encode(item['state']), item['bot_id'], item['stage'], item['generation'], item['status'],
                  item['error'], now if activity else item['activity'], now, item['idle_seconds'], item['id']))
    if commit:
        conn.commit()
        events.bus.publish('hud.changed', {'session': item['id'], 'project': item['project']})
    return view(item['id'], conn)


def update(sid, body):
    with _LOCK, connection() as conn:
        item = checked(conn, sid, body['revision'])
        if set(body) <= {'revision', 'draft', 'stage'} and body.get('draft', item['state'].get('draft')) == item['state'].get('draft') and body.get('stage', item['stage']) == item['stage']:
            return view(sid, conn)
        if 'bot_id' in body:
            valid_bot(conn, body['bot_id'])
            if body['bot_id'] != item['bot_id']:
                item['generation'] += 1
            item['bot_id'] = body['bot_id']
            if body.get('make_default'):
                conn.execute("INSERT INTO settings(key,project,value,updated_at) VALUES('hud_bot','',?,?) "
                             'ON CONFLICT(key,project) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                             (str(body['bot_id'] or ''), db.now_iso()))
        if 'idle_seconds' in body:
            if type(body['idle_seconds']) is not int or not 300 <= body['idle_seconds'] <= 86400:
                raise HTTPException(400, '空闲重置时间须为 5 分钟至 24 小时')
            item['idle_seconds'] = body['idle_seconds']
        if 'draft' in body:
            if item['project']:
                raise HTTPException(409, '项目已创建，请通过配置提案修改')
            if not isinstance(body['draft'], dict) or len(encode(body['draft'])) > 250000:
                raise HTTPException(400, '项目草稿格式错误或超过限制')
            if body['draft'] != item['state'].get('draft'):
                item['state'].pop('proposal', None)
                item['state']['cards'] = [c for c in item['state'].get('cards', []) if not c.get('proposal')]
            item['state']['draft'] = body['draft']
        if 'stage' in body:
            if body['stage'] not in STAGES:
                raise HTTPException(400, '未知向导阶段')
            if item['stage'] != body['stage'] and item['state'].get('question'):
                item['state'].setdefault('deferred_questions', []).append(item['state'].pop('question'))
            item['stage'] = body['stage']
        return store(conn, item, queue=('bot_id' in body or item['status'] != 'paused') and not item['state'].get('question'))


def answer(sid, body):
    text = str(body.get('text', '')).strip()
    if not text or len(text) > 12000:
        raise HTTPException(400, '请填写 1 至 12000 字的回答')
    with _LOCK, connection() as conn:
        item = checked(conn, sid, body['revision'])
        card = next((c for c in item['state'].get('cards', [])
                     if (c.get('question') or {}).get('id') == body.get('question_id') and body.get('question_id')), None)
        question = card['question'] if card else item['state'].get('question')
        dismissed = body.get('dismiss_proposal')
        if dismissed:
            question = None
        if body.get('question_id') != (question or {}).get('id'):
            raise HTTPException(409, '当前问题已变化，请核对后再提交')
        if dismissed:
            proposal_card = next((c for c in item['state'].get('cards', []) if (c.get('proposal') or {}).get('id') == dismissed), None)
            proposal = proposal_card['proposal'] if proposal_card else item['state'].get('proposal')
            if not proposal or proposal['id'] != dismissed:
                raise HTTPException(409, '配置提案已变化')
            if proposal_card:
                item['state']['cards'].remove(proposal_card)
            else:
                item['state'].pop('proposal')
            item['activity'] = time.time()
        if question:
            decisions = item['state'].setdefault('decisions', [])
            decisions.append({'question': question['title'], 'answer': text})
            if card:
                item['state']['cards'].remove(card)
            else:
                item['state'].pop('question')
        advance_generation(item)
        item['state'].pop('navigation', None)
        add_entry(conn, item, 'user', {'text': text})
        item['state']['user_request'] = text
        return store(conn, item, queue=True)


def bind(sid, request_id):
    with _LOCK, connection() as conn:
        conn.execute('BEGIN IMMEDIATE')
        item = row(conn, sid)
        receipt = conn.execute("SELECT result FROM project_setup_requests WHERE id=? AND status='ready'", (request_id,)).fetchone()
        if not receipt:
            raise HTTPException(409, '项目创建尚未完成，请查询创建结果后重试')
        project = json.loads(receipt[0])['project']
        if item['project'] and item['project'] != project:
            raise HTTPException(409, '维护会话已经属于另一个项目')
        if item['project'] == project:
            return view(sid, conn)
        other = conn.execute('SELECT id FROM hud_sessions WHERE project=? AND id<>?', (project, sid)).fetchone()
        if other:
            # An automatically opened session can be replaced only before interaction.
            if conn.execute('SELECT 1 FROM hud_entries WHERE session=?', (other[0],)).fetchone():
                raise HTTPException(409, '项目已有维护记录，请保留此草稿并核对归属')
            conn.execute('DELETE FROM hud_sessions WHERE id=?', (other[0],))
        conn.execute('UPDATE hud_sessions SET project=? WHERE id=?', (project, sid))
        item['project'], item['stage'] = project, 'maintenance'
        item['state'].pop('proposal', None)
        item['state']['cards'] = [c for c in item['state'].get('cards', []) if not c.get('proposal')]
        return store(conn, item, queue=bool(item['bot_id']))


def accept(sid, body):
    from . import project_workspaces as ws
    with _LOCK, ws.LOCK, connection() as conn:
        item = checked(conn, sid, body['revision'])
        if item['status'] in ('queued', 'running'):
            raise HTTPException(409, '助手正在处理补充，请等待回复后确认提案')
        card = next((c for c in item['state'].get('cards', []) if (c.get('proposal') or {}).get('id') == body['proposal_id']), None)
        proposal = card['proposal'] if card else item['state'].get('proposal')
        if not proposal or proposal['id'] != body['proposal_id']:
            raise HTTPException(409, '配置提案已变化')
        changes = proposal['changes']
        actual = {}
        if item['project']:
            if set(changes) - PROJECT_FIELDS:
                raise HTTPException(400, '此变更需要在项目设置中完成')
            actual = ws.project(conn, item['project'])
            if any(actual[key] != proposal.get('base_project', {}).get(key) for key in changes):
                raise HTTPException(409, '项目配置已被其他操作修改，请重新检查')
            ws.validate_config(changes, actual)
            if 'display_name' in changes and not changes['display_name'].strip():
                raise HTTPException(400, '显示名不能为空')
        else:
            item['state'].setdefault('draft', {}).update(changes)
        facts = item['state'].setdefault('facts', [])
        for fact in proposal['facts']:
            facts[:] = [f for f in facts if f['label'] != fact['label']]
            facts.append({**fact, 'confirmed_at': db.now_iso()})
        saved_text = ('已更新项目设置' if item['project'] else '已更新向导草稿') if changes else ''
        if proposal.get('documents'):
            saved_text += ('，' if saved_text else '') + '已保存项目文档'
        if proposal['facts']:
            saved_text += ('，并保存项目备注' if saved_text else '已保存项目备注')
        add_entry(conn, item, 'system', {'text': saved_text, 'proposal': proposal})
        if card:
            item['state']['cards'].remove(card)
        else:
            item['state'].pop('proposal')
        item['activity'] = time.time()
        with hud_documents.apply(proposal, actual):
            if item['project'] and changes:
                for key, value in changes.items():
                    conn.execute(f'UPDATE projects SET {key}=? WHERE name=?', (value, item['project']))
                ws.audit(conn, item['project'], 'project-assistant', 'project.update',
                         {'before': actual, 'after': {**actual, **changes}})
            result = store(conn, item, queue=True, commit=False)
            if item['project'] and any(key in changes and changes[key] != actual[key]
                                      for key in ('root', 'directory_mode', 'worktree_root')):
                from .codex_project_access import commit_directory_access
                members = [r[0] for r in conn.execute('SELECT profile FROM project_members WHERE project=?', (item['project'],))]
                commit_directory_access(conn, item['project'], members)
            else:
                conn.commit()
        events.bus.publish('hud.changed', {'session': item['id'], 'project': item['project']})
        events.bus.publish('project.updated', {'project': item['project']})
        return result


def project_snapshot(conn, project):
    from . import project_workspaces as ws
    config = dict(conn.execute('SELECT name,display_name,root,description,directory_mode,worktree_root,readiness_status '
                               'FROM projects WHERE name=?', (project,)).fetchone())
    config['template'] = (project_templates.reference(project).get('template') or {}).get('id', 'blank')
    config['settings'] = [dict(r) for r in conn.execute(
        "SELECT key,value FROM settings WHERE project=? AND key='owner_profile'", (project,))]
    config['members'] = [dict(r) for r in conn.execute('SELECT profile,role,role_id,active FROM project_members WHERE project=?', (project,))]
    resources = {r['path']: dict(r) for r in conn.execute(
        'SELECT path,workspace_id,revision,config FROM workspace_resources WHERE project=?', (project,))}
    workspaces, errors = {}, []
    for member in config['members']:
        try:
            path = ws.key(ws.selected_path(conn, config, member['profile']))
        except HTTPException as exc:
            errors.append({'profile': member['profile'], 'error': exc.detail})
            continue
        if path not in workspaces:
            saved = resources.get(path)
            workspaces[path] = {'path': path, 'members': [],
                                'workspace_id': saved['workspace_id'] if saved else None,
                                'revision': saved['revision'] if saved else 0,
                                'config': json.loads(saved['config']) if saved else None}
        workspaces[path]['members'].append(member['profile'])
    config['workspace_resources'] = list(workspaces.values())
    config['workspace_errors'] = errors
    config['requirements'] = [dict(r) for r in conn.execute(
        "SELECT id,title,status,gate_reason FROM requirements WHERE project=? AND status<>'done' ORDER BY id DESC LIMIT 20", (project,))]
    config['tasks'] = [dict(r) for r in conn.execute(
        "SELECT id,title,status FROM tasks WHERE project=? AND status NOT IN ('done','cancelled') ORDER BY id DESC LIMIT 40", (project,))]
    return config


def document_context(root, *, include_contents=True, preferred=()):
    """Only bounded ordinary project documentation is offered to the model."""
    return hud_inspection.documents(root, include_contents=include_contents, preferred=preferred)


def context(conn, item):
    from . import hud_tools, edit_policy
    state = item['state']
    config = project_snapshot(conn, item['project']) if item['project'] else state.get('draft', {})
    selected = config.get('template', 'blank')
    if isinstance(selected, dict):
        selected = selected.get('id', 'blank')
    template = project_templates.load(selected) if selected != 'blank' else {'id': 'blank', 'description': '空项目：从目标、输入和产出建立约定'}
    history = [json.loads(r[0]) for r in conn.execute('SELECT content FROM hud_entries WHERE session=? '
               'AND generation=? ORDER BY id DESC LIMIT 16', (item['id'], item['generation']))][::-1]
    preferred = [d['path'] for d in hud_knowledge.guidance(template)['documents']]
    preferred += ['doc/' + p[5:] for p in preferred if p.startswith('docs/')]
    materials = document_context(config.get('root'), include_contents=False, preferred=preferred)
    return {'stage': item['stage'], 'project_created': bool(item['project']), 'project': config,
            'template': {key: value for key, value in template.items() if key not in ('contents', 'references')},
            'template_root': str(project_templates.TEMPLATE_ROOT / selected),
            'installation_root': str(project_templates.TEMPLATE_ROOT.parents[1]),
            'config_version': hud_tools.version(hud_tools.configuration(conn, item)),
            'user_request': state.get('user_request', ''),
            'edit_approval_policy': edit_policy.read(conn),
            'materials': materials, 'inspection': hud_inspection.inspect(config, materials),
            'preparation': hud_knowledge.preparation(config, template, materials),
            'usage_knowledge': hud_knowledge.usage(history),
            'deferred_questions': state.get('deferred_questions', [])[-12:],
            'pending_proposal': state.get('proposal'),
            'pending_cards': state.get('cards', []),
            'decisions': state.get('decisions', [])[-60:], 'history': history,
            'allowed_changes': sorted(PROJECT_FIELDS if item['project'] else DRAFT_FIELDS)}


def validate_response(value):
    if not isinstance(value, dict) or set(value) - {'text', 'question', 'proposal', 'guide', 'notification', 'cards'}:
        raise ValueError('AI 返回格式无效，请重试')
    cards = value.get('cards', [])
    if not isinstance(cards, list):
        raise ValueError('AI 待办卡片格式无效')
    for card in cards:
        if (not isinstance(card, dict) or set(card) - {'text', 'question', 'proposal', 'notification'}
                or bool(card.get('question')) == bool(card.get('proposal'))):
            raise ValueError('每张待办卡片须包含一个问题或提案')
        validate_response(card)
    notification = value.get('notification')
    if notification is not None:
        if (not isinstance(notification, dict) or set(notification) - {'kind', 'action'}
                or notification.get('kind') not in ('none', 'pending', 'user_action')):
            raise ValueError('AI 提醒分类无效')
        action = notification.get('action', '')
        if not isinstance(action, str) or len(action) > 1200:
            raise ValueError('AI 提醒行动说明无效')
        if notification['kind'] == 'user_action' and not action.strip():
            raise ValueError('需要用户行动的提醒必须说明具体行动')
    text = value.get('text', '')
    if not isinstance(text, str) or not text.strip() or len(text) > 6000:
        raise ValueError('AI 回复正文无效')
    guide = value.get('guide')
    if guide is not None:
        if not isinstance(guide, dict) or set(guide) != {'title', 'steps'}:
            raise ValueError('AI 使用说明格式无效')
        if not isinstance(guide['title'], str) or not 1 <= len(guide['title']) <= 160:
            raise ValueError('AI 使用说明标题无效')
        if not isinstance(guide['steps'], list) or not 1 <= len(guide['steps']) <= 8:
            raise ValueError('AI 使用说明步骤无效')
        for step in guide['steps']:
            if not isinstance(step, dict) or set(step) != {'title', 'body'} or any(
                    not isinstance(step[k], str) or not 1 <= len(step[k]) <= limit
                    for k, limit in (('title', 160), ('body', 2000))):
                raise ValueError('AI 使用说明步骤无效')
    question = value.get('question')
    if question:
        if not isinstance(question, dict) or set(question) - {'title', 'reason', 'options'}:
            raise ValueError('AI 问题格式无效')
        if any(not isinstance(question.get(k), str) or not 1 <= len(question[k]) <= 1200 for k in ('title', 'reason')):
            raise ValueError('AI 问题缺少题目或原因')
        options = question.get('options', [])
        if not isinstance(options, list) or len(options) > 5 or any(not isinstance(o, str) or not 1 <= len(o) <= 160 for o in options):
            raise ValueError('AI 问题选项无效')
        question['options'] = options
        question['id'] = uuid.uuid4().hex
    proposal = value.get('proposal')
    if proposal:
        if not isinstance(proposal, dict) or set(proposal) - {'changes', 'facts', 'reason', 'documents'}:
            raise ValueError('AI 配置提案格式无效')
        if 'reason' in proposal and (not isinstance(proposal['reason'], str) or len(proposal['reason']) > 1200):
            raise ValueError('AI 修改说明格式无效')
        changes, facts = proposal.get('changes', {}), proposal.get('facts', [])
        hud_documents.validate(proposal.get('documents', []))
        if not isinstance(changes, dict) or set(changes) - DRAFT_FIELDS or any(not isinstance(v, str) or len(v) > 6000 for v in changes.values()):
            raise ValueError('AI 配置字段无效')
        if 'directory_mode' in changes and changes['directory_mode'] not in ('shared', 'worktree'):
            raise ValueError('协作方式无效')
        if 'template' in changes and changes['template'] not in ('blank', 'software', 'research', 'writing', 'product-prototype', 'algorithm', 'testing'):
            raise ValueError('模板无效')
        if not isinstance(facts, list) or len(facts) > 12:
            raise ValueError('AI 维护资料过多')
        for fact in facts:
            if not isinstance(fact, dict) or set(fact) != {'label', 'value'} or any(not isinstance(v, str) or not 1 <= len(v) <= 4000 for v in fact.values()):
                raise ValueError('AI 维护资料格式无效')
        proposal.update(id=uuid.uuid4().hex, changes=changes, facts=facts)
        # A configuration decision occupies the current interaction; the next
        # question is generated after confirmation against the updated facts.
        value['question'] = None
    if value.get('question') or value.get('proposal'):
        value['guide'] = None
    return value


def run_one(sid):
    from .hud_runtime import respond, progress_sink, tool_server
    from . import hud_tools
    from .hud_gateway import owner
    with _LOCK, connection() as conn:
        item = row(conn, sid)
        if item['status'] != 'queued' or not item['bot_id']:
            return
        if item['project'] and not conn.execute('SELECT 1 FROM projects WHERE name=? AND removed=0 AND archived=0', (item['project'],)).fetchone():
            conn.execute("UPDATE hud_sessions SET status='idle' WHERE id=?", (sid,))
            return
        advance_generation(item)
        conn.execute("UPDATE hud_sessions SET status='running',generation=?,checked=? WHERE id=?", (item['generation'], time.time(), sid))
        conn.commit()
        revision = item['revision']
    events.bus.publish('hud.changed', {'session': sid})
    last_progress = 0.0

    def progress(kind, text):
        nonlocal last_progress
        if kind not in ('stage', 'reasoning') or not isinstance(text, str) or not text.strip():
            return
        now = time.time()
        if kind == 'reasoning' and now - last_progress < 0.3:
            return
        with _LOCK, connection() as conn:
            current = row(conn, sid)
            if current['revision'] != revision or current['status'] != 'running':
                return
            previous = current['state'].get('progress', {})
            current['state']['progress'] = {
                'kind': kind, 'text': ' '.join(text.split())[-500:],
                'started': previous.get('started', now), 'updated': now,
            }
            conn.execute('UPDATE hud_sessions SET state=? WHERE id=? AND revision=? AND status=?',
                         (encode(current['state']), sid, revision, 'running'))
        last_progress = now
        events.bus.publish('hud.changed', {'session': sid})

    try:
        progress('stage', '正在读取项目资料')
        with connection() as conn:
            bot = valid_bot(conn, item['bot_id'])
            payload = context(conn, item)
            if item['project']:
                snapshot = {**payload['project'], 'documents': payload['materials']['signatures'],
                            'inspection': payload['inspection'], 'preparation': payload['preparation']}
                conn.execute('UPDATE hud_sessions SET digest=? WHERE id=? AND revision=?',
                             (hashlib.sha256(encode(snapshot).encode()).hexdigest(), sid, revision))
        with progress_sink(progress), owner(sid, item['generation']), tool_server(hud_tools.server(item)):
            response = respond(bot, payload)
        progress('stage', '正在整理检查结果')
        response = validate_response(response)
        for result in [response, *response.get('cards', [])]:
            proposal = result.get('proposal')
            if proposal and set(proposal['changes']) - set(payload['allowed_changes']):
                raise ValueError('此配置需要通过项目设置修改')
            if proposal:
                if item['project']:
                    from . import project_workspaces as ws
                    ws.validate_config(proposal['changes'], payload['project'])
                hud_documents.prepare(proposal, payload['project'], payload['materials'])
                before = {key: payload['project'].get(key) or '' for key in proposal['changes']}
                proposal['changes'] = {key: value for key, value in proposal['changes'].items() if value != before[key]}
                previous_facts = {fact['label']: fact['value'] for fact in item['state'].get('facts', [])}
                proposal['facts'] = [fact for fact in proposal['facts'] if previous_facts.get(fact['label']) != fact['value']]
                proposal['before'] = before
                proposal['before_facts'] = previous_facts
                if not proposal['changes'] and not proposal['facts'] and not proposal.get('documents'):
                    result['proposal'] = proposal = None
                    result['text'] = '这些内容与当前配置和项目备注一致，已核对完成。'
            if proposal and item['project']:
                proposal['base_project'] = {k: payload['project'][k] for k in PROJECT_FIELDS}
        error = ''
    except Exception as exc:
        response, error = None, str(exc)[:1500]
    with _LOCK, connection() as conn:
        current = row(conn, sid)
        if current['revision'] != revision or current['status'] != 'running':
            return
        current['state'].pop('progress', None)
        if error:
            current['status'], current['error'] = 'error', error
        else:
            current['state'].pop('user_request', None)
            for key in ('question', 'proposal'):
                if response.get(key):
                    current['state'][key] = response[key]
                elif key != 'proposal':
                    current['state'].pop(key, None)
            add_entry(conn, current, 'assistant', response)
            cards = current['state'].setdefault('cards', [])
            def card_key(card):
                value = card.get('question') or card.get('proposal') or {}
                return encode({k: v for k, v in value.items() if k in ('title', 'reason', 'options', 'changes', 'facts', 'documents')})
            existing = {card_key(card) for card in cards}
            for card in response.get('cards', []):
                if (card.get('question') or card.get('proposal')) and card_key(card) not in existing:
                    cards.append(card)
                    existing.add(card_key(card))
            notification = response.get('notification') or {'kind': 'none'}
            urgent = next((c for c in cards if (c.get('notification') or {}).get('kind') == 'user_action'), None)
            if notification['kind'] != 'user_action' and urgent:
                notification = urgent['notification']
            if notification['kind'] != 'user_action':
                has_todos = current['state'].get('question') or current['state'].get('proposal') or cards
                notification = {'kind': 'pending' if has_todos else 'none'}
            pending = ((urgent.get('proposal') or urgent.get('question')) if urgent else None) or current['state'].get('proposal') or current['state'].get('question') or {}
            current['state']['notification'] = {**notification, 'id': pending.get('id') or uuid.uuid4().hex}
            current['status'], current['error'] = ('waiting' if current['state'].get('question') or current['state'].get('proposal') or cards else 'idle'), ''
        current['activity'] = time.time()
        store(conn, current)


def scan():
    with connection() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM hud_sessions WHERE project IS NOT NULL AND status IN ('idle','waiting') ORDER BY checked")]
    # Filesystem and tool probes run outside the HUD lock and database transaction.
    for sid in ids:
        with _LOCK, connection() as conn:
            item = row(conn, sid)
            if item['status'] not in ('idle', 'waiting'):
                continue
            if not item['bot_id'] or time.time() - item['checked'] < BACKGROUND_CHECK_SECONDS:
                continue
            if not conn.execute('SELECT 1 FROM projects WHERE name=? AND removed=0 AND archived=0', (item['project'],)).fetchone():
                continue
            project = project_snapshot(conn, item['project'])
        selected = project['template']
        template = project_templates.load(selected) if selected != 'blank' else {'id': 'blank'}
        preferred = [d['path'] for d in hud_knowledge.guidance(template)['documents']]
        preferred += ['doc/' + p[5:] for p in preferred if p.startswith('docs/')]
        materials = document_context(project['root'], include_contents=False, preferred=preferred)
        snapshot = {**project, 'documents': materials['signatures'], 'inspection': hud_inspection.inspect(project, materials),
                    'preparation': hud_knowledge.preparation(project, template, materials)}
        digest = hashlib.sha256(encode(snapshot).encode()).hexdigest()
        with _LOCK, connection() as conn:
            current = row(conn, sid)
            if current['revision'] != item['revision'] or current['status'] not in ('idle', 'waiting'):
                continue
            if project_snapshot(conn, item['project']) != project:
                continue
            conn.execute('UPDATE hud_sessions SET checked=? WHERE id=?', (time.time(), item['id']))
            if digest != item['digest']:
                conn.execute("UPDATE hud_sessions SET digest=?,status='queued' WHERE id=?", (digest, item['id']))
    with connection() as conn:
        return [r[0] for r in conn.execute("SELECT id FROM hud_sessions WHERE status='queued' AND updated<? ORDER BY updated LIMIT 10", (time.time()-2,))]


def run_forever(stop_event):
    import logging
    with connection() as conn:
        conn.execute("UPDATE hud_sessions SET status='error',error='服务重启中断了维护检查，点击重试继续' WHERE status='running'")
    while not stop_event.is_set():
        try:
            for sid in scan():
                if stop_event.is_set():
                    break
                run_one(sid)
        except Exception:
            logging.getLogger(__name__).exception('Project maintenance check failed')
        stop_event.wait(3)
