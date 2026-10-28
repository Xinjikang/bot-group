"""Capture a real Hermes gateway's model requests across idle-session refreshes.

The local inference fixture returns a fixed response. It records actual outbound
messages/tools without a paid model call or production credentials.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import threading
import time
import uuid
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def mcp_main():
    description = os.environ['REFRESH_TOOL_DESCRIPTION']
    tools = [{'name': 'refresh_probe', 'description': description,
              'inputSchema': {'type': 'object', 'properties': {}}}]
    if os.environ.get('REFRESH_EXTRA_TOOL') == '1':
        tools.append({'name': 'removed_probe', 'description': 'Tool removed on the next turn',
                      'inputSchema': {'type': 'object', 'properties': {}}})
    for line in sys.stdin:
        msg = json.loads(line)
        if 'id' not in msg:
            continue
        method = msg['method']
        if method == 'initialize':
            result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'refresh-fixture', 'version': '1'}}
        elif method == 'tools/list':
            result = {'tools': tools}
        elif method == 'tools/call':
            result = {'content': [{'type': 'text', 'text': 'fixture'}]}
        else:
            result = {}
        print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}), flush=True)


class ModelCapture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append(body)
        (self.server.evidence / f'request-{len(self.server.requests)}.json').write_text(
            json.dumps(body, ensure_ascii=False, indent=2), encoding='utf-8')
        base = {'id': 'refresh-fixture', 'created': int(time.time()), 'model': body.get('model', '')}
        messages = body.get('messages', [])
        names = {tool.get('function', {}).get('name') for tool in body.get('tools', [])}
        searching = bool(messages and messages[-1].get('role') == 'user' and 'tool_search' in names)
        delta = {'role': 'assistant', 'content': 'REFRESH_CAPTURE_OK'}
        finish = 'stop'
        if searching:
            delta = {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'refresh-search', 'type': 'function',
                'function': {'name': 'tool_search', 'arguments': json.dumps({'queries': ['refresh_probe', 'removed_probe']})}}]}
            finish = 'tool_calls'
        if body.get('stream'):
            payload = ''.join('data: ' + json.dumps({**base, 'object': 'chat.completion.chunk', 'choices': [choice]})
                              + '\n\n' for choice in [
                                  {'index': 0, 'delta': delta, 'finish_reason': None},
                                  {'index': 0, 'delta': {}, 'finish_reason': finish}]) + 'data: [DONE]\n\n'
            content_type = 'text/event-stream'
        else:
            payload = json.dumps({**base, 'object': 'chat.completion', 'choices': [
                {'index': 0, 'message': {'role': 'assistant', 'content': 'REFRESH_CAPTURE_OK'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}})
            content_type = 'application/json'
        data = payload.encode()
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    from engine.hermes_gateway_client import GatewayClient, ensure
    from engine import db, session_adapter, hermes_gateway as gateway, hermes_gateway_sessions as sessions
    from scripts.probe_config import absolute_path, loopback_url
    import gw_probe as probe
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--evidence', required=True)
    args = parser.parse_args()
    url = loopback_url(args.url)
    evidence = absolute_path(args.evidence, '--evidence')
    record, detail = probe.install_record()
    if not record:
        raise RuntimeError(detail)
    target = urlsplit(url)
    model = ThreadingHTTPServer((target.hostname, target.port), ModelCapture)
    model.requests = []
    model.evidence = evidence
    evidence.mkdir(parents=True, exist_ok=False)
    run = probe.PROBE_DATA / 'task-256' / uuid.uuid4().hex
    home, work = run / 'home', run / 'workspace'
    home.mkdir(parents=True)
    work.mkdir()
    (home / 'memories').mkdir()
    ledger = run / 'override-fixture.db'
    with sqlite3.connect(ledger) as conn:
        conn.row_factory = sqlite3.Row
        db.init_schema(conn)
        conn.execute("INSERT INTO members(profile,display_name,role) VALUES('default','fixture','dev')")
        conn.execute("INSERT INTO projects(name,root,created_at) VALUES('isolated',?,?)", (str(work), db.now_iso()))
        bot_id = db._ensure_hermes_bot(conn, 'default')
        conn.commit()
    markers = {key: key.upper() + '_' + uuid.uuid4().hex for key in
               ('rule_a', 'rule_b', 'tool_a', 'tool_b', 'soul_a', 'soul_b', 'memory_a', 'memory_b')}
    config = {'model': {'default': 'gpt-4.1-mini', 'provider': 'custom', 'base_url': url + '/v1',
                        'api_key': 'isolated-fixture'}, 'mcp_servers': {},
              'agent': {'reasoning_effort': 'low', 'max_turns': 3}, 'checkpoints': {'enabled': False},
              'terminal': {'cwd': str(work)}, 'approvals': {'mode': 'smart'},
              'tools': {'tool_search': {'enabled': 'on'}}}

    def write_config():
        (home / 'config.yaml').write_text(json.dumps(config), encoding='utf-8')

    write_config()
    (work / 'AGENTS.md').write_text('Project rule marker: ' + markers['rule_a'], encoding='utf-8')
    (home / 'SOUL.md').write_text('Identity marker: ' + markers['soul_a'], encoding='utf-8')
    (home / 'memories' / 'MEMORY.md').write_text('Memory marker: ' + markers['memory_a'], encoding='utf-8')
    env = probe.probe_env()
    env.update(HERMES_HOME=str(home), HERMES_RUNTIME_DIR=str(run / 'runtime'),
               OPENAI_API_KEY='isolated-fixture', OPENAI_BASE_URL=url + '/v1')
    command = [str(record['python']), '-X', 'utf8', '-B', '-m', 'hermes_cli.main']
    result = {'home': str(home), 'workspace': str(work), 'model_fixture': url, 'checks': {}, 'turns': [],
              'evidence_kind': 'real gateway outbound HTTP payload; deterministic inference fixture'}
    endpoint = None
    worker = threading.Thread(target=model.serve_forever, daemon=True)
    worker.start()
    try:
        endpoint = ensure(command, home, env=env, timeout=120)
        result['endpoint'] = endpoint
        print('Isolated gateway ready', flush=True)
        with GatewayClient(endpoint) as client:
            snapshot = client.rpc('session.create', request_id=uuid.uuid4().hex, source='acp',
                                  cwd=str(work), editor={'mcp_servers': []})
            sid = snapshot['session_id']
            result['session_id'] = sid
            with sqlite3.connect(ledger) as conn:
                conn.execute("INSERT INTO session_bindings(project,bot_id,profile,kind,thread_id,session_id,runtime,generation,title,updated_at) "
                             "VALUES('isolated',?,'default','chat',7,?,'hermes',0,'refresh-fixture',?)",
                             (bot_id, sid, db.now_iso()))

            def engine_new():
                nonlocal snapshot
                with sqlite3.connect(ledger) as conn:
                    conn.row_factory = sqlite3.Row
                    with patch.object(gateway, 'enabled', return_value=True), \
                         patch.object(gateway, 'connection', side_effect=lambda *a, **k: (GatewayClient(endpoint), home, command)), \
                         patch.object(session_adapter.Path, 'cwd', return_value=work):
                        switched = session_adapter.switch_session(conn, 'isolated', 'default', 'chat', 7, 'refresh-fixture')
                    assert switched['ok'], switched
                    binding = dict(conn.execute("SELECT * FROM session_bindings WHERE project='isolated' AND bot_id=? AND kind='chat' AND thread_id=7", (bot_id,)).fetchone())
                    assert binding['session_id'] == switched['session_id']
                    assert binding['thread_id'] == 7 and binding['generation'] == switched['generation']
                    result.setdefault('engine_switches', []).append(switched)
                snapshot = sessions.attach(client, switched['session_id'])

            def turn(index, description, extra=False, model_args=()):
                nonlocal snapshot
                server = {'command': str(record['python']), 'args': ['-B', str(Path(__file__).resolve()), '--mcp'],
                          'env': {'REFRESH_TOOL_DESCRIPTION': description, 'REFRESH_EXTRA_TOOL': '1' if extra else '0'}}
                # Engine admissions use a fresh viewer; repeated resume on the
                # same attached viewer intentionally skips native config refresh.
                with GatewayClient(endpoint) as viewer:
                    with sqlite3.connect(ledger) as conn:
                        snapshot = sessions.configure_tracked(conn, 'isolated', viewer,
                            sessions.attach(viewer, snapshot['session_id']),
                            workdir=str(work), model_args=list(model_args), server=server)
                    count = len(model.requests)
                    admission = viewer.rpc('prompt.submit', session_id=snapshot['session_id'],
                                           submission_id=uuid.uuid4().hex, text=f'Reply OK. Refresh fixture turn {index}.')
                    deadline = time.monotonic() + 90
                    while time.monotonic() < deadline:
                        receipt = viewer.rpc('prompt.receipt', session_id=snapshot['session_id'],
                                             admission_id=admission['admission_id'], include_result=True)
                        if receipt['status'] == 'terminal':
                            break
                        time.sleep(0.5)
                    else:
                        raise TimeoutError('Fixture turn did not settle')
                result['turns'].append(receipt)
                assert receipt.get('outcome') == 'completed', receipt
                assert len(model.requests) > count, 'No request reached the inference fixture'
                # Auxiliary title/model-metadata requests are separate calls.
                body = [item for item in model.requests[count:] if 'tools' in item and
                        f'Refresh fixture turn {index}' in json.dumps(item.get('messages', []))][-1]
                system = json.dumps([m for m in body.get('messages', []) if m.get('role') in ('system', 'developer')])
                user_index = max(i for i, message in enumerate(body['messages']) if message.get('role') == 'user')
                tools = json.dumps([json.loads(message['content']).get('tools', {})
                                    for message in body['messages'][user_index + 1:] if message.get('role') == 'tool'])
                print(f'Turn {index}: outbound model payload captured', flush=True)
                return body, system, tools

            first, system, tools = turn(1, markers['tool_a'], True)
            result['checks']['first_rule'] = markers['rule_a'] in system
            result['checks']['first_soul'] = markers['soul_a'] in system
            result['checks']['first_memory'] = markers['memory_a'] in system
            result['checks']['first_tools'] = markers['tool_a'] in tools and 'removed_probe' in tools
            (work / 'AGENTS.md').write_text('Project rule marker: ' + markers['rule_b'], encoding='utf-8')
            (home / 'SOUL.md').write_text('Identity marker: ' + markers['soul_b'], encoding='utf-8')
            (home / 'memories' / 'MEMORY.md').write_text('Memory marker: ' + markers['memory_b'], encoding='utf-8')
            config['agent']['reasoning_effort'] = 'high'
            write_config()
            second, system, tools = turn(2, markers['tool_b'])
            result['checks']['rule_modified_same_sid_cached'] = markers['rule_a'] in system and markers['rule_b'] not in system
            result['checks']['tool_modified_removed'] = markers['tool_b'] in tools and markers['tool_a'] not in tools and 'removed_probe' not in tools
            result['checks']['identity_stable'] = markers['soul_a'] in system and markers['soul_b'] not in system
            result['checks']['memory_stable'] = markers['memory_a'] in system and markers['memory_b'] not in system
            result['checks']['reasoning_refresh'] = second.get('reasoning_effort') == 'high'
            (work / 'AGENTS.md').unlink()
            config['agent'].pop('reasoning_effort')
            write_config()
            third, system, tools = turn(3, markers['tool_b'])
            result['checks']['rule_deleted_same_sid_cached'] = markers['rule_a'] in system and markers['rule_b'] not in system
            result['checks']['reasoning_deleted'] = third.get('reasoning_effort') != 'high'
            result['checks']['history_kept'] = 'Refresh fixture turn 1' in json.dumps(third.get('messages', []))
            (work / 'AGENTS.md').write_text('Project rule marker: ' + markers['rule_b'], encoding='utf-8')
            engine_new()
            fourth, system, tools = turn(4, markers['tool_b'])
            result['checks']['new_modified_rule'] = markers['rule_b'] in system and markers['rule_a'] not in system
            result['checks']['new_identity'] = markers['soul_b'] in system and markers['soul_a'] not in system
            result['checks']['new_memory'] = markers['memory_b'] in system and markers['memory_a'] not in system
            result['checks']['new_session'] = snapshot['session_id'] != sid
            modified_sid = snapshot['session_id']
            (work / 'AGENTS.md').unlink()
            engine_new()
            _, system, _ = turn(5, markers['tool_b'])
            result['checks']['new_deleted_rule'] = markers['rule_a'] not in system and markers['rule_b'] not in system
            result['checks']['new_deleted_sid'] = snapshot['session_id'] != modified_sid
            result['checks']['engine_same_thread_generations'] = [s['generation'] for s in result['engine_switches']] == [1, 2]
            fifth, _, _ = turn(6, markers['tool_b'], model_args=['--reasoning', 'high'])
            result['checks']['engine_pin'] = fifth.get('reasoning_effort') == 'high'
            sixth, _, _ = turn(7, markers['tool_b'])
            result['checks']['engine_clear_after_reopen'] = sixth.get('reasoning_effort') == 'medium'
            snapshot = sessions.attach(client, snapshot['session_id'])
            sessions.mutate(client, snapshot, 'reasoning', {'effort': 'low'})
            seventh, _, _ = turn(8, markers['tool_b'])
            result['checks']['native_choice_after_reset_preserved'] = seventh.get('reasoning_effort') == 'low'
    finally:
        if endpoint:
            stopped = subprocess.run(command + ['gateway', 'stop', '--yes'], env=env, capture_output=True,
                                     text=True, encoding='utf-8', timeout=60,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            result['stop_exit_code'] = stopped.returncode
        model.shutdown()
        model.server_close()
        worker.join(timeout=5)
        (evidence / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result['checks'], indent=2))
    return 0 if all(result['checks'].values()) and result.get('stop_exit_code') == 0 else 1


if __name__ == '__main__':
    if '--mcp' in sys.argv:
        mcp_main()
    else:
        raise SystemExit(main())
