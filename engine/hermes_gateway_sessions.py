"""Session operations through the official gateway RPC contract."""
from __future__ import annotations

import json
import hashlib
import uuid

from .hermes_gateway_client import GatewayRefusal


def mutate(client, snapshot, operation, payload, *, request_id=None):
    return client.rpc('session.mutate', session_id=snapshot['session_id'],
        request_id=request_id or uuid.uuid4().hex, expected_revision=snapshot['revision'],
        expected_generation=snapshot['execution_generation'], operation=operation, payload=payload)


def attach(client, session_id):
    return client.rpc('session.resume', session_id=session_id)


def new(client, *, title, workdir, system_prompt, model_args, request_id=None, launch_options=None):
    options = dict(zip(model_args[::2], model_args[1::2]))
    params = {key.lstrip('-'): value for key, value in options.items()
              if key in ('--model', '--provider', '--reasoning')}
    params.update(launch_options or {})
    return client.rpc('session.create', request_id=request_id or uuid.uuid4().hex,
        title=title, source='acp', cwd=workdir, editor={'mcp_servers': []},
        client_env={'HERMES_EPHEMERAL_SYSTEM_PROMPT': system_prompt}, **params)


def configure(client, snapshot, *, workdir, model_args, server,
              reset_reasoning=False, reasoning_applied=None, edit_approval_policy='workspace_session',
              previous=None):
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
    if '--model' in options and model != previous.get('model'):
        payload = {'model': options['--model']}
        if '--provider' in options:
            payload['provider'] = options['--provider']
        mutate(client, snapshot, 'model', payload)
        snapshot = attach(client, sid)
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
    if previous.get('mcp') != fingerprint:
        mutate(client, snapshot, 'editor_mcp', {'mcp_servers': [spec]})
        snapshot = attach(client, sid)
    else:
        snapshot = client.rpc('session.resume', session_id=sid, editor={'mcp_servers': [spec]})
    return snapshot


def configure_tracked(conn, project, client, snapshot, *, workdir, model_args, server):
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
    result = configure(client, snapshot, workdir=workdir, model_args=model_args, server=server,
                       reset_reasoning=previous is not None, reasoning_applied=applied,
                       edit_approval_policy=edit_policy.read(conn), previous=prior)
    options = dict(zip(model_args[::2], model_args[1::2]))
    spec = {'name': 'task_queue', 'command': server['command'], 'args': server['args'],
            'env': [{'name': k, 'value': v} for k, v in sorted(server['env'].items())]}
    state = dict(model={k: options[k] for k in ('--model', '--provider') if k in options},
                 reasoning=options.get('--reasoning'),
                 mcp=hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest())
    conn.execute('INSERT INTO settings(project,key,value,updated_at) VALUES(?,?,?,?) '
                 'ON CONFLICT(key,project) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                 (project, config_key, json.dumps(state), db.now_iso()))
    conn.commit()
    return result


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
