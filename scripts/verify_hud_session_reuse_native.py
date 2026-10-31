"""Count real HUD AIAgent construction over isolated Hermes WS and a loopback model."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


async def verify(base, provider):
    from websockets.asyncio.server import serve
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection
    from gateway.session_execution import GatewaySessionAgentMixin
    from engine import db, project_hud, hud_gateway, hud_runtime
    from engine import hermes_connections as connections, hermes_gateway as gateway
    from engine.hermes_gateway_client import resume_connections

    home = base / 'home'
    conn = db.get_connection()
    db.init_schema(conn)
    project_hud.schema(conn)
    conn.execute('INSERT INTO hud_sessions(id,activity,updated) VALUES(?,?,?)', ('fixture', 0, 0))
    conn.commit()
    conn.close()
    runner = GatewayRunner()
    authority = await initialize_session_authority(runner, profile_id='fixture', instance_id='hud-probe')
    loop = asyncio.get_running_loop()
    methods, builds, tool_counts = [], [], []

    class Transport:
        def __init__(self, ws): self.ws = ws
        def write(self, frame):
            asyncio.run_coroutine_threadsafe(self.ws.send(json.dumps(frame)), loop)
            return True
        def close(self): pass

    async def handler(ws):
        viewer = AuthorityConnection(authority, Transport(ws), {'user_id': 'fixture-operator'})
        try:
            async for raw in ws:
                frame = json.loads(raw)
                methods.append(frame['method'])
                result = await viewer.dispatch(frame)
                if frame['method'] == 'tools.show' and result and 'result' in result:
                    tool_counts.append(result['result']['total'])
                if result is not None:
                    await ws.send(json.dumps(result))
        finally:
            await viewer.close()

    now = [0]
    resume_connections()
    pool = connections.Pool(revision=lambda: ('fixture-source', 'fixture-sha'),
                            pinned=lambda _: False, clock=lambda: now[0])
    original_build = GatewaySessionAgentMixin._build_fresh_agent

    def build(self, *args, **kwargs):
        agent = original_build(self, *args, **kwargs)
        builds.append(id(agent))
        return agent

    def turn(index):
        with hud_gateway.owner('fixture', 1 if index < 2 else 2):
            result = hud_runtime.respond({'id': 1, 'runtime': 'hermes', 'profile': 'default',
                                          'config_root': str(home)},
                                         {'project': {'description': f'Synthetic snapshot {index + 1}'}})
            assert result == {'text': 'OK'}, result
            return hud_gateway._saved()

    counts = []
    async with serve(handler, '127.0.0.1', 0, subprotocols=['hermes-gateway-v1']) as server:
        endpoint = {'api_origin': 'http://127.0.0.1:' + str(server.sockets[0].getsockname()[1]),
                    'code_sha': 'fixture-sha', 'profile_id': 'fixture', 'instance_id': 'hud-probe',
                    'authority_epoch': authority.epoch, 'runtime_protocol': 1}
        with patch.object(gateway, 'discover', return_value=endpoint), \
             patch('engine.hermes_gateway_client.ticket', return_value='isolated-fixture'), \
             patch.object(gateway, 'enabled', return_value=True), \
             patch.object(gateway, 'connection', side_effect=lambda *a, **kw:
                          (pool.acquire(home, [], callback=kw.get('on_event')), home, [])), \
             patch.object(hud_runtime, '_run', side_effect=AssertionError('Unexpected CLI dispatch')), \
             patch.object(GatewaySessionAgentMixin, '_build_fresh_agent', build):
            try:
                for index in range(4):
                    began = time.monotonic()
                    record = await asyncio.to_thread(turn, index)
                    assert record['terminal'], record
                    counts.append({'turn': index + 1, 'agent_builds': len(builds),
                                   'session_id': record['session_id'], 'admission_id': record['admission_id'],
                                   'elapsed_seconds': round(time.monotonic() - began, 3)})
                assert [r['agent_builds'] for r in counts] == [1, 1, 2, 2], counts
                assert counts[0]['session_id'] == counts[1]['session_id']
                assert counts[2]['session_id'] == counts[3]['session_id'] != counts[0]['session_id']
                assert len({r['admission_id'] for r in counts}) == 4
                assert methods.count('session.create') == 2, methods
                assert tool_counts == [0, 0, 0, 0], tool_counts
                assert len(provider.requests) == 4, provider.requests
                assert all(not request.get('tools') for request in provider.requests)
                for request in provider.requests:
                    system = '\n'.join(m['content'] for m in request['messages']
                                       if m['role'] == 'system' and isinstance(m['content'], str))
                    assert hud_runtime.SYSTEM.strip() in system, 'Dedicated instructions missing from system layer'
                assert 'Synthetic snapshot 2' in json.dumps(provider.requests[1])
                assert not any(m in ('session.mutate', 'session.detach') for m in methods)
                now[0] = 600
                await asyncio.to_thread(pool.sweep)
                assert methods.count('session.detach') == 2, methods
                assert all(entry.client is None for entry in pool.entries.values())
                result = {'passed': True, 'counts': counts, 'tools_total': tool_counts,
                          'model_requests': len(provider.requests), 'idle_detach': True,
                          'rpc_methods': methods,
                          'boundary': 'real Hermes WS/AIAgent and HUD SQLite; loopback model; synthetic ticket'}
                (base / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
                print(json.dumps(result))
            finally:
                (base / 'model-requests.json').write_text(
                    json.dumps(provider.requests, ensure_ascii=False, indent=2), encoding='utf-8')
                pool.stop()
                for entry in pool.entries.values():
                    if entry.client:
                        await asyncio.to_thread(entry.client.close)
                for entry in list(runner._agent_cache.values()):
                    await asyncio.to_thread(entry[0].close)
                authority.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hermes-source', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    if not (args.hermes_source / 'gateway/session_authority.py').is_file():
        parser.error('Existing Hermes source required; use an interpreter with Hermes and engine dependencies')
    base = args.evidence.resolve()
    if base.exists():
        parser.error('Use a new evidence directory')
    base.mkdir(parents=True)
    (base / 'home').mkdir()
    (base / 'work').mkdir()
    allowed = {'path', 'systemroot', 'systemdrive', 'windir', 'comspec', 'pathext', 'temp', 'tmp', 'lang', 'tz'}
    clean = {k: v for k, v in os.environ.items() if k.lower() in allowed}
    os.environ.clear()
    os.environ.update(clean)
    os.environ.update(HERMES_HOME=str(base / 'home'), USERPROFILE=str(base / 'home'), HOME=str(base / 'home'),
                      GROUP_DB=str(base / 'group.db'), GROUP_DATA_DIR=str(base), GROUP_ADMIN_PASSWORD='',
                      HERMES_DISABLE_LAZY_INSTALLS='1', TERMINAL_CWD=str(base / 'work'),
                      NO_PROXY='127.0.0.1,localhost', PYTHONUTF8='1', OPENAI_API_KEY='loopback-only')
    sys.path[:0] = [str(ROOT), str(args.hermes_source.resolve())]
    from scripts.verify_wake_prefix import Provider
    provider = Provider()
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    os.environ['OPENAI_BASE_URL'] = provider.origin + '/v1'
    import yaml
    (base / 'home/config.yaml').write_text(yaml.safe_dump({
        'model': {'default': 'mock-prefix', 'provider': 'custom', 'base_url': provider.origin + '/v1'},
        'updates': {'check': False}, 'models_dev': {'url': provider.origin + '/models'},
        'streaming': {'enabled': False}, 'auxiliary': {'title_generation': {'enabled': False}},
        'skills': {'creation_nudge_interval': 0}, 'memory': {'nudge_interval': 0},
    }), encoding='utf-8')
    try:
        asyncio.run(verify(base, provider))
    finally:
        provider.shutdown()
        provider.server_close()


if __name__ == '__main__':
    main()
