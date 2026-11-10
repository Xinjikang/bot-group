"""Run real HTTP/SSE contention checks on explicitly assigned isolated resources."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
import psutil
import uvicorn
from fastapi import FastAPI

from engine import api, auth, config, db, events, hermes_gateway_ledger as ledger, wake
from scripts.unified_test_support import isolated_database


async def exercise(url, path, headers, admin, waiting, timeout_ms, evidence):
    writer = db.get_connection()
    pending = None
    async with httpx.AsyncClient(base_url=url, timeout=5, trust_env=False) as client:
        health = await client.get('/health', headers=admin)
        assert health.status_code == 200, health.text
        assert Path(health.json()['db_path']).resolve() == Path(path).resolve(), health.text
        evidence['health'] = health.json()
        measurements = []
        async def read(route, credential):
            started = time.monotonic()
            response = await client.get(route, headers=credential)
            elapsed = time.monotonic() - started
            measurements.append({'path': route, 'status': response.status_code, 'seconds': elapsed})
            assert response.status_code == 200, response.text
            assert elapsed < .75, measurements[-1]
        async def wait_for_writer():
            deadline = time.monotonic() + 2
            while not waiting.is_set():
                assert time.monotonic() < deadline, 'Heartbeat did not reach its write transaction'
                await asyncio.sleep(.01)
        try:
            async with client.stream('GET', '/events', headers=admin) as response:
                assert response.status_code == 200
                lines = response.aiter_lines()
                assert await anext(lines) == ': ok'
                writer.execute('BEGIN IMMEDIATE')
                writer.execute("UPDATE members SET display_name='held transaction' WHERE profile='default'")
                waiting.clear()
                started = time.monotonic()
                pending = asyncio.create_task(client.post('/heartbeat', headers=headers, json={'pid': 77}))
                await wait_for_writer()
                await asyncio.gather(read('/health', admin), read('/commands', headers),
                    read('/members?project=alpha', headers), read('/messages?project=alpha', admin))
                published = time.monotonic()
                events.bus.publish('lock.probe', {'phase': 'write-lock-held'})
                async def next_event():
                    async for line in lines:
                        if line.startswith('data: ') and json.loads(line[6:])['type'] == 'lock.probe':
                            return line
                    raise AssertionError('SSE closed before the probe event')
                event = await asyncio.wait_for(next_event(), .75)
                evidence['sse'] = {'seconds': time.monotonic() - published, 'frame': event}
                await asyncio.sleep(.25)
                assert not pending.done(), 'Heartbeat finished while writer still held its lock'
                evidence['lock_held_seconds'] = time.monotonic() - started
                writer.rollback()
                result = await pending
                assert result.status_code == 200 and not result.json().get('idle'), result.text
                evidence['heartbeat_after_release'] = {'status': result.status_code,
                    'seconds': time.monotonic() - started, 'body': result.json()}
            assert writer.execute('SELECT pid FROM wake_runs').fetchone()[0] == 77
            timeout_ms[0] = 150
            writer.execute('BEGIN IMMEDIATE')
            waiting.clear()
            started = time.monotonic()
            pending = asyncio.create_task(client.post('/heartbeat', headers=headers, json={'pid': 88}))
            await wait_for_writer()
            await read('/commands', headers)
            result = await pending
            assert result.status_code == 503 and result.headers.get('retry-after') == '1', result.text
            evidence['heartbeat_timeout'] = {'status': result.status_code, 'seconds': time.monotonic() - started,
                                             'body': result.json()}
            writer.rollback()
            # A zero-timeout writer proves timeout/connection cleanup released all transactions.
            writer.execute('PRAGMA busy_timeout=0')
            writer.execute('BEGIN IMMEDIATE')
            writer.rollback()
            result = await client.post('/heartbeat', headers=headers, json={'pid': 99})
            assert result.status_code == 200, result.text
            assert writer.execute('SELECT pid FROM wake_runs').fetchone()[0] == 99
            evidence['reads_during_lock'] = measurements
            evidence['retry_after_timeout'] = result.json()
        finally:
            writer.rollback()
            writer.close()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True, choices=('127.0.0.1', '::1'))
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('--port must be an assigned unprivileged port')
    if not args.data_dir.is_absolute() or not args.evidence.is_absolute():
        parser.error('--data-dir and --evidence must be absolute')
    if not args.evidence.resolve().is_relative_to(ROOT / 'reports' / 'tests'):
        parser.error('--evidence must be under the project reports/tests directory')
    ignored = subprocess.run(['git', 'check-ignore', str(args.evidence / 'http-contention.json')],
                             cwd=ROOT, capture_output=True)
    if ignored.returncode:
        parser.error('--evidence must be ignored by Git')
    sock = socket.socket(socket.AF_INET6 if ':' in args.host else socket.AF_INET)
    # Reserve the exact assigned listener before creating test data; a conflict is fatal.
    try:
        sock.bind((args.host, args.port))
    except BaseException:
        sock.close()
        raise
    evidence = {'listener': list(sock.getsockname()), 'assigned_data_root': str(args.data_dir),
                'passed': False, 'pid': os.getpid(), 'busy_timeout_ms': [3000, 150]}
    server = thread = None
    previous_temp = tempfile.tempdir
    previous_loop = events._loop
    try:
        args.evidence.mkdir(parents=True, exist_ok=True)
        args.data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='authorize-lock-', dir=args.data_dir) as temporary:
            tempfile.tempdir = temporary
            with isolated_database() as path, \
                 patch.object(auth, 'signing_keys', return_value=(1, {1: b'isolated-lock-probe'})), \
                 patch.dict(os.environ, {'GROUP_ADMIN_PASSWORD': 'isolated-lock-probe'}):
                evidence['database'] = path
                evidence['data_directory'] = str(config.data_dir())
                assert Path(path).resolve().is_relative_to(args.data_dir.resolve())
                conn = db.get_connection()
                started = time.time() - 1
                session = wake.session_name('default', 'work', None, 'alpha')
                try:
                    conn.execute("INSERT INTO wake_runs(session,started_at,project,profile,runtime_session_id) "
                                 "VALUES(?,?,'alpha','default','sid')", (session, started))
                    ledger.prepare(conn, session=session, project='alpha', profile='default', kind='work',
                        thread_id=None, task_id=None, job_id=None, event_floor=0, source='', python='[]',
                        home=Path(path).parent, workdir=Path(path).parent, session_id='sid',
                        profile_id='probe', started_at=started)
                finally:
                    conn.close()
                context = dict(profile='default', project='alpha', thread_id=None,
                               session=session, wake_started_at=started)
                headers = {'X-Group-Profile': 'default', 'X-Group-Key': auth.execution_token('default', context)}
                auth._LOGIN_ATTEMPTS.clear()
                admin = {'Authorization': 'Bearer ' + auth.login('isolated-lock-probe')['token']}
                gaps = []
                @asynccontextmanager
                async def lifespan(app):
                    events.set_loop(asyncio.get_running_loop())
                    async def pulse():
                        last = time.monotonic()
                        while True:
                            await asyncio.sleep(.01)
                            now = time.monotonic()
                            gaps.append(now - last)
                            last = now
                    ticker = asyncio.create_task(pulse())
                    try:
                        yield
                    finally:
                        ticker.cancel()
                        await asyncio.gather(ticker, return_exceptions=True)
                app = FastAPI(lifespan=lifespan)
                app.state.stop_event = threading.Event()
                app.include_router(api.router_api)
                from engine.main import health
                app.add_api_route('/health', health, methods=['GET'])
                waiting = threading.Event()
                timeout_ms = [3000]
                connection_counts = {'opened': 0, 'closed': 0}
                class ObservedConnection(sqlite3.Connection):
                    def close(self):
                        connection_counts['closed'] += 1
                        super().close()
                def connect():
                    conn = sqlite3.connect(path, timeout=timeout_ms[0] / 1000, factory=ObservedConnection)
                    conn.row_factory = sqlite3.Row
                    conn.execute(f'PRAGMA busy_timeout={timeout_ms[0]}')
                    conn.execute('PRAGMA foreign_keys=ON')
                    conn.set_trace_callback(lambda sql: waiting.set() if sql == 'BEGIN IMMEDIATE' else None)
                    connection_counts['opened'] += 1
                    return conn
                with patch.object(db, 'get_connection', side_effect=connect):
                    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level='warning'))
                    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, name='isolated-lock-http')
                    thread.start()
                    try:
                        deadline = time.monotonic() + 10
                        while not server.started:
                            if not thread.is_alive() or time.monotonic() > deadline:
                                raise RuntimeError('Isolated HTTP server failed to start')
                            time.sleep(.01)
                        host = f'[{args.host}]' if ':' in args.host else args.host
                        asyncio.run(exercise(f'http://{host}:{args.port}', path, headers, admin,
                                             waiting, timeout_ms, evidence))
                    finally:
                        app.state.stop_event.set()
                        server.should_exit = True
                        thread.join(10)
                        assert not thread.is_alive(), 'Isolated HTTP server failed to stop'
                    evidence['event_loop'] = {'ticks': len(gaps), 'max_gap_seconds': max(gaps)}
                    assert len(gaps) >= 20 and max(gaps) < .25, evidence['event_loop']
                    evidence['connections'] = connection_counts
                    assert connection_counts['opened'] == connection_counts['closed'], connection_counts
        evidence['temporary_data_removed'] = not Path(temporary).exists()
        evidence['passed'] = True
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None and thread.is_alive():
            thread.join(10)
        sock.close()
        tempfile.tempdir = previous_temp
        events._loop = previous_loop
        evidence['server_stopped'] = thread is None or not thread.is_alive()
        evidence['port_released'] = not any(
            item.status == psutil.CONN_LISTEN and item.laddr.ip == args.host and item.laddr.port == args.port
            for item in psutil.net_connections(kind='tcp'))
        (args.evidence / 'http-contention.json').write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    assert evidence['server_stopped'] and evidence['port_released'], evidence
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
