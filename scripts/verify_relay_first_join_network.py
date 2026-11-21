"""Exercise both first joins and deterministic topic replies over real sockets."""
import argparse
import asyncio
import importlib
import ipaddress
import json
import os
import socket
import sys
import threading
import time
import traceback
from contextlib import ExitStack, closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from engine import auth, config, db, hermes_gateway, ip_allowlist, listen_control, project_models, queue
from engine import relay, relay_dialer, relay_server, signing_keys, topic_dispatch, wake
from scripts.relay_dispatch_fixture import DispatchGateway
from scripts.test_plugin_listen_pairing import _plugin_modules
from scripts.unified_test_support import isolated_database

ROOT = Path(__file__).resolve().parents[1]


def wait(check):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if value := check():
            return value
        time.sleep(0.05)
    raise AssertionError(f'timed out: {check.__name__}')


def result(resp):
    assert resp.is_success, (resp.request.url.path, resp.status_code, resp.text[:240])
    return resp.json()


def host_env(member, home):
    return patch.dict(os.environ, {'HERMES_PROFILE': member, 'HERMES_PROFILE_NAME': member,
                                   'HERMES_HOME': str(home), 'BOT_GROUP_RELAY_MEMBER': 'not-the-host'})


def listener(plg, home, lan, stack, proxy):
    stack.enter_context(patch.object(plg['listen'], 'STATE_DIR', home))
    obj = plg['listen'].listen_serve(address=lan, port=0, no_proxy=not proxy, mcp_python=sys.executable)
    if proxy:
        stack.callback(plg['mcpproxy'].stop_proxy)
    stack.callback(obj['_server'].stop)
    return obj


async def send_mcp(gateway, received, expected_url, text):
    spec = gateway.sessions[received['session_id']]['mcp']
    configured = {entry['name']: entry['value'] for entry in spec['env']}
    assert configured['GROUP_ENGINE_URL'] == expected_url
    assert Path(spec['command']).samefile(sys.executable) and Path(spec['args'][0]).samefile(ROOT / 'mcp/server.py')
    env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'TEMP', 'TMP') if k in os.environ}
    env.update(configured, NO_PROXY='*', PYTHONUTF8='1', HERMES_HOME=str(gateway.home))
    params = StdioServerParameters(command=spec['command'], args=['-B', *spec['args']], cwd=str(gateway.home), env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
        await client.initialize()
        sent = await client.call_tool('group_send', {'text': text, 'need_reply': False})
        assert not sent.isError, 'configured MCP did not send the reply'
        return json.loads(sent.content[0].text)['message_id']


def mention(api, gateway, other, uri, label):
    member = gateway.member
    opening, reply = f'@{member} check {label}', f'@user reply {label}'
    tid = result(api.post('/thread', json={'creator': 'user', 'project': 'alpha', 'title': label,
                                           'opening': 'Isolated relay check', 'members': []}))['thread_id']
    with closing(db.get_connection()) as conn:
        for _ in range(20):
            if conn.execute('SELECT status FROM threads WHERE id=?', (tid,)).fetchone()[0] == 'settled':
                break
            assert topic_dispatch.process_one(conn), 'initial empty topic must settle'
        else:
            raise AssertionError('initial empty topic was not settled')
    result(api.post(f'/thread/{tid}/members', json={'add': [member]}))
    named = result(api.post('/msg', json={'sender': 'user', 'project': 'alpha', 'thread_id': tid,
                                         'text': opening, 'need_reply': True}))
    assert named['mentions'] == [member], named
    with closing(db.get_connection()) as conn:
        for _ in range(20):
            turn = conn.execute('SELECT turn_profile,turn_parent_id FROM threads WHERE id=?', (tid,)).fetchone()
            if tuple(turn) == (member, named['message_id']):
                break
            assert topic_dispatch.process_one(conn), (tid, member, tuple(turn))
        else:
            raise AssertionError('mention did not assign the reply turn')
    before, before_other = len(gateway.submit_requests), len(other.submit_requests)
    with closing(db.get_connection()) as conn:
        job = conn.execute('SELECT * FROM wake_jobs WHERE trigger_msg_id=?', (named['message_id'],)).fetchone()
        assert job and (job['profile'], job['status']) == (member, 'queued')
        for _ in range(10):
            if not queue.process_one(conn):
                break
            status = conn.execute('SELECT status, detail FROM wake_jobs WHERE id=?', (job['id'],)).fetchone()
            if status['status'] != 'queued':
                break
        status = conn.execute('SELECT status, detail FROM wake_jobs WHERE id=?', (job['id'],)).fetchone()
        assert status['status'] == 'done', (member, dict(status), gateway.errors)
        execution = conn.execute('SELECT * FROM hermes_executions WHERE job_id=?', (job['id'],)).fetchone()
        assert execution and execution['state'] == 'running', (member, gateway.errors)
    assert len(gateway.submit_requests) == before + 1 and len(other.submit_requests) == before_other
    received = gateway.submissions[-1]
    assert tuple(received[k] for k in ('session_id', 'submission_id', 'admission_id')) == tuple(
        execution[k] for k in ('session_id', 'submission_id', 'admission_id'))
    assert opening in received['text'] and execution['parent_id'] == named['message_id']
    mid = asyncio.run(asyncio.wait_for(send_mcp(gateway, received, uri, reply), 35))
    # This is a one-answer experiment. The administrator closes the topic after
    # the reply, before releasing the runner to schedule ordinary discussion.
    assert result(api.post(f'/thread/{tid}/close', json={'conclusion': 'Isolated reply observed'}))['closed']
    gateway.settled(received)

    def settled():
        with closing(db.get_connection()) as conn:
            row = conn.execute('SELECT state,cleanup_pending FROM hermes_executions WHERE job_id=?', (job['id'],)).fetchone()
            return tuple(row) == ('terminal', 0)

    wait(settled)
    with closing(db.get_connection()) as conn:
        row = conn.execute("SELECT id,mentions FROM messages WHERE thread_id=? AND sender='user' AND content=?",
                           (tid, opening)).fetchone()
        assert row and member in json.loads(row['mentions']), (tid, member)
        replies = conn.execute("SELECT id,sender,content,parent_id FROM messages WHERE thread_id=? AND sender_type='bot'",
                               (tid,)).fetchall()
        assert [tuple(r) for r in replies] == [(mid, member, reply, row['id'])], [dict(r) for r in replies]
    assert len(gateway.submit_requests) == before + 1 and len(other.submit_requests) == before_other
    return {'thread_id': tid, 'member': member, 'mention_id': row['id'], 'reply_id': mid,
            'job_id': job['id'], 'job_status': status['status'], 'session_id': received['session_id'],
            'submission_id': received['submission_id'], 'remote_admission_id': received['admission_id'],
            'target_receives': 1, 'non_target_receives': 0}


def exercise(args, evidence):
    from engine.main import app
    with isolated_database() as path, ExitStack() as stack:
        stack.enter_context(patch.object(config, 'port', return_value=args.port))
        stack.enter_context(patch.object(config, 'host', return_value='127.0.0.1'))
        stack.enter_context(patch.object(auth, 'configured', return_value=False))
        with closing(db.get_connection()) as conn:
            signing_keys.initialize(conn)
        sock = stack.enter_context(socket.socket())
        sock.bind(('127.0.0.1', args.port))
        server = uvicorn.Server(uvicorn.Config(app, lifespan='off', proxy_headers=False,
                                               log_level='warning', timeout_graceful_shutdown=2))
        thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()

        def stop_engine():
            server.should_exit = True
            thread.join(8)
            assert not thread.is_alive()

        stack.callback(stop_engine)
        wait(lambda: server.started)
        api = stack.enter_context(httpx.Client(base_url=f'http://127.0.0.1:{args.port}', timeout=25, trust_env=False))
        remote = stack.enter_context(httpx.Client(base_url=f'http://{args.lan}:{args.port}', timeout=5, trust_env=False))
        a, b = 'fixture-cloud-a', 'fixture-cloud-b'
        pa, pb = _plugin_modules('first_join_a'), _plugin_modules('first_join_b')
        ha, hb = Path(path).parent / 'host-a', Path(path).parent / 'host-b'
        gateway_a = stack.enter_context(DispatchGateway(a, ha).listen())
        gateway_b = stack.enter_context(DispatchGateway(b, hb).listen())
        stack.enter_context(patch.object(hermes_gateway, 'enabled', return_value=True))
        stack.enter_context(patch.object(project_models, 'arguments', return_value=[]))
        (Path(path).parent / 'alpha').mkdir()
        for plg, home, gateway in ((pa, ha, gateway_a), (pb, hb, gateway_b)):
            home.mkdir()
            tickets = importlib.import_module(plg['config'].__package__ + '.tickets')
            stack.enter_context(patch.object(tickets, 'fetch_ticket', side_effect=gateway.fetch_ticket))
            stack.enter_context(patch.object(plg['mcp_digest'], 'MCP_DIR', ROOT / 'mcp'))
            stack.enter_context(patch.object(plg['pairing'], '_state_path', return_value=home / 'pairing.json'))
        with closing(db.get_connection()) as conn:
            assert not relay.list_nodes(conn) and not ip_allowlist.list_bindings(conn)
            matches = {r[0] for r in conn.execute('SELECT profile FROM members UNION SELECT profile FROM bots')} & {a, b}
            assert not matches, matches
            evidence['local_roster_matches'] = sorted(matches)
        with host_env(b, hb):
            host_b = pb['config'].host_member()
            lb = listener(pb, hb, args.lan, stack, True)
        url = lb['pairing_url']
        announced = parse_qs(urlsplit(url).query)['member'][0]
        assert result(api.post('/relay/pairings/preview', json={'pairing_url': url}))['member'] == announced == host_b == b
        assert result(api.get('/relay/nodes')) == []
        imported = result(api.post('/relay/pairings/import', json={'pairing_url': url}))
        stack.callback(relay_dialer.stop_dial, b)
        assert imported['member'] == b and imported['imported']
        assert not result(api.get('/admin/listen/status'))['remote_enabled']

        status = result(api.post('/admin/listen', json={'address': args.lan}))
        stack.callback(listen_control.ensure, None)
        assert status['remote_enabled'] and status['remote_address'] == args.lan, status
        assert remote.get('/health').status_code == 403
        issued = result(api.post('/relay/invitations'))
        assert issued['listen_configured'] and issued['engine_url'] == str(remote.base_url).rstrip('/')
        with host_env(a, ha):
            cfg = pa['config'].load(SimpleNamespace(extra={'engine_url': issued['engine_url'], 'invite': issued['invite_token'],
                                                           'mcp_python': sys.executable}))
        assert cfg and cfg.member == a and Path(cfg.home) == ha
        stack.enter_context(patch.object(pa['config'], 'plugin_version', return_value=relay_server._engine_release_version() or ''))
        out = importlib.import_module(pa['config'].__package__ + '.relay').RelayClient(cfg)
        out.start()
        stack.callback(out.stop)

        invitation = f"/relay/invitations/{issued['pairing_id']}"

        def awaiting_confirmation():
            view = result(api.get(invitation))
            return view if view['state'] == 'pending_confirmation' else None

        pending = wait(awaiting_confirmation)
        assert (pending['member'], pending['peer_ip']) == (a, args.lan)
        assert set(pending) == {'pairing_id', 'state', 'direction', 'member', 'peer_ip', 'expires_at'}
        assert {node['member'] for node in result(api.get('/relay/nodes'))} == {b}
        assert relay_server.active_link(a) is None and not out.generation
        with closing(db.get_connection()) as conn:
            assert not ip_allowlist.list_bindings(conn)
        assert remote.get('/health').status_code == 403
        confirmed = result(api.post(invitation + '/confirm'))
        assert confirmed['state'] == 'confirmed' and confirmed['member'] == a
        assert result(api.post(invitation + '/confirm'))['state'] == 'confirmed'
        assert api.post(invitation + '/cancel').status_code == 409
        evidence['pending_confirmation'] = pending

        def online():
            nodes = {n['member']: n for n in result(api.get('/relay/nodes'))}
            return nodes if set(nodes) == {a, b} and all(n['state'] == 'online' for n in nodes.values()) else None

        both = wait(online)
        wait(lambda: out.generation)
        assert both[a]['direction'] == 'listen' and both[b]['direction'] == 'dial'
        with closing(db.get_connection()) as conn, conn:
            assert ip_allowlist.list_bindings(conn) == [{'member': a, 'ips': [args.lan]}]
            redeemed = conn.execute('SELECT redeemed,redeemed_by FROM relay_invitations').fetchone()
            assert tuple(redeemed) == (1, a) and both[a]['peer_ip'] == args.lan
            evidence['first_join'] = {'a': {'host': cfg.member, 'received': redeemed['redeemed_by'], 'node': both[a]['member'],
                                             'peer': both[a]['peer_ip']},
                                      'b': {'host': host_b, 'link': announced, 'authenticated': imported['member'],
                                            'node': both[b]['member']}}
            for member in (a, b):
                bot = db._ensure_hermes_bot(conn, member)
                conn.execute('INSERT INTO members(profile,display_name,bot_id) VALUES(?,?,?)', (member, member, bot))
                conn.execute("INSERT INTO project_members(project,profile,active,bot_id) VALUES('alpha',?,1,?)", (member, bot))
        stopping = threading.Event()
        stack.enter_context(patch.object(wake, '_shutting_down', stopping))
        known_threads = set(threading.enumerate())

        def stop_workers():
            stopping.set()
            for worker in set(threading.enumerate()) - known_threads:
                if worker.name == 'hermes-reconcile':
                    worker.join(timeout=10)
                    assert not worker.is_alive(), 'isolated engine worker did not stop'

        stack.callback(stop_workers)
        evidence['replies'] = []
        evidence['replies'].append(mention(api, gateway_a, gateway_b, issued['engine_url'], 'before-a'))
        evidence['replies'].append(mention(api, gateway_b, gateway_a, lb['proxy_url'], 'before-b'))
        orig = relay_server.active_link(b)
        assert orig is not None
        with host_env(a, ha):
            reverse = listener(pa, ha, args.lan, stack, False)
        link = urlsplit(reverse['pairing_url'])
        fields = parse_qs(link.query)
        result(api.post(f'/relay/nodes/{a}/dial-endpoint', json={'host': link.hostname, 'port': link.port,
               'tls': True, 'fingerprint': fields['fingerprint'][0], 'pairing_token': fields['pairing_token'][0]}))
        result(api.post(f'/relay/nodes/{a}/switch', json={'direction': 'dial'}))
        stack.callback(relay_dialer.stop_dial, a)

        def switched():
            nodes = online()
            return nodes if nodes and nodes[a]['direction'] == 'dial' and not nodes[a]['switch_state'] else None

        after = wait(switched)
        assert after[b]['direction'] == 'dial' and relay_server.active_link(b) is orig
        evidence['switch'] = {m: {k: after[m][k] for k in ('direction', 'state', 'generation')} for m in (a, b)}
        evidence['replies'].append(mention(api, gateway_b, gateway_a, lb['proxy_url'], 'after-b'))
        assert relay_server.active_link(b) is orig
        evidence['actual_submissions'] = {a: len(gateway_a.submit_requests), b: len(gateway_b.submit_requests)}
        assert evidence['actual_submissions'] == {a: 1, b: 2}
        assert not gateway_a.errors and not gateway_b.errors, (gateway_a.errors, gateway_b.errors)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lan', required=True)
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--evidence', required=True, type=Path)
    parser.add_argument('--hermes-source', type=Path)
    args = parser.parse_args()
    address = ipaddress.IPv4Address(args.lan)
    if address.is_loopback or address.is_unspecified or not (address.is_private or address in ipaddress.ip_network('100.64.0.0/10')):
        parser.error('--lan requires a private non-loopback address')
    if args.lan not in {n['address'] for n in listen_control.enumerate_interfaces()} or not args.evidence.is_absolute():
        parser.error('select an actual local --lan address and an absolute --evidence path')
    if args.hermes_source:
        sys.path.append(str(args.hermes_source))
        from hermes_cli.profiles import current_profile_name
        assert callable(current_profile_name)
    args.evidence.mkdir(parents=True, exist_ok=True)
    doc = {'failures': []}
    old_hook = threading.excepthook

    def thread_error(args):
        doc['failures'].append(f'{args.thread.name}: {args.exc_type.__name__}')
        old_hook(args)

    threading.excepthook = thread_error
    try:
        exercise(args, doc)
    except Exception as exc:
        doc['failures'].append(f'{type(exc).__name__}: {exc}')
        traceback.print_exc()
    finally:
        threading.excepthook = old_hook
        output = args.evidence / 'first-join-evidence.json'
        output.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"{'FAIL' if doc['failures'] else 'OK'}: {output}")
    return int(bool(doc['failures']))


if __name__ == '__main__':
    sys.exit(main())