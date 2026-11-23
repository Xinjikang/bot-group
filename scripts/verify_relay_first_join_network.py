"""Exercise both first joins and deterministic topic replies over real sockets."""
import argparse
import asyncio
import importlib
import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
from contextlib import ExitStack, asynccontextmanager, closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from engine import auth, config, db, hermes_gateway, ip_allowlist, listen_control, queue
from engine import relay, relay_dialer, relay_server, signing_keys, topic_dispatch, wake
from scripts.relay_dispatch_fixture import DispatchGateway, isolated_git_origin
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


@asynccontextmanager
async def member_mcp(gateway, received, expected_url):
    spec = gateway.sessions[received['session_id']]['mcp']
    configured = {entry['name']: entry['value'] for entry in spec['env']}
    assert configured['GROUP_ENGINE_URL'] == expected_url
    assert Path(spec['command']).samefile(sys.executable)
    assert Path(spec['args'][0]).samefile(gateway.expected_mcp_entry)
    env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'TEMP', 'TMP') if k in os.environ}
    env.update(configured, NO_PROXY='*', PYTHONUTF8='1', HERMES_HOME=str(gateway.home))
    params = StdioServerParameters(command=spec['command'], args=['-B', *spec['args']], cwd=str(gateway.home), env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
        await client.initialize()
        yield client


async def send_mcp(gateway, received, expected_url, text, *, seen_id=None):
    async with member_mcp(gateway, received, expected_url) as client:
        sent = await client.call_tool('group_send', {'text': text, 'need_reply': False})
        assert not sent.isError, 'configured MCP did not send the reply'
        if seen_id is not None:
            seen = await client.call_tool('group_mark_seen', {'message_ids': [seen_id]})
            assert not seen.isError and json.loads(seen.content[0].text)['seen'] == 1, seen.content
        return json.loads(sent.content[0].text)['message_id']


async def claim_mcp(gateway, received, expected_url, task):
    async with member_mcp(gateway, received, expected_url) as client:
        info = await client.call_tool('task_get', {'project': 'alpha', 'task_id': task})
        assert not info.isError and json.loads(info.content[0].text)['task']['id'] == task, info.content
        claim = await client.call_tool('task_claim', {'project': 'alpha'})
        assert not claim.isError and json.loads(claim.content[0].text)['claimed'], claim.content


def mention(api, member, gateway, other, uri, label):
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
    verification = None
    if label == 'after-b':
        verification = result(api.post(f'/relay/nodes/{member}/mcp-verify'))
        assert verification['mcp_verify_state'] == 'ok', verification
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
            'target_receives': 1, 'non_target_receives': 0,
            **({'mcp_read': verification['mcp_verify_state']} if verification else {})}


def exercise(args, evidence):
    from engine.main import app
    from engine import hermes_connections, hermes_gateway_client, hermes_runtime
    with isolated_database() as path, ExitStack() as stack:
        stack.enter_context(patch.object(config, 'port', return_value=args.port))
        stack.enter_context(patch.object(config, 'host', return_value='127.0.0.1'))
        stack.enter_context(patch.object(auth, 'configured', return_value=False))
        with closing(db.get_connection()) as conn:
            db.init_schema(conn)
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
        profile = 'default'
        plugin_args = (args.plugin_dir,) if args.plugin_dir else ()
        pa, pb = _plugin_modules('first_join_a', *plugin_args), _plugin_modules('first_join_b', *plugin_args)
        ha, hb = Path(path).parent / 'host-a', Path(path).parent / 'host-b'
        gateway_a = stack.enter_context(DispatchGateway(profile, ha).listen())
        gateway_b = stack.enter_context(DispatchGateway(profile, hb).listen())
        local_home = Path(os.environ['HERMES_HOME'])
        local_home.mkdir(exist_ok=True)
        (local_home / 'config.yaml').write_text('model: fixture-model\n', encoding='utf-8')
        local_gateway = stack.enter_context(DispatchGateway(profile, local_home).listen())
        local_gateway.expected_mcp_entry = ROOT / 'mcp' / 'server.py'
        stack.enter_context(patch.object(config, 'engine_url', return_value=str(api.base_url).rstrip('/')))
        stack.enter_context(patch.object(wake, '_MCP_PYTHON', sys.executable))
        stack.enter_context(patch.object(hermes_runtime, 'command', return_value=[]))
        stack.enter_context(patch.object(hermes_gateway_client, 'ticket', return_value='fixture-ticket'))

        def local_connection(home, command, *, callback, business):
            assert Path(home) == local_home
            return hermes_gateway.GatewayClient({'profile_id': profile, 'instance_id': 'local',
                'api_origin': f'http://127.0.0.1:{local_gateway.port}'}, on_event=callback)

        stack.enter_context(patch.object(hermes_connections, 'acquire', side_effect=local_connection))
        stack.enter_context(patch.object(hermes_gateway, 'enabled', return_value=True))
        office = Path(path).parent / 'alpha'
        office.mkdir()
        origin = 'https://isolated.invalid/alpha.git'
        subprocess.run(['git', 'init', '-b', 'master', str(office)], check=True, capture_output=True)
        subprocess.run(['git', '-c', 'user.email=test@example.invalid', '-c', 'user.name=fixture',
                        'commit', '--allow-empty', '-m', 'fixture'], cwd=office, check=True, capture_output=True)
        bare = office.parent / 'upstream.git'
        subprocess.run(['git', 'clone', '--bare', str(office), str(bare)], check=True, capture_output=True)
        subprocess.run(['git', 'remote', 'add', 'origin', origin], cwd=office, check=True)
        stack.enter_context(patch.dict(os.environ, isolated_git_origin(origin, bare)))
        for plg, home, gateway in ((pa, ha, gateway_a), (pb, hb, gateway_b)):
            home.mkdir()
            tickets = importlib.import_module(plg['config'].__package__ + '.tickets')
            stack.enter_context(patch.object(tickets, 'fetch_ticket', side_effect=gateway.fetch_ticket))
            if args.plugin_dir:
                gateway.expected_mcp_entry = args.plugin_dir / 'mcp' / 'server.py'
                version = plg['config'].plugin_version()
                assert version == relay_server._engine_release_version()
                evidence['package_version'] = version
            else:
                gateway.expected_mcp_entry = ROOT / 'mcp' / 'server.py'
                stack.enter_context(patch.object(plg['mcp_digest'], 'MCP_DIR', ROOT / 'mcp'))
                stack.enter_context(patch.object(plg['config'], 'plugin_version',
                                                 return_value=relay_server._engine_release_version() or ''))
            stack.enter_context(patch.object(plg['pairing'], '_state_path', return_value=home / 'pairing.json'))
        with closing(db.get_connection()) as conn:
            assert not relay.list_nodes(conn) and not ip_allowlist.list_bindings(conn)
        local = result(api.post('/members', json={'profile': profile, 'display_name': profile,
                                                    'project': 'alpha', 'active': True}))
        assert local['display_name'] == profile
        with host_env(profile, hb):
            host_b = pb['config'].host_member()
            lb = listener(pb, hb, args.lan, stack, True)
        ui = None
        if args.browser:
            from scripts.relay_first_join_page import FirstJoinPage
            ui = FirstJoinPage(stack, str(api.base_url), args.evidence)
        url = lb['pairing_url']
        announced = parse_qs(urlsplit(url).query)['member'][0]
        preview = ui.preview(url) if ui else result(api.post('/relay/pairings/preview', json={'pairing_url': url}))
        assert preview['state'] == 'pending_confirmation' and preview['member'] == announced == host_b == profile
        outbound = f"/relay/pairings/{preview['pairing_id']}"
        assert result(api.get(outbound)) == preview
        assert result(api.get('/relay/nodes')) == [] and relay_server.active_link(profile) is None
        assert not pb['pairing'].pairing_metadata()['redeemed'] and not pb['pairing']._load_state().get('persistent')
        assert api.post('/relay/pairings/import', json={'pairing_url': url}).status_code == 409
        imported = ui.confirm(outbound, profile) if ui else result(api.post(outbound + '/confirm', json={'pairing_url': url}))
        b = imported['node_member']
        stack.callback(relay_dialer.stop_dial, b)
        assert imported['member'] == profile and imported['state'] == 'confirmed'
        evidence['outbound_confirmation'] = {'preview': preview, 'confirmed': imported}
        assert not result(api.get('/admin/listen/status'))['remote_enabled']

        if ui:
            issued = ui.invite(args.lan)
            status = result(api.get('/admin/listen/status'))
        else:
            status = result(api.post('/admin/listen', json={'address': args.lan}))
            issued = result(api.post('/relay/invitations'))
        stack.callback(listen_control.ensure, None)
        assert status['remote_enabled'] and status['remote_address'] == args.lan, status
        assert remote.get('/health').status_code == 403
        assert issued['listen_configured'] and issued['engine_url'] == str(remote.base_url).rstrip('/')
        with host_env(profile, ha):
            cfg = pa['config'].load(SimpleNamespace(extra={'engine_url': issued['engine_url'], 'invite': issued['invite_token'],
                                                           'mcp_python': sys.executable}))
        assert cfg and cfg.member == profile and Path(cfg.home) == ha
        out = importlib.import_module(pa['config'].__package__ + '.relay').RelayClient(cfg)
        out.start()
        stack.callback(out.stop)

        invitation = f"/relay/invitations/{issued['pairing_id']}"

        def awaiting_confirmation():
            view = result(api.get(invitation))
            return view if view['state'] == 'pending_confirmation' else None

        pending = wait(awaiting_confirmation)
        assert (pending['member'], pending['peer_ip']) == (profile, args.lan)
        assert set(pending) == {'pairing_id', 'state', 'direction', 'member', 'peer_ip', 'expires_at'}
        assert {node['member'] for node in result(api.get('/relay/nodes'))} == {b}
        assert relay_server.active_link(profile) is None and not out.generation
        with closing(db.get_connection()) as conn:
            assert not ip_allowlist.list_bindings(conn)
        assert remote.get('/health').status_code == 403
        confirmed = ui.confirm(invitation, profile) if ui else result(api.post(invitation + '/confirm'))
        assert confirmed['state'] == 'confirmed' and confirmed['member'] == profile
        a = confirmed['node_member']
        assert len({a, b, profile}) == 3
        assert result(api.post(invitation + '/confirm'))['state'] == 'confirmed'
        assert api.post(invitation + '/cancel').status_code == 409
        evidence['pending_confirmation'] = pending

        def online():
            nodes = {n['member']: n for n in result(api.get('/relay/nodes'))}
            return nodes if set(nodes) == {a, b} and all(n['state'] == 'online' for n in nodes.values()) else None

        both = wait(online)
        wait(lambda: out.generation)
        assert both[a]['direction'] == 'listen' and both[b]['direction'] == 'dial'
        assert result(api.get(outbound))['connected'] is True
        with closing(db.get_connection()) as conn, conn:
            assert ip_allowlist.list_bindings(conn) == [{'member': a, 'ips': [args.lan]}]
            redeemed = conn.execute('SELECT redeemed,redeemed_by FROM relay_invitations').fetchone()
            assert tuple(redeemed) == (1, profile) and both[a]['peer_ip'] == args.lan
            evidence['first_join'] = {'a': {'host': cfg.member, 'received': redeemed['redeemed_by'], 'node': both[a]['member'],
                                             'peer': both[a]['peer_ip']},
                                      'b': {'host': host_b, 'link': announced, 'authenticated': imported['member'],
                                            'node': both[b]['member']}}
        if ui:
            evidence['page_members'] = ui.cards_online(a, b, capture=True)
        candidates = result(api.get('/profiles/candidates', params={'project': 'alpha'}))['candidates']
        labels = {}
        evidence['workspaces'] = {}
        for member in (a, b):
            host_root = (ha if member == a else hb) / 'projects'
            assert result(api.put(f'/relay/nodes/{member}/workspace-root', json={'root': str(host_root)}))['root'] == str(host_root)
            candidate = next(item for item in candidates if item.get('member_profile') == member)
            assert candidate['profile'] == profile and candidate['host_type'] == 'relay'
            labels[member] = candidate['display_name']
            enrolled = result(api.post('/bots/register', json={**candidate, 'project': 'alpha'}))
            assert enrolled['member_profile'] == member
            prepared = result(api.post(f'/projects/alpha/members/{member}/prepare-workspace'))
            assert prepared['mode'] == 'shared' and prepared['branch'] == 'master'
            assert Path(prepared['path']) == host_root / 'alpha'
            evidence['workspaces'][member] = prepared
        from engine import session_adapter
        with patch.object(hermes_gateway, 'enabled', return_value=False):
            sid = session_adapter.create_session(a, 'workspace-reset-check', project='alpha')
            assert session_adapter.session_exists(a, sid)
            assert Path(gateway_a.sessions[sid]['snapshot']['info']['cwd']) == ha
            evidence['fresh_session_workspace'] = gateway_a.sessions[sid]['snapshot']['info']['cwd']
        assert set(labels.values()) == {'default（远程）', 'default（远程 2）'}
        members = {item['profile']: item['display_name'] for item in result(api.get('/members?project=alpha'))['members']}
        assert members[profile] == profile and all(members[k] == v for k, v in labels.items())
        assert relay_server.active_link(profile) is None
        evidence['member_names'] = {key: members[key] for key in (profile, a, b)}
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
        dispatch = mention
        if ui:
            # Playwright owns the main thread's event loop. MCP stdio needs its own.
            from concurrent.futures import ThreadPoolExecutor
            worker = stack.enter_context(ThreadPoolExecutor(max_workers=1))
            def dispatch(*args):
                return worker.submit(mention, *args).result()
        evidence['replies'] = []
        evidence['replies'].append(dispatch(api, profile, local_gateway, gateway_a, str(api.base_url).rstrip('/'), 'local'))
        assert not gateway_a.submit_requests and not gateway_b.submit_requests
        git_env = os.environ.copy()
        subprocess.run(['git', '-c', 'user.email=test@example.invalid', '-c', 'user.name=fixture',
                        'commit', '--allow-empty', '-m', 'unpublished'], cwd=office, env=git_env, check=True)
        with closing(db.get_connection()) as conn, conn:
            now = db.now_iso()
            req = conn.execute("INSERT INTO requirements(project,title,created_at,updated_at) VALUES('alpha','fixture',?,?)",
                               (now, now)).lastrowid
            task = conn.execute("INSERT INTO tasks(project,requirement_id,title,next_bot,created_at,description,"
                                "design_basis,acceptance_criteria,briefing_status) VALUES('alpha',?,'fixture',?,?,"
                                "'fixture','fixture','fixture','ready')", (req, b, now)).lastrowid
        from engine import taskpool
        unavailable = api.post(f'/projects/alpha/members/{b}/prepare-workspace')
        assert unavailable.status_code == 409 and '缺少办公机基线' in unavailable.json()['detail']
        claim = taskpool.task_claim('alpha', b)
        assert not claim['claimed'] and '缺少办公机基线' in claim['message']
        evidence['replies'].append(dispatch(api, a, gateway_a, gateway_b, issued['engine_url'], 'before-a'))
        evidence['replies'].append(dispatch(api, b, gateway_b, gateway_a, lb['proxy_url'], 'before-b'))
        for gateway, home in ((gateway_a, ha), (gateway_b, hb)):
            assert all(Path(s['snapshot']['info']['cwd']) == home for s in gateway.sessions.values())
        with closing(db.get_connection()) as conn:
            assert wake.enqueue(conn, b, 'work', 'scan:task', project='alpha')
            direct = result(api.post('/msg', json={'sender': 'user', 'project': 'alpha',
                                'text': f'@{b} 请从项目 origin 拉取基线', 'need_reply': True}))['message_id']
            assert queue.process_one(conn)
            admission = gateway_b.submissions[-1]
            assert '请从项目 origin 拉取基线' in admission['text'] and wake._TASK_SUFFIX.format(task_id=task) not in admission['text']
            row = conn.execute('SELECT task_id,workdir FROM hermes_executions WHERE submission_id=?',
                               (admission['submission_id'],)).fetchone()
            assert row['task_id'] is None and Path(row['workdir']) == hb
            from engine import scanner
            assert conn.execute("SELECT 1 FROM inbox i JOIN hermes_executions e ON e.execution_id=i.relay_execution_id "
                                "WHERE i.message_id=? AND i.profile=? AND i.status='pending' AND e.submission_id=?",
                                (direct, b, admission['submission_id'])).fetchone()
            scanner.scan_inbox(conn)
            assert not conn.execute("SELECT 1 FROM wake_jobs WHERE profile=? AND project='alpha' AND kind='work' "
                                    "AND status='queued' AND trigger='scan:group'", (b,)).fetchone()
            reply = send_mcp(gateway_b, admission, lb['proxy_url'], '@user 已收到仓库说明', seen_id=direct)
            reply_id = worker.submit(asyncio.run, reply).result() if ui else asyncio.run(reply)
            assert conn.execute('SELECT sender FROM messages WHERE id=? AND thread_id IS NULL', (reply_id,)).fetchone()[0] == b
            gateway_b.settled(admission)
            wait(lambda: conn.execute("SELECT 1 FROM hermes_executions WHERE submission_id=? AND state='terminal' AND cleanup_pending=0",
                                      (admission['submission_id'],)).fetchone())
            assert conn.execute('SELECT status FROM tasks WHERE id=?', (task,)).fetchone()[0] == 'pending'
            from engine import hermes_session_auth
            withdrawn = result(api.post('/msg', json={'sender': 'user', 'project': 'alpha',
                'text': f'@{b} withdrawn during setup', 'need_reply': True}))['message_id']
            configure = hermes_session_auth.configure
            def withdraw_after_configure(*params, **options):
                snapshot = configure(*params, **options)
                result(api.post(f'/message/{withdrawn}/retract', json={}))
                return snapshot
            before = len(gateway_b.submissions)
            with patch.object(hermes_session_auth, 'configure', side_effect=withdraw_after_configure):
                assert queue.process_one(conn)
            assert conn.execute('SELECT retract_at FROM messages WHERE id=?', (withdrawn,)).fetchone()[0]
            assert len(gateway_b.submissions) == before
            evidence['withdrawn_before_submit'] = {'message_id': withdrawn, 'new_submissions': 0}
        evidence['missing_baseline'] = dict(task_claim=claim['reason'], direct_message=direct, direct_reply=reply_id)
        orig = relay_server.active_link(b)
        assert orig is not None
        with host_env(profile, ha):
            reverse = listener(pa, ha, args.lan, stack, False)
        if ui:
            ui.switch(a, reverse['pairing_url'])
        else:
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
        with closing(db.get_connection()) as conn, conn:
            conn.execute("UPDATE projects SET directory_mode='worktree' WHERE name='alpha'")
        evidence['switch'] = {m: {k: after[m][k] for k in ('direction', 'state', 'generation')} for m in (a, b)}
        evidence['replies'].append(dispatch(api, b, gateway_b, gateway_a, lb['proxy_url'], 'after-b'))
        subprocess.run(['git', 'push', str(bare), 'master'], cwd=office, env=git_env, check=True)
        with closing(db.get_connection()) as conn:
            assert wake.enqueue(conn, b, 'work', 'scan:task', project='alpha')
            assert queue.process_one(conn)
            assigned = gateway_b.submissions[-1]
            assert wake._TASK_SUFFIX.format(task_id=task) in assigned['text']
            assert assigned['session_id'] == admission['session_id']
            claim_rpc = claim_mcp(gateway_b, assigned, lb['proxy_url'], task)
            if ui:
                worker.submit(asyncio.run, claim_rpc).result()
            else:
                asyncio.run(claim_rpc)
            gateway_b.settled(assigned)
            wait(lambda: conn.execute("SELECT 1 FROM hermes_executions WHERE submission_id=? AND state='terminal' AND cleanup_pending=0",
                                      (assigned['submission_id'],)).fetchone())
        worktree = hb / 'projects' / '.bot-group-worktrees' / 'alpha' / b
        assert any(Path(s['snapshot']['info']['cwd']) == worktree for s in gateway_b.sessions.values())
        assert subprocess.check_output(['git', 'branch', '--show-current'], cwd=worktree, text=True,
                                       env=os.environ.copy()).strip() == 'bot-group/' + b
        evidence['worktree_after_switch'] = dict(path=str(worktree), branch='bot-group/' + b)
        assert relay_server.active_link(b) is orig
        evidence['actual_submissions'] = {profile: len(local_gateway.submit_requests),
                                          a: len(gateway_a.submit_requests), b: len(gateway_b.submit_requests)}
        assert evidence['actual_submissions'] == {profile: 1, a: 1, b: 4}
        if ui:
            ui.finish(a, b)
        assert not gateway_a.errors and not gateway_b.errors, (gateway_a.errors, gateway_b.errors)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plugin-dir', type=Path, help='Extracted distribution to verify with its bundled MCP')
    parser.add_argument('--browser', action='store_true', help='Perform pairing and switching in Chrome')
    parser.add_argument('--lan', required=True)
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--evidence', required=True, type=Path)
    parser.add_argument('--hermes-source', type=Path)
    args = parser.parse_args()
    if args.plugin_dir:
        args.plugin_dir = args.plugin_dir.resolve()
        if not all((args.plugin_dir / path).is_file() for path in ('VERSION', 'manifest.json', 'mcp/server.py')):
            parser.error('--plugin-dir requires an extracted relay distribution')
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
