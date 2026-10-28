"""Three isolated API processes sharing a ledger and persistent signing keys."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.probe_config import absolute_path, loopback_url


def serve(url):
    from fastapi import FastAPI
    import uvicorn
    from engine import api, config, db, signing_keys, hermes_gateway_ledger as ledger
    conn = db.get_connection()
    try:
        db.init_schema(conn)
        signing_keys.initialize(conn)
    finally:
        conn.close()
    app = FastAPI()
    app.include_router(api.router_api)

    @app.get('/probe/status')
    def status():
        return dict(pid=os.getpid(), database=config.db_path(), data_dir=str(config.data_dir()),
                    generation=signing_keys.current()[0], instance=os.environ['SIGNING_PROBE_INSTANCE'])

    @app.post('/probe/finish/{session}')
    def finish(session: str):
        conn = db.get_connection()
        try:
            row = conn.execute('SELECT * FROM hermes_executions WHERE session=?', (session,)).fetchone()
            ledger.apply_receipt(conn, row['execution_id'], dict(admission_id=row['execution_id'],
                ref={'session_id': session, 'profile_id': 'fixture'}, authority_epoch=1,
                execution_generation=1, status='terminal', outcome='completed'))
            return {'state': 'terminal'}
        finally:
            conn.close()

    target = urlsplit(url)
    uvicorn.run(app, host=target.hostname, port=target.port, log_level='warning')


def probe(args):
    import httpx
    from engine import auth, db, signing_keys, hermes_gateway_ledger as ledger
    url = loopback_url(args.url)
    root = absolute_path(args.data_root, '--data-root')
    evidence = absolute_path(args.evidence, '--evidence')
    target = urlsplit(url)
    with socket.socket() as gate:
        gate.bind((target.hostname, target.port))
    evidence.mkdir(parents=True, exist_ok=False)
    home = root / 'task-258' / uuid.uuid4().hex
    home.mkdir(parents=True, exist_ok=False)
    database = home / 'group.db'
    os.environ.update(GROUP_DB=str(database), GROUP_DATA_DIR=str(home),
                      GROUP_ENGINE_KEY=secrets.token_hex(32))
    conn = db.get_connection()
    db.init_schema(conn)
    conn.execute("INSERT INTO members(profile,display_name,role) VALUES('default','fixture','dev')")
    conn.execute("INSERT INTO projects(name,root,created_at) VALUES('alpha',?,?)", (str(home), db.now_iso()))
    from scripts.project_test_support import enable_members
    enable_members(conn, ('alpha',))
    conn.commit()
    headers = {}
    for session, thread in (('A', 101), ('B', 102)):
        ledger.prepare(conn, session=session, project='alpha', profile='default', kind='chat', thread_id=thread,
            task_id=None, job_id=None, event_floor=0, source='', python='[]', home='', workdir='',
            session_id=session, profile_id='fixture', started_at=100.0)
        context = dict(profile='default', project='alpha', thread_id=thread, session=session, wake_started_at=100.0)
        headers[session] = {'X-Group-Profile': 'default', 'X-Group-Key': auth.execution_token('default', context)}
    conn.execute('UPDATE hermes_executions SET signing_generation=NULL')
    conn.commit()
    conn.close()
    result = dict(url=url, database=str(database), data_dir=str(home), boots=[], checks={}, released=False)
    process = None
    instance = uuid.uuid4().hex
    os.environ['SIGNING_PROBE_INSTANCE'] = instance
    client = httpx.Client(base_url=url, timeout=5, trust_env=False)

    def stop():
        if process is None:
            return
        import psutil
        try:
            children = psutil.Process(process.pid).children(recursive=True)
        except psutil.NoSuchProcess:
            children = []
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(children, timeout=10)
        if alive:
            raise RuntimeError('isolated API descendants failed to stop')
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)

    try:
        for boot in range(3):
            with (evidence / f'boot-{boot}.log').open('wb') as log:
                process = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve()),
                    '--serve', '--url', url], cwd=ROOT, env=dict(os.environ), stdout=log, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                deadline = time.monotonic() + 20
                while True:
                    try:
                        status = client.get('/probe/status').json()
                        break
                    except (httpx.HTTPError, ValueError):
                        if process.poll() is not None or time.monotonic() >= deadline:
                            raise RuntimeError('isolated API startup failed; inspect boot log')
                        time.sleep(.1)
                import psutil
                descendants = {p.pid for p in psutil.Process(process.pid).children(recursive=True)}
                assert status['pid'] in {process.pid, *descendants}
                assert status['instance'] == instance and Path(status['database']) == database
                assert Path(status['data_dir']) == home
                result['boots'].append(status)
                expected_a = 401 if boot == 2 else 200
                result['checks'][f'boot{boot}_A'] = client.get('/commands', headers=headers['A']).status_code == expected_a
                result['checks'][f'boot{boot}_B'] = client.get('/commands', headers=headers['B']).status_code == 200
                if boot == 1:
                    client.post('/probe/finish/A').raise_for_status()
                    for method, path in (('GET', '/commands'), ('GET', '/members?project=alpha'),
                                         ('POST', '/msg'), ('POST', '/task-tools/task_get'),
                                         ('POST', '/heartbeat'), ('POST', '/inbox/seen')):
                        result['checks'][f'terminal_{method}_{path}'] = client.request(method, path, headers=headers['A']).status_code == 401
                if boot == 2:
                    client.post('/probe/finish/B').raise_for_status()
                    generation, ring = signing_keys.read(home / 'secrets' / 'bot-signing.key')
                    result['checks']['retired_only_old'] = generation == 2 and set(ring) == {2}
                stop()
                process = None
        result['checks']['generations'] = [x['generation'] for x in result['boots']] == [1, 2, 2]
    finally:
        stop()
        client.close()
        with socket.socket() as check:
            result['released'] = check.connect_ex((target.hostname, target.port)) != 0
        (evidence / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))
    return 0 if result['released'] and all(result['checks'].values()) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--data-root')
    parser.add_argument('--evidence')
    parser.add_argument('--serve', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        serve(loopback_url(args.url))
        return 0
    return probe(args)


if __name__ == '__main__':
    raise SystemExit(main())
