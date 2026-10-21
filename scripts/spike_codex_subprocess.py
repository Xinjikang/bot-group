"""Opt-in live Codex + unchanged MCP + isolated engine API feasibility probe.

Run with --live. Uses existing Codex authentication, creates test Codex sessions,
and writes evidence to a new temporary directory. Never opens production group.db.
"""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine import db
from scripts.unified_test_support import isolated_api, isolated_database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live explicitly authorizes real model calls')
    import psutil
    codex = shutil.which('codex')
    assert codex, 'Codex CLI missing'
    evidence = Path(tempfile.mkdtemp(prefix='bot_group_codex_spike_'))
    work = evidence / 'workspace'
    work.mkdir()
    hermes = evidence / 'hermes'
    hermes.mkdir()
    print('EVIDENCE=' + str(evidence), flush=True)
    token = 'probe-' + uuid.uuid4().hex
    summary = {'evidence': str(evidence), 'runs': [], 'status': 'running'}
    # Keep the account's existing authentication and all config files untouched.
    base = [codex, 'exec', '--ignore-user-config', '--json',
            '--skip-git-repo-check', '-c', 'sandbox_mode="read-only"',
            '-c', 'approval_policy="never"',
            '-c', 'memories.use_memories=false',
            '-c', 'memories.generate_memories=false']
    active = []

    def stop_tree(proc):
        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
        targets = children + [parent]
        for target in targets:
            try:
                target.kill()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(targets, timeout=10)
        proc.wait(timeout=10)
        assert not alive, 'test process descendants survived stop'
        return [p.pid for p in targets]

    try:
        with patch.dict(os.environ, {'HERMES_HOME': str(hermes), 'PYTHONDONTWRITEBYTECODE': '1'}):
            with isolated_database() as path, isolated_api() as url, ExitStack() as cleanup:
                assert Path(path).resolve() != (ROOT / 'group.db').resolve()
                conn = db.get_connection()
                cleanup.callback(conn.close)
                stamp = time.time()
                context = dict(profile='default', project='alpha', thread_id=None,
                               session='default-alpha-work', wake_started_at=stamp)
                conn.execute('INSERT INTO wake_runs(session,started_at,profile,project) VALUES(?,?,?,?)',
                             (context['session'], stamp, 'default', 'alpha'))
                for project, content in [('alpha', token), ('beta', 'CROSS_PROJECT_MUST_NOT_APPEAR')]:
                    mid = conn.execute("INSERT INTO messages(project,ts,sender,sender_type,content,mentions,need_reply) VALUES(?,?,'dev','bot',?,'[\"default\"]',1)",
                                       (project, db.now_iso(), content)).lastrowid
                    conn.execute("INSERT INTO inbox(message_id,profile,status) VALUES(?,'default','pending')", (mid,))
                conn.commit()
                mcp_env = {'GROUP_ENGINE_URL': url,
                           'TASK_QUEUE_ROLE': 'default', 'HERMES_PROFILE': 'default',
                           'HERMES_HOME': str(hermes), 'HERMES_CHAT_MODE': '0',
                           'PYTHONDONTWRITEBYTECODE': '1'}

                def run(label, prompt, thread_id=None, stop_on_tool=False):
                    context['wake_started_at'] = time.time()
                    conn.execute('INSERT INTO wake_runs(session,started_at,profile,project) VALUES(?,?,?,?)',
                                 (context['session'], context['wake_started_at'], 'default', 'alpha'))
                    conn.commit()
                    env = dict(mcp_env, XDG_GROUP_ENGINE_EXECUTION=json.dumps(context))
                    server = {'command': sys.executable, 'args': [str(ROOT / 'mcp/server.py')],
                              'env': env, 'required': True, 'startup_timeout_sec': 30,
                              'tools': {name: {'approval_mode': 'approve'} for name in
                                        ['group_read_inbox', 'group_send', 'group_mark_seen', 'req_list']},
                              'enabled_tools': ['group_read_inbox', 'group_send', 'group_mark_seen', 'req_list']}
                    # TOML inline table; values are JSON strings/arrays/booleans, all valid TOML here.
                    def toml(value):
                        if isinstance(value, dict):
                            return '{' + ','.join(json.dumps(k) + '=' + toml(v) for k, v in value.items()) + '}'
                        return json.dumps(value)
                    cmd = base + ['-c', 'mcp_servers.probe=' + toml(server)]
                    if thread_id:
                        cmd += ['resume', thread_id, prompt]
                    else:
                        cmd += [prompt]
                    record = {'label': label, 'execution': dict(context)}
                    stdout_path = evidence / (label + '.jsonl')
                    stderr_path = evidence / (label + '.stderr')
                    events = []
                    pending = queue.Queue()
                    with stderr_path.open('w', encoding='utf-8') as err:
                        proc = subprocess.Popen(cmd, cwd=work, env=os.environ.copy(),
                                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                stderr=err, text=True, encoding='utf-8', errors='replace',
                                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                        active.append(proc)
                        cleanup.callback(lambda p=proc: stop_tree(p) if p.poll() is None else None)
                        def read():
                            for line in proc.stdout:
                                pending.put(line)
                            pending.put(None)
                        reader = threading.Thread(target=read, daemon=True)
                        reader.start()
                        deadline = time.monotonic() + 180
                        with stdout_path.open('w', encoding='utf-8') as log:
                            while True:
                                if time.monotonic() > deadline:
                                    record['timeout'] = True
                                    record['killed_pids'] = stop_tree(proc)
                                    raise RuntimeError(label + ' timed out; see evidence')
                                try:
                                    line = pending.get(timeout=1)
                                except queue.Empty:
                                    continue
                                if line is None:
                                    break
                                log.write(line)
                                log.flush()
                                try:
                                    event = json.loads(line)
                                except ValueError:
                                    continue
                                events.append(event)
                                item = event.get('item', {})
                                print(label, event.get('type'), item.get('type', ''), item.get('tool', ''), flush=True)
                                if stop_on_tool and event.get('type') == 'item.started' and item.get('type') == 'mcp_tool_call':
                                    record['killed_pids'] = stop_tree(proc)
                                    record['stopped_during_mcp'] = True
                                    stop_on_tool = False
                        record['exit_code'] = proc.wait(timeout=10)
                        reader.join(timeout=5)
                    active.remove(proc)
                    record['thread_id'] = next((e.get('thread_id') for e in events if e.get('type') == 'thread.started'), None)
                    record['completed'] = any(e.get('type') == 'turn.completed' for e in events)
                    record['mcp_calls'] = [e['item'] for e in events if e.get('type') == 'item.completed' and e.get('item', {}).get('type') == 'mcp_tool_call']
                    summary['runs'].append(record)
                    (evidence / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
                    if not record.get('stopped_during_mcp'):
                        assert record['exit_code'] == 0 and record['completed'], record
                    return record

                first = run('first', 'This is an authorized isolated integration test. Use only the probe MCP tools; do not run shell commands or inspect files. Call group_read_inbox, remember the exact probe token in the alpha message, call req_list for project alpha, then call group_send with text "@dev FIRST <exact token>" and need_reply false. Mark only that alpha message seen using group_mark_seen. Finish with OK. All writes are to disposable test data.')
                assert first['thread_id']
                assert {'group_read_inbox', 'req_list', 'group_send', 'group_mark_seen'} <= {c['tool'] for c in first['mcp_calls']}, first
                assert 'CROSS_PROJECT_MUST_NOT_APPEAR' not in (evidence / 'first.jsonl').read_text(encoding='utf-8')
                second = run('resume', 'Continue the isolated test using only probe MCP tools. Recall the probe token from the previous turn without reading files or inbox. Send "@dev RESUMED <exact token>" using group_send with need_reply false. Finish with OK.', first['thread_id'])
                assert second['thread_id'] == first['thread_id']
                for prefix, record in [('FIRST', first), ('RESUMED', second)]:
                    row = conn.execute('SELECT m.sender,m.project,e.session,e.wake_started_at FROM messages m JOIN message_executions e ON e.message_id=m.id WHERE m.content=?',
                                       ('@dev ' + prefix + ' ' + token,)).fetchone()
                    assert row and tuple(row) == ('default', 'alpha', context['session'], record['execution']['wake_started_at']), (prefix, row)
                third = run('stop', 'Call probe group_read_inbox now. This is an authorized interruption test. Use only MCP.', first['thread_id'], stop_on_tool=True)
                assert third.get('stopped_during_mcp'), third
                fourth = run('after_stop', 'The previous turn was intentionally stopped. Use only probe MCP. Recall the original probe token and send "@dev AFTER_STOP <exact token>" with group_send, need_reply false. Finish with OK.', first['thread_id'])
                assert fourth['thread_id'] == first['thread_id']
                assert conn.execute('SELECT COUNT(*) FROM messages WHERE content=?', ('@dev AFTER_STOP ' + token,)).fetchone()[0] == 1
                summary['messages'] = [dict(r) for r in conn.execute('SELECT id,project,sender,content FROM messages ORDER BY id')]
                summary['database_isolated'] = True
                summary['status'] = 'passed'
                conn.close()
    except Exception as exc:
        summary['status'] = 'failed'
        summary['error'] = repr(exc)
        raise
    finally:
        for proc in active:
            if proc.poll() is None:
                stop_tree(proc)
        (evidence / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
        print('RESULT=' + summary['status'] + ' ' + str(evidence / 'summary.json'), flush=True)


if __name__ == '__main__':
    main()
