"""HUD conversations over the shared Hermes public gateway adapter."""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import time
import uuid

from . import hermes_gateway as gateway, hermes_gateway_sessions as sessions
from .hermes_gateway_client import GatewayTransportError

_OWNER = ContextVar('hud_gateway_owner', default=None)
_GENERATION = ContextVar('hud_gateway_generation', default=None)


@contextmanager
def owner(sid, generation=None):
    token = _OWNER.set(sid)
    generation_token = _GENERATION.set(generation)
    try:
        yield
    finally:
        _OWNER.reset(token)
        _GENERATION.reset(generation_token)


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


def respond(bot, prompt, workdir, progress, *, system_prompt='', server=None, edit_approval_policy='workspace_session'):
    from .bot_identity import hermes_profile_home
    home = hermes_profile_home(bot).resolve()
    workdir = str(Path(workdir).resolve())
    digest = hashlib.sha256((system_prompt + '\0' + prompt).encode('utf-8')).hexdigest()
    binding = hashlib.sha256(json.dumps([
        _OWNER.get(), _GENERATION.get(), bot.get('id'), bot['profile'], str(home), workdir,
        system_prompt, 'native-file-memory-v2', bool(server),
    ], ensure_ascii=False).encode('utf-8')).hexdigest()
    record = _saved()
    text = ''

    def event(frame):
        nonlocal text
        data = frame.get('params') or {}
        if frame.get('method') != 'event' or data.get('session_id') != record.get('session_id'):
            return
        payload = data.get('payload') or {}
        if data.get('type') == 'reasoning.delta':
            delta = payload.get('text')
            if isinstance(delta, str):
                text = (text + delta)[-500:]
                progress('reasoning', text)
        elif data.get('type') in ('tool.start', 'tool.complete'):
            text = ''
            name = payload.get('name') or payload.get('tool_name')
            labels = {'read_file': '读取文件', 'search_files': '查找资料', 'write_file': '写入文档',
                      'patch': '修改文档', 'memory': '更新记忆', 'tool_search': '查找可用工具',
                      'tool_describe': '读取工具说明', 'tool_call': '调用工具'}
            label = ('更新项目配置' if isinstance(name, str) and name.endswith('update_project_config') else
                     '处理项目配置' if isinstance(name, str) and name.endswith('project_config') else
                     '打开项目配置' if isinstance(name, str) and name.endswith('open_project_page') else labels.get(name, '执行工具'))
            if data['type'] == 'tool.start':
                progress('stage', '正在' + label)
            else:
                progress('stage', label + ('失败，正在处理' if payload.get('is_error') else '完成，正在继续分析'))

    progress('stage', '正在连接 Hermes 网关')
    # A previous admission belongs to its original profile even after the HUD bot changes.
    previous_pending = record and not record.get('terminal')
    target_home = Path(record['home']) if previous_pending else home
    target_profile = record.get('profile', bot['profile']) if previous_pending else bot['profile']
    client, _, _ = gateway.connection(target_profile, home=target_home, on_event=event)
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
                same_binding = record.get('binding') == binding
                if 'binding' not in record:
                    same_binding = (target_home == home and target_profile == bot['profile']
                                    and record.get('workdir') == workdir)
                if record['digest'] == digest and same_binding:
                    return result
            if target_home != home or target_profile != bot['profile']:
                client.close()
                client, _, _ = gateway.connection(bot['profile'], home=home, on_event=event)
        if not record or record.get('terminal') or (not record.get('attempted') and (
                record.get('digest') != digest or record.get('binding') != binding)):
            session_id = record.get('session_id') if record.get('binding') == binding else None
            prior_mcp = record.get('mcp_fingerprint')
            record = {'request_id': uuid.uuid4().hex, 'submission_id': uuid.uuid4().hex,
                      'home': str(home), 'profile': bot['profile'], 'binding': binding,
                      'digest': digest, 'deadline': time.time()+600, 'workdir': workdir}
            if session_id:
                record['session_id'] = session_id
                record['mcp_fingerprint'] = prior_mcp
            _save(record)
        spec = ({'name': 'project_config', 'command': server['command'], 'args': server['args'],
                 'env': [{'name': key, 'value': value} for key, value in sorted(server['env'].items())]}
                if server else None)
        editor = {'mcp_servers': [spec] if spec else []}
        fingerprint = hashlib.sha256(json.dumps(editor, sort_keys=True).encode()).hexdigest()
        if record.get('session_id'):
            snapshot = sessions.attach(client, record['session_id'])
            if record.get('mcp_fingerprint') != fingerprint:
                sessions.mutate(client, snapshot, 'editor_mcp', editor)
                snapshot = sessions.attach(client, record['session_id'])
            else:
                snapshot = client.rpc('session.resume', session_id=record['session_id'], editor=editor)
        else:
            snapshot = sessions.new(client, title='项目助手 · ' + record['request_id'][:12],
                                    workdir=record['workdir'], system_prompt=system_prompt, model_args=[],
                                    request_id=record['request_id'], editor=editor, launch_options={
                                        'toolsets': ['file', 'memory', 'no_mcp'],
                                        # Native inventory caches definitions outside the editor scope;
                                        # verbose construction resolves tools in the execution scope.
                                        'verbose': True})
            record['session_id'] = snapshot['session_id']
            _save(record)
            if server:
                sessions.mutate(client, snapshot, 'editor_mcp', editor)
                snapshot = sessions.attach(client, record['session_id'])
        record['session_id'] = snapshot['session_id']
        record['mcp_fingerprint'] = fingerprint
        _save(record)
        if snapshot.get('running') or snapshot.get('pending'):
            raise RuntimeError('项目助手会话仍有执行待完成，请稍后重试')
        # The host's definition cache omits session-tool scope from its key.
        # Querying private tools outside execution can cache an incomplete list.
        tool_view = client.rpc('tools.show', session_id=record['session_id']) if not server else {}
        names = {tool.get('name') if isinstance(tool, dict) else tool
                 for section in tool_view.get('sections', []) for tool in section.get('tools', [])}
        # Memory providers add their own native tools when the agent is built.
        # Session launch toolsets remain the authority for the permitted tools.
        native = {'search_files', 'read_file', 'write_file', 'patch'}
        if not server and not native.issubset(names):
            raise RuntimeError('项目助手工具清单缺少原生文件工具，已停止提交')
        if snapshot.get('info', {}).get('edit_approval_policy') != edit_approval_policy:
            sessions.mutate(client, snapshot, 'edit_policy', {'policy': edit_approval_policy})
            if server:
                snapshot = sessions.attach(client, record['session_id'])
                sessions.mutate(client, snapshot, 'editor_mcp', editor)
                client.rpc('session.resume', session_id=record['session_id'], editor=editor)
        client.rpc('config.set', session_id=record['session_id'], key='show_reasoning', value='1')
        record['attempted'] = True
        _save(record)
        progress('stage', '正在等待模型响应')
        receipt = client.rpc('prompt.submit', session_id=record['session_id'],
                             submission_id=record['submission_id'], text=prompt)
        record['admission_id'] = receipt['admission_id']
        _save(record)
        result = _wait(client, record, progress)
        after = client.rpc('tools.show', session_id=record['session_id']) if not server else {}
        record['tools'] = [tool.get('name') if isinstance(tool, dict) else tool
                          for section in after.get('sections', []) for tool in section.get('tools', [])]
        _save(record)
        return result
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
