"""Explicit --live smoke test, with isolated engine data and Codex home."""
import argparse
import os
import shutil
import sys
import tempfile
import time
import httpx
import json
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if not args.live:
        print('Use --live to run a real Codex turn in isolated test state.')
        return
    source = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    with tempfile.TemporaryDirectory(prefix='codex-driver-live-') as directory:
        root = Path(directory)
        home = root / 'codex'
        home.mkdir()
        (home / 'config.toml').write_text(
            'sandbox_mode = "read-only"\napproval_policy = "on-request"\n'
            'approvals_reviewer = "auto_review"\n', encoding='utf-8')
        work = root / 'work'
        work.mkdir()
        shutil.copy2(source / 'auth.json', home / 'auth.json')
        with patch.dict(os.environ, {'GROUP_DB': str(root / 'group.db'), 'CODEX_HOME': str(home),
                                     'HERMES_HOME': str(root / 'hermes')}):
            from engine import bot_identity, codex_reasoning, db, discovery, queue, session_adapter, stop, wake
            from scripts.unified_test_support import isolated_api
            conn = db.get_connection()
            try:
                db.init_schema(conn)
                conn.execute("INSERT INTO projects(name,root,directory_mode,created_at) VALUES('probe',?,'shared',?)",
                             (str(work), db.now_iso()))
                bot = discovery.register_bot(conn, 'codex', 'default', str(home), 'probe')
                member = bot_identity.enroll(conn, bot['bot_id'], 'probe')
                conn.commit()
                marker_a, marker_b = 'PROJECT_A_' + uuid.uuid4().hex, 'PROJECT_B_' + uuid.uuid4().hex
                conn.execute("UPDATE projects SET display_name=? WHERE name='probe'", (marker_a,))
                conn.commit()
                with isolated_api() as url, patch.dict(os.environ, {'GROUP_ENGINE_URL': url}):
                    prompt = ('This is an isolated driver test. Call group_list_members(role="验证伙伴"), '
                              'then call group_read_inbox once, then call '
                              'group_send(text="@user CODEX_DRIVER_LIVE_OK", '
                              'need_reply=false). Use only MCP tools; finish afterward.')
                    with httpx.Client(base_url=url, timeout=20) as client:
                        response = client.patch(f'/projects/probe/members/{member}/role', json={'role': '验证伙伴'})
                        response.raise_for_status()
                        response = client.post('/thread', json={'project': 'probe', 'creator': 'user',
                            'title': 'isolated Codex mention', 'members': [member], 'opening': '隔离话题启动验证'})
                        response.raise_for_status()
                        topic = response.json()['thread_id']
                        response = client.post('/msg', json={'project': 'probe', 'sender': 'user',
                            'thread_id': topic, 'text': f'@{member} {prompt}', 'need_reply': True})
                        response.raise_for_status()
                        mentioned = response.json()['message_id']
                    assert conn.execute('SELECT 1 FROM inbox WHERE message_id=? AND profile=? AND status=?',
                                        (mentioned, member, 'pending')).fetchone()
                    # Exercise the detached-engine fallback without CODEX_BIN or PATH resolution.
                    with patch.dict(os.environ, {'CODEX_BIN': ''}), patch.object(wake.shutil, 'which', return_value=None):
                        print('Native CLI discovered:', wake._codex_bin(), flush=True)
                        assert queue.process_one(conn)
                    job = conn.execute('SELECT * FROM wake_jobs WHERE profile=? AND thread_id=? ORDER BY id LIMIT 1',
                                       (member, topic)).fetchone()
                    assert job['status'] == 'done', dict(job)
                    session = wake.session_name(member, 'chat', topic, 'probe')
                    deadline = time.monotonic() + 120
                    record = None
                    while time.monotonic() < deadline:
                        record = conn.execute('SELECT * FROM wake_runs WHERE session=? ORDER BY started_at DESC LIMIT 1',
                                              (session,)).fetchone()
                        if record and record['ended_at'] is not None:
                            break
                        time.sleep(0.2)
                    else:
                        stop.stop_profile(member, 'probe', topic)
                        raise AssertionError('native execution timeout')
                    data = codex_reasoning.read(session, record['started_at'])
                    assert record['exit_code'] == 0, data
                    assert record['runtime_session_id'], 'native thread binding missing'
                    sent = conn.execute("SELECT id FROM messages WHERE project='probe' AND sender=? "
                                        "AND content='CODEX_DRIVER_LIVE_OK'", (member,)).fetchone()
                    if not sent:
                        sent = conn.execute("SELECT id FROM messages WHERE project='probe' AND sender=? "
                                            "AND content LIKE '%CODEX_DRIVER_LIVE_OK%'", (member,)).fetchone()
                    assert sent, data
                    assert any(e['reply_message_id'] == sent['id'] for e in data['entries']), data
                    roster_results = []
                    for row in conn.execute('SELECT payload FROM codex_run_events WHERE session=? AND wake_started_at=?',
                                            (session, record['started_at'])):
                        event = json.loads(row['payload'])
                        item = event.get('item', {})
                        if event['type'] == 'item.completed' and item.get('tool') == 'group_list_members':
                            for content in (item.get('result') or {}).get('content', []):
                                if content.get('type') == 'text':
                                    roster_results.append(json.loads(content['text']))
                    assert roster_results and roster_results[-1]['members'] == [
                        row for row in roster_results[-1]['members'] if row['profile'] == member and row['role'] == '验证伙伴'
                    ] and roster_results[-1]['count'] == 1, roster_results
                    assert conn.execute('SELECT thread_id FROM messages WHERE id=?', (sent['id'],)).fetchone()[0] == topic
                    print('PASS: user @mention, exact chat queue, native CLI without PATH, MCP project roster/inbox/send, thread binding and reasoning', flush=True)
                    old_thread = record['runtime_session_id']
                    from scripts.spike_codex_developer_instructions import inspect_rollout

                    def check_context(row, marker):
                        counts = inspect_rollout(home, row['runtime_session_id'], marker)
                        assert counts['developer_marker_count'] == 1, counts
                        assert counts['user_marker_count'] == 0, counts
                        print('CONTEXT ' + json.dumps(counts), flush=True)
                        return counts

                    def check_reply(row, marker):
                        events = [json.loads(r['payload']) for r in conn.execute(
                            'SELECT payload FROM codex_run_events WHERE session=? AND wake_started_at=?',
                            (session, row['started_at']))]
                        replies = [e['item']['text'] for e in events if e.get('type') == 'item.completed'
                                   and e.get('item', {}).get('type') == 'agent_message']
                        assert replies and replies[-1].strip() == marker, replies

                    initial_context = check_context(record, marker_a)
                    ping = 'Isolated context check. Use no tools. Reply only with the current project display name from developer instructions.'
                    run = wake._spawn(member, 'chat', ping, topic, 'probe')
                    assert run['ok'], run
                    deadline = time.monotonic() + 120
                    while time.monotonic() < deadline:
                        resumed = conn.execute('SELECT * FROM wake_runs WHERE session=? ORDER BY started_at DESC LIMIT 1',
                                               (session,)).fetchone()
                        if resumed and resumed['started_at'] != record['started_at'] and resumed['ended_at'] is not None:
                            break
                        time.sleep(0.2)
                    else:
                        raise AssertionError('native resume timeout')
                    assert resumed['exit_code'] == 0, codex_reasoning.read(session, resumed['started_at'])
                    assert resumed['runtime_session_id'] == old_thread
                    resumed_context = check_context(resumed, marker_a)
                    assert resumed_context['developer_messages'] == initial_context['developer_messages']
                    check_reply(resumed, marker_a)
                    print('PASS: resume uses developer project value, one context copy, zero user copies', flush=True)
                    conn.execute("UPDATE projects SET display_name=? WHERE name='probe'", (marker_b,))
                    conn.commit()
                    with httpx.Client(base_url=url, timeout=30) as client:
                        response = client.post('/msg', json={'project': 'probe', 'sender': 'user',
                            'thread_id': topic, 'text': f'/new @{member}'})
                        response.raise_for_status()
                        assert response.json()['ok'], response.json()
                    assert session_adapter.codex_thread_id(conn, 'probe', member, 'chat', topic) is None
                    run = wake._spawn(member, 'chat', ping, topic, 'probe')
                    assert run['ok'], run
                    deadline = time.monotonic() + 120
                    while time.monotonic() < deadline:
                        fresh = conn.execute('SELECT * FROM wake_runs WHERE session=? ORDER BY started_at DESC LIMIT 1',
                                             (session,)).fetchone()
                        if fresh and fresh['started_at'] != resumed['started_at'] and fresh['ended_at'] is not None:
                            break
                        time.sleep(0.2)
                    else:
                        raise AssertionError('new native session timeout')
                    assert fresh['exit_code'] == 0, codex_reasoning.read(session, fresh['started_at'])
                    assert fresh['runtime_session_id'] and fresh['runtime_session_id'] != old_thread
                    assert session_adapter.codex_thread_id(conn, 'probe', member, 'chat', topic) == fresh['runtime_session_id']
                    check_context(fresh, marker_b)
                    check_reply(fresh, marker_b)
                    print('PASS: HTTP /new, next native execution creates and binds a different thread', flush=True)
                    run = wake._spawn(member, 'work', 'Wait for further instructions.', None, 'probe')
                    assert run['ok'], run
                    session = wake.session_name(member, 'work', None, 'probe')
                    with httpx.Client(base_url=url, timeout=30) as client:
                        response = client.post('/msg', json={'project': 'probe', 'sender': 'user',
                            'text': f'/stop @{member}'})
                        response.raise_for_status()
                        stopped = response.json()['result']
                    assert stopped['stopped'] and stopped['killed'] == 1, stopped
                    deadline = time.monotonic() + 10
                    while session in wake._running_sessions and time.monotonic() < deadline:
                        time.sleep(0.1)
                    assert session not in wake._running_sessions
                    assert wake.find_bot_processes(member, session=session) == []
                    assert session_adapter.codex_thread_id(conn, 'probe', member, 'chat', topic) == fresh['runtime_session_id']
                    print('PASS: HTTP /stop, native process exit, chat binding preserved', flush=True)
            finally:
                for active in wake.active_runs():
                    stop.stop_profile(active['profile'], active['project'], active['thread_id'])
                conn.close()


if __name__ == '__main__':
    main()
