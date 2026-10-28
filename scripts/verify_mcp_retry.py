"""Real stdio MCP survives an isolated API outage and a commit/reply crash."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.probe_config import absolute_path, loopback_url


def serve(url, expected_database):
    import uvicorn
    from fastapi import FastAPI
    from engine import api, db
    assert Path(db.db_path()).resolve() == Path(expected_database).resolve()
    app = FastAPI()
    app.include_router(api.router_api)

    @app.get('/retry-health')
    def health():
        return {'database': db.db_path(), 'pid': os.getpid(), 'instance': os.environ['MCP_RETRY_INSTANCE']}

    @app.middleware('http')
    async def lose_committed_response(request, call_next):
        response = await call_next(request)
        if (os.environ.get('MCP_RETRY_DROP_REPLY') == '1'
                and request.url.path == '/task-tools/req_create' and response.status_code == 200):
            os._exit(23)
        return response

    target = urlsplit(url)
    uvicorn.run(app, host=target.hostname, port=target.port, log_level='error')


class APIProcess:
    def __init__(self, url, database, directory):
        self.url, self.database, self.directory = url, database, directory
        self.process = None
        self.log = None
        self.instances = []

    def start(self, drop=False):
        import httpx
        self.stop()
        self.log = (self.directory / 'api.log').open('a', encoding='utf-8')
        instance = secrets.token_hex(16)
        env = {**os.environ, 'MCP_RETRY_DROP_REPLY': '1' if drop else '0', 'MCP_RETRY_INSTANCE': instance}
        self.process = subprocess.Popen([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve()),
            '--serve', '--url', self.url, '--expected-database', self.database], env=env,
            stdout=self.log, stderr=self.log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        deadline = time.monotonic() + 10
        with httpx.Client(timeout=0.5) as client:
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError('Isolated API exited; inspect api.log')
                try:
                    result = client.get(self.url + '/retry-health').json()
                    assert result['database'] == self.database and result['instance'] == instance, result
                    self.instances.append(result)
                    return
                except httpx.RequestError:
                    time.sleep(0.05)
        raise RuntimeError('Isolated API readiness deadline exceeded')

    def stop(self):
        import psutil
        if self.process is not None:
            if self.process.poll() is None:
                # Windows venv launchers may own a separate interpreter child.
                try:
                    descendants = psutil.Process(self.process.pid).children(recursive=True)
                except psutil.NoSuchProcess:
                    descendants = []
                for child in reversed(descendants):
                    try:
                        child.terminate()
                    except psutil.NoSuchProcess:
                        pass
                if self.process.poll() is None:
                    self.process.terminate()
                psutil.wait_procs(descendants, timeout=5)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.process = None
        if self.log:
            self.log.close()
            self.log = None


async def verify(url, data_root, evidence):
    import httpx
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
    from engine import auth, db
    from scripts.unified_test_support import isolated_database, server_parameters, REQUIREMENT_FIELDS, role_id_of

    # Reject a port conflict before creating data or issuing any HTTP request.
    target = urlsplit(url)
    with socket.socket() as check:
        check.bind((target.hostname, target.port))
    data_root.mkdir(parents=True, exist_ok=True)
    evidence.mkdir(parents=True, exist_ok=False)
    with patch.object(tempfile, 'tempdir', str(data_root)), patch.dict(os.environ,
            {'GROUP_ENGINE_KEY': secrets.token_hex(32), 'GROUP_DATA_DIR': str(data_root)}), isolated_database() as database:
        server = APIProcess(url, database, evidence)
        conn = db.get_connection()
        conn.execute("INSERT INTO wake_runs(session,started_at,project,profile) VALUES('retry-work',1,'alpha','default')")
        conn.commit()
        conn.close()
        context = {'profile': 'default', 'project': 'alpha', 'thread_id': None,
                   'session': 'retry-work', 'wake_started_at': 1}
        params = server_parameters(database, url)
        params.env['XDG_GROUP_ENGINE_EXECUTION'] = json.dumps(context)
        params.env['GROUP_ENGINE_TOKEN'] = auth.execution_token('default', context)
        headers = {'X-Group-Profile': 'default', 'X-Group-Key': params.env['GROUP_ENGINE_TOKEN']}
        results = {'url': url, 'database': database, 'data_root': str(data_root), 'checks': []}
        try:
            await asyncio.to_thread(server.start)
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()

                async def call(name, **arguments):
                    response = await session.call_tool(name, arguments)
                    assert not response.isError, response
                    result = json.loads(response.content[0].text)
                    assert 'error' not in result, result
                    return result

                await call('req_list', project='alpha')
                await asyncio.to_thread(server.stop)

                async def restart_after_offline():
                    await asyncio.sleep(1.2)
                    await asyncio.to_thread(server.start)

                recovery = asyncio.create_task(restart_after_offline())
                started = time.monotonic()
                try:
                    first = await call('req_create', project='alpha', title='Offline recovery', **REQUIREMENT_FIELDS)
                finally:
                    await recovery
                elapsed = time.monotonic() - started
                assert elapsed < 30, elapsed
                results['checks'].append({'offline_recovery_seconds': elapsed, 'requirement_id': first['requirement_id']})

                await asyncio.to_thread(server.start, True)

                async def restart_after_crash():
                    code = await asyncio.to_thread(server.process.wait, 10)
                    assert code == 23, code
                    await asyncio.to_thread(server.start)

                recovery = asyncio.create_task(restart_after_crash())
                try:
                    second = await call('req_create', project='alpha', title='Commit then disconnect', **REQUIREMENT_FIELDS)
                finally:
                    await recovery
                conn = db.get_connection()
                rows = conn.execute('SELECT id,title FROM requirements ORDER BY id').fetchall()
                receipts = conn.execute("SELECT operation_id,response FROM mcp_operations WHERE state='completed'").fetchall()
                conn.close()
                assert len(rows) == 2, [dict(r) for r in rows]
                operation_id = next(r['operation_id'] for r in receipts if json.loads(r['response']) == second)
                receipt = await call('operation_get', operation_id=operation_id)
                assert receipt['state'] == 'completed' and receipt['result'] == second, receipt
                async with httpx.AsyncClient() as client:
                    replay = await client.post(url + '/task-tools/req_create',
                        headers={**headers, 'X-Group-Operation-Id': operation_id},
                        json={'arguments': {'project': 'alpha', 'title': 'Commit then disconnect',
                                            'version': '', 'priority': 0, **REQUIREMENT_FIELDS}, 'execution': context})
                assert replay.status_code == 200 and replay.json() == second, replay.text
                results['checks'].append({'commit_reply_crash_reconciled': True, 'operation_id': operation_id,
                                          'requirement_count': len(rows), 'duplicate_response_identical': True})

                created = await call('task_create', project='alpha', requirement_id=first['requirement_id'],
                    tasks=[{'title': 'Retry workflow', 'next_bot': 'default', 'description': 'Isolated flow',
                            'design_basis': 'MCP transaction contract', 'acceptance_criteria': 'One node per action',
                            'prerequisites': []}], complete=True)
                tid = created['task_ids'][0]
                assert (await call('task_claim', project='alpha'))['task']['id'] == tid
                await call('task_log', project='alpha', task_id=tid, type='dev', next_bot='default',
                           expected_role_id=role_id_of('alpha', 'default'), result='Isolated workflow verified')
                await call('task_complete', project='alpha', task_id=tid, result='Accepted isolated workflow')
                await call('req_complete', project='alpha', requirement_id=first['requirement_id'])
                topic = await call('group_create_thread', title='Atomic topic', members=['architect'], opening='Isolated topic')
                conn = db.get_connection()
                assert conn.execute('SELECT status FROM tasks WHERE id=?', (tid,)).fetchone()[0] == 'done'
                assert conn.execute('SELECT count(*) FROM nodes WHERE task_id=?', (tid,)).fetchone()[0] == 3
                assert conn.execute('SELECT count(*) FROM messages WHERE thread_id=?', (topic['thread_id'],)).fetchone()[0] == 1
                assert conn.execute('SELECT count(*) FROM requirements').fetchone()[0] == 2
                conn.close()
                results['checks'].append({'task_flow': 'done', 'node_count': 3, 'topic_opening_count': 1})
        finally:
            await asyncio.to_thread(server.stop)
        with socket.socket() as check:
            check.bind((target.hostname, target.port))
        results.update(instances=server.instances, resources_released=True, same_stdio_session=True)
        (evidence / 'result.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--data-root')
    parser.add_argument('--evidence')
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--expected-database')
    args = parser.parse_args()
    origin = loopback_url(args.url)
    if args.serve:
        serve(origin, absolute_path(args.expected_database, '--expected-database'))
    else:
        asyncio.run(asyncio.wait_for(verify(origin, absolute_path(args.data_root, '--data-root'),
                                           absolute_path(args.evidence, '--evidence')), timeout=100))
