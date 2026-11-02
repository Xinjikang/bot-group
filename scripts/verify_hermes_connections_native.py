"""Native resume/detach over pooled loopback WS; synthetic ticket, zero model calls."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


async def verify(base):
    from websockets.asyncio.server import serve
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch
    from engine import hermes_connections as connections, hermes_gateway as gateway
    from engine.hermes_gateway_client import resume_connections

    home = base/'home'
    native_db = SessionDB(base/'native.db')
    native_db.create_session('connection-fixture', source='test')
    epoch = begin_runtime_epoch(native_db, instance_id='isolated-connections')
    authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id=str(home),
                                 instance_id='isolated-connections', db=native_db, epoch=epoch)
    authority.sessions['connection-fixture'] = LiveSession(None, 'isolated-route')
    viewers, methods = [], []
    loop = asyncio.get_running_loop()

    class Transport:
        def __init__(self, ws): self.ws = ws
        def write(self, frame):
            asyncio.run_coroutine_threadsafe(self.ws.send(json.dumps(frame)), loop)
            return True
        def close(self): pass

    async def handler(ws):
        viewer = AuthorityConnection(authority, Transport(ws), {'user_id': 'fixture-operator'})
        viewers.append(viewer)
        try:
            async for raw in ws:
                frame = json.loads(raw)
                methods.append(frame['method'])
                result = await viewer.dispatch(frame)
                if result is not None: await ws.send(json.dumps(result))
        finally:
            await viewer.close()

    resume_connections()
    now = [0]
    pool = connections.Pool(revision=lambda: ('fixture-source', 'fixture-sha'),
                            pinned=lambda _: False, clock=lambda: now[0])
    checks = []
    async with serve(handler, '127.0.0.1', 0, subprotocols=['hermes-gateway-v1']) as server:
        endpoint = {'api_origin': 'http://127.0.0.1:' + str(server.sockets[0].getsockname()[1]),
                    'code_sha': 'fixture-sha', 'profile_id': str(home), 'instance_id': 'isolated-connections',
                    'authority_epoch': epoch+7, 'runtime_protocol': 1}
        with patch.object(gateway, 'discover', return_value=endpoint), \
             patch.object(gateway, 'ensure', side_effect=AssertionError('unexpected CLI startup')), \
             patch('engine.hermes_gateway_client.ticket', return_value='isolated-fixture'):
            try:
                first = await asyncio.to_thread(pool.acquire, home, [])
                second = await asyncio.to_thread(pool.acquire, home, [])
                assert first.client is second.client and len(viewers) == 1
                checks.append('one authenticated WS for concurrent leases; native runtime.describe')
                await asyncio.to_thread(first.rpc, 'session.resume', session_id='connection-fixture', omit_messages=True)
                await asyncio.to_thread(second.rpc, 'session.resume', session_id='connection-fixture', omit_messages=True)
                await asyncio.to_thread(first.close)
                assert 'connection-fixture' in viewers[0].subscriptions
                checks.append('first lease release preserves shared native subscription')
                await asyncio.to_thread(second.close)
                assert 'connection-fixture' in viewers[0].subscriptions and not first.client.closed
                checks.append('last lease release preserves native subscription and WS')
                third = await asyncio.to_thread(pool.acquire, home, [])
                await asyncio.to_thread(third.rpc, 'session.resume', session_id='connection-fixture', omit_messages=True)
                assert third.client is first.client and 'connection-fixture' in viewers[0].subscriptions
                await asyncio.to_thread(third.close)
                checks.append('next lease resumes retained subscription on the same WS')
                now[0] = 600
                await asyncio.to_thread(pool.sweep)
                assert first.client.closed and pool.status()['state'] == 'waiting'
                assert 'session.detach' in methods
                checks.append('600 seconds business idle closes WS and reports waiting')
            finally:
                pool.stop()
                for entry in pool.entries.values():
                    if entry.client: await asyncio.to_thread(entry.client.close)
    native_db.close()
    result = {'passed': True, 'checks': checks, 'rpc_methods': methods,
              'boundary': 'real Hermes authority and RPC handlers; isolated database and loopback WS; synthetic control identity and ticket; zero model calls'}
    (base/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('PASS ' + str(len(checks)) + ' native connection checks')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hermes-source', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    source, base = args.hermes_source.resolve(), args.evidence.resolve()
    if not (source/'gateway/session_authority.py').is_file(): parser.error('Hermes source is required')
    if base.exists(): parser.error('Use a new evidence directory')
    base.mkdir(parents=True); (base/'home').mkdir()
    for key in list(os.environ):
        if key.startswith(('GROUP_', 'XDG_GROUP_', 'HERMES_', 'CODEX_')): os.environ.pop(key)
    os.environ.update(HERMES_HOME=str(base/'home'), HERMES_DISABLE_LAZY_INSTALLS='1',
                      GROUP_DB=str(base/'group.db'), GROUP_DATA_DIR=str(base), GROUP_ADMIN_PASSWORD='')
    (base/'home/config.yaml').write_text('{}\n', encoding='utf-8')
    sys.path[:0] = [str(ROOT), str(source)]
    asyncio.run(verify(base))


if __name__ == '__main__':
    main()
