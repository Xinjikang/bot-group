"""Real Hermes approval waiters and RPC handlers through an isolated loopback WS.

Run with a Hermes dependency interpreter and explicit source/evidence paths.
The transport uses a synthetic local ticket; no model calls or commands execute.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


async def verify(base):
    from websockets.asyncio.server import serve
    from gateway.session_authority import LiveSession, SessionAuthority
    from gateway.session_controls import AuthorityConnection
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch, admit_session_input, claim_session_input
    from tools.approval import resolve_gateway_approval
    from tools.approval_gateway_wait import _await_gateway_decision
    from engine import db, hermes_approvals as approvals, hermes_gateway_ledger as ledger
    from engine.hermes_gateway_client import GatewayClient

    loop = asyncio.get_running_loop()
    native_db = SessionDB(base / 'native.db')
    native_db.create_session('approval-native', source='test')
    epoch = begin_runtime_epoch(native_db, instance_id='isolated-approval')
    admit_session_input(native_db, epoch=epoch, principal_id='operator', session_id='approval-native', request_id='turn', payload={'text': 'fixture'})
    execution = claim_session_input(native_db, epoch=epoch, session_id='approval-native')
    generation = execution['generation']
    authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id=str(base / 'home'),
                                 instance_id='isolated-approval', db=native_db, epoch=epoch)
    live = authority.sessions['approval-native'] = LiveSession(None, 'isolated-route')

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
                result = await viewer.dispatch(json.loads(raw))
                if result is not None:
                    await ws.send(json.dumps(result))
        finally:
            await viewer.close()

    conn = db.get_connection(); db.init_schema(conn)
    row = ledger.prepare(conn, session='native-fixture', project='fixture', profile='default', kind='work',
        thread_id=None, task_id=None, job_id=None, event_floor=0, source='', python='[]', home=str(base / 'home'),
        workdir=str(base), session_id='approval-native', profile_id=str(base / 'home'), started_at=time.time())
    conn.execute("UPDATE hermes_executions SET state='running', generation=? WHERE execution_id=?", (generation, row['execution_id']))
    conn.commit(); row = ledger.read(conn, row['execution_id'])
    tasks = []
    native_responses = []
    def notify(data):
        live.controls.register('approval-native', live.route, generation, data)
        return True

    def sync_snapshot(row, client):
        worker_conn = db.get_connection()
        try:
            approvals.sync(worker_conn, row, client)
        finally:
            worker_conn.close()

    async with serve(handler, '127.0.0.1', 0, subprotocols=['hermes-gateway-v1']) as server:
        endpoint = {'api_origin': 'http://127.0.0.1:' + str(server.sockets[0].getsockname()[1])}
        def connect(*args, **kwargs): return GatewayClient(endpoint), None, None
        with patch('engine.hermes_gateway_client.ticket', return_value='isolated-fixture'), \
             patch('engine.hermes_gateway.connection', side_effect=connect):
            try:
                for index, choice in enumerate(('once', 'deny', 'native-deny')):
                    tasks.append(asyncio.create_task(asyncio.to_thread(_await_gateway_decision, live.route, notify,
                        {'command': 'echo synthetic-' + str(index), 'description': '合成审批，无命令执行',
                         'allow_session': False, 'allow_permanent': False, 'pattern_keys': [str(index)]})))
                    client, _, _ = await asyncio.to_thread(connect)
                    try:
                        deadline = time.monotonic() + 15
                        while True:
                            await asyncio.to_thread(sync_snapshot, row, client)
                            pending = approvals.listing()['approvals']
                            if pending: break
                            if time.monotonic() >= deadline: raise RuntimeError('Native approval did not reach bot-group')
                            await asyncio.sleep(.1)
                        prompt = pending[0]
                        if choice == 'native-deny':
                            resolve_gateway_approval(live.route, 'deny', request_id=prompt['prompt_id'])
                        result = await asyncio.to_thread(approvals.respond, row['execution_id'], prompt['prompt_id'], generation,
                                                         'once' if choice == 'native-deny' else choice)
                        expected = 'already_resolved' if choice == 'native-deny' else 'resolved'
                        assert result['status'] == expected, result
                        answer = await asyncio.wait_for(tasks[-1], 5)
                        assert answer['choice'] == ('deny' if choice == 'native-deny' else choice), answer
                        duplicate = await asyncio.to_thread(approvals.respond, row['execution_id'], prompt['prompt_id'], generation, 'once')
                        assert duplicate['status'] == 'already_resolved'
                        assert approvals.listing()['approvals'] == []
                        native_responses.append({'choice': choice, 'result': result, 'waiter': answer['choice']})
                    finally:
                        await asyncio.to_thread(client.close)
            finally:
                resolve_gateway_approval(live.route, 'deny', resolve_all=True)
                await asyncio.gather(*tasks, return_exceptions=True)
    conn.close(); native_db.close()
    (base / 'result.json').write_text(json.dumps({'passed': True, 'checks': native_responses,
        'boundary': 'real Hermes native waiters and RPC handlers; loopback WS with synthetic transport ticket; no model or command execution'},
        ensure_ascii=False, indent=2), encoding='utf-8')
    print('PASS real Hermes waiters -> WS snapshot -> bot-group decision -> native settlement and duplicate handling')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--hermes-source', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    source, base = args.hermes_source.resolve(), args.evidence.resolve()
    if not (source / 'gateway/session_authority.py').is_file(): parser.error('Hermes source is required')
    if base.exists(): parser.error('Use a new evidence directory')
    base.mkdir(parents=True); (base / 'home').mkdir()
    for key in list(os.environ):
        if key.startswith(('GROUP_', 'XDG_GROUP_', 'HERMES_', 'CODEX_')): os.environ.pop(key)
    os.environ.update(HERMES_HOME=str(base / 'home'), HERMES_DISABLE_LAZY_INSTALLS='1',
                      GROUP_DB=str(base / 'group.db'), GROUP_DATA_DIR=str(base), GROUP_ADMIN_PASSWORD='')
    (base / 'home/config.yaml').write_text('approvals:\n  mode: manual\n  timeout: 20\n', encoding='utf-8')
    sys.path[:0] = [str(ROOT), str(source)]
    asyncio.run(verify(base))


if __name__ == '__main__':
    main()
