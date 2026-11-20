"""Bidirectional relay controls in real Chrome, against a synthetic admin API."""
import argparse
import mimetypes
from copy import deepcopy
from pathlib import Path
from urllib.parse import unquote, urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
FINGERPRINT = 'a' * 64


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    evidence = args.evidence
    evidence.mkdir(parents=True, exist_ok=True)

    members = [
        {'profile': 'hermes-remote', 'display_name': 'ECS Hermes', 'runtime': 'hermes', 'active': 1},
    ]
    state = {
        'nodes': [],
        'node': None,
        'node_gets': 0,
        'nodes_fail': False,
        'listen': {'remote_enabled': False, 'remote_address': None, 'remote_port': None,
                   'loopback': '127.0.0.1:18042', 'error': ''},
        'listen_interfaces': [{'address': '100.64.0.10', 'version': 4, 'family': 'IPv4'}],
        'listen_posts': [],
        'switch_mode': 'success',
        'switch_posts': [],
        'probe_ok': True,
        'probe_stage': 'ok',
        'probe_posts': [],
        'endpoint_posts': [],
        'mcp_verify_posts': [],
        'mcp_verify_ok': True,
        'bindings': [{'member': 'hermes-remote', 'ips': ['100.64.0.5']}],
        'issued': [],
    }
    errors, writes = [], []

    def node():
        return state['node']

    def new_node():
        state['node'] = {
            'member': 'hermes-remote', 'display_name': 'ECS Hermes',
            'credential_revoked': False, 'generation': 1, 'state': 'offline',
            'code_version': '0.2.16', 'last_heartbeat': None,
            'mcp_fingerprint': '', 'expected_mcp_sha': '', 'capacity_used': 0,
            'peer_ip': None, 'direction': 'listen', 'switch_state': '',
            'switch_target': '', 'switch_error': '', 'offline_reason': '',
            'dial_state': 'idle', 'dial_endpoint': None, 'dial_failure': None,
            'mcp_verify_state': '', 'mcp_verify_error': '',
        }

    def route(req):
        path = urlparse(req.request.url).path
        if path.startswith('/app/'):
            file = ROOT / 'frontend' / (path[5:] or 'index.html')
            req.fulfill(body=file.read_bytes(),
                        content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
            return
        if path == '/admin/source-ips' and req.request.method == 'GET':
            req.fulfill(json={'loopback_always_allowed': ['127.0.0.1'], 'bindings': deepcopy(state['bindings'])})
            return
        if path == '/admin/source-ips/add' and req.request.method == 'POST':
            req.fulfill(json={'member': 'hermes-remote', 'ip': '100.64.0.5'})
            return
        if path == '/admin/listen/interfaces' and req.request.method == 'GET':
            req.fulfill(json={'interfaces': deepcopy(state['listen_interfaces'])})
            return
        if path == '/admin/listen/status' and req.request.method == 'GET':
            req.fulfill(json=deepcopy(state['listen']))
            return
        if path == '/admin/listen' and req.request.method == 'POST':
            body = req.request.post_data_json
            state['listen_posts'].append(deepcopy(body))
            if body.get('address'):
                state['listen'].update({'remote_enabled': True, 'remote_address': body['address'],
                                        'remote_port': 18042})
            else:
                state['listen'].update({'remote_enabled': False, 'remote_address': None,
                                        'remote_port': None})
            req.fulfill(json=deepcopy(state['listen']))
            return
        if path == '/relay/nodes' and req.request.method == 'GET':
            state['node_gets'] += 1
            if state['nodes_fail']:
                req.fulfill(status=503, json={'detail': '节点服务暂不可用'})
            else:
                req.fulfill(json=[deepcopy(node())] if node() else [])
            return
        if path == '/relay/nodes' and req.request.method == 'POST':
            body = req.request.post_data_json
            state['issued'].append('TEST-CREDENTIAL')
            new_node()
            req.fulfill(json={'member': body.get('member'), 'credential': 'TEST-CREDENTIAL'})
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
        if path.endswith('/dial-endpoint/probe'):
            body = req.request.post_data_json
            state['probe_posts'].append(deepcopy(body))
            req.fulfill(json={'member': 'hermes-remote', 'ok': state['probe_ok'],
                              'stage': state['probe_stage'], 'address': f"{body.get('host')}:{body.get('port')}",
                              'error': '' if state['probe_ok'] else '证书指纹不匹配'})
            return
        if path.endswith('/dial-endpoint'):
            body = req.request.post_data_json
            state['endpoint_posts'].append(deepcopy(body))
            state['node']['dial_endpoint'] = {
                'host': body.get('host'), 'port': body.get('port'), 'tls': True,
                'fingerprint': body.get('fingerprint'),
                'fingerprint_short': body.get('fingerprint', '')[:8],
            }
            req.fulfill(json={'member': 'hermes-remote', 'saved': True})
            return
        if path.endswith('/mcp-verify'):
            state['mcp_verify_posts'].append(deepcopy(req.request.post_data_json))
            if state['mcp_verify_ok']:
                state['node'].update({'mcp_verify_state': 'ok', 'mcp_verify_error': ''})
                req.fulfill(json={'member': 'hermes-remote', 'mcp_verify_state': 'ok',
                                  'mcp_verify_error': ''})
            else:
                state['node'].update({'mcp_verify_state': 'failed',
                                      'mcp_verify_error': '权限拒绝(403): 项目只读被拒'})
                req.fulfill(json={'member': 'hermes-remote', 'mcp_verify_state': 'failed',
                                  'mcp_verify_error': '权限拒绝(403): 项目只读被拒'})
            return
        if path.endswith('/switch'):
            body = req.request.post_data_json
            target = body.get('direction')
            state['switch_posts'].append(deepcopy(body))
            if state['switch_mode'] == 'success':
                state['node'].update({'direction': target, 'switch_state': '', 'switch_target': '',
                                      'switch_error': '', 'state': 'online', 'offline_reason': '',
                                      'dial_state': 'connected' if target == 'dial' else 'idle',
                                      'dial_failure': None,
                                      'mcp_verify_state': '', 'mcp_verify_error': '',
                                      'mcp_fingerprint': 'abcdef0123456789', 'last_heartbeat': 1760000000})
            elif state['switch_mode'] == 'transition':
                state['node'].update({'switch_target': target, 'switch_state': 'switching',
                                      'state': 'offline', 'offline_reason': 'switch',
                                      'dial_state': 'connecting' if target == 'dial' else 'idle',
                                      'mcp_verify_state': '', 'mcp_verify_error': ''})
            elif state['switch_mode'] == 'fail':
                state['node'].update({'switch_target': target, 'switch_state': 'switching',
                                      'switch_error': '拨号重连次数耗尽，等待重试或切回',
                                      'state': 'offline', 'offline_reason': 'switch',
                                      'dial_state': 'abandoned',
                                      'mcp_verify_state': '', 'mcp_verify_error': '',
                                      'dial_failure': {'stage': 'tls', 'address': 'cloud.example:18443',
                                                       'error': '证书指纹不匹配'}})
            req.fulfill(json={'member': 'hermes-remote', 'direction': target,
                              'switch_state': 'switching' if state['switch_mode'] != 'success' else '',
                              'dial_state': state['node']['dial_state']})
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

        page.locator('#btn-settings').click()
        page.locator('#btn-relay-nodes').click()
        page.wait_for_function('document.querySelector("#relay-nodes-dialog").open')
        page.wait_for_function('document.querySelector("#relay-nodes-status").textContent.includes("暂无")')
        assert page.locator('#relay-direction-badge').inner_text() == '未登记节点'
        assert page.locator('#relay-direction-listen').is_disabled()
        page.screenshot(path=str(evidence / 'direction-empty-1280-dark.png'))

        # Register a node through the existing panel.
        page.locator('#relay-member').select_option('hermes-remote')
        page.locator('#relay-register-ip').fill('100.64.0.5')
        page.locator('#relay-register-submit').click()
        page.wait_for_function('!document.querySelector("#relay-direction-listen").disabled')
        page.wait_for_function('document.querySelector("#relay-direction-node").value === "hermes-remote"')
        assert page.locator('#relay-direction-listen').get_attribute('aria-pressed') == 'true'
        assert '等待连接' in page.locator('#relay-direction-status').inner_text()
        assert '未开启远程监听' in page.locator('#relay-direction-local').inner_text()
        page.screenshot(path=str(evidence / 'direction-listen-offline-1280-dark.png'))

        # Enable the engine listener and mark the node online: three stages complete.
        page.locator('#relay-listen-address').select_option('100.64.0.10')
        page.locator('#relay-listen-save').click()
        page.wait_for_function('document.querySelector("#relay-listen-badge").textContent === "已开启"')
        state['node'].update({'state': 'online', 'mcp_fingerprint': 'abcdef0123456789'})
        page.locator('#relay-refresh').click()
        page.wait_for_function('document.querySelector("#relay-direction-badge").textContent.includes("已连接")')
        assert page.locator('#relay-stage-listen').get_attribute('class') == 'relay-stage-done'
        assert page.locator('#relay-stage-auth').get_attribute('class') == 'relay-stage-done'
        assert page.locator('#relay-stage-mcp').get_attribute('class') != 'relay-stage-done'
        assert 'MCP 待只读验证' in page.locator('#relay-direction-status').inner_text()
        assert '已测通 · 推荐' in page.locator('#relay-direction-listen').inner_text()
        assert page.locator('#relay-direction-dial').is_enabled()

        # Direction B without a pairing import is refused, and the form is displayed.
        page.locator('#relay-direction-dial').click()
        assert '先导入云端配对链接' in page.locator('#relay-direction-status').inner_text()
        assert page.locator('#relay-listen').is_hidden()
        assert page.locator('#relay-pairing').is_visible()

        # A bad link never saves anything.
        page.locator('#relay-pairing-url').fill('https://cloud.example:18443?pairing_token=x&fingerprint=' + FINGERPRINT)
        page.locator('#relay-pairing-parse').click()
        assert '必须使用 wss' in page.locator('#relay-pairing-status').inner_text()
        assert state['probe_posts'] == [] and state['endpoint_posts'] == []

        # Parse and confirm; show identity and short fingerprint before import.
        page.locator('#relay-pairing-url').fill(
            f'wss://cloud.example:18443?pairing_token=ONE-SHOT-SECRET&fingerprint={FINGERPRINT}')
        page.locator('#relay-pairing-parse').click()
        page.wait_for_function('!document.querySelector("#relay-pairing-preview").hidden')
        assert 'ECS Hermes' in page.locator('#relay-pairing-identity').inner_text()
        assert page.locator('#relay-pairing-fingerprint').inner_text() == 'aaaaaaaa…'
        assert state['probe_posts'] == []

        # Certificate probe failure is explicit and does not save the endpoint.
        state['probe_ok'] = False
        state['probe_stage'] = 'tls'
        page.locator('#relay-pairing-confirm').click()
        page.wait_for_function('document.querySelector("#relay-pairing-status").textContent.includes("预检失败")')
        assert '未保存' in page.locator('#relay-pairing-status').inner_text()
        assert state['endpoint_posts'] == []
        assert state['switch_posts'] == []

        # Probe success saves the endpoint and starts the dial direction.
        state['probe_ok'] = True
        state['probe_stage'] = 'ok'
        state['switch_mode'] = 'success'
        page.locator('#relay-pairing-confirm').click()
        page.wait_for_function('document.querySelector("#relay-pairing-status").textContent.includes("导入完成")')
        page.wait_for_function('document.querySelector("#relay-direction-status").textContent.includes("已连通")')
        page.wait_for_function('document.querySelector("#relay-stage-mcp").className === "relay-stage-done"')
        assert len(state['mcp_verify_posts']) == 1
        assert 'MCP 只读验证已通过' in page.locator('#relay-direction-status').inner_text()
        assert len(state['probe_posts']) == 2
        assert len(state['endpoint_posts']) == 1
        assert state['endpoint_posts'][0]['pairing_token'] == 'ONE-SHOT-SECRET'
        assert state['endpoint_posts'][0]['fingerprint'] == FINGERPRINT
        assert 'wss://cloud.example:18443' in page.locator('#relay-direction-remote').inner_text()
        assert page.locator('#relay-pairing-preview').is_hidden()
        assert page.locator('#relay-pairing-url').input_value() == ''
        page.screenshot(path=str(evidence / 'direction-dial-online-1280-dark.png'))

        # Switch back through a transient state: the page shows 切换中 without a timer verdict.
        state['switch_mode'] = 'transition'
        page.locator('#relay-direction-listen').click()
        page.wait_for_function('document.querySelector("#relay-direction-badge").textContent === "切换中"')
        assert '先断旧通道' in page.locator('#relay-direction-status').inner_text()
        state['node'].update({'direction': 'listen', 'switch_state': '', 'switch_target': '',
                              'state': 'online', 'offline_reason': '', 'dial_state': 'idle',
                              'mcp_fingerprint': 'abcdef0123456789'})
        page.wait_for_function('document.querySelector("#relay-direction-badge").textContent.includes("已连接")')
        assert page.locator('#relay-direction-listen').get_attribute('aria-pressed') == 'true'

        # A failed switch shows the stage, retry and switch-back actions.
        state['switch_mode'] = 'fail'
        state['node']['direction'] = 'listen'
        page.locator('#relay-direction-dial').click()
        page.wait_for_function('document.querySelector("#relay-direction-failure").textContent.includes("TLS")')
        assert '切换失败' in page.locator('#relay-direction-status').inner_text()
        assert page.locator('#relay-direction-retry').is_visible()
        assert page.locator('#relay-direction-back').is_visible()
        page.screenshot(path=str(evidence / 'direction-failed-1280-dark.png'))
        assert '导入完成' not in page.locator('#relay-pairing-status').inner_text()
        state['switch_mode'] = 'success'
        page.locator('#relay-direction-retry').click()
        page.wait_for_function('document.querySelector("#relay-direction-status").textContent.includes("已连通")')
        assert page.locator('#relay-direction-dial').get_attribute('aria-pressed') == 'true'
        page.wait_for_function('document.querySelector("#relay-stage-mcp").className === "relay-stage-done"')

        # B was online, then dropped unexpectedly and exhausted retries. A retry
        # on the same direction must send a real switch request, not be a no-op.
        state['node'].update({'state': 'offline', 'offline_reason': 'unexpected',
                              'switch_state': '', 'switch_target': '', 'switch_error': '',
                              'dial_state': 'abandoned', 'dial_failure': None,
                              'mcp_verify_state': '', 'mcp_verify_error': ''})
        page.locator('#relay-refresh').click()
        page.wait_for_function('document.querySelector("#relay-direction-badge").textContent === "拨号已停止"')
        with page.expect_request(lambda request: request.method == 'POST'
                                 and request.url.endswith('/switch')) as switch_request:
            page.locator('#relay-direction-retry').click()
        assert switch_request.value, '同方向离线重试必须实际发出 /switch'
        page.wait_for_function('document.querySelector("#relay-direction-status").textContent.includes("已连通")')
        page.wait_for_function('document.querySelector("#relay-stage-mcp").className === "relay-stage-done"')

        # A fingerprint alone is not enough: a failed scoped read shows the failure
        # and never marks the MCP stage complete.
        state['mcp_verify_ok'] = False
        state['node'].update({'mcp_verify_state': '', 'mcp_verify_error': ''})
        page.locator('#relay-refresh').click()
        page.wait_for_function('document.querySelector("#relay-stage-mcp").className === "relay-stage-failed"')
        assert 'MCP 只读验证失败' in page.locator('#relay-direction-status').inner_text()
        assert '权限拒绝(403)' in page.locator('#relay-direction-failure').inner_text()
        state['mcp_verify_ok'] = True
        state['node'].update({'mcp_verify_state': '', 'mcp_verify_error': ''})
        page.locator('#relay-refresh').click()
        page.wait_for_function('document.querySelector("#relay-stage-mcp").className === "relay-stage-done"')
        state['node'].update({'state': 'offline', 'offline_reason': 'unexpected',
                              'switch_state': '', 'switch_target': '', 'switch_error': '',
                              'dial_state': 'idle', 'dial_failure': None})
        page.locator('#relay-refresh').click()
        page.wait_for_function('document.querySelector("#relay-direction-status").textContent.includes("连接异常掉线")')
        assert page.locator('#relay-direction-badge').inner_text() == '异常掉线'
        state['node'].update({'state': 'online', 'offline_reason': '',
                              'dial_state': 'connected', 'mcp_fingerprint': 'abcdef0123456789'})
        page.locator('#relay-refresh').click()
        page.wait_for_function('document.querySelector("#relay-direction-badge").textContent.includes("已连接")')

        # 390px dark/light with the direction and pairing panes visible, no horizontal overflow.
        page.set_viewport_size({'width': 390, 'height': 844})
        for theme in ('dark', 'light'):
            page.evaluate('(theme) => applyTheme(theme)', theme)
            box = page.locator('#relay-nodes-dialog').bounding_box()
            assert box['x'] >= 0 and box['x'] + box['width'] <= 390
            overflow = page.locator('#relay-nodes-dialog').evaluate('(el) => el.scrollWidth - el.clientWidth')
            assert overflow <= 0, (theme, overflow)
            assert page.locator('#relay-direction-badge').is_visible()
            assert page.locator('#relay-pairing').is_visible()
            page.screenshot(path=str(evidence / f'direction-390-{theme}.png'))
        page.set_viewport_size({'width': 1280, 'height': 900})
        page.evaluate("applyTheme('dark')")

        assert not errors, errors
        browser.close()
    print('PASS: direction A/B, pairing preview/probe, MCP actual-verify ok/failed, same-direction offline retry, import/switch, switch-back, failure retry, 390px')


if __name__ == '__main__':
    main()
