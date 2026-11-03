"""Real native delegation, engine reconciliation and MCP writes with an isolated model.

All external paths are explicit. The model emits deterministic tool calls; no provider
credentials or production engine are used. Both success and failure stop the owned gateway.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack, closing
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from unittest.mock import patch
from urllib.parse import urlsplit
import uuid
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.verify_subagent_lifecycle_native import Model


def until(predicate, label, seconds=120):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.1)
    raise TimeoutError(label)


def main():
    from scripts.probe_config import absolute_path
    from scripts.unified_test_support import isolated_database, isolated_api
    from engine import auth, db, heartbeat, hermes_gateway as gateway, hermes_connections, wake
    from engine import hermes_gateway_ledger as ledger, hermes_session_auth as grants
    from engine.hermes_gateway_client import GatewayClient, ensure
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('hermes-python', 'hermes-source', 'run-dir', 'evidence'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--mcp-path', choices=('native', 'client'), default='native',
                        help='native model tools, or separate real MCP client while the native callback is held')
    parser.add_argument('--trace-tools', action='store_true', help='Instrument only the isolated native tool-definition module')
    parser.add_argument('--scope-cache-fix', action='store_true',
                        help='Experimentally add session tool scope to the isolated native schema cache key')
    args = parser.parse_args()
    run, evidence, source, python = [absolute_path(getattr(args, n.replace('-', '_')), '--' + n)
        for n in ('run-dir', 'evidence', 'hermes-source', 'hermes-python')]
    if not python.is_file() or not (source / 'gateway/session_authority.py').is_file():
        parser.error('Hermes interpreter/source missing')
    run.mkdir(parents=True, exist_ok=False)
    evidence.mkdir(parents=True, exist_ok=False)
    home, work = run / 'h', run / 'w'
    home.mkdir(); work.mkdir()
    model = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    model.evidence, model.requests, model.guard = evidence, [], threading.Lock()
    for name in ('child_entered', 'parent_entered', 'release_child', 'release_parent'):
        setattr(model, name, threading.Event())
    origin = f'http://127.0.0.1:{model.server_port}'
    config = {'model': {'default': 'gpt-4.1-mini', 'provider': 'custom', 'base_url': origin + '/v1',
                       'api_key': 'loopback-fixture'}, 'mcp_servers': {},
              'platform_toolsets': {'acp': ['delegation']}, 'agent': {'max_turns': 16},
              'terminal': {'cwd': str(work)}, 'approvals': {'mode': 'smart'},
              'checkpoints': {'enabled': False}, 'updates': {'check': False},
              'models_dev': {'url': origin + '/models'},
              'auxiliary': {'title_generation': {'enabled': False}},
              'skills': {'creation_nudge_interval': 0}, 'memory': {'nudge_interval': 0}}
    (home / 'config.yaml').write_text(json.dumps(config), encoding='utf-8')
    allowed = {'path', 'pathext', 'comspec', 'systemroot', 'windir', 'temp', 'tmp',
               'localappdata', 'appdata', 'programdata', 'userprofile', 'systemdrive'}
    env = {k: v for k, v in os.environ.items() if k.lower() in allowed}
    env.update(HERMES_HOME=str(home), HERMES_RUNTIME_DIR=str(run / 'r'), PYTHONPATH=str(source),
               OPENAI_API_KEY='loopback-fixture', OPENAI_BASE_URL=origin + '/v1',
               NO_PROXY='127.0.0.1,localhost', PYTHONUTF8='1', HERMES_DISABLE_LAZY_INSTALLS='1')
    if args.trace_tools or args.scope_cache_fix:
        overlay = run / 'trace'
        overlay.mkdir()
        trace_wrapper = '\n'.join([
            'import json as _trace_json, traceback as _trace_stack, threading as _trace_thread',
            '_trace_original = get_tool_definitions',
            'def get_tool_definitions(*args, **kwargs):',
            '    result = _trace_original(*args, **kwargs)',
            '    from tools.registry import current_session_tool_scope',
            '    item = dict(args=args, kwargs=kwargs, scope=registry.current_scope_key(), session_scope=current_session_tool_scope(),',
            '                names=[t["function"]["name"] for t in result],',
            '                thread=_trace_thread.current_thread().name, stack=_trace_stack.format_stack(limit=8))',
            '    with open(' + repr(str(evidence / 'tool-trace.jsonl')) + ', "a", encoding="utf-8") as output:',
            '        output.write(_trace_json.dumps(item, default=str) + "\\n")',
            '    return result',
        ])
        if args.scope_cache_fix:
            trace_wrapper += '\n' + '\n'.join([
                '_scope_cache_original = _tool_defs_cache_key',
                'def _tool_defs_cache_key(*args, **kwargs):',
                '    from tools.registry import current_session_tool_scope',
                '    key = _scope_cache_original(*args, **kwargs)',
                '    return None if key is None else (*key, current_session_tool_scope())',
            ])
        (overlay / 'sitecustomize.py').write_text(
            'import model_tools\nexec(' + repr(trace_wrapper) + ', model_tools.__dict__)\n', encoding='utf-8')
        env['PYTHONPATH'] = str(overlay) + os.pathsep + str(source)
    command = [str(python), '-X', 'utf8', '-B', '-m', 'hermes_cli.main']
    result = {'engine_commit': subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip(),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'boundary': 'real native gateway, reconciler, MCP and HTTP writes; deterministic loopback model',
              'mcp_path': args.mcp_path,
              'trace_tools': args.trace_tools,
              'scope_cache_fix': args.scope_cache_fix,
              'home': str(home), 'source': str(source), 'snapshots': []}
    endpoint = None
    threading.Thread(target=model.serve_forever, daemon=True).start()

    def save():
        (evidence / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')

    try:
        print('Starting owned native gateway', flush=True)
        endpoint = ensure(command, home, env=env, timeout=180)
        result['endpoint'] = endpoint
        with ExitStack() as stack:
            database = stack.enter_context(isolated_database())
            conn = stack.enter_context(closing(db.get_connection()))
            url = stack.enter_context(isolated_api())
            stack.enter_context(patch.object(auth, 'signing_keys', return_value=(1, {1: b'native-wait-fixture-key'})))
            stack.enter_context(patch.object(wake, '_engine_url', return_value=url))
            stack.enter_context(patch.object(wake, '_MCP_PYTHON', sys.executable))
            stack.enter_context(patch.object(wake, 'wake_system_prompt', return_value='Isolated scheduling validation.'))
            for obj, name, value in [(wake, '_running_sessions', set()), (wake, '_running_wakes', {}),
                                     (gateway, '_workers', set()), (heartbeat, '_beats', {})]:
                stack.enter_context(patch.object(obj, name, value))

            pool = hermes_connections.Pool(
                opener=lambda home, command, callback, **kw: GatewayClient(endpoint, on_event=callback),
                revision=lambda: ('fixture', endpoint.get('code_sha')), pinned=lambda home: True)

            def close_pool():
                pool.stop(); pool.join()

            stack.callback(close_pool)
            stack.enter_context(patch.object(hermes_connections, '_POOL', pool))

            def connect(profile, **options):
                if options.get('home') and Path(options['home']).resolve() != home.resolve():
                    raise AssertionError('Foreign native home')
                return hermes_connections.acquire(home, command), home, command

            stack.enter_context(patch.object(gateway, 'connection', side_effect=connect))
            conn.execute("UPDATE projects SET root=? WHERE name='alpha'", (str(work),))
            bot = db._ensure_hermes_bot(conn, 'default')
            conn.execute("INSERT INTO requirements(id,project,title,created_at,updated_at) VALUES(1,'alpha','fixture',?,?)",
                         (db.now_iso(), db.now_iso()))
            conn.execute("INSERT INTO tasks(id,project,requirement_id,title,status,claimed_by,created_at) "
                         "VALUES(1,'alpha',1,'fixture','in_progress','default',?)", (db.now_iso(),))
            role = conn.execute("SELECT role_id FROM project_members WHERE project='alpha' AND profile='architect'").fetchone()[0]
            conn.commit()

            def callback_response(body):
                if args.mcp_path == 'client':
                    return {'role': 'assistant', 'content': 'PARENT_FINISHED'}, 'stop'
                calls, discovered = [], set()
                for message in body['messages']:
                    for call in message.get('tool_calls', []):
                        function = call['function']
                        calls.append(function['name'])
                        if function['name'] == 'tool_call':
                            calls.extend(c['name'] for c in json.loads(function['arguments']).get('calls', []))
                    if message.get('role') == 'tool':
                        try:
                            payload = json.loads(message.get('content', ''))
                        except (ValueError, TypeError):
                            continue
                        if isinstance(payload, dict):
                            discovered.update(payload.get('tools', {}))
                names = [t['function']['name'] for t in body.get('tools', [])]
                target = 'group_send' if not any(n.endswith('group_send') for n in calls) else 'task_log'
                if any(n.endswith('task_log') for n in calls):
                    return {'role': 'assistant', 'content': 'PARENT_FINISHED'}, 'stop'
                name = next((n for n in [*names, *discovered] if n.endswith(target)), 'tool_search')
                arguments = ({'text': '@architect DELEGATION_CALLBACK_OK', 'need_reply': False} if target == 'group_send' else
                    {'project': 'alpha', 'task_id': 1, 'type': 'dev', 'result': 'isolated callback proof',
                     'next_bot': 'architect', 'expected_role_id': role})
                if name == 'tool_search':
                    arguments = {'queries': [target]}
                elif name not in names:
                    arguments = {'calls': [{'name': name, 'arguments': arguments}]}
                    name = 'tool_call'
                return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call_' + uuid.uuid4().hex[:12],
                    'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}}]}, 'tool_calls'

            model.callback_response = callback_response

            def row():
                return conn.execute('SELECT * FROM hermes_executions ORDER BY started_at DESC LIMIT 1').fetchone()

            def snapshot(label):
                current = row()
                result['snapshots'].append({'label': label, 'execution': dict(current) if current else None,
                    'task': dict(conn.execute('SELECT * FROM tasks WHERE id=1').fetchone()),
                    'active_runs': wake.active_runs(),
                    'failures': conn.execute("SELECT COUNT(*) FROM events WHERE event_type='execution.failed'").fetchone()[0]})
                save()
                return current

            async def invoke_mcp():
                from mcp import ClientSession, StdioServerParameters
                from mcp.client.stdio import stdio_client
                grant = conn.execute('SELECT * FROM hermes_session_grants WHERE revoked=0').fetchone()
                spec = grants.server(grant, grant['signing_generation'])
                params = StdioServerParameters(command=spec['command'], args=spec['args'], env={**os.environ, **spec['env']})
                with (evidence / 'mcp-client.log').open('w', encoding='utf-8') as error_log:
                    async with stdio_client(params, errlog=error_log) as (reader, writer):
                        async with ClientSession(reader, writer) as client:
                            await client.initialize()
                            results = []
                            for name, arguments in [('group_send', {'text': '@architect DELEGATION_CALLBACK_OK', 'need_reply': False}),
                                    ('task_log', {'project': 'alpha', 'task_id': 1, 'type': 'dev', 'result': 'isolated callback proof',
                                                  'next_bot': 'architect', 'expected_role_id': role})]:
                                response = await client.call_tool(name, arguments)
                                results.append(response.model_dump(mode='json'))
                                assert not response.isError, results[-1]
                            result['mcp_client_results'] = results

            try:
                submitted = gateway.spawn('default', 'work', 'PARENT_SCENARIO_PROBE: delegate and wait for the callback.',
                    None, 'alpha', bot=dict(conn.execute('SELECT * FROM bots WHERE id=?', (bot,)).fetchone()),
                    workdir=str(work), model_args=[], bound_task=1)
                result['spawn'] = submitted
                assert submitted['ok'], submitted
                until(lambda: row() and row()['state'] == 'waiting_delegation', 'Engine never entered delegation wait')
                assert model.child_entered.wait(10)
                first = snapshot('parent_terminal_child_running')
                assert not wake.active_runs()
                assert result['snapshots'][-1]['failures'] == 0
                assert result['snapshots'][-1]['task']['status'] == 'in_progress'
                print('Engine wait established, task retained, thinking cleared', flush=True)
                with closing(GatewayClient(endpoint)) as remote:
                    result['native_wait'] = remote.rpc('session.resume', session_id=first['session_id'], omit_messages=True)
                    result['native_children'] = remote.rpc('subagent.list', session_id=first['session_id'])
                assert result['native_wait']['running'] is False
                assert any(c['status'] == 'running' for c in result['native_children']['subagents'])
                model.release_child.set()
                until(model.parent_entered.is_set, 'Native parent callback absent', 60)
                snapshot('automatic_callback_before_tools')
                if args.mcp_path == 'client':
                    until(lambda: row()['admission_id'] != first['admission_id'], 'Callback admission not adopted')
                    asyncio.run(invoke_mcp())
                model.release_parent.set()
                until(lambda: row()['state'] == 'terminal' and not row()['cleanup_pending'] and not gateway._workers,
                      'Engine callback did not settle', 150)
                final = snapshot('finished')
                result['turns'] = [dict(r) for r in conn.execute('SELECT * FROM hermes_native_turns')]
                result['messages'] = [dict(r) for r in conn.execute("SELECT * FROM messages WHERE sender='default'")]
                result['nodes'] = [dict(r) for r in conn.execute('SELECT * FROM nodes WHERE task_id=1')]
                assert final['outcome'] == 'completed' or final['cancel_reason'] == 'superseded', dict(final)
                assert len(result['turns']) == 2, result['turns']
                assert len(result['messages']) == 1 and 'DELEGATION_CALLBACK_OK' in result['messages'][0]['content'], result['messages']
                assert len(result['nodes']) == 1, result['nodes']
                assert result['snapshots'][-1]['task']['next_bot'] == 'architect'
                assert result['snapshots'][-1]['failures'] == 0
                assert conn.execute('SELECT COUNT(*) FROM hermes_executions').fetchone()[0] == 1
                result['passed'] = True
                print('Real message and task-node writes verified; MCP path: ' + args.mcp_path, flush=True)
            finally:
                model.release_child.set(); model.release_parent.set()
                if row() and row()['state'] != 'terminal':
                    ledger.cancel(conn, row()['execution_id'], 'user')
                deadline = time.monotonic() + 35
                while gateway._workers and time.monotonic() < deadline:
                    time.sleep(.1)
                if gateway._workers:
                    conn.execute("UPDATE hermes_executions SET state='manual' WHERE state<>'terminal'")
                    conn.commit()
                    until(lambda: not gateway._workers, 'Test reconciler did not stop', 15)
                import sqlite3
                with closing(sqlite3.connect(evidence / 'engine.db')) as backup:
                    conn.backup(backup)
    except BaseException as exc:
        result['error'] = repr(exc)
        raise
    finally:
        model.release_child.set(); model.release_parent.set()
        stopped = subprocess.run(command + ['gateway', 'stop', '--yes'], env=env,
            capture_output=True, text=True, encoding='utf-8', timeout=90,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        result['stop'] = {'code': stopped.returncode, 'stdout': stopped.stdout, 'stderr': stopped.stderr}
        if endpoint:
            target = urlsplit(endpoint['api_origin'])
            with socket.socket() as check:
                result['gateway_port_released'] = check.connect_ex((target.hostname, target.port)) != 0
        result['gateway_pid_file_removed'] = not (home / 'gateway.pid').exists()
        model.shutdown(); model.server_close(); save()
        print(json.dumps({k: result.get(k) for k in ('passed', 'error', 'gateway_port_released', 'gateway_pid_file_removed')}), flush=True)
    assert stopped.returncode == 0 and result['gateway_port_released'] and result['gateway_pid_file_removed']


if __name__ == '__main__':
    main()
