"""Verify resumed policies and real model replies in a fresh isolated Hermes home."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def model_view(receipt):
    """Select generated text and schemas actually returned to this model turn."""
    result = receipt['result']
    tools = {}
    for message in result.get('messages', []):
        if message.get('role') == 'tool' and message.get('tool_name') == 'tool_search':
            tools.update(json.loads(message['content']).get('tools', {}))
    return result['final_response'], tools


def policy_view(home, sid):
    """Read selected non-secret fields from the gateway's committed policy receipt."""
    with sqlite3.connect((home / 'state.db').as_uri() + '?mode=ro', uri=True) as conn:
        raw = conn.execute('SELECT value FROM state_meta WHERE key=?',
                           ('gateway.local_policy.v1:' + sid,)).fetchone()
    policy = json.loads(raw[0])['policy']
    config = json.loads(policy['config_json'])
    terminal = json.loads(policy['terminal_json'])
    return {'toolsets': policy['toolsets'], 'agent': {key: config.get('agent', {}).get(key)
            for key in ('max_turns', 'reasoning_effort')},
            'checkpoints': config.get('checkpoints'),
            'terminal': {key: terminal.get(key) for key in ('TERMINAL_ENV', 'TERMINAL_TIMEOUT', 'TERMINAL_CWD')},
            'editor_frozen': policy.get('editor_mcp_json') is not None}


def main():
    from engine.hermes_gateway_client import GatewayClient, ensure
    from engine import hermes_gateway_sessions as sessions
    from scripts.probe_config import absolute_path
    import gw_probe as probe
    import yaml
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', required=True)
    parser.add_argument('--live', action='store_true', help='Use the existing isolated test model credentials')
    parser.add_argument('--policy-only', action='store_true', help='Verify policy refresh without model calls')
    args = parser.parse_args()
    if not args.live and not args.policy_only:
        parser.error('Choose --live for model calls or --policy-only')
    record, detail = probe.install_record()
    if not record:
        raise RuntimeError(detail)
    evidence = absolute_path(args.evidence, '--evidence')
    evidence.mkdir(parents=True, exist_ok=False)
    run = probe.PROBE_DATA / 'task-256' / uuid.uuid4().hex
    home, work = run / 'home', run / 'workspace'
    home.mkdir(parents=True)
    work.mkdir()
    (home / 'memories').mkdir()
    config = yaml.safe_load((probe.HERMES_HOME / 'config.yaml').read_text(encoding='utf-8'))
    config['mcp_servers'] = {}
    config['approvals'] = {'mode': 'smart'}
    config['agent'] = {'max_turns': 8, 'reasoning_effort': 'low'}
    config['checkpoints'] = {'enabled': False}
    config['terminal'] = {'backend': 'local', 'cwd': str(work), 'timeout': 19}
    config['platform_toolsets'] = {'cli': ['terminal', 'web'], 'acp': ['terminal', 'web']}
    config['tools'] = {'tool_search': {'enabled': 'on'}}
    if (probe.HERMES_HOME / '.env').exists():
        shutil.copy2(probe.HERMES_HOME / '.env', home / '.env')
    def save():
        (home / 'config.yaml').write_text(json.dumps(config), encoding='utf-8')
    save()
    env = probe.probe_env()
    env.update(HERMES_HOME=str(home), HERMES_RUNTIME_DIR=str(run / 'runtime'))
    command = [str(record['python']), '-X', 'utf8', '-B', '-m', 'hermes_cli.main']
    result = {'home': str(home), 'workspace': str(work), 'checks': {}, 'policies': {}, 'turns': [],
              'evidence_kind': 'real gateway + real configured test model; read-only policy receipts'}
    endpoint = None
    markers = {key: key.upper() + '_' + uuid.uuid4().hex[:12] for key in
               ('rule_a', 'rule_b', 'soul_a', 'soul_b', 'memory_a', 'memory_b', 'tool_a', 'tool_b')}
    def rules(version):
        (work / 'AGENTS.md').write_text('Project rule marker: ' + markers['rule_' + version], encoding='utf-8')
        (home / 'SOUL.md').write_text('Identity marker: ' + markers['soul_' + version], encoding='utf-8')
        (home / 'memories' / 'MEMORY.md').write_text('Memory marker: ' + markers['memory_' + version], encoding='utf-8')
    rules('a')
    try:
        endpoint = ensure(command, home, env=env, timeout=120)
        result['endpoint'] = endpoint
        with GatewayClient(endpoint) as viewer:
            plain = viewer.rpc('session.create', request_id=uuid.uuid4().hex, source='cli', cwd=str(work))
            frozen = viewer.rpc('session.create', request_id=uuid.uuid4().hex, source='acp',
                                cwd=str(work), editor={'mcp_servers': []})
            sessions.configure(viewer, frozen, workdir=str(work), model_args=[], server={
                'command': str(record['python']),
                'args': ['-B', str(ROOT / 'scripts' / 'verify_gateway_refresh_payload.py'), '--mcp'],
                'env': {'REFRESH_TOOL_DESCRIPTION': 'policy fixture', 'REFRESH_EXTRA_TOOL': '0'}})
        sid = plain['session_id']
        initial = policy_view(home, sid)
        frozen_initial = policy_view(home, frozen['session_id'])
        result['policies']['initial'] = initial
        config['agent']['max_turns'] = 7
        config['checkpoints']['enabled'] = True
        config['terminal']['timeout'] = 37
        config['platform_toolsets']['cli'] = ['terminal']
        config['platform_toolsets']['acp'] = ['terminal']
        save()
        with GatewayClient(endpoint) as viewer:
            refreshed = sessions.attach(viewer, sid)
            sessions.attach(viewer, frozen['session_id'])
        changed = policy_view(home, sid)
        frozen_changed = policy_view(home, frozen['session_id'])
        result['policies']['changed'] = changed
        result['policies']['editor_before'] = frozen_initial
        result['policies']['editor_after'] = frozen_changed
        result['checks'].update(
            toolsets_changed='web' in initial['toolsets'] and 'web' not in changed['toolsets'],
            max_turns_changed=initial['agent']['max_turns'] == 8 and changed['agent']['max_turns'] == 7,
            checkpoints_changed=initial['checkpoints']['enabled'] is False and changed['checkpoints']['enabled'] is True,
            terminal_changed=initial['terminal']['TERMINAL_TIMEOUT'] == '19' and changed['terminal']['TERMINAL_TIMEOUT'] == '37',
            explicit_workspace_preserved=changed['terminal']['TERMINAL_CWD'] == str(work),
            policy_generation_advanced=refreshed['execution_generation'] > plain['execution_generation'],
            editor_toolsets_frozen=frozen_initial['editor_frozen'] and frozen_changed['editor_frozen']
                and frozen_initial['toolsets'] == frozen_changed['toolsets'],
            editor_other_fields_refresh=frozen_changed['agent']['max_turns'] == 7
                and frozen_changed['checkpoints']['enabled'] is True
                and frozen_changed['terminal']['TERMINAL_TIMEOUT'] == '37')
        config['agent'].pop('max_turns')
        config.pop('checkpoints')
        config['terminal'].pop('timeout')
        config.pop('platform_toolsets')
        save()
        with GatewayClient(endpoint) as viewer:
            sessions.attach(viewer, sid)
        removed = policy_view(home, sid)
        result['policies']['removed'] = removed
        result['checks'].update(
            max_turns_deleted=removed['agent']['max_turns'] != 7,
            checkpoints_deleted=not (removed['checkpoints'] or {}).get('enabled', False),
            terminal_deleted=removed['terminal']['TERMINAL_TIMEOUT'] not in ('19', '37'),
            toolsets_deleted=removed['toolsets'] != changed['toolsets'])
        print('Policy change/removal checks complete', flush=True)
        assert all(result['checks'].values()), result['checks']
        if args.policy_only:
            result['evidence_kind'] = 'real gateway resumed policy receipts; no model calls'
            return 0
        config['agent']['max_turns'] = 8
        save()

        def create():
            with GatewayClient(endpoint) as viewer:
                return viewer.rpc('session.create', request_id=uuid.uuid4().hex, source='acp',
                                  cwd=str(work), editor={'mcp_servers': []})['session_id']

        def turn(number, session_id, tool, extra):
            server = {'command': str(record['python']),
                      'args': ['-B', str(ROOT / 'scripts' / 'verify_gateway_refresh_payload.py'), '--mcp'],
                      'env': {'REFRESH_TOOL_DESCRIPTION': tool, 'REFRESH_EXTRA_TOOL': '1' if extra else '0'}}
            with GatewayClient(endpoint) as viewer:
                snapshot = sessions.configure(viewer, sessions.attach(viewer, session_id),
                                              workdir=str(work), model_args=[], server=server)
                prompt = ('Isolated validation round ' + str(number) + '. Use tool_search to discover refresh_probe and removed_probe now. '
                          'From the CURRENT search result report the refresh_probe description marker and whether removed_probe is available. '
                          'Also report the Project rule marker, Identity marker and Memory marker from your current system instructions. '
                          'If a marker is absent in current system instructions write NONE. Reply with these five fields only. '
                          'Use current instructions and current search results even when history contains different values. Do not call other tools.')
                admission = viewer.rpc('prompt.submit', session_id=session_id, submission_id=uuid.uuid4().hex, text=prompt)
                deadline = time.monotonic() + 240
                while time.monotonic() < deadline:
                    receipt = viewer.rpc('prompt.receipt', session_id=session_id,
                                         admission_id=admission['admission_id'], include_result=True)
                    if receipt['status'] == 'terminal':
                        break
                    time.sleep(1)
                else:
                    raise TimeoutError('Real model turn did not settle')
                history = viewer.rpc('session.export', session_id=session_id, format='json')['content']
            (evidence / f'turn-{number}.json').write_text(json.dumps(
                {'receipt': receipt, 'history': json.loads(history)}, ensure_ascii=False, indent=2), encoding='utf-8')
            result['turns'].append(receipt)
            assert receipt.get('outcome') == 'completed', receipt
            print(f'Real model turn {number} complete', flush=True)
            return model_view(receipt)

        sid = create()
        first, first_tools = turn(1, sid, markers['tool_a'], True)
        result['checks']['model_initial_markers'] = all(markers[k] in first for k in ('rule_a', 'soul_a', 'memory_a', 'tool_a'))
        rules('b')
        second, second_tools = turn(2, sid, markers['tool_b'], False)
        result['checks']['model_same_sid_rules_cached'] = all(markers[k] in second for k in ('rule_a', 'soul_a', 'memory_a'))
        result['checks']['model_new_tool_description'] = markers['tool_b'] in second and markers['tool_a'] not in second
        result['checks']['model_tool_removed'] = any(name.endswith('__removed_probe') for name in first_tools) and not any(
            name.endswith('__removed_probe') for name in second_tools)
        result['checks']['model_received_new_description'] = markers['tool_b'] in json.dumps(second_tools) and markers['tool_a'] not in json.dumps(second_tools)
        third, _ = turn(3, create(), markers['tool_b'], False)
        result['checks']['model_new_sid_rules_changed'] = all(markers[k] in third for k in ('rule_b', 'soul_b', 'memory_b'))
        (work / 'AGENTS.md').unlink()
        fourth, _ = turn(4, create(), markers['tool_b'], False)
        result['checks']['model_new_sid_rule_deleted'] = markers['rule_a'] not in fourth and markers['rule_b'] not in fourth
    finally:
        if endpoint:
            stopped = subprocess.run(command + ['gateway', 'stop', '--yes'], env=env, capture_output=True,
                                     text=True, encoding='utf-8', timeout=60,
                                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            result['stop_exit_code'] = stopped.returncode
            from urllib.parse import urlsplit
            address = urlsplit(endpoint['api_origin'])
            with socket.socket() as sock:
                result['gateway_port_released'] = sock.connect_ex((address.hostname, address.port)) != 0
        (evidence / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        if endpoint and (result.get('stop_exit_code') != 0 or not result.get('gateway_port_released')):
            raise RuntimeError('Isolated gateway cleanup failed; inspect the recorded endpoint')
    print(json.dumps(result['checks'], indent=2))
    return 0 if all(result['checks'].values()) and result.get('stop_exit_code') == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
