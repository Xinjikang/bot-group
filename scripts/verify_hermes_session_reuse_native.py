"""Real Hermes WS admissions, AIAgent cache and MCP process reuse against a loopback model."""
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


async def verify(args, base, provider, api_url):
    from websockets.asyncio.server import serve
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection
    from gateway.session_execution import GatewaySessionAgentMixin
    from engine import db, wake, hermes_connections as connections, hermes_gateway as gateway
    from engine import hermes_gateway_ledger as ledger, hermes_session_auth as grants, signing_keys
    from engine.hermes_gateway_client import resume_connections

    home = base / 'home'; work = base / 'work'
    conn = db.get_connection()
    db.init_schema(conn)
    conn.execute("INSERT INTO projects(name,root,created_at) VALUES('probe',?,?)", (str(work), db.now_iso()))
    conn.execute("INSERT INTO members(profile,display_name,role) VALUES('fixture','fixture','dev')")
    bot_id = db._ensure_hermes_bot(conn, 'fixture')
    conn.execute("INSERT INTO project_members(project,profile,bot_id,active) VALUES('probe','fixture',?,1)", (bot_id,))
    conn.commit(); signing_keys.initialize(conn); conn.close()
    wake._MCP_PYTHON = str(args.mcp_python.resolve())
    runner = GatewayRunner()
    authority = await initialize_session_authority(runner, profile_id='fixture', instance_id='reuse-probe')
    viewers, methods, builds = [], [], []
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
                methods.append((frame['method'], frame.get('params', {}).get('operation')))
                result = await viewer.dispatch(frame)
                if result is not None: await ws.send(json.dumps(result))
        finally:
            await viewer.close()

    resume_connections()
    now = [0]
    pool = connections.Pool(revision=lambda: ('fixture-source', 'fixture-sha'),
                            pinned=lambda _: False, clock=lambda: now[0])
    original_config = wake._mcp_server_config
    wrapper = base / 'mcp_entry.py'
    wrapper.write_text('import os, runpy, sys\nfrom pathlib import Path\n'
        'with Path(sys.argv[1]).open("a") as f: f.write(str(os.getpid()) + "\\n")\n'
        'runpy.run_path(sys.argv[2], run_name="__main__")\n', encoding='utf-8')

    def mcp_config(*a, **kw):
        server = original_config(*a, **kw)
        server['args'] = [str(wrapper), str(base / 'mcp-starts.txt'), str(ROOT / 'mcp/server.py')]
        return server

    original_build = GatewaySessionAgentMixin._build_fresh_agent
    def build(self, *a, **kw):
        agent = original_build(self, *a, **kw)
        builds.append(id(agent))
        return agent

    def settle():
        connection = db.get_connection()
        try:
            row = connection.execute('SELECT * FROM hermes_executions ORDER BY started_at DESC LIMIT 1').fetchone()
            with pool.acquire(home, []) as client:
                row = ledger.reconcile(client, connection, row['execution_id'])
                if row['state'] == 'terminal':
                    assert row['outcome'] == 'completed', dict(row)
                    gateway._finish(connection, row, client)
            return row['state'] == 'terminal'
        finally:
            connection.close()

    counts = []
    async with serve(handler, '127.0.0.1', 0, subprotocols=['hermes-gateway-v1']) as server:
        endpoint = {'api_origin': 'http://127.0.0.1:' + str(server.sockets[0].getsockname()[1]),
                    'code_sha': 'fixture-sha', 'profile_id': 'fixture', 'instance_id': 'reuse-probe',
                    'authority_epoch': authority.epoch, 'runtime_protocol': 1}
        with patch.object(gateway, 'discover', return_value=endpoint), \
             patch('engine.hermes_gateway_client.ticket', return_value='isolated-fixture'), \
             patch.object(gateway, 'connection', side_effect=lambda *a, **kw: (pool.acquire(home, []), home, [])), \
             patch.object(gateway, '_start_worker'), patch.object(gateway, '_publish'), \
             patch.object(wake, '_handle_task_exit'), patch.object(wake, 'wake_system_prompt', return_value='Reply OK.'), \
             patch.object(wake, '_mcp_server_config', side_effect=mcp_config), \
             patch.object(wake, '_engine_url', return_value=api_url), \
             patch.object(GatewaySessionAgentMixin, '_build_fresh_agent', build):
            try:
                for index in range(4):
                    if index == 2:
                        def rotate():
                            connection = db.get_connection()
                            try:
                                signing_keys.initialize(connection)
                            finally:
                                connection.close()
                            grants.maintain()
                        await asyncio.to_thread(rotate)
                    began = time.monotonic()
                    result = await asyncio.to_thread(gateway.spawn, 'fixture', 'work', f'Fixture turn {index}', None,
                        'probe', bot={'id': bot_id}, workdir=str(work), model_args=[])
                    assert result['ok'], result
                    deadline = time.monotonic() + 60
                    while not await asyncio.to_thread(settle):
                        assert time.monotonic() < deadline, 'Native admission timed out'
                        await asyncio.sleep(.05)
                    starts = (base / 'mcp-starts.txt').read_text().splitlines()
                    counts.append({'turn': index + 1, 'agent_builds': len(builds), 'mcp_starts': len(starts),
                                   'elapsed_seconds': round(time.monotonic()-began, 3)})
                assert [(r['agent_builds'], r['mcp_starts']) for r in counts] == [(1,1),(1,1),(2,2),(2,2)], counts
                assert sum(method == 'session.mutate' and op == 'editor_mcp' for method, op in methods) == 2
                assert not any(method == 'session.detach' for method, _ in methods)
                now[0] = 600
                await asyncio.to_thread(pool.sweep)
                assert any(method == 'session.detach' for method, _ in methods)
                assert all(entry.client is None for entry in pool.entries.values())
                result = {'passed': True, 'counts': counts, 'rpc_methods': methods,
                          'model_requests': len(provider.requests), 'idle_detach': True,
                          'boundary': 'real Hermes GatewayRunner, WS, AIAgent and stdio MCP; isolated SQLite/home; loopback model; synthetic ticket'}
                (base / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
                print(json.dumps({k:v for k,v in result.items() if k != 'rpc_methods'}))
            finally:
                pool.stop()
                for entry in pool.entries.values():
                    if entry.client: await asyncio.to_thread(entry.client.close)
                from tools.mcp_tool_lifecycle import shutdown_mcp_servers
                await asyncio.to_thread(shutdown_mcp_servers)
                for entry in list(runner._agent_cache.values()):
                    await asyncio.to_thread(entry[0].close)
                authority.db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hermes-source', type=Path, required=True)
    parser.add_argument('--mcp-python', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    if not (args.hermes_source / 'gateway/session_authority.py').is_file() or not args.mcp_python.is_file():
        parser.error('Existing Hermes source and MCP interpreter required')
    base = args.evidence.resolve()
    if base.exists(): parser.error('Use a new evidence directory')
    base.mkdir(parents=True); (base / 'home').mkdir(); (base / 'work').mkdir()
    allowed = {'path','systemroot','systemdrive','windir','comspec','pathext','temp','tmp','lang','tz'}
    clean = {k:v for k,v in os.environ.items() if k.lower() in allowed}
    os.environ.clear(); os.environ.update(clean)
    os.environ.update(HERMES_HOME=str(base / 'home'), USERPROFILE=str(base / 'home'), HOME=str(base / 'home'),
                      GROUP_DB=str(base / 'group.db'), GROUP_DATA_DIR=str(base), GROUP_ADMIN_PASSWORD='',
                      HERMES_DISABLE_LAZY_INSTALLS='1', TERMINAL_CWD=str(base / 'work'),
                      NO_PROXY='127.0.0.1,localhost', PYTHONUTF8='1', OPENAI_API_KEY='loopback-only')
    sys.path[:0] = [str(ROOT), str(args.hermes_source.resolve())]
    from scripts.verify_wake_prefix import Provider
    provider = Provider(); thread = threading.Thread(target=provider.serve_forever, daemon=True); thread.start()
    os.environ['OPENAI_BASE_URL'] = provider.origin + '/v1'
    import yaml
    (base / 'home/config.yaml').write_text(yaml.safe_dump({
        'model': {'default': 'mock-prefix', 'provider': 'custom', 'base_url': provider.origin + '/v1'},
        'updates': {'check': False}, 'models_dev': {'url': provider.origin + '/models'},
        'streaming': {'enabled': False}, 'auxiliary': {'title_generation': {'enabled': False}},
        'skills': {'creation_nudge_interval': 0}, 'memory': {'nudge_interval': 0},
    }), encoding='utf-8')
    try:
        from scripts.unified_test_support import isolated_api
        with isolated_api() as api_url:
            asyncio.run(verify(args, base, provider, api_url))
    finally:
        provider.shutdown(); provider.server_close()


if __name__ == '__main__':
    main()
