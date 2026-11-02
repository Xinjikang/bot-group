"""Verify native Hermes memory in a fresh isolated profile using the selected installation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--profile', default='default')
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    if not args.home.is_absolute() or not args.home.is_dir() or not args.evidence.is_absolute():
        parser.error('existing absolute selected home and new absolute evidence directory required')
    source = args.home if args.profile == 'default' else args.home / 'profiles' / args.profile
    args.evidence.mkdir(parents=True, exist_ok=False)
    base = args.evidence
    profile = 'hud-memory-test-' + uuid.uuid4().hex[:12]
    home, work = args.home / 'profiles' / profile, base / 'project'
    if home.resolve().parent != (args.home / 'profiles').resolve():
        parser.error('test profile must stay inside the selected profiles directory')
    home.mkdir(parents=True, exist_ok=False)
    work.mkdir()
    import yaml
    original = yaml.safe_load((source / 'config.yaml').read_text(encoding='utf-8')) or {}
    selected = {key: original[key] for key in ('model', 'custom_providers', 'providers', 'reasoning',
                                              'inference', 'memory', 'agent') if key in original}
    selected.update(updates={'check': False}, checkpoints={'enabled': False},
                    auxiliary={'title_generation': {'enabled': False}})
    (home / 'config.yaml').write_text(yaml.safe_dump(selected, allow_unicode=True), encoding='utf-8')
    (home / 'profile.yaml').write_text('display_name: Isolated memory acceptance\n', encoding='utf-8')
    source_files = [source / name for name in ('config.yaml', '.env', 'auth.json', 'SOUL.md', 'memories/MEMORY.md', 'memories/USER.md')]
    fingerprints = lambda: {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in source_files if p.is_file()}
    before = fingerprints()
    for path in source_files:
        if path.name in ('.env', 'auth.json') and path.is_file():
            shutil.copyfile(path, home / path.name)
    os.environ.update(HERMES_HOME=str(args.home), HERMES_DISABLE_LAZY_INSTALLS='1',
                      GROUP_DB=str(base / 'group.db'), GROUP_DATA_DIR=str(base / 'data'))
    from engine import db, hud_gateway, hud_runtime, project_hud as hud, hermes_connections
    from engine.hermes_gateway_client import resume_connections
    from engine import hermes_gateway_client as native
    def control(verb):
        exchange = native._pipe_exchange if os.name == 'nt' else native._socket_exchange
        request = {'protocol': 1, 'id': 1, 'verb': verb, 'params': {'name': profile}}
        return json.loads(exchange(args.home, json.dumps(request).encode() + b'\n', 30))
    resume_connections()
    with hud.connection() as conn:
        db.init_schema(conn)
        conn.execute('INSERT INTO hud_sessions(id,activity,updated) VALUES(?,?,?)', ('memory-acceptance', 0, 0))
    bot = {'id': 1, 'runtime': 'hermes', 'profile': profile, 'config_root': str(args.home)}
    nonce = 'HUD-MEM-' + uuid.uuid4().hex
    records = []
    def turn(generation, request):
        payload = {'project': {'name': 'memory-acceptance', 'root': str(work)}, 'user_request': request}
        progress = []
        began = time.monotonic()
        with hud_runtime.progress_sink(lambda kind, text: progress.append({'kind': kind, 'text': text})), \
             hud_gateway.owner('memory-acceptance', generation):
            result = hud_runtime.respond(bot, payload)
            saved = hud_gateway._saved()
        records.append({'generation': generation, 'request': request, 'result': result,
                        'session_id': saved['session_id'], 'tools': saved.get('tools'),
                        'seconds': round(time.monotonic()-began, 2), 'progress': progress})
        (base / 'result.json').write_text(json.dumps({'passed': False, 'records': records},
            ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'generation': generation, 'result': result, 'seconds': records[-1]['seconds']},
                         ensure_ascii=False), flush=True)
        return result
    memory_file = home / 'memories' / 'MEMORY.md'
    try:
        served = control('serve-profile')
        assert served.get('ok'), served
        turn(1, '这是隔离验收，已授权本轮原生记忆写入。请用 memory 工具记住约定：'
             '“跨会话记忆验收的代号为 ' + nonce + '”。只保存这条合成约定，完成后汇报。')
        assert memory_file.is_file() and nonce in memory_file.read_text(encoding='utf-8'), 'Native memory artifact missing'
        shutil.copyfile(memory_file, base / 'memory-after-write.md')
        recall = '请查询长期记忆：跨会话记忆验收的代号是什么？给出完整代号。'
        assert nonce not in recall
        result = turn(2, recall)
        assert records[0]['session_id'] != records[1]['session_id'], 'Native session was reused'
        assert nonce in json.dumps(result, ensure_ascii=False), 'Fresh session did not recall the stored value'
        assert all('memory' in record['tools'] for record in records), 'Native memory tool absent'
        turn(3, '请用原生 memory 工具忘记“跨会话记忆验收的代号”这条合成约定，完成后汇报。')
        assert nonce not in memory_file.read_text(encoding='utf-8'), 'Native forget did not remove the stored value'
        shutil.copyfile(memory_file, base / 'memory-after-forget.md')
        assert not any(work.iterdir()), 'Acceptance wrote project files instead of native memory'
        assert before == fingerprints(), 'Selected profile data changed'
        report = {'passed': True, 'records': records, 'native_memory_file': str(memory_file),
                  'fresh_session_recalled': True, 'forget_verified': True, 'source_unchanged': True,
                  'boundary': 'selected native Hermes/model; isolated profile and SQLite; synthetic memory only'}
        (base / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        hermes_connections.stop()
        try:
            result = control('unserve-profile')
            assert result.get('ok'), result
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(str(home/'state.db'))) as conn:
                conn.row_factory = sqlite3.Row
                messages = [dict(row) for row in conn.execute('SELECT session_id,role,content,tool_calls,tool_name FROM messages ORDER BY id')]
            (base/'native-messages.json').write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding='utf-8')
        finally:
            for name in ('.env', 'auth.json'):
                (home / name).unlink(missing_ok=True)
        if home.resolve().parent != (args.home/'profiles').resolve() or not home.name.startswith('hud-memory-test-'):
            raise RuntimeError('Isolated profile cleanup escaped the selected profiles directory')
        pending = []
        def writable_remove(function, path, exc):
            import stat
            target = Path(path)
            if not target.resolve().is_relative_to(home.resolve()):
                raise RuntimeError('Cleanup target escaped the isolated profile')
            if getattr(exc[1], 'winerror', None) in (32, 145):
                pending.append(str(target))
                return
            target.chmod(target.stat().st_mode | stat.S_IWRITE)
            try:
                function(path)
            except OSError as error:
                if getattr(error, 'winerror', None) not in (32, 145):
                    raise
                pending.append(str(target))
        shutil.rmtree(home, onerror=writable_remove)
        report_path = base/'result.json'
        report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.is_file() else {'passed': False}
        report['cleanup'] = {'unserved': True, 'credentials_removed': True, 'pending_paths': pending}
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
