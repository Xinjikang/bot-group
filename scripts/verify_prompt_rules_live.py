"""Three real Hermes wakes with isolated engine data, project and runtime home."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import verify_project_templates_live as live


def tool_calls(raw):
    """Flatten native and wrapped calls while retaining exact arguments."""
    result = []
    try:
        parsed = json.loads(raw or '[]')
    except ValueError:
        return [{'name': 'invalid_tool_call', 'arguments': raw}]
    for call in parsed:
        if not isinstance(call, dict):
            result.append({'name': 'invalid_tool_call', 'arguments': call})
            continue
        function = call.get('function', call)
        arguments = function.get('arguments', {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                pass
        if function.get('name') == 'tool_call' and isinstance(arguments, dict):
            nested = arguments.get('calls', [])
            result.extend(tool_calls(nested if isinstance(nested, str) else json.dumps(nested)))
        else:
            result.append({'name': function.get('name'), 'arguments': arguments})
    return result


def verify_turn(client, conn, setup, home, topic, number):
    from engine import wake, stop
    marker = f'RULE_CHECK_{number}'
    request = (
        f'第 {number} 轮资料核验：S1 记载 A 方案 5 个席位合计 10 单位；'
        'S2 记载 B 方案 4 个席位合计 12 单位。请计算每席成本并给出推荐。'
        '同时说明当前项目的规则令牌、关闭任务的角色和启用成员数量。'
        '只需在当前话题回复，用 JSON 包含 rule_token、closing_role、member_count、'
        'costs、recommendation 字段。costs 使用 A、B 作为键，每席成本为数值；'
        'recommendation 使用方案字母。'
        f'回复以 @user {marker} 开头，处理完本条消息后结束。'
    )
    response = client.post('/msg', json={'project': setup['project'], 'sender': 'user',
        'thread_id': topic, 'text': '@' + setup['actor'] + ' ' + request, 'need_reply': True})
    response.raise_for_status()
    incoming = response.json()['message_id']
    before = conn.execute('SELECT COUNT(*) FROM wake_runs').fetchone()[0]
    assert before == number - 1, 'Unexpected additional wake'
    job_id = live.dispatch_topic(conn, setup['actor'], setup['project'], topic)
    session = wake.session_name(setup['actor'], 'chat', topic, setup['project'])
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        row = conn.execute('SELECT * FROM wake_runs WHERE session=? ORDER BY started_at DESC LIMIT 1',
                           (session,)).fetchone()
        if row and row['ended_at'] is not None:
            break
        time.sleep(0.2)
    else:
        stop.stop_profile(setup['actor'], setup['project'], topic)
        raise RuntimeError('Isolated wake exceeded 240 seconds')
    record = dict(row)
    assert record['exit_code'] == 0, 'Native wake failed'
    with closing(sqlite3.connect('file:' + (home / 'state.db').as_posix() + '?mode=ro', uri=True)) as native:
        native.row_factory = sqlite3.Row
        messages = [dict(r) for r in native.execute(
            'SELECT id,session_id,role,content,tool_calls,tool_name,timestamp FROM messages '
            'WHERE timestamp>=? AND timestamp<=? ORDER BY timestamp,id',
            (record['started_at'], record['ended_at']))]
    assert messages, 'Missing native transcript'
    calls = [call for message in messages for call in tool_calls(message['tool_calls'])]
    file_calls = [call for call in calls if re.search(r'agents(?:\.override)?\.md',
                  json.dumps(call['arguments']), flags=re.I)]
    replies = conn.execute('SELECT id,content FROM messages WHERE project=? AND sender=? '
                           'AND thread_id=? AND content LIKE ?',
                           (setup['project'], setup['actor'], topic, '%' + marker + '%')).fetchall()
    reply = None
    if len(replies) == 1:
        content = replies[0]['content']
        reply, _ = json.JSONDecoder().raw_decode(content[content.index('{'):])
    seen = conn.execute('SELECT status FROM inbox WHERE message_id=? AND profile=?',
                        (incoming, setup['actor'])).fetchone()
    valid = (reply and reply.get('rule_token') == setup['rule_token']
             and reply.get('closing_role') == '调研负责人'
             and reply.get('member_count') == len(setup['roles'])
             and reply.get('costs') == {'A': 2, 'B': 3} and reply.get('recommendation') == 'A')
    return {'turn': number, 'wake': record, 'job_id': job_id, 'reply': reply,
            'pending_seen': bool(seen and seen['status'] == 'seen'), 'calls': calls,
            'rules_file_calls': file_calls, 'messages': messages,
            'passed': bool(valid and seen and seen['status'] == 'seen' and not file_calls)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--hermes-home', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    if not args.live:
        parser.error('Real model verification requires --live and authorized authentication reuse')
    if not args.evidence.is_absolute() or args.evidence.exists():
        parser.error('--evidence must be a new absolute file path')
    source = yaml.safe_load((args.hermes_home / 'config.yaml').read_text(encoding='utf-8'))
    model = source.get('model', {})
    if model.get('provider') != 'ollama-cloud':
        parser.error('This probe supports the selected Ollama Cloud Hermes profile')
    before = live.host_snapshot(args.hermes_home)
    evidence = {'model': model.get('default'), 'provider': model.get('provider'),
                'budget': {'wakes': 3, 'seconds_per_wake': 240}, 'runs': []}
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix='prompt-rules-live-') as directory:
            base = Path(directory)
            codex, home = live.temporary_homes(base, None, args.hermes_home, ['research'])
            essentials = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP',
                          'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'PROGRAMFILES',
                          'PROGRAMFILES(X86)', 'SYSTEMDRIVE', 'NUMBER_OF_PROCESSORS'}
            env = {key: value for key, value in os.environ.items() if key.upper() in essentials}
            env.update(GROUP_DATA_DIR=str(base), GROUP_DB=str(base / 'group.db'),
                       GROUP_SECRET='isolated-prompt-rules-probe', HERMES_HOME=str(home),
                       CODEX_HOME=str(codex), PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
            with patch.dict(os.environ, env, clear=True):
                from engine import db, stop, wake
                from scripts.unified_test_support import isolated_api
                with closing(db.get_connection()) as conn:
                    db.init_schema(conn)
                    try:
                        with isolated_api() as url, patch.dict(os.environ, {'GROUP_ENGINE_URL': url}):
                            with httpx.Client(base_url=url, timeout=30) as client:
                                setup = live.setup_project(client, conn, base, 'research', codex, home)
                                live.hermes_configuration(home, base / 'group.db', url, setup['actor'])
                                path = home / 'config.yaml'
                                config = yaml.safe_load(path.read_text(encoding='utf-8'))
                                config['model']['default'] = model['default']
                                config['toolsets'] = ['file', 'mcp-task_queue']
                                config['platform_toolsets']['cli'] = ['file', 'mcp-task_queue']
                                path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding='utf-8')
                                response = client.post('/thread', json={'project': setup['project'],
                                    'creator': 'user', 'title': 'Isolated rule loading verification',
                                    'members': [setup['actor']], 'opening': '资料核验'})
                                response.raise_for_status()
                                topic = response.json()['thread_id']
                                for number in range(1, 4):
                                    print(f'Running real wake {number}/3', flush=True)
                                    result = verify_turn(client, conn, setup, home, topic, number)
                                    evidence['runs'].append(result)
                                    print(f'Wake {number}: passed={result["passed"]}, '
                                          f'rules_file_calls={len(result["rules_file_calls"])}', flush=True)
                                    if not result['passed']:
                                        break
                    finally:
                        for active in wake.active_runs():
                            stop.stop_profile(active['profile'], active['project'], active['thread_id'])
                        evidence['diagnostics'] = live.native_diagnostics(conn, home)
    except Exception as exc:
        evidence['error'] = type(exc).__name__ + ': ' + str(exc)
    evidence['source_configuration_unchanged'] = before == live.host_snapshot(args.hermes_home)
    evidence['passed'] = (len(evidence['runs']) == 3 and all(r['passed'] for r in evidence['runs'])
                          and evidence['source_configuration_unchanged'])
    args.evidence.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('PASS' if evidence['passed'] else 'FAIL: see evidence', flush=True)
    return 0 if evidence['passed'] else 1


if __name__ == '__main__':
    sys.exit(main())
