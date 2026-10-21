"""Opt-in template verification: three turns, temporary engine and runtime homes.

Run only after approval of the selected providers, synthetic inputs and reuse of
the explicitly selected homes' authentication. See scripts/TEST_INDEX.md.
Credentials never enter evidence or output; evidence belongs in test reports.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def host_snapshot(home):
    # Compare configuration, not actively changing production session/log files.
    names = ('AGENTS.md', 'SOUL.md', 'config.toml', 'config.yaml', 'auth.json', '.env')
    return {name: digest(home / name) for name in names}


def temporary_homes(base, codex_source, hermes_source, scenarios=('software', 'research', 'writing')):
    codex = base / 'codex'; hermes = base / 'hermes'
    codex.mkdir(); hermes.mkdir()
    if any(name in scenarios for name in ('software', 'writing')):
        shutil.copy2(codex_source / 'auth.json', codex / 'auth.json')
        (codex / 'config.toml').write_text(
            'model = "gpt-6.1-sol"\nsandbox_mode = "read-only"\n'
            'approval_policy = "on-request"\napprovals_reviewer = "auto_review"\n', encoding='utf-8')
    if 'research' not in scenarios:
        return codex, hermes
    from dotenv import dotenv_values
    key = dotenv_values(hermes_source / '.env').get('OLLAMA_API_KEY')
    if not key:
        raise RuntimeError('Required Ollama Cloud authentication is unavailable')
    # Copy the single provider credential; leave all other original .env values out.
    (hermes / '.env').write_text('OLLAMA_API_KEY=' + json.dumps(key) + '\n', encoding='utf-8')
    return codex, hermes


def hermes_configuration(home, database, url, profile):
    config = {
        'model': {'default': 'deepseek-v4-pro:0813', 'provider': 'ollama-cloud',
                  'base_url': 'https://ollama.com/v1'},
        'fallback_providers': [], 'toolsets': ['mcp-task_queue'],
        'platform_toolsets': {'cli': ['mcp-task_queue']},
        'agent': {'max_turns': 12, 'api_max_retries': 0},
        'mcp_servers': {'task_queue': {'command': sys.executable,
            'args': [str(ROOT / 'mcp' / 'server.py')], 'enabled': True,
            'timeout': 30, 'connect_timeout': 30,
            'env': {'GROUP_DB': str(database), 'GROUP_ENGINE_URL': url,
                    'TASK_QUEUE_ROLE': profile, 'HERMES_PROFILE': profile,
                    'HERMES_CHAT_MODE': '1'}}},
    }
    (home / 'config.yaml').write_text(yaml.safe_dump(config, allow_unicode=True), encoding='utf-8')


def setup_project(client, conn, base, name, codex, hermes):
    from engine import project_templates
    manifest = project_templates.load(name)
    bots = []
    actor_role = 'analyst' if name == 'research' else 'backend' if name == 'software' else 'writer'
    for index, role in enumerate(manifest['roles']):
        runtime = 'hermes' if name == 'research' and role['id'] == 'analyst' else 'codex'
        home = hermes if runtime == 'hermes' else codex
        # Distinct physical configuration roots satisfy the global Bot identity key.
        if role['id'] != actor_role:
            home = base / ('stub-' + name + '-' + str(index)); home.mkdir()
        existing = conn.execute('SELECT id FROM bots WHERE host_type=? AND config_root=? AND profile=?',
                                (runtime, str(home), 'default')).fetchone()
        bid = existing['id'] if existing else conn.execute(
            'INSERT INTO bots(host_type,config_root,profile,display_name,runtime) VALUES(?,?,?,?,?)',
            (runtime, str(home), 'default', 'Isolated ' + role['id'], runtime)).lastrowid
        bots.append({'bot_id': bid, 'role': role['id'], 'permissions': []})
    conn.commit()
    work = base / ('project-' + name)
    display = 'TEMPLATE_LIVE_' + name.upper() + '_' + uuid.uuid4().hex[:8]
    response = client.post('/projects/initializations/preview', json={
        'root': str(work), 'display_name': display, 'description': 'Synthetic task181 verification only',
        'template': {'id': name, 'version': manifest['version']},
        'members': bots, 'owner_bot_id': bots[0]['bot_id']})
    response.raise_for_status(); plan = response.json()
    response = client.post('/projects/initializations/' + plan['initialization_id'] + '/submit',
                           json={'revision': plan['revision']})
    response.raise_for_status(); result = response.json()
    assert result['status'] == 'ready', result
    token = 'RULE_' + uuid.uuid4().hex
    with (work / 'AGENTS.md').open('a', encoding='utf-8') as stream:
        stream.write('\n## Isolated verification\n规则令牌：' + token + '\n')
    roles = {m['role']: result['applied_team']['member_profiles'][str(m['bot_id'])] for m in bots}
    actor = roles['analyst' if name == 'research' else 'backend' if name == 'software' else 'writer']
    return {'project': result['project'], 'display_name': display, 'root': work,
            'actor': actor, 'roles': roles, 'rule_token': token,
            'files': [{'path': f['path'], 'sha256': digest(work / f['path'])} for f in result['files']]}


def prompt(name, marker):
    scenarios = {
        'software': 'Software fixture: add(a,b)=a+b. Return tests:[5,0] for add(2,3) and add(-1,1).',
        'research': 'Synthetic sources: S1 says A costs 10 units for 5 seats; S2 says B costs 12 units for 4 seats. '
                    'Return costs:{"A":2,"B":3}, sources:["S1","S2"], recommendation:"A", and inference_boundary explaining the scope of this comparison.',
        'writing': 'Synthetic source S1: active project members inherit task.update from their assigned role. '
                   'Return article with two Chinese onboarding sentences and fact_check explicitly citing S1.',
    }
    return ('隔离模板阅读验证。使用当前项目 AGENTS.md 的流程和“规则令牌”，'
            '先调用 group_list_members 和 group_read_inbox。这里只进行资料核验与话题回复。'
            '请用 JSON 回答，template 必须为模板ID字符串 "' + name + '"，'
            'project_name 为当前项目显示名，rule_token 从AGENTS读取，'
            'closing_role（流程中关闭任务的角色名称）、member_count。'
            'scenario 为字符串 "' + name + '"，以下场景结果字段放在JSON顶层。'
            + scenarios[name] + ' '
            '最后调用 group_send(text="@user ' + marker + ' " + JSON字符串, need_reply=false)，'
            '实质回复后 group_mark_seen 标记自己的未读消息，然后结束本轮。'
            '只访问当前隔离项目规则和所给合成资料。')


def codex_usage(conn, record):
    events = [json.loads(r[0]) for r in conn.execute(
        'SELECT payload FROM codex_run_events WHERE session=? AND wake_started_at=? ORDER BY id',
        (record['session'], record['started_at']))]
    usage = [e.get('usage') for e in events if e.get('type') == 'turn.completed']
    calls = [e['item'].get('tool') for e in events if e.get('type') == 'item.completed'
             and e.get('item', {}).get('type') == 'mcp_tool_call']
    return {'usage': usage, 'mcp_tools': calls}


def hermes_usage(home):
    with closing(sqlite3.connect('file:' + (home / 'state.db').as_posix() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        sessions = [dict(r) for r in conn.execute('SELECT id,title,model,input_tokens,output_tokens FROM sessions')]
        tools = [r[0] for r in conn.execute('SELECT tool_name FROM messages WHERE tool_name IS NOT NULL')]
        # Preserve the isolated runtime's final reply and MCP error context even
        # when the model never sends the required result to the engine topic.
        messages = [dict(r) for r in conn.execute(
            "SELECT role,tool_name,substr(content,1,4096) AS content FROM messages "
            "WHERE role IN ('assistant','tool') ORDER BY id DESC LIMIT 20")]
        return {'sessions': sessions, 'mcp_tools': tools, 'runtime_messages': list(reversed(messages))}


def native_diagnostics(conn, hermes):
    """Save scoped attempted executions before their temporary databases vanish."""
    attempts = []
    for row in conn.execute('SELECT * FROM wake_runs ORDER BY started_at'):
        record = dict(row)
        attempt = {key: record[key] for key in (
            'runtime', 'session', 'runtime_session_id', 'started_at', 'ended_at', 'exit_code')}
        try:
            attempt.update(hermes_usage(hermes) if record['runtime'] == 'hermes' else codex_usage(conn, record))
        except Exception as exc:
            attempt['collection_error'] = type(exc).__name__ + ': ' + str(exc)
        attempt['topic_replies'] = [dict(r) for r in conn.execute(
            'SELECT id,thread_id,content FROM messages WHERE project=? AND sender=? ORDER BY id',
            (record['project'], record['profile']))]
        attempt['pending'] = [dict(r) for r in conn.execute(
            'SELECT i.message_id,i.status FROM inbox i JOIN messages m ON m.id=i.message_id '
            'WHERE m.project=? AND i.profile=? ORDER BY i.message_id',
            (record['project'], record['profile']))]
        attempts.append(attempt)
    return attempts


def dispatch_topic(conn, profile, project, topic):
    """Process only this fixture's queued turn through the real chat dispatcher."""
    from engine import queue
    jobs = conn.execute(
        "SELECT id FROM wake_jobs WHERE status='queued' AND profile=? AND project=? "
        "AND kind='chat' AND thread_id=?", (profile, project, topic)).fetchall()
    assert len(jobs) == 1, 'Expected one queued current topic turn'
    job_id = jobs[0]['id']
    # Keep the exact current job, discard follow-ups from earlier fixture topics.
    # Real queue processing also registers the window required by GET /inbox.
    conn.execute("UPDATE wake_jobs SET status='cancelled' WHERE status='queued' AND id!=?", (job_id,))
    conn.commit()
    assert queue.process_one(conn), 'Current topic turn was not processed'
    job = conn.execute('SELECT status,chat_context_until FROM wake_jobs WHERE id=?', (job_id,)).fetchone()
    assert job and job['status'] == 'done' and job['chat_context_until'], 'Chat context window absent'
    return job_id


def validate_reply(result, setup, name):
    """Validate identity and scenario values, preserving either JSON layout."""
    assert isinstance(result, dict), 'Model result must be a JSON object'
    assert result.get('template') == name, 'Template ID mismatch'
    assert result.get('project_name') == setup['display_name'], 'Project name mismatch'
    assert result.get('rule_token') == setup['rule_token'], 'Project rule token mismatch'
    expected_role = {'software': '项目负责人', 'research': '调研负责人', 'writing': '主编'}[name]
    assert result.get('closing_role') == expected_role, 'Closing role mismatch'
    assert result.get('member_count') == len(setup['roles']), 'Member count mismatch'
    # The task checks the actual analysis/article; the model can represent these
    # values at the root or under scenario. Keep the original response as evidence.
    details = result['scenario'] if isinstance(result.get('scenario'), dict) else result
    if name == 'software':
        assert details.get('tests') == [5, 0], 'Software test results mismatch'
        keys = ('tests',)
    elif name == 'research':
        assert details.get('costs') == {'A': 2, 'B': 3}, 'Research costs mismatch'
        assert set(details.get('sources', [])) == {'S1', 'S2'}, 'Research sources mismatch'
        assert details.get('recommendation') == 'A' and details.get('inference_boundary'), 'Research conclusion incomplete'
        keys = ('costs', 'sources', 'recommendation', 'inference_boundary')
    else:
        assert details.get('article') and 'S1' in details.get('fact_check', ''), 'Writing source check incomplete'
        keys = ('article', 'fact_check')
    return {key: details[key] for key in keys}


def execute(client, conn, setup, name, hermes):
    from engine import wake, stop
    marker = 'LIVE_RESULT_' + uuid.uuid4().hex
    response = client.post('/thread', json={'project': setup['project'], 'creator': 'user',
        'title': 'Isolated ' + name + ' verification', 'members': [setup['actor']], 'opening': '模板真机核验'})
    response.raise_for_status(); topic = response.json()['thread_id']
    response = client.post('/msg', json={'project': setup['project'], 'sender': 'user', 'thread_id': topic,
        'text': '@' + setup['actor'] + ' ' + prompt(name, marker), 'need_reply': True})
    response.raise_for_status(); incoming = response.json()['message_id']
    count_before = conn.execute('SELECT COUNT(*) FROM wake_runs').fetchone()[0]
    assert count_before < 3, 'Three native executions per attempt exhausted'
    job_id = dispatch_topic(conn, setup['actor'], setup['project'], topic)
    session = wake.session_name(setup['actor'], 'chat', topic, setup['project'])
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        row = conn.execute('SELECT * FROM wake_runs WHERE session=? ORDER BY started_at DESC LIMIT 1', (session,)).fetchone()
        if row and row['ended_at'] is not None:
            break
        time.sleep(0.2)
    else:
        stop.stop_profile(setup['actor'], setup['project'], topic)
        raise RuntimeError('Isolated ' + name + ' native execution timed out')
    record = dict(row)
    assert conn.execute('SELECT COUNT(*) FROM wake_runs').fetchone()[0] == count_before + 1, 'Unexpected native execution'
    if record['exit_code'] != 0:
        raise RuntimeError('Isolated ' + name + ' native execution failed; exit=' + str(record['exit_code']))
    sent = conn.execute('SELECT id,content FROM messages WHERE project=? AND sender=? AND thread_id=? AND content LIKE ?',
        (setup['project'], setup['actor'], topic, '%' + marker + '%')).fetchall()
    assert len(sent) == 1, 'Expected one model result in isolated topic'
    content = sent[0]['content']; result, _ = json.JSONDecoder().raw_decode(content[content.index('{'):])
    validated_scenario = validate_reply(result, setup, name)
    pending = conn.execute('SELECT status FROM inbox WHERE message_id=? AND profile=?', (incoming, setup['actor'])).fetchone()
    assert pending and pending['status'] == 'seen', 'Model did not mark its pending seen'
    usage = hermes_usage(hermes) if name == 'research' else codex_usage(conn, record)
    assert record['runtime_session_id'], 'Native session identity absent'
    native_session = record['runtime_session_id']
    if name == 'research':
        matches = [s['id'] for s in usage['sessions'] if s['title'] == record['runtime_session_id']]
        assert len(matches) == 1, 'Hermes native session identity ambiguous'
        native_session = matches[0]
    return {'template': name, 'runtime': record['runtime'], 'native_session': native_session,
        'engine_session_binding': record['runtime_session_id'], 'dispatch_job_id': job_id,
        'exit_code': record['exit_code'], 'duration_seconds': round(record['ended_at'] - record['started_at'], 2),
        'reply_message_id': sent[0]['id'], 'thread_id': topic, 'model_reply': result,
        'validated_scenario': validated_scenario, **usage,
        'files': setup['files'], 'pending_seen': True, 'project_rules_read': True}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--evidence', type=Path, help='Absolute output path in the main project reports/tests directory')
    parser.add_argument('--codex-home', type=Path, help='Explicit source Codex home for authentication reuse')
    parser.add_argument('--hermes-home', type=Path, help='Explicit source Hermes home for authentication reuse')
    parser.add_argument('--hermes-bin', help='Native Hermes executable; otherwise use runtime discovery')
    parser.add_argument('--scenarios', nargs='+', choices=('software', 'research', 'writing'),
                        default=['software', 'research', 'writing'])
    args = parser.parse_args()
    if len(set(args.scenarios)) != len(args.scenarios):
        parser.error('Each scenario may run only once per attempt')
    if not args.live:
        print('Use --live after owner approval; requested native model runs consume quota.')
        return
    if not args.evidence or not args.evidence.is_absolute():
        parser.error('--live requires an absolute --evidence path in the main project reports/tests directory')
    if any(name in args.scenarios for name in ('software', 'writing')) and not args.codex_home:
        parser.error('Codex scenarios require --codex-home for explicit authentication selection')
    if 'research' in args.scenarios and not args.hermes_home:
        parser.error('Research requires --hermes-home for explicit authentication selection')
    codex_source = args.codex_home
    hermes_source = args.hermes_home
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    before = {'codex': host_snapshot(codex_source) if codex_source else {},
              'hermes': host_snapshot(hermes_source) if hermes_source else {}}
    evidence = {'invocation': 'Explicit --live with selected scenarios and authentication source homes',
        'isolation': 'Temporary SQLite, random loopback API without workers, temporary homes/projects',
        'providers': {'codex': 'OpenAI authenticated Codex service', 'hermes': 'https://ollama.com/v1'},
        'budget': {'native_turns': 3, 'timeout_per_turn_seconds': 240},
        'requested_scenarios': args.scenarios, 'runs': []}
    status = 0
    try:
        with tempfile.TemporaryDirectory(prefix='template181-live-') as directory:
            base = Path(directory); database = base / 'group.db'
            codex, hermes = temporary_homes(base, codex_source, hermes_source, args.scenarios)
            # Limit inherited process environment to OS/runtime essentials and isolated endpoints.
            env = {k: v for k, v in os.environ.items() if k.upper() in {
                'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP', 'USERPROFILE',
                'LOCALAPPDATA', 'APPDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)', 'SYSTEMDRIVE', 'NUMBER_OF_PROCESSORS'}}
            env.update({'GROUP_DB': str(database), 'CODEX_HOME': str(codex), 'HERMES_HOME': str(hermes),
                'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'})
            if args.hermes_bin:
                env['HERMES_BIN'] = args.hermes_bin
            with patch.dict(os.environ, env, clear=True):
                from engine import db, stop, wake
                from scripts.unified_test_support import isolated_api
                conn = db.get_connection(); db.init_schema(conn)
                try:
                    with isolated_api() as url, patch.dict(os.environ, {'GROUP_ENGINE_URL': url}):
                        with httpx.Client(base_url=url, timeout=30) as client:
                            for name in args.scenarios:
                                setup = setup_project(client, conn, base, name, codex, hermes)
                                if name == 'research': hermes_configuration(hermes, database, url, setup['actor'])
                                print('Running isolated ' + name + ' native model verification', flush=True)
                                evidence['runs'].append(execute(client, conn, setup, name, hermes))
                                print('PASS: ' + name, flush=True)
                finally:
                    for active in wake.active_runs():
                        stop.stop_profile(active['profile'], active['project'], active['thread_id'])
                    evidence['native_execution_ledger'] = [dict(r) for r in conn.execute(
                        'SELECT runtime,session,started_at,ended_at,exit_code FROM wake_runs ORDER BY started_at')]
                    evidence['actual_native_executions'] = len(evidence['native_execution_ledger'])
                    try:
                        evidence['native_diagnostics'] = native_diagnostics(conn, hermes)
                    except Exception as exc:
                        evidence['diagnostic_collection_error'] = type(exc).__name__ + ': ' + str(exc)
                    finally:
                        conn.close()
    except Exception as exc:
        evidence['error'] = str(exc); status = 1
    after = {'codex': host_snapshot(codex_source) if codex_source else {},
             'hermes': host_snapshot(hermes_source) if hermes_source else {}}
    evidence['original_configuration'] = {'before': before, 'after': after, 'unchanged': before == after}
    evidence['passed'] = status == 0 and len(evidence['runs']) == len(args.scenarios) and before == after
    evidence['complete_template_coverage'] = evidence['passed'] and set(args.scenarios) == {'software', 'research', 'writing'}
    evidence['cost_note'] = 'Token usage is runtime-reported; monetary cost requires provider billing.'
    Path(args.evidence).write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('PASS' if evidence['passed'] else 'FAIL: see scoped evidence file', flush=True)
    sys.exit(0 if evidence['passed'] else 1)


if __name__ == '__main__':
    main()
