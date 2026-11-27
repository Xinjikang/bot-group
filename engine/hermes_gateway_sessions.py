"""Session operations through the official gateway RPC contract."""
from __future__ import annotations

import json
import hashlib
import logging
import uuid

from .hermes_gateway_client import GatewayRefusal, GatewayTransportError


def initialization_rpc(client, method, **params):
    """Retry one pooled initialization RPC with its exact durable request identity."""
    from .hermes_connections import Lease
    if method not in {'runtime.describe', 'session.list', 'session.resume', 'session.create', 'session.mutate'}:
        raise ValueError('Unsupported initialization RPC')
    if method in {'session.create', 'session.mutate'} and not params.get('request_id'):
        raise ValueError('Initialization write requires a stable request ID')
    try:
        return client.rpc(method, **params)
    except GatewayTransportError:
        if not isinstance(client, Lease):
            raise
        logging.getLogger(__name__).warning('Hermes initialization reconnect method=%s retry=1', method)
        client.reconnect()
        # The native receipt checks the original payload, revision and generation
        # before returning a committed result or admitting an uncommitted request.
        return client.rpc(method, **params)


def mutate(client, snapshot, operation, payload, *, request_id=None):
    return initialization_rpc(client, 'session.mutate', session_id=snapshot['session_id'],
        request_id=request_id or uuid.uuid4().hex, expected_revision=snapshot['revision'],
        expected_generation=snapshot['execution_generation'], operation=operation, payload=payload)


def attach(client, session_id, *, omit_messages=False):
    options = {'omit_messages': True} if omit_messages else {}
    return initialization_rpc(client, 'session.resume', session_id=session_id, **options)


def new(client, *, title, workdir, system_prompt, model_args, request_id=None, launch_options=None, editor=None):
    options = dict(zip(model_args[::2], model_args[1::2]))
    params = {key.lstrip('-'): value for key, value in options.items()
              if key in ('--model', '--provider', '--reasoning')}
    params.update(launch_options or {})
    if workdir is not None:
        params['cwd'] = workdir
    return initialization_rpc(client, 'session.create', request_id=request_id or uuid.uuid4().hex,
        title=title, source='acp', editor=editor if editor is not None else {'mcp_servers': []},
        client_env={'HERMES_EPHEMERAL_SYSTEM_PROMPT': system_prompt}, **params)


def configure(client, snapshot, *, workdir, model_args, server,
              reset_reasoning=False, reasoning_applied=None, edit_approval_policy='workspace_session',
              previous=None, force_mcp=False, model_failed=None, refresh_model=True):
    """Apply changed policy and restore an unchanged private MCP transport on resume."""
    previous = previous or {}
    sid = snapshot['session_id']
    if snapshot['info']['source'] != 'acp':
        raise GatewayRefusal('editor_mcp_requires_acp_session')
    if snapshot['running'] or snapshot['pending']:
        raise GatewayRefusal('session_busy')
    from .edit_policy import POLICIES
    if edit_approval_policy not in POLICIES:
        raise GatewayRefusal('invalid_edit_approval_policy')
    if snapshot['info'].get('cwd') != workdir:
        mutate(client, snapshot, 'workspace', {'cwd': workdir})
        snapshot = attach(client, sid)
    options = dict(zip(model_args[::2], model_args[1::2]))
    model = {k: options[k] for k in ('--model', '--provider') if k in options}
    if refresh_model and '--model' in options and model != previous.get('applied_model'):
        payload = {'model': options['--model']}
        if '--provider' in options:
            payload['provider'] = options['--provider']
        try:
            receipt = mutate(client, snapshot, 'model', payload)
        except GatewayRefusal as exc:
            if exc.reason != 'model_resolution_failed':
                raise
            receipt = {'status': exc.reason}
        snapshot = attach(client, sid)
        if not (receipt.get('operation') == 'model' and receipt.get('session_id') == sid
                and receipt.get('model') and isinstance(receipt.get('revision'), int)
                and not receipt.get('status')):
            if model_failed is None:
                raise GatewayRefusal(receipt.get('status') or 'model_switch_unconfirmed')
            model_failed(receipt)
    if '--reasoning' in options and options['--reasoning'] != previous.get('reasoning'):
        mutate(client, snapshot, 'reasoning', {'effort': options['--reasoning']})
        if reasoning_applied:
            reasoning_applied(options['--reasoning'])
        snapshot = attach(client, sid)
    elif '--reasoning' not in options and reset_reasoning:
        mutate(client, snapshot, 'reasoning', {'effort': None})
        if reasoning_applied:
            reasoning_applied(None)
        snapshot = attach(client, sid)
    # Native ordinary-edit policy and native dangerous-command smart review are
    # independent. No client-side reviewer or blanket approval responder is used.
    if snapshot['info'].get('edit_approval_policy') != edit_approval_policy:
        mutate(client, snapshot, 'edit_policy', {'policy': edit_approval_policy})
        snapshot = attach(client, sid)
    spec = {'name': 'task_queue', 'command': server['command'], 'args': server['args'],
            'env': [{'name': key, 'value': value} for key, value in sorted(server['env'].items())]}
    fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    if force_mcp or previous.get('mcp') != fingerprint:
        result = mutate(client, snapshot, 'editor_mcp', {'mcp_servers': [spec]})
        if force_mcp and 'task_queue' not in result.get('mcp_servers', []):
            raise GatewayRefusal('mcp_initialization_unavailable')
        snapshot = attach(client, sid)
    else:
        snapshot = initialization_rpc(client, 'session.resume', session_id=sid, editor={'mcp_servers': [spec]})
    return snapshot


def configure_tracked(conn, project, client, snapshot, *, workdir, model_args, server, force_mcp=False,
                      member=None, refresh_model=True):
    """Reset removed engine overrides while preserving native session choices."""
    from . import db, edit_policy
    identity = json.dumps([client.endpoint['profile_id'], snapshot['session_id']])
    key = 'hermes_reasoning:' + hashlib.sha256(identity.encode()).hexdigest()
    previous = conn.execute('SELECT value FROM settings WHERE project=? AND key=?', (project, key)).fetchone()

    def applied(effort):
        if effort is None:
            conn.execute('DELETE FROM settings WHERE project=? AND key=?', (project, key))
        else:
            conn.execute('INSERT INTO settings(project,key,value,updated_at) VALUES(?,?,?,?) '
                         'ON CONFLICT(key,project) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                         (project, key, effort, db.now_iso()))
        conn.commit()

    config_key = 'hermes_config:' + hashlib.sha256(identity.encode()).hexdigest()
    saved = conn.execute('SELECT value FROM settings WHERE project=? AND key=?', (project, config_key)).fetchone()
    prior = json.loads(saved['value']) if saved else {}
    failures = []
    result = configure(client, snapshot, workdir=workdir, model_args=model_args, server=server,
                       reset_reasoning=previous is not None, reasoning_applied=applied,
                       edit_approval_policy=edit_policy.read(conn), previous=prior, force_mcp=force_mcp,
                       model_failed=failures.append, refresh_model=refresh_model)
    options = dict(zip(model_args[::2], model_args[1::2]))
    spec = {'name': 'task_queue', 'command': server['command'], 'args': server['args'],
            'env': [{'name': k, 'value': v} for k, v in sorted(server['env'].items())]}
    target = {k: options[k] for k in ('--model', '--provider') if k in options}
    state = dict(reasoning=options.get('--reasoning'),
                 mcp=hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest())
    notice_id = None
    if not refresh_model:
        for key_name in ('applied_model', 'failed_target'):
            if key_name in prior:
                state[key_name] = prior[key_name]
    elif failures:
        state['applied_model'] = prior.get('applied_model')
        if member is not None:
            state['failed_target'] = target
            if prior.get('failed_target') != target:
                notice_id = _model_notice(conn, project, member, target, result['info'], failures[0])
    else:
        state['applied_model'] = target
    conn.execute('INSERT INTO settings(project,key,value,updated_at) VALUES(?,?,?,?) '
                 'ON CONFLICT(key,project) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                 (project, config_key, json.dumps(state), db.now_iso()))
    conn.commit()
    if notice_id is not None:
        from . import events
        events.bus.publish('message.new', dict(project=project, message_id=notice_id, sender='engine', thread_id=None))
    return result


def _model_notice(conn, project, member, target, info, receipt):
    from . import db
    warnings = {w.get('kind') for w in receipt.get('warnings', []) if isinstance(w, dict)}
    labels = {'context_cache': '长上下文', 'cost': '模型费用', 'data_policy': '数据使用政策'}
    if receipt.get('status') == 'confirmation_required':
        guards = '、'.join(label for kind, label in labels.items() if kind in warnings)
        reason = f'Hermes 要求确认{guards}'
    elif receipt.get('status') == 'model_resolution_failed':
        reason = 'Hermes 无法解析目标模型或供应商'
    else:
        reason = 'Hermes 尚未确认应用目标模型'
    def route(model, provider):
        return f'{model}（{provider}）' if provider else model
    wanted = route(target['--model'], target.get('--provider'))
    active = route(info.get('model') or '原模型', info.get('provider'))
    help_text = f'若要切换模型，请输入 /new @{member}'
    if warnings == {'context_cache'}:
        help_text += '，或修改配置 model.switch_context_confirm_tokens=0'
    text = f'@user 切换到 {wanted} 失败，本轮继续使用 {active}。{reason}。{help_text}。'
    mid = conn.execute("INSERT INTO messages(project,ts,sender,sender_type,content,mentions,need_reply) "
                       """VALUES(?,?,'engine','engine',?,'["user"]',0)""",
                       (project, db.now_iso(), text)).lastrowid
    conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(?,'user','pending')", (mid,))
    return mid


def migrate_legacy(client, snapshot, *, workdir):
    """Import a resumable ACP copy while retaining the original stored history."""
    old_sid = snapshot['session_id']
    new_sid = 'bg-' + uuid.uuid5(uuid.NAMESPACE_URL,
        client.endpoint['profile_id'] + ':' + old_sid + ':acp').hex
    try:
        return attach(client, new_sid)
    except GatewayRefusal as exc:
        if exc.reason != 'not_found':
            raise
    if snapshot['running'] or snapshot['pending']:
        raise GatewayRefusal('session_busy')
    exported = client.rpc('session.export', session_id=old_sid, format='json')
    data = json.loads(exported['content'])
    data.update(id=new_sid, source='acp', cwd=workdir,
                title=(data.get('title') or old_sid) + '#gateway',
                parent_session_id=None, session_key=None, chat_id=None, origin_json=None,
                ended_at=None, end_reason=None)
    client.rpc('session.mutate', session_id=new_sid, request_id='import-' + new_sid,
               expected_revision=0, operation='import', payload={'sessions': [data]})
    resumed = attach(client, new_sid)
    if resumed['info']['source'] != 'acp':
        raise GatewayRefusal('session_migration_invalid')
    return resumed
