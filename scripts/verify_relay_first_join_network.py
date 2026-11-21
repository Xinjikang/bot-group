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

from engine import auth, config, db, hermes_session_auth, ip_allowlist, listen_control
from engine import relay, relay_dialer, relay_server, signing_keys, topic_dispatch
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
    obj = plg['listen'].listen_serve(address=lan, port=0, no_proxy=not proxy)
    if proxy:
        stack.callback(plg['mcpproxy'].stop_proxy)
    stack.callback(obj['_server'].stop)
    return obj


def fixture_scope(member, tid, home):
    start = time.time()
    session = f'{member}-alpha-chat-{tid}'
    sid = f'fixture-{tid}'
    with closing(db.get_connection()) as conn, conn:
        assert conn.execute('SELECT turn_profile FROM threads WHERE id=?', (tid,)).fetchone()[0] == member
        bot = conn.execute('SELECT bot_id FROM members WHERE profile=?', (member,)).fetchone()[0]
        conn.execute("INSERT INTO session_bindings(project,bot_id,profile,kind,thread_id,session_id,runtime,generation,title,updated_at) VALUES('alpha',?,?,'chat',?,?,'hermes',0,?,?)",
                     (bot, member, tid, sid, session, db.now_iso()))
        grant = hermes_session_auth.grant(conn, project='alpha', profile=member, bot_id=bot, kind='chat',
                                         thread_id=tid, session=session, home=str(home), command=[sys.executable],
                                         workdir=str(home), model_args=[])
        conn.execute("INSERT INTO hermes_executions(execution_id,session,started_at,project,profile,kind,thread_id,session_id,profile_id,submission_id,state,source,python,home,workdir,submit_deadline,reconcile_deadline) VALUES(?,?,?,'alpha',?,'chat',?,?,?,?,'running','fixture',?,?,?,?,?)",
                     (session, session, start, member, tid, sid, member, session, sys.executable,
                      str(home), str(home), start + 60, start + 120))
        conn.execute("INSERT INTO wake_runs(session,profile,thread_id,runtime_session_id,bot_id,runtime,started_at,project) VALUES(?,?,?,?,?,'hermes',?,'alpha')",
                     (session, member, tid, sid, bot, start))
    return dict(hermes_session_auth.context(grant), credential_type='session')


async def send_mcp(member, uri, home, scope, text):
    env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'TEMP', 'TMP') if k in os.environ}
    env.update(GROUP_ENGINE_URL=uri, XDG_GROUP_ENGINE_URL='', HERMES_HOME=str(home), HERMES_PROFILE=member,
               GROUP_ENGINE_TOKEN=auth.session_token(member, scope), XDG_GROUP_ENGINE_EXECUTION=json.dumps(scope),
               NO_PROXY='*', PYTHONUTF8='1')
    params = StdioServerParameters(command=sys.executable, args=['-B', str(ROOT / 'mcp/server.py')], cwd=str(home), env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
        await client.initialize()
        sent = await client.call_tool('group_send', {'text': text, 'need_reply': False})
        assert not sent.isError, sent
        return json.loads(sent.content[0].text)['message_id']


def mention(api, member, home, uri, label):
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
    scope = fixture_scope(member, tid, home)
    mid = asyncio.run(asyncio.wait_for(send_mcp(member, uri, home, scope, reply), 35))
    with closing(db.get_connection()) as conn:
        row = conn.execute("SELECT id,mentions FROM messages WHERE thread_id=? AND sender='user' AND content=?",
                           (tid, opening)).fetchone()
        assert row and member in json.loads(row['mentions']), (tid, member)
        replies = conn.execute("SELECT id,sender,content,parent_id FROM messages WHERE thread_id=? AND sender_type='bot'",
                               (tid,)).fetchall()
        assert [tuple(r) for r in replies] == [(mid, member, reply, row['id'])], [dict(r) for r in replies]
    return {'thread_id': tid, 'member': member, 'mention_id': row['id'], 'reply_id': mid}


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
        for plg, home in ((pa, ha), (pb, hb)):
            home.mkdir()
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
            cfg = pa['config'].load(SimpleNamespace(extra={'engine_url': issued['engine_url'], 'invite': issued['invite_token']}))
        assert cfg and cfg.member == a and Path(cfg.home) == ha
        stack.enter_context(patch.object(pa['config'], 'plugin_version', return_value=relay_server._engine_release_version() or ''))
        out = importlib.import_module(pa['config'].__package__ + '.relay').RelayClient(cfg)
        out.start()
        stack.callback(out.stop)

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
        evidence['replies'] = []
        evidence['replies'].append(mention(api, a, ha, issued['engine_url'], 'before-a'))
        evidence['replies'].append(mention(api, b, hb, lb['proxy_url'], 'before-b'))
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
        evidence['replies'].append(mention(api, b, hb, lb['proxy_url'], 'after-b'))
        assert relay_server.active_link(b) is orig


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