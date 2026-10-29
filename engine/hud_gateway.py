"""HUD conversations over the shared Hermes public gateway adapter."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
from pathlib import Path
import time
import uuid

from . import hermes_gateway as gateway, hermes_gateway_sessions as sessions
from .hermes_gateway_client import GatewayTransportError

_OWNER = ContextVar('hud_gateway_owner', default=None)


@contextmanager
def owner(sid):
    token = _OWNER.set(sid)
    try:
        yield
    finally:
        _OWNER.reset(token)


def _saved():
    from . import project_hud as hud
    if not _OWNER.get():
        return {}
    with hud.connection() as conn:
        return hud.row(conn, _OWNER.get())['state'].get('gateway', {})


def _save(value):
    from . import project_hud as hud
    if not _OWNER.get():
        return
    with hud._LOCK, hud.connection() as conn:
        item = hud.row(conn, _OWNER.get())
        item['state']['gateway'] = value
        conn.execute('UPDATE hud_sessions SET state=? WHERE id=?', (hud.encode(item['state']), item['id']))


def respond(bot, prompt, workdir, progress, *, system_prompt=''):
    root = Path(bot['config_root'])
    home = root if bot['profile'] == 'default' else root / 'profiles' / bot['profile']
    digest = hashlib.sha256((system_prompt + '\0' + prompt).encode('utf-8')).hexdigest()
    record = _saved()
    text = ''

    def event(frame):
        nonlocal text
        data = frame.get('params') or {}
        if (frame.get('method') != 'event' or data.get('session_id') != record.get('session_id')
                or data.get('type') != 'reasoning.delta'):
            return
        delta = (data.get('payload') or {}).get('text')
        if isinstance(delta, str):
            text = (text + delta)[-500:]
            progress('reasoning', text)

    progress('stage', '正在连接 Hermes 网关')
    # A previous admission belongs to its original profile even after the HUD bot changes.
    previous_pending = record and not record.get('terminal')
    target_home = Path(record['home']) if previous_pending else home
    client, _, _ = gateway.connection(bot['profile'], home=target_home, on_event=event)
    try:
        description = client.rpc('runtime.describe')
        if 'durable-admission-v1' not in description.get('capabilities', []):
            raise RuntimeError('Hermes 网关缺少持久执行回执能力，请更新宿主')
        if previous_pending:
            if record.get('attempted') and not record.get('admission_id'):
                raise RuntimeError('维护检查提交回执未知，请先核对网关中的原执行；会话：' + record.get('session_id', ''))
            if record.get('admission_id'):
                sessions.attach(client, record['session_id'])
                client.rpc('config.set', session_id=record['session_id'], key='show_reasoning', value='1')
                result = _wait(client, record, progress)
                if record['digest'] == digest and record['home'] == str(home):
                    return result
            if target_home != home:
                client.close()
                client, _, _ = gateway.connection(bot['profile'], home=home, on_event=event)
        if not record or record.get('terminal') or (not record.get('attempted') and (
                record.get('digest') != digest or record.get('home') != str(home))):
            record = {'request_id': uuid.uuid4().hex, 'submission_id': uuid.uuid4().hex,
                      'home': str(home), 'digest': digest, 'deadline': time.time()+180, 'workdir': workdir}
            _save(record)
        snapshot = sessions.new(client, title='项目助手 · ' + record['request_id'][:12],
                                workdir=record['workdir'], system_prompt=system_prompt, model_args=[],
                                request_id=record['request_id'], launch_options={
                                    'toolsets': ['no_mcp'], 'ignore_rules': True, 'max_turns': 2,
                                    'checkpoints': False, 'accept_hooks': False})
        record['session_id'] = snapshot['session_id']
        _save(record)
        tool_view = client.rpc('tools.show', session_id=record['session_id'])
        if tool_view.get('total') or any(section.get('tools') for section in tool_view.get('sections', [])):
            raise RuntimeError('维护会话的工具隔离未生效，已停止提交')
        client.rpc('config.set', session_id=record['session_id'], key='show_reasoning', value='1')
        record['attempted'] = True
        _save(record)
        progress('stage', '正在等待模型响应')
        receipt = client.rpc('prompt.submit', session_id=record['session_id'],
                             submission_id=record['submission_id'], text=prompt)
        record['admission_id'] = receipt['admission_id']
        _save(record)
        return _wait(client, record, progress)
    except GatewayTransportError as exc:
        raise RuntimeError('Hermes 网关连接中断，原执行信息已保留；重新检查将核对原回执') from exc
    finally:
        client.close()


def _wait(client, record, progress):
    cancel_deadline = record.get('cancel_deadline')
    while True:
        receipt = client.rpc('prompt.receipt', session_id=record['session_id'],
                             admission_id=record['admission_id'], include_result=True)
        if receipt['status'] == 'terminal':
            record['terminal'] = True
            _save(record)
            if receipt.get('outcome') != 'completed':
                raise RuntimeError('Hermes 网关维护检查未完成：' + str(receipt.get('outcome', 'unknown')))
            result = receipt.get('result') or {}
            if not isinstance(result.get('final_response'), str):
                raise RuntimeError('Hermes 网关回执缺少维护结果')
            return result['final_response']
        if time.time() >= record['deadline']:
            if cancel_deadline is None:
                cancel_deadline = time.time()+30
                record['cancel_deadline'] = cancel_deadline
                _save(record)
                progress('stage', '检查超时，正在停止原执行')
                if receipt['status'] == 'queued':
                    client.rpc('prompt.cancel', session_id=record['session_id'], admission_id=record['admission_id'])
                elif receipt['status'] == 'started':
                    client.rpc('session.interrupt', session_id=record['session_id'],
                               execution_generation=receipt['execution_generation'])
            if time.time() >= cancel_deadline:
                raise RuntimeError('维护检查停止状态待确认，原执行已保留；重新检查将核对原回执')
        time.sleep(0.3)
