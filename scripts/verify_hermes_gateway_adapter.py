"""Exercise the public CLI/control/WS boundary in a fresh isolated Hermes home."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.hermes_gateway_client import GatewayClient, ensure
from engine import hermes_gateway_sessions as sessions


def verify_engine(endpoint, home, command, evidence):
    """Run the actual engine adapter and task_queue against isolated HTTP/SQLite."""
    from engine import db, wake, reasoning, session_adapter, hermes_gateway as gateway
    from scripts.unified_test_support import isolated_database, isolated_api
    with isolated_database(), patch.dict(os.environ, {'GROUP_ENGINE_KEY': uuid.uuid4().hex}):
        conn = db.get_connection()
        try:
            bot_id = db._ensure_hermes_bot(conn, 'default')
            bot = conn.execute('SELECT * FROM bots WHERE id=?', (bot_id,)).fetchone()
            workspace = Path(conn.execute("SELECT root FROM projects WHERE name='alpha'").fetchone()[0])
            workspace.mkdir()
            conn.execute("INSERT INTO inbox(profile,message_id,status) VALUES('default',1,'pending')")
            conn.commit()
            with isolated_api() as url, patch.object(wake, '_engine_url', return_value=url), \
                    patch.object(gateway, 'connection', side_effect=lambda *a, **k: (GatewayClient(endpoint), home, command)):
                result = gateway.spawn('default', 'work',
                    'Call group_read_inbox. Send exactly one reply to @user containing GATEWAY_ENGINE_VERIFIED '
                    'using group_send with need_reply=false. Then call group_mark_seen for message 1. '
                    'This isolated test ends after those calls; do not create tasks or inspect files.',
                    None, 'alpha', bot=bot, workdir=str(workspace), model_args=[])
                assert result['ok'], result
                logical = wake.session_name('default', 'work', None, 'alpha')
                deadline = time.monotonic() + 240
                while gateway.active(logical) and time.monotonic() < deadline:
                    time.sleep(1)
                row = conn.execute('SELECT state,outcome,cleanup_pending,detail FROM hermes_executions '
                                   'WHERE session=?', (logical,)).fetchone()
                evidence['engine_execution'] = dict(row)
                assert row['state'] == 'terminal' and row['outcome'] == 'completed' and not row['cleanup_pending'], dict(row)
                reply = conn.execute("SELECT id FROM messages WHERE sender='default' AND content LIKE '%GATEWAY_ENGINE_VERIFIED%'").fetchone()
                assert reply is not None, 'Expected isolated engine reply'
                assert conn.execute('SELECT 1 FROM message_executions WHERE message_id=?', (reply['id'],)).fetchone()
                assert conn.execute("SELECT status FROM inbox WHERE profile='default' AND message_id=1").fetchone()[0] == 'seen'
                evidence['checks'].append('engine_native_mcp_reply_seen_terminal_cleanup')
                print('Engine native MCP reply/seen/terminal cleanup: passed', flush=True)
                first_run = conn.execute('SELECT * FROM hermes_executions WHERE session=?', (logical,)).fetchone()
                with patch.object(gateway, 'enabled', return_value=True):
                    trace = reasoning.read_reasoning('default', logical, wake_started_at=first_run['started_at'],
                        wake_ended_at=first_run['ended_at'], hermes_session_id=first_run['session_id'])
                assert trace['entries'], 'Official history export must retain engine trace'
                evidence['checks'].append('official_history_reasoning_projection')
                result = gateway.spawn('default', 'work',
                    'For an isolated cancellation test, write a detailed 5000 word history of mathematics. '
                    'Do not call tools or send messages.', None, 'alpha', bot=bot,
                    workdir=str(workspace), model_args=[])
                assert result['ok'], result
                assert gateway.cancel(logical, timeout=60), 'Cancellation did not reach terminal cleanup'
                cancelled = conn.execute('SELECT state,outcome,cleanup_pending,cancel_reason FROM hermes_executions '
                                        'WHERE session=? ORDER BY started_at DESC LIMIT 1', (logical,)).fetchone()
                evidence['engine_cancel'] = dict(cancelled)
                assert cancelled['state'] == 'terminal' and not cancelled['cleanup_pending'], dict(cancelled)
                evidence['checks'].append('engine_cancel_terminal_and_native_rewind')
                print('Engine cancellation and native rewind: passed', flush=True)
                with GatewayClient(endpoint) as viewer:
                    history = viewer.rpc('session.export', session_id=first_run['session_id'], format='json')['content']
                    assert 'GATEWAY_ENGINE_VERIFIED' in history, 'Cancellation removed earlier completed history'
                with patch.object(gateway, 'enabled', return_value=True):
                    switched = session_adapter.switch_session(conn, 'alpha', 'default', 'work', None, logical)
                assert switched['ok'] and switched['session_id'] != first_run['session_id'], switched
                evidence['checks'].append('official_new_session_preserves_previous_history')
        finally:
            conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    directory = args.evidence.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    import gw_probe as probe
    record, _ = probe.install_record()
    if not record:
        raise RuntimeError('Isolated Hermes dependency environment unavailable')
    source = ROOT / '.probe-worktrees' / 'hermes-v0225'
    home = directory / 'hermes-home'
    workspace = directory / 'workspace'
    home.mkdir()
    workspace.mkdir()
    # Reuse only the earlier isolated probe's model configuration and credentials.
    # Fresh state, runtime and managed configuration belong to this evidence folder.
    import yaml
    config = yaml.safe_load((probe.HERMES_HOME / 'config.yaml').read_text(encoding='utf-8')) or {}
    config['mcp_servers'] = {}
    config['approvals'] = {**config.get('approvals', {}), 'mode': 'smart'}
    (home / 'config.yaml').write_text(json.dumps(config), encoding='utf-8')
    if (probe.HERMES_HOME / '.env').is_file():
        shutil.copy2(probe.HERMES_HOME / '.env', home / '.env')
    env = probe.probe_env()
    env.update(HERMES_HOME=str(home), HERMES_RUNTIME_DIR=str(directory / 'runtime'),
               PYTHONPATH=str(source), PYTHONIOENCODING='utf-8')
    command = [str(record['python']), '-X', 'utf8', '-B', '-m', 'hermes_cli.main']
    evidence = {'source': str(source), 'home': str(home), 'workspace': str(workspace), 'checks': []}
    endpoint = None
    client = None
    admission = None
    try:
        endpoint = ensure(command, home, env=env, timeout=60)
        evidence['endpoint'] = endpoint
        print('Official CLI gateway ensure: ready', flush=True)
        with GatewayClient(endpoint) as client:
            description = client.rpc('runtime.describe')
            evidence['description'] = description
            assert 'durable-admission-v1' in description['capabilities']
            evidence['checks'].append('public_cli_control_ws')
            created = client.rpc('session.create', request_id=uuid.uuid4().hex, source='acp',
                                 cwd=str(workspace), editor={'mcp_servers': []})
            sid = created['session_id']
            evidence['session_id'] = sid
            mode = client.rpc('config.get', session_id=sid, key='approvals.mode')
            evidence['approval_mode'] = mode
            assert mode['value'] == 'smart', mode
            evidence['checks'].append('native_smart_mode')
            result = client.rpc('session.mutate', session_id=sid, request_id=uuid.uuid4().hex,
                expected_revision=created['revision'], expected_generation=created['execution_generation'],
                operation='edit_policy', payload={'policy': 'workspace_session'})
            evidence['edit_policy'] = result
            evidence['checks'].append('native_workspace_edit_policy')
            resumed = client.rpc('session.resume', session_id=sid)
            assert resumed['session_id'] == sid
            evidence['checks'].append('resume_same_session')
            print('Public WS session + native approval policies: passed', flush=True)
            for turn in (1, 2):
                tag = f'context-{turn}-{uuid.uuid4().hex[:8]}'
                server = {'command': str(record['python']),
                          'args': ['-B', str(ROOT / 'scripts/probe_mcp_server.py')],
                          'env': {'PROBE_MCP_TAG': tag}}
                try:
                    resumed = sessions.configure(client, resumed, workdir=str(workspace), model_args=[], server=server)
                except Exception:
                    info = client.rpc('session.info', session_id=sid)
                    evidence['failed_inventory'] = {key: info.get(key) for key in ('tools', 'mcp_servers', 'source')}
                    raise
                prompt = ('Call the probe_whoami MCP tool exactly once and return its PROBE_MCP_TAG value. '
                          'Use the actual tool result; do not guess or reuse earlier results.')
                admission = client.rpc('prompt.submit', session_id=sid, submission_id=uuid.uuid4().hex, text=prompt)
                deadline = time.monotonic() + 240
                while time.monotonic() < deadline:
                    receipt = client.rpc('prompt.receipt', session_id=sid,
                        admission_id=admission['admission_id'], include_result=True)
                    if receipt['status'] == 'terminal':
                        break
                    time.sleep(2)
                else:
                    raise TimeoutError('Probe admission did not settle')
                evidence[f'turn_{turn}'] = receipt
                assert tag in json.dumps(receipt), receipt
                evidence['checks'].append(f'fresh_mcp_credentials_turn_{turn}')
                resumed = sessions.attach(client, sid)
                print(f'Native MCP turn {turn}: fresh context verified', flush=True)
            with GatewayClient(endpoint) as reconnected:
                recovered = reconnected.rpc('prompt.receipt', session_id=sid, admission_id=admission['admission_id'])
                assert recovered['status'] == 'terminal'
                assert sessions.attach(reconnected, sid)['session_id'] == sid
                evidence['checks'].append('reconnect_new_ticket_terminal_receipt')
            sessions.clear_mcp(client, sid)
            legacy_id = 'legacy-test-' + uuid.uuid4().hex
            marker = 'LEGACY_HISTORY_' + uuid.uuid4().hex
            client.rpc('session.mutate', session_id=legacy_id, request_id=uuid.uuid4().hex,
                expected_revision=0, operation='import', payload={'sessions': [{
                    'id': legacy_id, 'source': 'cli', 'cwd': str(workspace),
                    'messages': [{'role': 'user', 'content': marker, 'timestamp': time.time()}]}]})
            legacy = sessions.attach(client, legacy_id)
            migrated = sessions.migrate_legacy(client, legacy, workdir=str(workspace))
            assert migrated['info']['source'] == 'acp'
            assert marker in json.dumps(migrated['messages'])
            assert sessions.attach(client, legacy['session_id'])['session_id'] == legacy['session_id']
            evidence['checks'].append('legacy_session_retained_and_migrated')
            print('Official session export/import: passed', flush=True)
        verify_engine(endpoint, home, command, evidence)
    finally:
        if endpoint:
            stopped = subprocess.run(command + ['gateway', 'stop', '--yes'], env=env,
                capture_output=True, text=True, encoding='utf-8', timeout=60,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            evidence['stop_exit_code'] = stopped.returncode
        (directory / 'result.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
