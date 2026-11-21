"""Check a recovery notice from unknown to release through real HTTP, SSE and Chrome."""
from argparse import ArgumentParser
from contextlib import ExitStack
import json
from pathlib import Path
import socket
import sys
import tempfile
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
from playwright.sync_api import expect, sync_playwright

from engine import auth, db, events, hermes_gateway as gateway, hermes_gateway_ledger as ledger, queue
from scripts.test_task_resume_recovery import ResumeRecoveryTests
from scripts.unified_test_support import isolated_api
from scripts.verify_task_resume_recovery import LedgerTransport, base_browser_route, dispatch_after_release


# Observe native EventSource events. Disable only the 10 s UI fallback to distinguish
# delivery on the connected page from explicit EventSource reconnection on the other.
OBSERVE_EVENTS = """
localStorage.setItem('group-engine.current-project', 'alpha');
window.recoveryFrames = [];
const nativeInterval = window.setInterval;
window.setInterval = (fn, delay, ...args) => delay === 10000 ? 0 : nativeInterval(fn, delay, ...args);
window.EventSource = class extends window.EventSource {
  constructor(...args) {
    super(...args);
    this.addEventListener('message', event => recoveryFrames.push(JSON.parse(event.data)));
  }
};
"""


def observe(args):
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
        stack.enter_context(patch.object(gateway, 'connection', return_value=(transport, home, [])))
        cue = fixture.reserve()
        assert not fixture.poll(transport)
        started = transport.claim()
        assert started['status'] == 'started' and started['request_id'] == cue['request_id']
        transport.epoch = runtime.begin_runtime_epoch(native, instance_id='unknown-then-terminal')
        runtime.recover_session_inputs(native, epoch=transport.epoch)
        assert not fixture.poll(transport)
        mid = fixture.cue()['notice_message_id']
        assert mid is not None
        command = f'/release-recovery {mid}'
        command_body = {'project': 'alpha', 'sender': 'user', 'text': command}
        stack.enter_context(patch.object(auth, 'configured', return_value=False))
        url = stack.enter_context(isolated_api(frontend=True, port=args.port, sse=True))
        api = stack.enter_context(httpx.Client(base_url=url, trust_env=False, timeout=8))

        def messages(since=0):
            return api.get('/messages', params={'project': 'alpha', 'since_id': since,
                                                'recovery_notices': 1}).raise_for_status().json()

        def inbox():
            return [dict(row) for row in fixture.conn.execute(
                "SELECT message_id, status FROM inbox WHERE profile='user' AND message_id=?", (mid,))]

        def notice():
            rows = [m for m in messages()['messages']
                    if m['sender'] == 'engine' and '#991 自动续办受阻' in m['content']]
            assert [m['id'] for m in rows] == [mid]
            return rows[0]

        original = notice()
        assert 'unknown' in original['content'] and command not in original['content']
        denied_unknown = api.post('/msg', json=command_body)
        assert denied_unknown.status_code == 400
        assert inbox() == [{'message_id': mid, 'status': 'pending'}]
        with sync_playwright() as pw, tempfile.TemporaryDirectory(dir=args.browser_data, prefix='recovery-transition-') as profile:
            browser = pw.chromium.launch_persistent_context(profile, channel='chrome', headless=True,
                args=[f'--remote-debugging-port={args.browser_port}', '--remote-debugging-address=127.0.0.1'],
                service_workers='block', viewport={'width': 1280, 'height': 800})
            try:
                version = api.get(f'http://127.0.0.1:{args.browser_port}/json/version').raise_for_status().json()
                assert urlsplit(version['webSocketDebuggerUrl']).port == args.browser_port
                errors = []

                def route(req):
                    target = urlsplit(req.request.url)
                    if target.netloc != urlsplit(url).netloc:
                        return req.abort()
                    if target.path in ('/events', '/auth/events'):
                        return req.continue_()
                    return base_browser_route(req)

                def open_view(page):
                    page.set_default_timeout(8000)
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.add_init_script(OBSERVE_EVENTS)
                    page.route('**/*', route)
                    page.goto(url + '/app/')
                    box = page.locator(f'[data-message-id="{mid}"] .msg-content')
                    expect(box).to_contain_text('unknown')
                    page.wait_for_function('S.es?.readyState === EventSource.OPEN && S.messageRequest == null')
                    assert page.evaluate('S.lastMsgId') == mid
                    page.evaluate('(mid) => { window.originalNotice = document.querySelector(`[data-message-id="${mid}"] .msg-content`); }', mid)
                    return box, page.locator(f'[data-message-id="{mid}"] .recovery-notice-state')

                page = browser.pages[0]
                box, action = open_view(page)
                missed = browser.new_page()
                # Narrow the message column using the existing three-panel layout.
                missed.set_viewport_size({'width': 1076, 'height': 844})
                missed_box, missed_action = open_view(missed)
                missed.evaluate('S.es.close()')
                page.screenshot(path=str(args.evidence / 'unknown.png'))

                terminal = runtime.resolve_unknown_session_input(native, epoch=transport.epoch,
                    admission_id=started['admission_id'], generation=started['generation'])
                assert terminal['status'] == 'terminal'
                assert fixture.poll(transport) and fixture.cue()['state'] == 'unaccepted'
                expect(action).to_contain_text('暂不能解除')
                frames = page.evaluate('recoveryFrames')
                event = {'type': 'recovery.notice.changed', 'data': {'project': 'alpha', 'message_id': mid}}
                assert event in frames and not any(f['type'] == 'message.new' for f in frames)
                denied_cleanup = api.post('/msg', json=command_body)
                assert denied_cleanup.status_code == 400
                assert gateway._finish(fixture.conn, fixture.current(), transport)
                expect(action).to_contain_text(command)
                assert page.evaluate('(mid) => originalNotice === document.querySelector(`[data-message-id="${mid}"] .msg-content`)', mid)
                assert box.inner_text() == original['content']
                delta = messages(mid)
                assert delta['messages'] == [] and delta['recovery_notices'] == {str(mid): 'releasable'}
                assert notice() == original
                page.screenshot(path=str(args.evidence / 'live-terminal.png'))

                ledger.apply_receipt(fixture.conn, fixture.execution, transport.receipt(terminal))
                ledger.apply_receipt(fixture.conn, fixture.execution, transport.receipt({**terminal, 'status': 'unknown'}))
                db.recover_on_startup(fixture.conn)
                assert not queue.process_one(fixture.conn)
                for _ in range(2):
                    events.bus.publish(event['type'], event['data'])
                page.wait_for_function("recoveryFrames.filter(f => f.type === 'recovery.notice.changed').length >= 4")
                page.wait_for_function('S.messageRequest == null')
                expect(action).to_have_count(1)
                expect(action).to_contain_text(command)
                assert inbox() == [{'message_id': mid, 'status': 'pending'}]
                expect(missed_action).to_have_count(0)
                missed.evaluate('connectSSE()')
                expect(missed_action).to_be_visible()
                expect(missed_action).to_contain_text(command)
                column_width = missed.locator('.col-msgs').bounding_box()['width']
                assert 300 <= column_width < 450, column_width
                assert not missed.evaluate('recoveryFrames.some(f => f.type === "recovery.notice.changed")')
                assert missed_box.inner_text() == original['content'] and missed.evaluate('originalNotice.isConnected')
                missed.screenshot(path=str(args.evidence / 'reconnected-narrow.png'))

                composer = page.locator('#composer-text')
                composer.fill(command)
                with page.expect_response(lambda r: urlsplit(r.url).path == '/msg' and r.request.method == 'POST') as sent:
                    composer.press('Enter')
                released = sent.value.json()
                assert sent.value.ok and released.get('ok') and released['notice_message_id'] == mid, released
                expect(action).to_contain_text('已解除占用')
                expect(missed_action).to_contain_text('已解除占用')
                assert command not in action.inner_text() and notice() == original
                assert inbox() == [{'message_id': mid, 'status': 'seen'}]
                assert fixture.conn.execute('SELECT status FROM wake_jobs WHERE id=?', (fixture.job_id,)).fetchone()[0] == 'cancelled'
                assert sum(method == 'prompt.submit' for method, _ in transport.calls) == 1
                new_work = dispatch_after_release(fixture, transport, api)
                assert new_work['native_admission_id'] != started['admission_id'] and not errors, errors
                admissions = runtime.list_session_admissions(native, session_id=transport.sid, pending_only=False)
                assert sum(row['request_id'] == cue['request_id'] for row in admissions) == 1
                assert sum(method == 'prompt.submit' for method, _ in transport.calls) == 2
                return {'notice_id': mid, 'original_message': original, 'stored_after_release': notice(),
                        'browser_events': page.evaluate('recoveryFrames'), 'reconnected_without_terminal_frame': True,
                        'cleanup_denied': denied_cleanup.status_code, 'unknown_denied': denied_unknown.status_code,
                        'delta': delta, 'release': released, 'inbox_after_release': inbox(),
                        'old_admission': started['admission_id'], 'original_request_count': 1,
                        'new_work': new_work, 'rpc_calls': transport.calls}
            finally:
                browser.close()


def main():
    parser = ArgumentParser(description=__doc__)
    for name in ('hermes-source', 'test-data', 'browser-data', 'evidence'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--browser-port', required=True, type=int)
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
    tempfile.tempdir = str(args.test_data)
    sys.path.append(str(args.hermes_source))
    try:
        result = {'passed': True, **observe(args)}
    except Exception as exc:
        (args.evidence / 'result.json').write_text(json.dumps({'passed': False, 'error': str(exc)},
            ensure_ascii=False, indent=2), encoding='utf-8')
        raise
    (args.evidence / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
