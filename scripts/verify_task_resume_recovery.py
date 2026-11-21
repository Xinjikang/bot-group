"""Verify task recovery against isolated Hermes admissions, real HTTP and the actual Web UI.

The RPC adapter uses a real temporary Hermes ledger. A deterministic consumer
claims B's admitted input and uses B's scoped task endpoints; no production gateway,
provider, task data or history from the prior session is used.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx

from engine import auth, db, hermes_gateway as gateway, hermes_gateway_ledger as ledger, queue, wake
from scripts.test_task_resume_recovery import ResumeRecoveryTests
from scripts.unified_test_support import isolated_api
from scripts.verify_scanner_settle_browser import route as base_browser_route


def until(read, label, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = read()
        if result:
            return result
        time.sleep(.05)
    raise TimeoutError(label)


class LedgerTransport:
    """Test RPC boundary; only storage-issued receipts can start a recovery."""

    def __init__(self, native, runtime, sid, profile):
        self.native, self.runtime = native, runtime
        self.sid, self.profile = sid, profile
        self.epoch = runtime.begin_runtime_epoch(native, instance_id='recovery-fixture')
        self.calls = []

    def receipt(self, row):
        return {'ref': {'session_id': self.sid, 'profile_id': self.profile}, 'admission_id': row['admission_id'],
                'status': row['status'], 'authority_epoch': row['owner_epoch'] or self.epoch,
                'execution_generation': row['generation'], 'outcome': row['outcome'], 'input_id': row['request_id']}

    def admit(self, request, text):
        return self.runtime.admit_session_input(self.native, epoch=self.epoch, principal_id='fixture-member',
                                               session_id=self.sid, request_id=request, payload={'text': text})

    def rpc(self, method, session_id, **params):
        self.calls.append((method, session_id))
        assert session_id == self.sid, 'Unexpected RPC to the old or foreign session'
        if method == 'session.resume':
            rows = self.runtime.list_session_admissions(self.native, session_id=session_id)
            return {'session_id': session_id, 'running': any(row['status'] == 'started' for row in rows),
                    'pending': [self.receipt(row) for row in rows], 'prompts': [], 'messages': []}
        if method == 'prompt.submit':
            return self.receipt(self.admit(params['submission_id'], params['text']))
        if method == 'prompt.receipt':
            rows = self.runtime.list_session_admissions(self.native, session_id=session_id, pending_only=False)
            return self.receipt(next(row for row in rows if row['admission_id'] == params['admission_id']))
        raise AssertionError(f'Unexpected RPC: {method}')

    def close(self):
        pass

    def claim(self):
        return self.runtime.claim_session_input(self.native, epoch=self.epoch, session_id=self.sid)


def task_tool(api, fixture, operation, **args):
    result = api.post('/task-tools/' + operation, headers=fixture.headers, json={
        'arguments': {'project': 'alpha', 'task_id': 991, **args},
        'execution': {**fixture.context, 'credential_type': 'session'},
    })
    result.raise_for_status()
    value = result.json()
    assert 'error' not in value, value
    return value


def browser_notice(args, url, notice):
    from playwright.sync_api import sync_playwright
    from tempfile import TemporaryDirectory
    with sync_playwright() as pw, TemporaryDirectory(dir=args.browser_data, prefix='task-recovery-') as profile:
        browser = pw.chromium.launch_persistent_context(profile, channel='chrome', headless=True,
            viewport={'width': 1280, 'height': 800}, args=[f'--remote-debugging-port={args.browser_port}',
                                                         '--remote-debugging-address=127.0.0.1'])
        try:
            endpoint = f'http://127.0.0.1:{args.browser_port}/json/version'
            with httpx.Client(trust_env=False, timeout=2) as cdp:
                version = cdp.get(endpoint).raise_for_status().json()
            assert urlsplit(version['webSocketDebuggerUrl']).port == args.browser_port
            page = browser.pages[0]
            page.add_init_script("localStorage.setItem('group-engine.current-project','alpha')")
            def route(request):
                if urlsplit(request.request.url).netloc != urlsplit(url).netloc:
                    return request.abort()
                return base_browser_route(request)
            page.route('**/*', route)
            with page.expect_response(lambda resp: urlsplit(resp.url).path == '/messages' and resp.ok) as response:
                page.goto(url + '/app/')
            raw = response.value.json()['messages']
            assert any(item['id'] == notice for item in raw), 'The real /messages did not return the notice'
            node = page.locator(f'[data-message-id="{notice}"] .msg-content')
            node.wait_for(state='visible', timeout=10000)
            text = node.inner_text()
            assert all(part in text for part in ('#991', 'unknown', '核对旧执行结果')), text
            page.screenshot(path=str(args.evidence / 'blocked-notice.png'))
            return {'message_id': notice, 'ui_text': text, 'viewport': page.viewport_size,
                    'page': page.url, 'browser_port': args.browser_port, 'profile': profile,
                    'messages_status': response.value.status}
        finally:
            browser.close()


def exercise(args, *, blocked):
    from hermes_state import SessionDB
    import hermes_state_runtime as runtime

    with ExitStack() as stack:
        fixture = ResumeRecoveryTests()
        stack.callback(fixture.doCleanups)
        fixture.setUp()
        home = Path(fixture.row['home'])
        assert home.is_relative_to(args.test_data)
        native = stack.enter_context(SessionDB(home / 'state.db'))
        native.create_session(fixture.row['session_id'], source='acp')
        transport = LedgerTransport(native, runtime, fixture.row['session_id'], fixture.row['profile_id'])
        prior = None
        if blocked:
            transport.admit('old-execution-not-to-replay', 'Previously submitted native input')
            prior = transport.claim()
            assert prior['status'] == 'started'
            transport.epoch = runtime.begin_runtime_epoch(native, instance_id='replacement-fixture')
            runtime.recover_session_inputs(native, epoch=transport.epoch)
            fixture.conn.execute("UPDATE hermes_executions SET admission_id=? WHERE execution_id=?",
                                 (prior['admission_id'], fixture.execution))
            fixture.conn.commit()
        workers = set()
        stack.enter_context(patch.object(gateway, '_workers', workers))
        stack.enter_context(patch.object(wake, '_running_wakes', {}))
        stack.enter_context(patch.object(gateway, 'connection', return_value=(transport, home, [])))
        stack.enter_context(patch.object(auth, 'configured', return_value=False))
        url = stack.enter_context(isolated_api(frontend=blocked, port=args.port))
        assert urlsplit(url).hostname == '127.0.0.1' and urlsplit(url).port == args.port
        api = stack.enter_context(httpx.Client(base_url=url, trust_env=False, timeout=8))
        try:
            assert queue.process_one(fixture.conn)
            cue = fixture.cue()
            assert cue['job_id'] == fixture.job_id and cue['execution_id'] == fixture.execution
            if blocked:
                notice = until(lambda: fixture.cue()['notice_message_id'], 'User notice missing')
                until(lambda: not workers, 'Blocked worker did not release its lease')
                saved = runtime.list_session_admissions(native, session_id=transport.sid)
                assert [(r['admission_id'], r['status']) for r in saved] == [(prior['admission_id'], 'unknown')]
                assert not any(method != 'session.resume' for method, _ in transport.calls), transport.calls
                assert fixture.conn.execute('SELECT status FROM wake_jobs WHERE id=?', (fixture.job_id,)).fetchone()[0] == 'queued'
                target = fixture.conn.execute('SELECT status FROM inbox WHERE message_id=? AND profile=?', (notice, 'user')).fetchone()
                assert target and target['status'] == 'pending'
                return {**browser_notice(args, url, notice), 'runtime_status': 'unknown', 'wake_status': 'queued',
                        'recovery_id': cue['request_id'], 'native_admission_id': prior['admission_id'],
                        'rpc_calls': transport.calls}
            until(lambda: fixture.cue()['admission_id'], 'Recovery request was never admitted')
            admitted = runtime.list_session_admissions(native, session_id=transport.sid)
            assert len(admitted) == 1 and admitted[0]['status'] == 'queued'
            assert fixture.current()['state'] == 'manual'
            assert fixture.conn.execute('SELECT status FROM wake_jobs WHERE id=?', (fixture.job_id,)).fetchone()[0] == 'queued'
            started = transport.claim()
            assert started['status'] == 'started' and started['request_id'] == cue['request_id']
            assert started['payload']['text'] == cue['text'] and '任务 #991' in started['payload']['text']
            until(lambda: fixture.cue()['state'] == 'started', 'Engine did not confirm the actual native start')
            assert task_tool(api, fixture.fixture, 'task_get')['task']['id'] == 991
            role = fixture.conn.execute("SELECT role_id FROM project_members WHERE project='alpha' AND profile='architect'").fetchone()[0]
            task_tool(api, fixture.fixture, 'task_log', type='dev', result='B consumed the original task recovery input',
                      next_bot='architect', expected_role_id=role)
            nodes = fixture.conn.execute('SELECT id,task_id,actor,result FROM nodes WHERE task_id=991').fetchall()
            assert len(nodes) == 1 and nodes[0]['actor'] == 'default' and 'B consumed' in nodes[0]['result']
            runtime.settle_session_input(native, epoch=transport.epoch, admission_id=started['admission_id'],
                                         generation=started['generation'], outcome='completed')
            try:
                until(lambda: fixture.current()['state'] == 'terminal' and not workers, 'Recovery worker did not settle')
            except TimeoutError as exc:
                state = fixture.current()
                raise TimeoutError(f'{exc}: state={state["state"]}; detail={state["detail"]}; '
                                   f'cleanup_pending={state["cleanup_pending"]}; RPCs={transport.calls}') from exc
            assert len([method for method, _ in transport.calls if method == 'prompt.submit']) == 1
            assert fixture.conn.execute('SELECT COUNT(*) FROM hermes_executions').fetchone()[0] == 1
            return {'execution_id': fixture.execution, 'native_admission_id': started['admission_id'],
                    'current_session_b': transport.sid, 'received_text': started['payload']['text'],
                    'recovery_id': cue['request_id'], 'job_id': cue['job_id'], 'task_node': dict(nodes[0]),
                    'rpc_calls': transport.calls}
        finally:
            if workers:
                fixture.conn.execute("UPDATE hermes_task_recoveries SET state='abandoned' WHERE state='pending'")
                fixture.conn.execute("UPDATE hermes_executions SET state='manual' WHERE state<>'terminal'")
                fixture.conn.commit()
                until(lambda: not workers, 'Validation leaked a worker')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('hermes-source', 'test-data', 'browser-data', 'evidence'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--browser-port', type=int, required=True)
    args = parser.parse_args()
    if (not all(path.is_absolute() for path in (args.hermes_source, args.test_data, args.browser_data, args.evidence))
            or not (args.hermes_source / 'hermes_state_runtime.py').is_file()
            or not args.test_data.is_dir() or not args.browser_data.is_dir()
            or len({args.port, args.browser_port}) != 2 or not all(0 < p <= 65535 for p in (args.port, args.browser_port))):
        parser.error('Specify existing isolated paths and two distinct assigned ports')
    for port in (args.port, args.browser_port):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))
    args.evidence.mkdir(parents=True, exist_ok=False)
    # No production home, credentials or provider are involved in the native storage fixture.
    tempfile.tempdir = str(args.test_data)
    sys.path.append(str(args.hermes_source))
    result = {'backend': f'127.0.0.1:{args.port}', 'test_data': str(args.test_data)}
    try:
        result['current_work_session'] = exercise(args, blocked=False)
        (args.evidence / 'current-session.json').write_text(json.dumps(result['current_work_session'],
             ensure_ascii=False, indent=2), encoding='utf-8')
        result['user_notice'] = exercise(args, blocked=True)
        (args.evidence / 'user-notice.json').write_text(json.dumps(result['user_notice'],
             ensure_ascii=False, indent=2), encoding='utf-8')
        for port in (args.port, args.browser_port):
            with socket.socket() as probe:
                probe.settimeout(1)
                assert probe.connect_ex(('127.0.0.1', port)) != 0, f'Test port {port} still listening'
        result['passed'] = True
    except BaseException as exc:
        result.update(passed=False, error=repr(exc))
        raise
    finally:
        (args.evidence / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
