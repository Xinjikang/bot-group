"""Real Hermes executions across UI and script restarts of an isolated engine."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def mcp_wait():
    from mcp.server.fastmcp import FastMCP
    server = FastMCP('restart_probe')
    @server.tool()
    def restart_wait() -> str:
        """Wait for the isolated restart controller, then allow the final message."""
        root = Path(os.environ['RESTART_PROBE_WORK'])
        label = os.environ['RESTART_PROBE_LABEL']
        if label not in ('A', 'B'):
            raise ValueError('Invalid probe label')
        (root / ('ready_' + label)).touch()
        deadline = time.monotonic() + 360
        while not (root / 'continue').exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('Restart controller did not release the probe')
            time.sleep(.2)
        return 'Both restarts complete. Send the requested final message once.'
    server.run()


def serve(url):
    from fastapi import Depends
    from engine import main, hermes_gateway as gateway, db
    from engine.hermes_gateway_client import GatewayClient
    endpoint = json.loads(os.environ['RESTART_PROBE_ENDPOINT'])
    command = json.loads(os.environ['RESTART_PROBE_COMMAND'])
    home = Path(os.environ['HERMES_HOME'])
    gateway.connection = lambda *a, **k: (GatewayClient(endpoint), home, command)
    issued = {}
    original_configure = gateway.sessions.configure_tracked
    def configure(conn, project, client, snapshot, **kwargs):
        snapshot = original_configure(conn, project, client, snapshot, **kwargs)
        server = kwargs['server']
        context = json.loads(server['env']['XDG_GROUP_ENGINE_EXECUTION'])
        issued[context['thread_id']] = server['env']['GROUP_ENGINE_TOKEN']
        label = conn.execute('SELECT title FROM threads WHERE id=?', (context['thread_id'],)).fetchone()[0]
        specs = [{'name': 'task_queue', 'command': server['command'], 'args': server['args'],
                  'env': [{'name': k, 'value': v} for k, v in server['env'].items()]},
                 {'name': 'restart_probe', 'command': sys.executable, 'args': [__file__, '--mcp'],
                  'env': [{'name': 'RESTART_PROBE_WORK', 'value': os.environ['RESTART_PROBE_WORK']},
                          {'name': 'RESTART_PROBE_LABEL', 'value': label}]}]
        gateway.sessions.mutate(client, snapshot, 'editor_mcp', {'mcp_servers': specs})
        return gateway.sessions.attach(client, snapshot['session_id'])
    gateway.sessions.configure_tracked = configure

    @main.app.post('/probe/spawn')
    def spawn(payload: dict, identity=Depends(main._resolve_identity)):
        main._admin_only(identity, 'Probe administrator required')
        conn = db.get_connection()
        try:
            bot_id = db._ensure_hermes_bot(conn, 'default')
            bot = conn.execute('SELECT * FROM bots WHERE id=?', (bot_id,)).fetchone()
        finally:
            conn.close()
        result = gateway.spawn('default', 'chat', payload['signal'], payload['thread'], 'alpha',
                               bot=bot, workdir=os.environ['RESTART_PROBE_WORK'], model_args=[])
        return {**result, 'credential': issued.pop(payload['thread'], None)}

    target = urlsplit(url)
    main.main(['--host', target.hostname, '--port', str(target.port), '--data-dir', os.environ['GROUP_DATA_DIR']])


def verify(args):
    import httpx
    import yaml
    import gw_probe as probe
    from engine import db, auth, signing_keys
    from engine.hermes_gateway_client import GatewayClient, ensure
    from scripts.probe_config import absolute_path, loopback_url
    from scripts.project_test_support import enable_members

    url = loopback_url(args.backend)
    target = urlsplit(url)
    with socket.socket() as check:
        check.bind((target.hostname, target.port))
    evidence = absolute_path(args.evidence, '--evidence')
    evidence.mkdir(parents=True, exist_ok=False)
    record, detail = probe.install_record()
    if not record:
        raise RuntimeError(detail)
    run = probe.PROBE_DATA / 'task-257' / uuid.uuid4().hex
    home, workspace, data = run / 'home', run / 'workspace', run / 'engine'
    for directory in (home, workspace, data):
        directory.mkdir(parents=True)
    config = yaml.safe_load((probe.HERMES_HOME / 'config.yaml').read_text(encoding='utf-8'))
    config.update(mcp_servers={}, approvals={'mode': 'smart'},
                  terminal={'backend': 'local', 'cwd': str(workspace), 'timeout': 150})
    # Editor MCP names are session-scoped and appended by Hermes after config validation.
    config['platform_toolsets'] = {'acp': ['todo']}
    config.setdefault('agent', {})['max_turns'] = 20
    (home / 'config.yaml').write_text(json.dumps(config), encoding='utf-8')
    shutil.copy2(probe.HERMES_HOME / '.env', home / '.env')
    command = [str(record['python']), '-X', 'utf8', '-B', '-m', 'hermes_cli.main']
    env = probe.probe_env()
    env.update(HERMES_HOME=str(home), HERMES_RUNTIME_DIR=str(run / 'runtime'),
               PYTHONPATH=os.pathsep.join((str(ROOT), str(probe.WORKTREE_SRC))),
               GROUP_DATA_DIR=str(data), GROUP_DB=str(data / 'group.db'),
               GROUP_ENGINE_HOST=target.hostname, GROUP_ENGINE_PORT=str(target.port),
               GROUP_ENGINE_URL=url, GROUP_ATTACHMENTS_DIR=str(data / 'attachments'),
               GROUP_ADMIN_PASSWORD='isolated-restart-password', GROUP_SECRET=secrets.token_hex(32),
               GROUP_ENGINE_KEY=secrets.token_hex(32), RESTART_PROBE_WORK=str(workspace),
               RESTART_PROBE_COMMAND=json.dumps(command), GROUP_MCP_PYTHON=sys.executable)
    os.environ.update(env)
    # Installation readiness belongs to each data directory, including this new fixture.
    subprocess.run([sys.executable, str(ROOT / 'tools' / 'install.py'), 'repair'],
                   cwd=ROOT, env=env, check=True, stdout=subprocess.DEVNULL,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    conn = db.get_connection()
    db.init_schema(conn)
    conn.execute("INSERT OR IGNORE INTO members(profile,display_name,role) VALUES('default','probe','dev')")
    conn.execute("INSERT INTO projects(name,root,created_at) VALUES('alpha',?,?)", (str(workspace), db.now_iso()))
    enable_members(conn, ('alpha',), ('default', 'user'))
    topics = []
    for label in ('A', 'B'):
        topics.append(conn.execute("INSERT INTO threads(project,title,creator,members,created_at,last_active_at,turn_profile,turn_wake_at) "
            "VALUES('alpha',?,'default','[\"user\",\"default\"]',?,?,'default',?)",
            (label, db.now_iso(), db.now_iso(), db.now_iso())).lastrowid)
        conn.execute("INSERT INTO wake_jobs(profile,project,kind,trigger,thread_id,status,enqueued_at,chat_context_until) "
            "VALUES('default','alpha','chat','probe',?,'done',?,?)",
            (topics[-1], db.now_iso(), (datetime.now() + timedelta(minutes=15)).isoformat()))
    conn.commit()
    headers = {'Authorization': 'Bearer ' + auth.login('isolated-restart-password')['token']}
    client = httpx.Client(base_url=url, headers=headers, timeout=5, trust_env=False)
    result = {'url': url, 'data_dir': str(data), 'database': str(data / 'group.db'),
              'checks': {}, 'boots': [], 'executions': [], 'resources_released': False}
    endpoint = None
    initial = None

    def health(previous=None):
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            if previous:
                from engine.restart import read_receipt
                receipt = read_receipt()
                if receipt.get('state') == 'failed':
                    raise RuntimeError(receipt.get('error', 'Restart failed'))
            try:
                value = client.get('/health').json()
                if value.get('instance_id') and value['instance_id'] != previous:
                    assert Path(value['db_path']) == data / 'group.db'
                    assert Path(value['program_root']) == ROOT
                    result['boots'].append(value)
                    return value
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(.2)
        raise TimeoutError('Isolated engine readiness deadline exceeded')

    def rows():
        return [dict(r) for r in conn.execute('SELECT * FROM hermes_executions ORDER BY thread_id')]

    def pids():
        return [conn.execute('SELECT pid FROM wake_runs WHERE session=? AND started_at=?',
                             (row['session'], row['started_at'])).fetchone()[0] for row in before]

    def check_running(label, previous):
        current = rows()
        assert len(current) == 2, current
        for before, after in zip(previous, current):
            for field in ('execution_id', 'admission_id', 'submission_id', 'session_id', 'signing_generation',
                          'cancel_deadline', 'reconcile_deadline', 'execution_deadline'):
                assert before[field] == after[field], field
            assert after['cancel_reason'] is None and after['state'] != 'terminal', after
            assert client.get('/commands', headers=tokens[after['thread_id']]).status_code == 200
        result['checks'][label] = True
        result.setdefault('mcp_pid_snapshots', []).append(pids())
        assert pids() == result['initial_mcp_pids']

    try:
        endpoint = ensure(command, home, env=env, timeout=120)
        env['RESTART_PROBE_ENDPOINT'] = json.dumps(endpoint)
        result['endpoint'] = endpoint
        with (evidence / 'boot-initial.log').open('wb') as log:
            initial = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', __file__, '--serve', '--backend', url],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        boot = health()
        print('Initial engine ready', flush=True)
        tokens = {}
        for label, thread in zip(('A', 'B'), topics):
            signal = (f'Isolated restart test {label}. First call group_read_inbox. '
                      'Use tool_search if necessary to find restart_wait, then call restart_wait exactly once. '
                      'Wait for its successful result. '
                      f'Then call group_send exactly once with text "[收口] @user RESTART_VERIFIED_{label}" and need_reply=false. '
                      'Finish after sending. Use only these three tools and tool_search as needed.')
            response = client.post('/probe/spawn', json={'thread': thread, 'signal': signal}, timeout=120)
            response.raise_for_status()
            assert response.json()['ok'], response.json()
            tokens[thread] = {'X-Group-Profile': 'default', 'X-Group-Key': response.json()['credential']}
        deadline = time.monotonic() + 150
        while not all((workspace / f'ready_{label}').exists() for label in ('A', 'B')):
            if time.monotonic() > deadline:
                raise TimeoutError('Model did not enter both controlled MCP waits')
            time.sleep(1)
        before = rows()
        result['before'] = before
        initial_pids = pids()
        result['initial_mcp_pids'] = initial_pids
        print('Both real model executions waiting; restarting UI path', flush=True)
        start = time.monotonic()
        response = client.post('/admin/restart')
        response.raise_for_status()
        boot = health(boot['instance_id'])
        result['ui_restart_seconds'] = time.monotonic() - start
        check_running('ui_restart_original_admissions_credentials', before)
        print('UI restart passed; restarting script path', flush=True)
        # Exercise recovery after the original submission-reconciliation window.
        while time.time() < max(row['started_at'] for row in before) + 155:
            time.sleep(1)
        start = time.monotonic()
        with (evidence / 'script-restart.log').open('wb') as log:
            completed = subprocess.run([sys.executable, str(ROOT / 'tools' / 'service.py'), 'restart',
                '--data-dir', str(data)], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                timeout=75, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        assert completed.returncode == 0, 'script restart failed; inspect log'
        boot = health(boot['instance_id'])
        result['script_restart_seconds'] = time.monotonic() - start
        check_running('script_restart_original_admissions_credentials', before)
        with (evidence / 'shortcut-start.log').open('wb') as log:
            completed = subprocess.run([sys.executable, str(ROOT / 'tools' / 'service.py'), 'start', '--no-browser',
                '--data-dir', str(data)], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        assert completed.returncode == 0
        assert client.get('/health').json()['instance_id'] == boot['instance_id']
        result['checks']['shortcut_reuses_healthy_instance'] = True
        (workspace / 'continue').touch()
        print('Both restarts and shortcut passed; waiting for model replies', flush=True)
        deadline = time.monotonic() + 180
        while any(r['state'] != 'terminal' or r['cleanup_pending'] for r in rows()):
            if time.monotonic() > deadline:
                raise TimeoutError('Recovered executions did not finish cleanup')
            time.sleep(1)
        final = rows()
        result['executions'] = final
        assert all(r['outcome'] == 'completed' and r['cancel_reason'] is None for r in final)
        for label, thread in zip(('A', 'B'), topics):
            assert conn.execute('SELECT count(*) FROM messages WHERE thread_id=? AND sender=? AND content LIKE ?',
                (thread, 'default', f'%RESTART_VERIFIED_{label}%')).fetchone()[0] == 1
            assert client.get('/commands', headers=tokens[thread]).status_code == 401
        result['checks']['two_original_executions_completed_no_duplicate_messages'] = True
        final_pids = pids()
        result['final_mcp_pids'] = final_pids
        assert all(initial_pids) and initial_pids == final_pids
        result['checks']['same_mcp_processes_survived_both_restarts'] = True
        result['checks']['terminal_credentials_revoked'] = True
        assert conn.execute('SELECT count(*) FROM hermes_executions').fetchone()[0] == 2
        with GatewayClient(endpoint) as viewer:
            for label, row in zip(('A', 'B'), final):
                history = viewer.rpc('session.export', session_id=row['session_id'], format='json')['content']
                for secret in [env['GROUP_SECRET'], env['GROUP_ENGINE_KEY'], env['GROUP_ADMIN_PASSWORD'],
                               *(token['X-Group-Key'] for token in tokens.values())]:
                    history = history.replace(secret, '[REDACTED]')
                (evidence / f'history-{label}.json').write_text(history, encoding='utf-8')
        result['checks']['no_duplicate_dispatch'] = True
    finally:
        try:
            from engine.maintenance.stop_engine import stop
            stop(url, timeout=25)
        finally:
            if initial is not None:
                initial.wait(timeout=10)
            if endpoint:
                ended = subprocess.run(command + ['gateway', 'stop', '--yes'], env=env,
                    capture_output=True, text=True, encoding='utf-8', timeout=60,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                result['gateway_stop'] = ended.returncode
            with socket.socket() as check:
                result['resources_released'] = check.connect_ex((target.hostname, target.port)) != 0
            client.close()
            conn.close()
            (evidence / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    assert result['resources_released'] and result['gateway_stop'] == 0
    print(json.dumps(result['checks']), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend')
    parser.add_argument('--evidence')
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--mcp', action='store_true')
    args = parser.parse_args()
    if args.mcp:
        mcp_wait()
    elif args.serve and args.backend:
        serve(args.backend)
    elif args.evidence and args.backend:
        verify(args)
    else:
        parser.error('--evidence is required')
