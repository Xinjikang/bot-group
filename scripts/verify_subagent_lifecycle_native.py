"""Probe native child visibility using a real gateway and a controlled loopback model.

Requires explicit isolated resource paths. No engine server or production model is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Model(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"data": []}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        with self.server.guard:
            index = len(self.server.requests)
            self.server.requests.append(body)
        (self.server.evidence / f'model-{index:03}.json').write_text(
            json.dumps(body, ensure_ascii=False, indent=2), encoding='utf-8')
        messages = body.get('messages', [])
        users = [str(m.get('content', '')) for m in messages if m.get('role') == 'user']
        child = any('CHILD_UNIT_' in u for u in users) and not any('PARENT_SCENARIO_' in u for u in users)
        last_user = users[-1] if users else ''
        delegated = any(t.get('function', {}).get('name') == 'delegate_task'
                        for m in messages for t in m.get('tool_calls', []))
        msg = {'role': 'assistant', 'content': 'PARENT_WAITING'}
        finish = 'stop'
        if child:
            self.server.child_entered.set()
            if not self.server.release_child.wait(240):
                raise TimeoutError('Child model barrier expired')
            msg['content'] = 'CHILD_FINISHED'
        elif delegated and 'PARENT_SCENARIO_' not in last_user:
            self.server.parent_entered.set()
            if not self.server.release_parent.wait(240):
                raise TimeoutError('Parent model barrier expired')
            msg['content'] = 'PARENT_FINISHED'
        elif not delegated and any('PARENT_SCENARIO_' in u for u in users):
            names = {t.get('function', {}).get('name') for t in body.get('tools', [])}
            if 'delegate_task' in names:
                name, args = 'delegate_task', {'tasks': [{'goal': 'CHILD_UNIT_PROBE: reply CHILD_FINISHED'}]}
            else:
                name, args = 'tool_search', {'queries': ['delegate_task']}
            msg = {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'call_' + uuid.uuid4().hex[:12], 'type': 'function',
                 'function': {'name': name, 'arguments': json.dumps(args)}}]}
            finish = 'tool_calls'
        base = {'id': f'probe-{index}', 'created': int(time.time()), 'model': body.get('model', '')}
        if body.get('stream'):
            delta = dict(msg)
            if 'tool_calls' in delta:
                delta['tool_calls'] = [dict(t, index=i) for i, t in enumerate(delta['tool_calls'])]
            payload = ''.join('data: ' + json.dumps({**base, 'object': 'chat.completion.chunk', 'choices': [c]}) + '\n\n'
                for c in [{'index': 0, 'delta': delta, 'finish_reason': None},
                          {'index': 0, 'delta': {}, 'finish_reason': finish}]) + 'data: [DONE]\n\n'
            content_type = 'text/event-stream'
        else:
            payload = json.dumps({**base, 'object': 'chat.completion', 'choices': [
                {'index': 0, 'message': msg, 'finish_reason': finish}],
                'usage': {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30}})
            content_type = 'application/json'
        data = payload.encode()
        try:
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


def main():
    from engine.hermes_gateway_client import GatewayClient, ensure
    from scripts.probe_config import absolute_path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hermes-python', required=True)
    parser.add_argument('--hermes-source', required=True)
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--evidence', required=True)
    parser.add_argument('--restart', action='store_true')
    args = parser.parse_args()
    run = absolute_path(args.run_dir, '--run-dir')
    evidence = absolute_path(args.evidence, '--evidence')
    source = absolute_path(args.hermes_source, '--hermes-source')
    python = absolute_path(args.hermes_python, '--hermes-python')
    if not python.is_file() or not (source / 'gateway/session_authority.py').is_file():
        parser.error('Existing Hermes interpreter and source required')
    run.mkdir(parents=True, exist_ok=False)
    evidence.mkdir(parents=True, exist_ok=False)
    home, work = run / 'h', run / 'w'
    home.mkdir(); work.mkdir()
    model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    model.evidence, model.requests, model.guard = evidence, [], threading.Lock()
    for key in ('child_entered', 'parent_entered', 'release_child', 'release_parent'):
        setattr(model, key, threading.Event())
    origin = f'http://127.0.0.1:{model.server_port}'
    cfg = {'model': {'default': 'gpt-4.1-mini', 'provider': 'custom', 'base_url': origin + '/v1',
                     'api_key': 'loopback-fixture'}, 'mcp_servers': {},
           'platform_toolsets': {'acp': ['delegation']}, 'agent': {'max_turns': 8},
           'terminal': {'cwd': str(work)}, 'approvals': {'mode': 'smart'},
           'checkpoints': {'enabled': False}, 'updates': {'check': False},
           'models_dev': {'url': origin + '/models'},
           'auxiliary': {'title_generation': {'enabled': False}},
           'skills': {'creation_nudge_interval': 0}, 'memory': {'nudge_interval': 0}}
    (home / 'config.yaml').write_text(json.dumps(cfg), encoding='utf-8')
    allowed = {'path', 'pathext', 'comspec', 'systemroot', 'windir', 'temp', 'tmp',
               'localappdata', 'appdata', 'programdata', 'userprofile', 'systemdrive'}
    env = {k: v for k, v in os.environ.items() if k.lower() in allowed}
    env.update(HERMES_HOME=str(home), HERMES_RUNTIME_DIR=str(run / 'r'), PYTHONPATH=str(source),
               OPENAI_API_KEY='loopback-fixture', OPENAI_BASE_URL=origin + '/v1',
               NO_PROXY='127.0.0.1,localhost', PYTHONUTF8='1', HERMES_DISABLE_LAZY_INSTALLS='1')
    command = [str(python), '-X', 'utf8', '-B', '-m', 'hermes_cli.main']
    result = {'run': str(run), 'home': str(home), 'source': str(source), 'snapshots': [],
              'boundary': 'real native gateway, admissions, delegate_task and callback; deterministic loopback model'}
    result['engine_commit'] = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip()
    result['script_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    events = []
    client = endpoint = None
    threading.Thread(target=model.serve_forever, daemon=True).start()

    def save():
        (evidence / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        (evidence / 'events.json').write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding='utf-8')

    def durable():
        try:
            with sqlite3.connect((home / 'state.db').as_uri() + '?mode=ro', uri=True, timeout=1) as conn:
                conn.row_factory = sqlite3.Row
                return [dict(r) for r in conn.execute('SELECT delegation_id,parent_session_id,state,delivery_state,owner_pid,event_json FROM async_delegations')]
        except sqlite3.Error as exc:
            return {'error': str(exc)}

    def snapshot(label, viewer=None):
        viewer = viewer or client
        row = {'label': label, 'time': time.time(), 'durable': durable()}
        for method in ('session.resume', 'subagent.list'):
            try:
                params = {'session_id': result['session_id']}
                if method == 'session.resume': params['omit_messages'] = True
                value = viewer.rpc(method, timeout=5, **params)
                if method == 'session.resume':
                    value = {k: value.get(k) for k in ('session_id', 'running', 'pending', 'execution_generation', 'revision')}
                row[method] = value
            except Exception as exc:
                row[method] = {'error': str(exc)}
        result['snapshots'].append(row)
        save()
        return row

    def stop():
        stopped = subprocess.run(command + ['gateway', 'stop', '--yes'], env=env,
            capture_output=True, text=True, encoding='utf-8', timeout=90,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        result.setdefault('stops', []).append({'code': stopped.returncode, 'stdout': stopped.stdout, 'stderr': stopped.stderr})

    try:
        print('Starting isolated native gateway', flush=True)
        endpoint = ensure(command, home, env=env, timeout=180)
        result['endpoint'] = endpoint
        save()
        client = GatewayClient(endpoint, on_event=events.append)
        created = client.rpc('session.create', request_id=uuid.uuid4().hex, source='acp', cwd=str(work), editor={'mcp_servers': []})
        result['session_id'] = created['session_id']
        result['create_info'] = created.get('info')
        admission = client.rpc('prompt.submit', session_id=result['session_id'], submission_id=uuid.uuid4().hex,
                               text='PARENT_SCENARIO_PROBE: delegate one child and end this turn, then consume its callback.')
        result['admission'] = admission
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            receipt = client.rpc('prompt.receipt', session_id=result['session_id'], admission_id=admission['admission_id'], include_result=True)
            result['receipt'] = receipt
            if receipt.get('status') == 'terminal' or receipt.get('state') == 'terminal':
                break
            time.sleep(.25)
        else:
            raise TimeoutError('Initial admission did not terminate')
        assert receipt.get('outcome') == 'completed', receipt
        assert model.child_entered.wait(10), 'Child never reached the model barrier'
        held = snapshot('parent_terminal_child_running')
        assert held['session.resume']['running'] is False and not held['session.resume']['pending'], held
        assert any(s['status'] == 'running' for s in held['subagent.list']['subagents']), held
        print('Parent terminal; child held running; querying fresh connection', flush=True)
        client.close()
        client = GatewayClient(endpoint, on_event=events.append)
        snapshot('engine_connection_recreated_child_running')
        result['reconnect_replay'] = client.rpc('session.events.since', session_id=result['session_id'],
            replay_epoch=created['replay_epoch'], last_sequence=0, timeout=5)
        assert not result['reconnect_replay']['snapshot_required'], result['reconnect_replay']
        if args.restart:
            print('Restarting only isolated gateway while child is running', flush=True)
            client.close(); client = None
            stop()
            result['after_stop_durable'] = durable()
            model.release_child.set()
            endpoint = ensure(command, home, env=env, timeout=180)
            result['restart_endpoint'] = endpoint
            client = GatewayClient(endpoint, on_event=events.append)
            snapshot('gateway_restarted')
            result['restart_replay'] = client.rpc('session.events.since', session_id=result['session_id'],
                replay_epoch=created['replay_epoch'], last_sequence=0, timeout=5)
            result['recovered_receipt'] = client.rpc('prompt.receipt', session_id=result['session_id'],
                admission_id=admission['admission_id'], include_result=True, timeout=5)
        else:
            model.release_child.set()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not model.parent_entered.is_set():
            snapshot('completion_transition')
            time.sleep(.1)
        result['parent_resumed'] = model.parent_entered.is_set()
        snapshot('parent_callback_held' if model.parent_entered.is_set() else 'callback_not_observed')
        assert result['parent_resumed'], 'No automatic parent callback observed within 60s'
        print('Parent resumed: ' + str(model.parent_entered.is_set()), flush=True)
        model.release_parent.set()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            row = snapshot('settling')
            state = row['session.resume']
            if state.get('running') is False and not state.get('pending'): break
            time.sleep(.5)
        final = snapshot('final')
        assert final['session.resume']['running'] is False and not final['session.resume']['pending'], final
        assert final['session.resume']['execution_generation'] == 2, final
        result['final_replay_without_cursor'] = client.rpc('session.events.since', session_id=result['session_id'], last_sequence=0, timeout=5)
    except BaseException as exc:
        result['error'] = repr(exc)
        raise
    finally:
        model.release_child.set(); model.release_parent.set()
        if client is not None: client.close()
        try:
            stop()
            if endpoint:
                target = urlsplit(endpoint['api_origin'])
                with socket.socket() as check:
                    result['gateway_port_released'] = check.connect_ex((target.hostname, target.port)) != 0
            result['gateway_pid_file_removed'] = not (home / 'gateway.pid').exists()
        finally:
            model.shutdown(); model.server_close(); save()
        print(json.dumps({k: result.get(k) for k in ('parent_resumed', 'error', 'gateway_port_released', 'gateway_pid_file_removed')}), flush=True)
    assert all(s['code'] == 0 for s in result['stops']), result['stops']
    assert result['gateway_port_released'] and result['gateway_pid_file_removed'], 'Gateway cleanup incomplete'


if __name__ == '__main__':
    main()
