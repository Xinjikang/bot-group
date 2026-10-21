"""Opt-in isolated live probe: inline developer instructions and resume caching."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine.wake import _codex_bin, system_prompt_for


def inspect_rollout(home, thread_id, marker):
    paths = list((home / 'sessions').rglob(f'*{thread_id}*.jsonl'))
    assert len(paths) == 1, paths
    rows = [json.loads(line) for line in paths[0].read_text(encoding='utf-8').splitlines() if line.strip()]
    counts = dict(developer_messages=0, developer_marker_count=0, user_marker_count=0,
                  turn_context_marker_count=0)
    for row in rows:
        payload = row.get('payload', {})
        if row.get('type') == 'response_item' and payload.get('type') == 'message':
            text = '\n'.join(item.get('text', '') for item in payload.get('content', []))
            if payload.get('role') == 'developer':
                counts['developer_messages'] += 1
                counts['developer_marker_count'] += text.count(marker)
            if payload.get('role') == 'user':
                counts['user_marker_count'] += text.count(marker)
        if row.get('type') == 'turn_context':
            counts['turn_context_marker_count'] += json.dumps(payload).count(marker)
    return dict(path=str(paths[0]), **counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required for real model calls')
    source = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    assert (source / 'auth.json').is_file(), 'Existing Codex authentication required'
    evidence = Path(tempfile.mkdtemp(prefix='codex-instructions-'))
    home, work = evidence / 'home', evidence / 'work'
    home.mkdir()
    work.mkdir()
    auth = home / 'auth.json'
    env = dict(os.environ, CODEX_HOME=str(home))
    env.pop('XDG_GROUP_ENGINE_EXECUTION', None)
    cli = _codex_bin()
    summary = {'evidence': str(evidence), 'runs': [], 'status': 'running'}
    summary['cli_version'] = subprocess.check_output([cli, '--version'], text=True).strip()
    base = [cli, 'exec', '--ignore-user-config', '--json', '--skip-git-repo-check',
            '-c', 'sandbox_mode="read-only"', '-c', 'approval_policy="on-request"',
            '-c', 'approvals_reviewer="auto_review"',
            '-c', 'memories.use_memories=false', '-c', 'memories.generate_memories=false']
    marker_a, marker_b = 'RULE_A_' + uuid.uuid4().hex, 'RULE_B_' + uuid.uuid4().hex

    def instructions(marker):
        return (system_prompt_for('work') + '\n' + json.dumps({'project': 'isolated-probe', 'mode': 'work'})
                + '\nFor this isolated validation only: treat each user message as a probe ping. '
                'Do not call tools, claim tasks or send messages. Reply with exactly this token: ' + marker)

    def save():
        (evidence / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')

    def run(label, marker, developer_text=None, thread_id=None):
        command = list(base)
        if developer_text is not None:
            command += ['-c', 'developer_instructions=' + json.dumps(developer_text, ensure_ascii=False)]
        if thread_id:
            command += ['resume', thread_id]
        command += ['Probe ping. Return the configured validation token only.']
        record = {'label': label, 'inline_supplied': developer_text is not None}
        summary['runs'].append(record)
        print('START ' + label, flush=True)
        with (evidence / (label + '.stdout.jsonl')).open('w', encoding='utf-8') as out, \
             (evidence / (label + '.stderr.txt')).open('w', encoding='utf-8') as err:
            proc = subprocess.Popen(command, cwd=work, env=env, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                record['exit_code'] = proc.wait(timeout=150)
            except subprocess.TimeoutExpired:
                import psutil
                parent = psutil.Process(proc.pid)
                targets = parent.children(recursive=True) + [parent]
                for target in targets:
                    try:
                        target.kill()
                    except psutil.NoSuchProcess:
                        pass
                proc.wait(timeout=10)
                record['timeout'] = True
                raise
        events = []
        for line in (evidence / (label + '.stdout.jsonl')).read_text(encoding='utf-8').splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        record['thread_id'] = next((e['thread_id'] for e in events if e.get('type') == 'thread.started'), thread_id)
        completed = [e for e in events if e.get('type') == 'turn.completed']
        messages = [e['item']['text'] for e in events if e.get('type') == 'item.completed'
                    and e.get('item', {}).get('type') == 'agent_message']
        record['messages'] = messages
        record['usage'] = completed[-1].get('usage', {}) if completed else {}
        # exec emits cumulative thread usage on resume; compare against the prior turn.
        previous = next((r['usage'] for r in reversed(summary['runs'][:-1])
                         if r.get('thread_id') == record['thread_id']), {})
        record['usage_delta'] = {k: v - previous.get(k, 0) for k, v in record['usage'].items()}
        assert record['exit_code'] == 0 and completed, 'Successful model turn required; inspect evidence'
        assert messages and messages[-1].strip() == marker, record
        record['rollout'] = inspect_rollout(home, record['thread_id'], marker)
        assert record['rollout']['developer_marker_count'] == 1, record
        assert record['rollout']['user_marker_count'] == 0, record
        save()
        print(json.dumps(record), flush=True)
        return record['thread_id']

    print('EVIDENCE=' + str(evidence), flush=True)
    try:
        shutil.copy2(source / 'auth.json', auth)
        thread = run('new_a', marker_a, instructions(marker_a))
        assert run('resume_same_a', marker_a, instructions(marker_a), thread) == thread
        assert run('resume_omitted_a', marker_a, thread_id=thread) == thread
        new_thread = run('new_b', marker_b, instructions(marker_b))
        assert new_thread != thread
        assert all(r['usage_delta'].get('cached_input_tokens', 0) > 0 for r in summary['runs'][1:3]), 'No resume cache hit observed'
        summary['status'] = 'passed'
    except Exception as exc:
        summary['status'] = 'failed'
        summary['error'] = str(exc)
        raise
    finally:
        save()
        auth.unlink(missing_ok=True)
    print('PASS: new, identical resume, omitted resume, new value, cache hit', flush=True)


if __name__ == '__main__':
    main()
