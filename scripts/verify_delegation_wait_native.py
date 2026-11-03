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
    from engine import auth, db, heartbeat, hermes_gateway as gateway, hermes_connections, queue, router, wake
    from engine import hermes_gateway_ledger as ledger, hermes_session_auth as grants
    from engine.hermes_gateway_client import GatewayClient, ensure
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('hermes-python', 'hermes-source', 'run-dir', 'evidence'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--mcp-path', choices=('native', 'client'), default='native',
                        help='native model tools, or separate real MCP client while the native callback is held')
    parser.add_argument('--scenario', choices=('work', 'direct', 'topic'), default='work')
    parser.add_argument('--toolsets', nargs='+',
                        help='Override isolated ACP toolsets; omitted uses Hermes ACP defaults')
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
              'platform_toolsets': {'acp': args.toolsets} if args.toolsets else {}, 'agent': {'max_turns': 16},
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
    command = [str(python), '-X', 'utf8', '-B', '-m', 'hermes_cli.main']

    def source_identity():
        return {'commit': subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip(),
                'status': subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain'], text=True),
                'model_tools_sha256': hashlib.sha256((source / 'model_tools.py').read_bytes()).hexdigest()}

    result = {'engine_commit': subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], text=True).strip(),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'boundary': 'real native gateway, reconciler, MCP and HTTP writes; deterministic loopback model',
              'mcp_path': args.mcp_path,
              'scenario': args.scenario,
              'toolsets': args.toolsets,
              'source_before': source_identity(),
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
            conn.execute("UPDATE project_members SET bot_id=? WHERE project='alpha' AND profile='default'", (bot,))
            conn.execute("UPDATE members SET bot_id=? WHERE profile='default'", (bot,))
            conn.execute("INSERT INTO requirements(id,project,title,created_at,updated_at) VALUES(1,'alpha','fixture',?,?)",
                         (db.now_iso(), db.now_iso()))
            conn.execute("INSERT INTO tasks(id,project,requirement_id,title,status,claimed_by,created_at) "
                         "VALUES(1,'alpha',1,'fixture','in_progress','default',?)", (db.now_iso(),))
            role = conn.execute("SELECT role_id FROM project_members WHERE project='alpha' AND profile='architect'").fetchone()[0]
            conn.commit()
            kind, tid = ('chat', 10) if args.scenario == 'topic' else ('work', None)
            def create_topic(topic_id):
                conn.execute("INSERT INTO threads(id,project,title,creator,members,status,created_at,last_active_at,"
                    "turn_profile,turn_wake_at,round) VALUES(?,'alpha','fixture','user',?,'open',?,?,'default',?,1)",
                    (topic_id, json.dumps(['default', 'architect', 'user']), db.now_iso(), db.now_iso(), db.now_iso(-900)))
                conn.commit()

            if tid:
                create_topic(tid)
            message_text = 'TOPIC_CALLBACK_OK' if tid else '@architect DELEGATION_CALLBACK_OK'

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
                if any(n.endswith('task_log') for n in calls) or tid and target == 'task_log':
                    return {'role': 'assistant', 'content': 'PARENT_FINISHED'}, 'stop'
                name = next((n for n in [*names, *discovered] if n.endswith(target)), 'tool_search')
                arguments = ({'text': message_text, 'need_reply': False} if target == 'group_send' else
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
            model.direct_response = lambda body: ({'role': 'assistant', 'content': 'DIRECT_HANDLED'}, 'stop')

            def row():
                return conn.execute("SELECT * FROM hermes_executions WHERE profile='default' AND kind=? "
                    'AND thread_id IS ? ORDER BY started_at DESC LIMIT 1', (kind, tid)).fetchone()

            def inject_message(profile, text, topic_id=None):
                mid = conn.execute("INSERT INTO messages(project,ts,sender,sender_type,content,mentions,need_reply,thread_id) "
                    "VALUES('alpha',?,'user','user',?,?,1,?)",
                    (db.now_iso(), text, json.dumps([profile]), topic_id)).lastrowid
                conn.execute('INSERT INTO inbox(message_id,profile) VALUES(?,?)', (mid, profile))
                conn.commit()
                return mid

            def advance_queue():
                with patch.object(gateway, 'enabled', return_value=True):
                    assert queue.process_one(conn), 'Ready queue item was not selected'
                failed = conn.execute("SELECT * FROM wake_jobs WHERE status='failed' ORDER BY id DESC LIMIT 1").fetchone()
                assert failed is None, dict(failed)

            def snapshot(label):
                current = row()
                result['snapshots'].append({'label': label, 'execution': dict(current) if current else None,
                    'task': dict(conn.execute('SELECT * FROM tasks WHERE id=1').fetchone()),
                    'topic': dict(conn.execute('SELECT * FROM threads WHERE id=?', (tid,)).fetchone()) if tid else None,
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
                            calls = [('group_send', {'text': message_text, 'need_reply': False})]
                            if not tid:
                                calls.append(
                                    ('task_log', {'project': 'alpha', 'task_id': 1, 'type': 'dev', 'result': 'isolated callback proof',
                                                  'next_bot': 'architect', 'expected_role_id': role}))
                            for name, arguments in calls:
                                response = await client.call_tool(name, arguments)
                                results.append(response.model_dump(mode='json'))
                                assert not response.isError, results[-1]
                            result['mcp_client_results'] = results

            try:
                submitted = gateway.spawn('default', kind, 'PARENT_SCENARIO_PROBE: delegate and wait for the callback.',
                    tid, 'alpha', bot=dict(conn.execute('SELECT * FROM bots WHERE id=?', (bot,)).fetchone()),
                    workdir=str(work), model_args=[], bound_task=None if tid else 1)
                result['spawn'] = submitted
                assert submitted['ok'], submitted
                until(lambda: row() and row()['state'] == 'waiting_delegation', 'Engine never entered delegation wait')
                assert model.child_entered.wait(10)
                first = snapshot('parent_terminal_child_running')
                assert not wake.active_runs()
                assert result['snapshots'][-1]['failures'] == 0
                assert result['snapshots'][-1]['task']['status'] == 'in_progress'
                if tid:
                    router.advance_timed_out_turns(conn)
                    assert conn.execute('SELECT turn_profile FROM threads WHERE id=?', (tid,)).fetchone()[0] == 'default'
                print('Engine wait established, task retained, thinking cleared', flush=True)
                with closing(GatewayClient(endpoint)) as remote:
                    result['native_wait'] = remote.rpc('session.resume', session_id=first['session_id'], omit_messages=True)
                    result['native_children'] = remote.rpc('subagent.list', session_id=first['session_id'])
                assert result['native_wait']['running'] is False
                assert any(c['status'] == 'running' for c in result['native_children']['subagents'])
                if args.scenario in ('direct', 'topic'):
                    # Keep the waiting session at the front of the real queue.
                    wake.enqueue(conn, 'default', kind, 'scan:task' if kind == 'work' else 'scan:group',
                                 thread_id=tid, project='alpha')
                    other_profile, other_tid = ('default', 20) if tid else ('architect', None)
                    if other_tid:
                        create_topic(other_tid)
                    else:
                        other_bot = db._ensure_hermes_bot(conn, other_profile)
                        conn.execute("UPDATE project_members SET bot_id=? WHERE project='alpha' AND profile=?",
                                     (other_bot, other_profile))
                        conn.commit()
                    mid = inject_message(other_profile, 'DIRECT_SCENARIO_INDEPENDENT', other_tid)
                    wake.enqueue(conn, other_profile, kind, 'scan:group', thread_id=other_tid,
                                 trigger_msg_id=mid, project='alpha')
                    advance_queue()

                    def other_finished():
                        other = conn.execute('SELECT * FROM hermes_executions WHERE profile=? AND kind=? AND thread_id IS ? '
                            'ORDER BY started_at DESC LIMIT 1', (other_profile, kind, other_tid)).fetchone()
                        return other if other and other['state'] == 'terminal' and not other['cleanup_pending'] else None

                    result['independent_execution'] = dict(until(other_finished, 'Independent session never completed'))
                    assert result['independent_execution']['outcome'] == 'completed'
                    assert row()['state'] == 'waiting_delegation' and not model.release_child.is_set()
                    print('Independent native session completed while original delegation remained waiting', flush=True)
                if args.scenario == 'direct':
                    for number in (1, 2):
                        mid = inject_message('default', f'DIRECT_SCENARIO_{number}')
                        wake.enqueue(conn, 'default', 'work', 'scan:task', project='alpha')
                        advance_queue()
                        until(lambda: row()['state'] == 'waiting_delegation' and conn.execute(
                            'SELECT COUNT(*) FROM hermes_native_turns WHERE terminal=1').fetchone()[0] == number + 1,
                            f'Direct input {number} never completed')
                        snapshot(f'direct_input_{number}_completed_child_still_running')
                        assert row()['task_id'] == 1 and not wake.active_runs()
                        assert not model.release_child.is_set()
                    result['direct_inputs'] = [dict(r) for r in conn.execute('SELECT * FROM hermes_delegation_inputs')]
                    assert len(result['direct_inputs']) == 2
                prior_admission = row()['admission_id']
                model.release_child.set()
                until(model.parent_entered.is_set, 'Native parent callback absent', 60)
                snapshot('automatic_callback_before_tools')
                if args.mcp_path == 'client':
                    until(lambda: row()['admission_id'] != prior_admission, 'Callback admission not adopted')
                    asyncio.run(invoke_mcp())
                model.release_parent.set()
                until(lambda: row()['state'] == 'terminal' and not row()['cleanup_pending'] and not gateway._workers,
                      'Engine callback did not settle', 150)
                final = snapshot('finished')
                result['turns'] = [dict(r) for r in conn.execute('SELECT * FROM hermes_native_turns WHERE execution_id=?',
                                                               (final['execution_id'],))]
                result['messages'] = [dict(r) for r in conn.execute("SELECT * FROM messages WHERE sender='default'")]
                result['nodes'] = [dict(r) for r in conn.execute('SELECT * FROM nodes WHERE task_id=1')]
                assert final['outcome'] == 'completed' or final['cancel_reason'] == 'superseded', dict(final)
                assert len(result['turns']) == (4 if args.scenario == 'direct' else 2), result['turns']
                assert len(result['messages']) == 1 and message_text in result['messages'][0]['content'], result['messages']
                assert len(result['nodes']) == (0 if tid else 1), result['nodes']
                if tid:
                    assert result['messages'][0]['thread_id'] == tid
                    assert result['snapshots'][-1]['topic']['turn_profile'] != 'default'
                else:
                    assert result['snapshots'][-1]['task']['next_bot'] == 'architect'
                assert result['snapshots'][-1]['failures'] == 0
                assert conn.execute('SELECT COUNT(*) FROM hermes_executions').fetchone()[0] == (1 if args.scenario == 'work' else 2)
                result['passed'] = True
                print('Scoped message/workflow writes verified; MCP path: ' + args.mcp_path, flush=True)
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
        result['source_after'] = source_identity()
        result['source_unchanged'] = result['source_before'] == result['source_after']
        result['passed'] = bool(result.get('passed') and result['source_unchanged'] and stopped.returncode == 0
                                and result.get('gateway_port_released') and result['gateway_pid_file_removed'])
        model.shutdown(); model.server_close(); save()
        print(json.dumps({k: result.get(k) for k in ('passed', 'error', 'gateway_port_released', 'gateway_pid_file_removed')}), flush=True)
    assert stopped.returncode == 0 and result['gateway_port_released'] and result['gateway_pid_file_removed']
    assert result['source_before'] == result['source_after'], 'Hermes source identity changed during verification'


if __name__ == '__main__':
    main()
