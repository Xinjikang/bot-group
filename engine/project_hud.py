"""Durable project-maintenance conversations, separate from group execution."""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from contextlib import contextmanager

from fastapi import HTTPException
from . import db, events, project_templates, hud_inspection

_LOCK = threading.RLock()
DRAFT_FIELDS = {'display_name', 'description', 'root', 'template', 'directory_mode', 'worktree_root'}
STAGES = ('purpose', 'collaboration', 'roles', 'rules', 'members', 'resources', 'review', 'maintenance')


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
    if item['status'] != 'running' and time.time() - item['activity'] > item['idle_seconds']:
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


def store(conn, item, *, queue=False, activity=True):
    now = time.time()
    if activity:
        advance_generation(item)
    if queue:
        item['status'] = 'queued' if item['bot_id'] else 'idle'
        item['error'] = ''
        item['state'].pop('progress', None)
    conn.execute('UPDATE hud_sessions SET state=?,bot_id=?,stage=?,generation=?,status=?,error=?, '
                 'activity=?,updated=?,revision=revision+1,idle_seconds=? WHERE id=?',
                 (encode(item['state']), item['bot_id'], item['stage'], item['generation'], item['status'],
                  item['error'], now if activity else item['activity'], now, item['idle_seconds'], item['id']))
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
        question = item['state'].get('question')
        if body.get('question_id') != (question or {}).get('id'):
            raise HTTPException(409, '当前问题已变化，请核对后再提交')
        if question:
            decisions = item['state'].setdefault('decisions', [])
            decisions.append({'question': question['title'], 'answer': text})
            item['state'].pop('question')
        advance_generation(item)
        add_entry(conn, item, 'user', {'text': text})
        item['state'].pop('proposal', None)
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
        other = conn.execute('SELECT id FROM hud_sessions WHERE project=? AND id<>?', (project, sid)).fetchone()
        if other:
            # An automatically opened session can be replaced only before interaction.
            if conn.execute('SELECT 1 FROM hud_entries WHERE session=?', (other[0],)).fetchone():
                raise HTTPException(409, '项目已有维护记录，请保留此草稿并核对归属')
            conn.execute('DELETE FROM hud_sessions WHERE id=?', (other[0],))
        conn.execute('UPDATE hud_sessions SET project=? WHERE id=?', (project, sid))
        item['project'], item['stage'] = project, 'maintenance'
        item['state'].pop('proposal', None)
        share_facts(conn, item)
        return store(conn, item, queue=item['status'] != 'paused')


def share_facts(conn, item):
    if item['project']:
        conn.execute("INSERT INTO settings(key,project,value,updated_at) VALUES('maintenance_facts',?,?,?) "
                     'ON CONFLICT(key,project) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                     (item['project'], encode(item['state'].get('facts', [])), db.now_iso()))


def accept(sid, body):
    with _LOCK, connection() as conn:
        item = checked(conn, sid, body['revision'])
        proposal = item['state'].get('proposal')
        if not proposal or proposal['id'] != body['proposal_id']:
            raise HTTPException(409, '配置提案已变化')
        changes = proposal['changes']
        if item['project']:
            actual = conn.execute('SELECT display_name,description FROM projects WHERE name=?', (item['project'],)).fetchone()
            if dict(actual) != proposal.get('base_project'):
                raise HTTPException(409, '项目配置已被其他操作修改，请重新检查')
            if set(changes) - {'display_name', 'description'}:
                raise HTTPException(400, '此变更需要在项目设置中完成')
            for key, value in changes.items():
                conn.execute(f'UPDATE projects SET {key}=? WHERE name=?', (value, item['project']))
        else:
            item['state'].setdefault('draft', {}).update(changes)
        facts = item['state'].setdefault('facts', [])
        for fact in proposal['facts']:
            facts[:] = [f for f in facts if f['label'] != fact['label']]
            facts.append({**fact, 'confirmed_at': db.now_iso()})
        share_facts(conn, item)
        saved_text = ('已更新项目设置' if item['project'] else '已更新向导草稿') if changes else ''
        if proposal['facts']:
            saved_text += ('，并保存项目备注' if saved_text else '已保存项目备注')
        add_entry(conn, item, 'system', {'text': saved_text, 'proposal': proposal})
        item['state'].pop('proposal')
        result = store(conn, item, queue=True)
        events.bus.publish('project.updated', {'project': item['project']})
        return result


def project_snapshot(conn, project):
    config = dict(conn.execute('SELECT name,display_name,root,description,directory_mode,worktree_root,readiness_status '
                               'FROM projects WHERE name=?', (project,)).fetchone())
    config['settings'] = [dict(r) for r in conn.execute(
        "SELECT key,value FROM settings WHERE project=? AND (key='owner_profile' OR key LIKE 'workspace_resources:%')", (project,))]
    config['members'] = [dict(r) for r in conn.execute('SELECT profile,role,active FROM project_members WHERE project=?', (project,))]
    config['requirements'] = [dict(r) for r in conn.execute(
        "SELECT id,title,status,gate_reason FROM requirements WHERE project=? AND status<>'done' ORDER BY id DESC LIMIT 20", (project,))]
    config['tasks'] = [dict(r) for r in conn.execute(
        "SELECT id,title,status FROM tasks WHERE project=? AND status NOT IN ('done','cancelled') ORDER BY id DESC LIMIT 40", (project,))]
    return config


def document_context(root, *, include_contents=True):
    """Only bounded ordinary project documentation is offered to the model."""
    return hud_inspection.documents(root, include_contents=include_contents)


def context(conn, item):
    state = item['state']
    config = project_snapshot(conn, item['project']) if item['project'] else state.get('draft', {})
    selected = config.get('template', 'blank')
    if item['project']:
        selected = (project_templates.reference(item['project']).get('template') or {}).get('id', 'blank')
    if isinstance(selected, dict):
        selected = selected.get('id', 'blank')
    template = project_templates.load(selected) if selected != 'blank' else {'id': 'blank', 'description': '空项目：从目标、输入和产出建立约定'}
    history = [json.loads(r[0]) for r in conn.execute('SELECT content FROM hud_entries WHERE session=? '
               'AND generation=? ORDER BY id DESC LIMIT 16', (item['id'], item['generation']))][::-1]
    materials = document_context(config.get('root'))
    return {'stage': item['stage'], 'project_created': bool(item['project']), 'project': config, 'template': template,
            'materials': materials, 'inspection': hud_inspection.inspect(config, materials), 'facts': state.get('facts', []),
            'deferred_questions': state.get('deferred_questions', [])[-12:],
            'decisions': state.get('decisions', [])[-60:], 'history': history,
            'allowed_changes': sorted({'display_name', 'description'} if item['project'] else DRAFT_FIELDS)}


def validate_response(value):
    if not isinstance(value, dict) or set(value) - {'text', 'question', 'proposal', 'guide'}:
        raise ValueError('AI 返回格式无效，请重试')
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
        if not isinstance(proposal, dict) or set(proposal) - {'changes', 'facts', 'reason'}:
            raise ValueError('AI 配置提案格式无效')
        if 'reason' in proposal and (not isinstance(proposal['reason'], str) or len(proposal['reason']) > 1200):
            raise ValueError('AI 修改说明格式无效')
        changes, facts = proposal.get('changes', {}), proposal.get('facts', [])
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
    from .hud_runtime import respond, progress_sink
    from .hud_gateway import owner
    with _LOCK, connection() as conn:
        item = row(conn, sid)
        if item['status'] != 'queued' or not item['bot_id']:
            return
        if item['project'] and not conn.execute('SELECT 1 FROM projects WHERE name=? AND removed=0 AND archived=0', (item['project'],)).fetchone():
            conn.execute("UPDATE hud_sessions SET status='idle' WHERE id=?", (sid,))
            return
        if time.time() - item['activity'] > item['idle_seconds']:
            item['generation'] += 1
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
                snapshot = {**payload['project'], 'documents': payload['materials']['signatures'], 'inspection': payload['inspection']}
                conn.execute('UPDATE hud_sessions SET digest=? WHERE id=? AND revision=?',
                             (hashlib.sha256(encode(snapshot).encode()).hexdigest(), sid, revision))
        with progress_sink(progress), owner(sid):
            response = respond(bot, payload)
        progress('stage', '正在整理检查结果')
        response = validate_response(response)
        proposal = response.get('proposal')
        if proposal and set(proposal['changes']) - set(payload['allowed_changes']):
            raise ValueError('此配置需要通过项目设置修改')
        if proposal:
            before = {key: payload['project'].get(key) or '' for key in proposal['changes']}
            proposal['changes'] = {key: value for key, value in proposal['changes'].items() if value != before[key]}
            previous_facts = {fact['label']: fact['value'] for fact in payload['facts']}
            proposal['facts'] = [fact for fact in proposal['facts'] if previous_facts.get(fact['label']) != fact['value']]
            proposal['before'] = before
            proposal['before_facts'] = previous_facts
            if not proposal['changes'] and not proposal['facts']:
                response['proposal'] = proposal = None
                response['text'] = '这些内容与当前配置和项目备注一致，已核对完成。'
        if proposal and item['project']:
            proposal['base_project'] = {k: payload['project'][k] for k in ('display_name', 'description')}
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
            for key in ('question', 'proposal'):
                if response.get(key):
                    current['state'][key] = response[key]
                else:
                    current['state'].pop(key, None)
            add_entry(conn, current, 'assistant', response)
            current['status'], current['error'] = ('waiting' if response.get('question') or response.get('proposal') else 'idle'), ''
        current['activity'] = time.time()
        store(conn, current)


def scan():
    with connection() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM hud_sessions WHERE project IS NOT NULL AND status='idle' ORDER BY checked")]
    # Filesystem and tool probes run outside the HUD lock and database transaction.
    for sid in ids:
        with _LOCK, connection() as conn:
            item = row(conn, sid)
            if item['status'] != 'idle':
                continue
            if not item['bot_id'] or time.time() - item['checked'] < 60:
                continue
            if not conn.execute('SELECT 1 FROM projects WHERE name=? AND removed=0 AND archived=0', (item['project'],)).fetchone():
                continue
            project = project_snapshot(conn, item['project'])
        materials = document_context(project['root'])
        snapshot = {**project, 'documents': materials['signatures'], 'inspection': hud_inspection.inspect(project, materials)}
        digest = hashlib.sha256(encode(snapshot).encode()).hexdigest()
        with _LOCK, connection() as conn:
            current = row(conn, sid)
            if current['revision'] != item['revision'] or current['status'] != 'idle':
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
