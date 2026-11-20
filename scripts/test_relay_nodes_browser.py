"""Remote relay node management in real Chrome, against a synthetic admin API."""
import argparse
import mimetypes
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    evidence = args.evidence
    evidence.mkdir(parents=True, exist_ok=True)

    members = [
        {'profile': 'hermes-remote', 'display_name': 'ECS Hermes', 'runtime': 'hermes', 'active': 1},
        {'profile': 'hermes-spare', 'display_name': '备用 Hermes', 'runtime': 'hermes', 'active': 0},
        {'profile': 'codex-local', 'display_name': '本地 Codex', 'runtime': 'codex', 'active': 1},
    ]
    state = {
        'nodes': [],
        'register_posts': 0,
        'revoke_posts': 0,
        'node_gets': 0,
        'register_fail': False,
        'register_hold': False,
        'nodes_fail': False,
        'held': [],
        'issued': [],
        'next_credential': 1,
    }
    errors, writes = [], []

    def node(member, display_name, **extra):
        data = {'member': member, 'display_name': display_name, 'credential_revoked': False,
                'generation': 1, 'state': 'offline', 'code_version': '',
                'last_heartbeat': None, 'mcp_fingerprint': '', 'expected_mcp_sha': '',
                'capacity_used': 0}
        data.update(extra)
        return data

    def register_response(body):
        member = body.get('member', '').strip()
        display_name = body.get('display_name', '') or member
        credential = f'TEST-RELAY-CREDENTIAL-{state["next_credential"]}'
        state['next_credential'] += 1
        state['issued'].append(credential)
        existing = next((n for n in state['nodes'] if n['member'] == member), None)
        if existing:
            existing['display_name'] = display_name
            existing['credential_revoked'] = False
        else:
            state['nodes'].append(node(member, display_name))
        return {'member': member, 'credential': credential}

    def route(req):
        path = urlparse(req.request.url).path
        if path.startswith('/app/'):
            file = ROOT / 'frontend' / (path[5:] or 'index.html')
            req.fulfill(body=file.read_bytes(),
                        content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
            return
        if path == '/relay/nodes':
            if req.request.method == 'GET':
                state['node_gets'] += 1
                if state['nodes_fail']:
                    req.fulfill(status=503, json={'detail': '节点服务暂不可用'})
                else:
                    req.fulfill(json=deepcopy(state['nodes']))
                return
            body = req.request.post_data_json
            state['register_posts'] += 1
            writes.append(('POST', path, body))
            if state['register_fail']:
                req.fulfill(status=500, json={'detail': '登记失败，请检查成员标识'})
                return
            if state['register_hold']:
                state['held'].append(req)
                return
            req.fulfill(json=register_response(body))
            return
        if path.startswith('/relay/nodes/') and path.endswith('/revoke'):
            member = path[len('/relay/nodes/'):-len('/revoke')]
            state['revoke_posts'] += 1
            writes.append(('POST', path, {}))
            row = next((n for n in state['nodes'] if n['member'] == member), None)
            if row is None:
                req.fulfill(status=404, json={'detail': '节点不存在'})
                return
            row['credential_revoked'] = True
            row['state'] = 'offline'
            req.fulfill(json={'member': member, 'revoked': True})
            return
        if path == '/members':
            req.fulfill(json={'members': deepcopy(members)})
            return
        if path == '/auth/status':
            req.fulfill(json={'configured': False})
            return
        if path == '/system/updates':
            req.fulfill(json={'status': 'unavailable', 'update_available': False})
            return
        if path == '/system/version':
            req.fulfill(json={'current_version': 'test', 'version_label': '测试版本'})
            return
        if path == '/user/display-name':
            req.fulfill(json={'profile': 'user', 'display_name': '管理员'})
            return
        if path == '/appearance/background':
            req.fulfill(status=204, headers={'X-Background-Configured': 'true'})
            return
        if path == '/projects':
            req.fulfill(json={'projects': []})
            return
        if path in ('/messages', '/threads'):
            req.fulfill(json={path[1:]: []})
            return
        if path == '/tasks':
            req.fulfill(json={'tasks': [], 'requirements': [], 'pending_approval': []})
            return
        req.fulfill(json={'projects': [], 'members': [], 'messages': [], 'threads': [],
                          'tasks': [], 'requirements': [], 'commands': [], 'roles': [],
                          'state': {}, 'entries': []})

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        context = browser.new_context(viewport={'width': 1280, 'height': 900},
                                      permissions=['clipboard-read', 'clipboard-write'])
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', route)
        page.goto('http://127.0.0.1:18045/app/index.html')
        page.wait_for_function('document.querySelector("#login").hidden && S.isUserIdentity === true')

        def open_settings():
            page.locator('#btn-settings').click()

        def open_dialog():
            open_settings()
            page.locator('#btn-relay-nodes').click()
            page.wait_for_function('document.querySelector("#relay-nodes-dialog").open')
            page.wait_for_function('!document.querySelector("#relay-nodes-status").textContent.includes("正在读取")')

        def close_dialog():
            page.locator('#relay-close').click()
            page.wait_for_function('!document.querySelector("#relay-nodes-dialog").open')

        # Admin entry and empty state.
        open_settings()
        assert page.locator('#btn-relay-nodes').is_visible()
        page.locator('#btn-relay-nodes').click()
        page.wait_for_function('document.querySelector("#relay-nodes-dialog").open')
        page.wait_for_function('document.querySelector("#relay-nodes-status").textContent.includes("暂无已登记节点")')
        assert page.locator('#relay-readonly').is_hidden()
        assert page.locator('#relay-admin-body').is_visible()
        options = page.locator('#relay-member option').evaluate_all('(items) => items.map(i => i.value)')
        assert options == ['', 'hermes-remote', 'hermes-spare'], options
        assert page.locator('#relay-register-submit').is_disabled()
        page.screenshot(path=str(evidence / 'relay-nodes-empty-1280-dark.png'))

        # Register: a double click while the request is in flight must not rotate twice.
        state['register_hold'] = True
        page.locator('#relay-member').select_option('hermes-remote')
        assert page.locator('#relay-register-submit').is_enabled()
        page.locator('#relay-register-submit').click()
        page.wait_for_function('document.querySelector("#relay-register-submit").disabled')
        page.locator('#relay-register-submit').dispatch_event('click')
        assert state['register_posts'] == 1, state['register_posts']
        state['register_hold'] = False
        state['held'].pop().fulfill(json=register_response({'member': 'hermes-remote',
                                                            'display_name': 'ECS Hermes'}))
        page.wait_for_function('!document.querySelector("#relay-credential").hidden')
        assert page.locator('#relay-issued-member').input_value() == 'hermes-remote'
        credential = page.locator('#relay-issued-credential').input_value()
        assert credential == 'TEST-RELAY-CREDENTIAL-1'
        assert page.locator('#relay-node-list').inner_text().count('ECS Hermes') == 1
        assert '离线' in page.locator('#relay-node-list').inner_text()
        assert '未上报' in page.locator('#relay-node-list').inner_text()
        page.screenshot(path=str(evidence / 'relay-nodes-issued-1280-dark.png'),
                        mask=[page.locator('#relay-issued-credential')])

        # Copy member and credential, including the actual clipboard value.
        page.locator('#relay-copy-member').click()
        page.wait_for_function('document.querySelector("#relay-register-status").textContent.includes("已复制成员标识")')
        assert page.evaluate('navigator.clipboard.readText()') == 'hermes-remote'
        page.locator('#relay-copy-credential').click()
        page.wait_for_function('document.querySelector("#relay-register-status").textContent.includes("已复制连接凭据")')
        assert page.evaluate('navigator.clipboard.readText()') == credential

        # Reopen the dialog: the credential is not recoverable and no rotation occurs.
        close_dialog()
        posts = state['register_posts']
        open_dialog()
        assert page.locator('#relay-credential').is_hidden()
        assert credential not in page.locator('#relay-nodes-dialog').inner_text()
        assert state['register_posts'] == posts

        # Handshake state from the same page after refresh.
        state['nodes'][0].update({'state': 'online', 'last_heartbeat': 1760000000.0,
                                  'mcp_fingerprint': 'abcdef0123456789', 'capacity_used': 2,
                                  'code_version': '0.2.1'})
        page.locator('#relay-refresh').click()
        page.wait_for_function('document.querySelector("#relay-node-list").textContent.includes("在线")')
        text = page.locator('#relay-node-list').inner_text()
        assert 'abcdef0123456789' in text
        assert '2' in text and '0.2.1' in text
        assert state['register_posts'] == posts
        assert page.locator('#relay-credential').is_hidden()

        # Revoke needs a separate confirmation; cancel does nothing.
        page.locator('#relay-node-list button[data-action="revoke"]').click()
        page.wait_for_function('document.querySelector("#app-alert").open')
        assert '吊销 hermes-remote' in page.locator('#app-alert-message').inner_text()
        assert '当前在线连接会立即断开' in page.locator('#app-alert-message').inner_text()
        page.locator('#app-alert button', has_text='取消').click()
        assert state['revoke_posts'] == 0
        page.locator('#relay-node-list button[data-action="revoke"]').click()
        page.wait_for_function('document.querySelector("#app-alert").open')
        page.locator('#app-alert button', has_text='确定').click()
        page.wait_for_function('document.querySelector("#relay-nodes-status").textContent.includes("已吊销")')
        page.wait_for_function('document.querySelector("#relay-node-list").textContent.includes("凭据已吊销")')
        assert state['revoke_posts'] == 1
        assert page.locator('#relay-node-list button[data-action="revoke"]').count() == 0

        # Reissue needs its own confirmation and replaces the credential.
        page.locator('#relay-node-list button[data-action="reissue"]').click()
        page.wait_for_function('document.querySelector("#app-alert").open')
        assert '重新签发 hermes-remote' in page.locator('#app-alert-message').inner_text()
        assert '旧凭据立即失效' in page.locator('#app-alert-message').inner_text()
        page.locator('#app-alert button', has_text='确定').click()
        page.wait_for_function('!document.querySelector("#relay-credential").hidden')
        new_credential = page.locator('#relay-issued-credential').input_value()
        assert new_credential == 'TEST-RELAY-CREDENTIAL-2'
        assert new_credential != credential
        assert credential not in page.locator('#relay-nodes-dialog').inner_text()
        page.wait_for_function('document.querySelector("#relay-node-list").textContent.includes("离线")')

        # Bot identity: read-only explanation, no node requests.
        close_dialog()
        gets_before_bot = state['node_gets']
        page.evaluate('S.isUserIdentity = false; document.dispatchEvent(new Event("service-auth-changed"))')
        open_settings()
        assert page.locator('#btn-relay-nodes').is_visible()
        page.locator('#btn-relay-nodes').click()
        page.wait_for_function('document.querySelector("#relay-nodes-dialog").open')
        assert page.locator('#relay-readonly').is_visible()
        assert '仅可查看说明' in page.locator('#relay-readonly').inner_text()
        assert page.locator('#relay-admin-body').is_hidden()
        assert state['node_gets'] == gets_before_bot
        page.screenshot(path=str(evidence / 'relay-nodes-bot-readonly-1280-dark.png'))
        close_dialog()
        page.evaluate('S.isUserIdentity = true; document.dispatchEvent(new Event("service-auth-changed"))')

        # Request failure feedback and recovery.
        state['nodes_fail'] = True
        open_dialog()
        page.wait_for_function('document.querySelector("#relay-nodes-status").dataset.error === "true"')
        assert '读取远程节点失败' in page.locator('#relay-nodes-status').inner_text()
        state['nodes_fail'] = False
        page.locator('#relay-refresh').click()
        page.wait_for_function('document.querySelector("#relay-nodes-status").dataset.error === "false"')
        state['register_fail'] = True
        page.locator('#relay-member').select_option('hermes-spare')
        page.locator('#relay-register-submit').click()
        page.wait_for_function('document.querySelector("#relay-register-status").textContent.includes("登记失败")')
        assert page.locator('#relay-credential').is_hidden()
        state['register_fail'] = False
        close_dialog()

        # 390px, both themes, no horizontal overflow.
        page.set_viewport_size({'width': 390, 'height': 844})
        open_dialog()
        box = page.locator('#relay-nodes-dialog').bounding_box()
        assert box['x'] >= 0 and box['x'] + box['width'] <= 390
        overflow = page.locator('#relay-nodes-dialog').evaluate('(el) => el.scrollWidth - el.clientWidth')
        assert overflow <= 0, overflow
        for theme in ('dark', 'light'):
            page.evaluate('(theme) => applyTheme(theme)', theme)
            page.screenshot(path=str(evidence / f'relay-nodes-390-{theme}.png'))
        page.evaluate("applyTheme('dark')")
        close_dialog()

        # Page reload: credentials and node state are re-read, never persisted in browser storage.
        page.set_viewport_size({'width': 1280, 'height': 900})
        page.reload()
        page.wait_for_function('document.querySelector("#login").hidden && S.isUserIdentity === true')
        open_dialog()
        assert page.locator('#relay-credential').is_hidden()
        assert new_credential not in page.locator('#relay-nodes-dialog').inner_text()
        stored = page.evaluate('JSON.stringify({local: {...localStorage}, session: {...sessionStorage}})')
        assert new_credential not in stored and credential not in stored
        assert not errors, errors
        assert all(path.startswith('/relay/nodes') for method, path, _ in writes if method == 'POST'), writes
        browser.close()
    print('PASS: relay node list, Hermes member selection, one-time credential/copy, double-click guard, '
          'online refresh, revoke/reissue confirms, bot read-only, errors and 390px themes')


if __name__ == '__main__':
    main()
