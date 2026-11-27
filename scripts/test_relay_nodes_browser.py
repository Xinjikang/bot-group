"""Remote relay node management in real Chrome, against a synthetic admin API."""
import argparse
import mimetypes
from copy import deepcopy
from pathlib import Path
from urllib.parse import unquote, urlparse

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
        {'profile': 'hermes-extra', 'display_name': '备用 Hermes 2', 'runtime': 'hermes', 'active': 1},
        {'profile': 'codex-local', 'display_name': '本地 Codex', 'runtime': 'codex', 'active': 1},
    ]
    state = {
        'nodes': [],
        'register_posts': 0,
        'revoke_posts': 0,
        'node_gets': 0,
        'register_fail': False,
        'register_hold': False,
        'register_lost': False,
        'nodes_fail': False,
        'nodes_hold': False,
        'forbidden': False,
        'bindings': [],
        'bindings_fail': False,
        'binding_gets': 0,
        'binding_fail': False,
        'binding_hold': False,
        'binding_posts': [],
        'binding_deletes': [],
        'held_bindings': [],
        'listen': {'remote_enabled': False, 'remote_address': None, 'remote_port': None,
                   'loopback': '127.0.0.1:18042', 'error': ''},
        'listen_interfaces': [
            {'address': '100.64.0.10', 'version': 4, 'family': 'IPv4', 'network': 'Tailscale', 'name': 'Tailscale'},
            {'address': '100.64.0.11', 'version': 4, 'family': 'IPv4'},
            {'address': 'fd00::10', 'version': 6, 'family': 'IPv6'},
            {'address': '1.1.1.1', 'version': 4, 'family': 'IPv4', 'network': '公网', 'name': '以太网'},
            {'address': '172.20.0.1', 'version': 4, 'family': 'IPv4', 'network': 'Hyper-V 本机虚拟机',
             'name': 'vEthernet (Default Switch)'},
        ],
        'listen_gets': 0,
        'listen_status_gets': 0,
        'listen_posts': [],
        'listen_fail': False,
        'listen_status_fail': False,
        'listen_apply_fail': False,
        'listen_lost': False,
        'listen_hold': False,
        'held_listen': [],
        'held': [],
        'held_nodes': [],
        'register_bodies': [],
        'issued': [],
        'next_credential': 1,
        'renewal_state': 'waiting',
    }
    errors, writes = [], []

    def node(member, display_name, **extra):
        data = {'member': member, 'display_name': display_name, 'credential_revoked': False,
                'generation': 1, 'state': 'offline', 'code_version': '',
                'last_heartbeat': None, 'mcp_fingerprint': '', 'expected_mcp_sha': '',
                'capacity_used': 0, 'peer_ip': None}
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
        if path == '/admin/source-ips':
            if req.request.method == 'GET':
                state['binding_gets'] += 1
                if state['bindings_fail']:
                    req.fulfill(status=503, json={'detail': '来源 IP 绑定服务暂不可用'})
                else:
                    req.fulfill(json={'loopback_always_allowed': ['127.0.0.1', '::1'],
                                      'bindings': deepcopy(state['bindings'])})
                return
            body = req.request.post_data_json
            state['binding_posts'].append(deepcopy(body))
            writes.append(('POST', path, body))
            if state['binding_fail']:
                req.fulfill(status=400, json={'detail': '无效的 IP 地址'})
                return
            if state['binding_hold']:
                state['held_bindings'].append((req, body))
                return
            member = body.get('member', '')
            ips = []
            for ip in body.get('ips', []):
                if ip and ip not in ips:
                    ips.append(ip)
            row = next((item for item in state['bindings'] if item['member'] == member), None)
            if row:
                row['ips'] = ips
            else:
                state['bindings'].append({'member': member, 'ips': ips})
            req.fulfill(json={'member': member, 'ips': ips})
            return
        if path == '/admin/source-ips/add' and req.request.method == 'POST':
            body = req.request.post_data_json
            state['binding_posts'].append(deepcopy(body))
            writes.append(('POST', path, body))
            if state['binding_fail']:
                req.fulfill(status=400, json={'detail': '无效的 IP 地址'})
                return
            if state['binding_hold']:
                state['held_bindings'].append((req, body))
                return
            member = body.get('member', '')
            ip = body.get('ip', '')
            row = next((item for item in state['bindings'] if item['member'] == member), None)
            if row:
                if ip and ip not in row['ips']:
                    row['ips'].append(ip)
            else:
                state['bindings'].append({'member': member, 'ips': [ip]})
            req.fulfill(json={'member': member, 'ip': ip})
            return
        if path.startswith('/admin/source-ips/') and req.request.method == 'DELETE':
            rest = path[len('/admin/source-ips/'):]
            member, ip = rest.split('/', 1)
            member = unquote(member)
            ip = unquote(ip)
            state['binding_deletes'].append({'member': member, 'ip': ip})
            writes.append(('DELETE', path, {}))
            row = next((item for item in state['bindings'] if item['member'] == member), None)
            if not row or ip not in row['ips']:
                req.fulfill(status=404, json={'detail': '绑定不存在'})
                return
            row['ips'] = [item for item in row['ips'] if item != ip]
            req.fulfill(json={'member': member, 'removed': ip})
            return
        if path == '/admin/listen/interfaces' and req.request.method == 'GET':
            state['listen_gets'] += 1
            if state['listen_fail']:
                req.fulfill(status=503, json={'detail': '网卡枚举服务暂不可用'})
            else:
                req.fulfill(json={'interfaces': deepcopy(state['listen_interfaces'])})
            return
        if path == '/admin/listen/status' and req.request.method == 'GET':
            state['listen_status_gets'] += 1
            if state['listen_status_fail']:
                req.fulfill(status=503, json={'detail': '监听状态服务暂不可用'})
            else:
                req.fulfill(json=deepcopy(state['listen']))
            return
        if path == '/admin/listen' and req.request.method == 'POST':
            body = req.request.post_data_json
            state['listen_posts'].append(deepcopy(body))
            writes.append(('POST', path, body))
            if state['listen_hold']:
                state['held_listen'].append((req, body))
                return
            if state['listen_lost']:
                req.abort('failed')
                return
            if state['listen_apply_fail']:
                req.fulfill(status=400, json={'detail': '所选网卡端口已占用'})
                return
            address = body.get('address', '')
            if address:
                state['listen'].update({'remote_enabled': True, 'remote_address': address,
                                        'remote_port': 18042, 'error': ''})
            else:
                state['listen'].update({'remote_enabled': False, 'remote_address': None,
                                        'remote_port': None, 'error': ''})
            req.fulfill(json=deepcopy(state['listen']))
            return
        if path == '/relay/nodes/hermes-remote/workspace-root' and req.request.method == 'PUT':
            value = req.request.post_data_json['root']
            if not value.startswith('/'):
                req.fulfill(status=409, json={'detail': '请输入远程主机上的绝对路径'})
            else:
                next(n for n in state['nodes'] if n['member'] == 'hermes-remote')['workspace_root'] = value
                req.fulfill(json={'root': value})
            return
        if path == '/relay/nodes/hermes-remote/renewal' and req.request.method == 'POST':
            state['renewal_state'] = 'waiting'
            req.fulfill(json={'pairing_id': 'test-renewal', 'engine_url': 'https://engine.example',
                              'fingerprint': 'a' * 64, 'invite_token': 'test-renewal-ticket'})
            return
        if path == '/relay/invitations/test-renewal/cancel':
            state['renewal_state'] = 'cancelled'
            req.fulfill(json={'state': 'cancelled'})
            return
        if path == '/relay/invitations/test-renewal':
            req.fulfill(json={'state': state['renewal_state'], 'node_member': 'hermes-remote'})
            return
        if path == '/relay/nodes':
            if req.request.method == 'GET':
                state['node_gets'] += 1
                if state['forbidden']:
                    req.fulfill(status=403, json={'detail': '仅管理员可查看远程节点'})
                elif state['nodes_fail']:
                    req.fulfill(status=503, json={'detail': '节点服务暂不可用'})
                elif state['nodes_hold']:
                    state['held_nodes'].append(req)
                else:
                    req.fulfill(json=deepcopy(state['nodes']))
                return
            body = req.request.post_data_json
            state['register_posts'] += 1
            state['register_bodies'].append(deepcopy(body))
            writes.append(('POST', path, body))
            if state['register_lost']:
                req.abort('failed')
                return
            if state['register_fail']:
                req.fulfill(status=500, json={'detail': '登记失败，请检查成员标识'})
                return
            if state['register_hold']:
                state['held'].append(req)
                return
            req.fulfill(json=register_response(body))
            return
        if path.startswith('/relay/nodes/') and req.request.method == 'DELETE':
            member = path.rsplit('/', 1)[1]
            writes.append(('DELETE', path, {}))
            state['nodes'] = [n for n in state['nodes'] if n['member'] != member]
            req.fulfill(json={'member': member, 'removed': True})
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
        page.goto('http://localhost/app/index.html')
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
        state['nodes_hold'] = True
        open_settings()
        assert page.locator('#btn-relay-nodes').is_visible()
        page.locator('#btn-relay-nodes').click()
        page.wait_for_function('document.querySelector("#relay-nodes-dialog").open')
        page.wait_for_function('document.querySelector("#relay-nodes-status").textContent.includes("正在读取")')
        assert not page.get_by_text("暂无远程 Hermes。", exact=True).count()
        page.wait_for_function('document.querySelector("#relay-listen-badge").textContent === "仅本机"')
        assert state['held_nodes']
        state['nodes_hold'] = False
        state['held_nodes'].pop().fulfill(json=[])
        page.wait_for_function('document.querySelector("#relay-nodes-status").textContent.includes("暂无已登记节点")')
        page.wait_for_function('document.querySelector("#relay-listen-badge").textContent === "仅本机"')
        assert '未开启远程监听' in page.locator('#relay-listen-summary').inner_text()
        assert page.locator('#relay-listen-address option').all_text_contents() == [
            '选择本机网卡地址', 'Tailscale · 100.64.0.10 · IPv4', '100.64.0.11 · IPv4', 'fd00::10 · IPv6',
            '公网 · 以太网 · 1.1.1.1 · IPv4', 'Hyper-V 本机虚拟机 · vEthernet (Default Switch) · 172.20.0.1 · IPv4',
        ]
        assert page.locator('#relay-readonly').is_hidden()
        assert page.locator('#relay-admin-body').is_visible()
        assert page.locator('#relay-member').count() == 0
        assert page.locator('#relay-add').is_enabled()
        page.screenshot(path=str(evidence / 'relay-nodes-empty-1280-dark.png'))
        state['nodes'] = [node('hermes-remote', 'ECS Hermes'), node('hermes-spare', '备用 Hermes')]
        state['bindings'] = [{'member': 'hermes-remote', 'ips': ['100.64.0.5'], 'networks': {'100.64.0.5': 'Tailscale'}},
                             {'member': 'hermes-spare', 'ips': ['100.64.0.6']}]
        page.locator('#relay-refresh').click()
        page.locator('#relay-open-listen').click()
        # Listen configuration: enable a concrete interface, show the actual address, copy it, then disable.
        page.wait_for_function('!document.querySelector("#relay-close").disabled')
        page.wait_for_function('!document.querySelector("#relay-listen-address").disabled')
        page.locator('#relay-listen-address').select_option('100.64.0.10')
        page.wait_for_function('!document.querySelector("#relay-listen-save").disabled')
        page.locator('#relay-listen-save').click()
        page.wait_for_function('document.querySelector("#relay-listen-badge").textContent === "已开启"')
        page.wait_for_function('!document.querySelector("#relay-listen-copy").disabled')
        assert state['listen_posts'][-1] == {'address': '100.64.0.10'}
        assert page.locator('#relay-listen-summary').inner_text() == '实际监听 Tailscale · 100.64.0.10:18042'
        assert '允许来源' in page.locator('#relay-listen-sources').inner_text()
        assert page.locator('#relay-listen-url').input_value() == 'https://100.64.0.10:18042'
        assert page.locator('#relay-listen-url-row').is_visible()
        page.locator('#relay-listen-copy').click()
        page.wait_for_function('document.querySelector("#relay-listen-status").textContent.includes("已复制")')
        assert page.evaluate('navigator.clipboard.readText()') == 'https://100.64.0.10:18042'
        page.wait_for_function('!document.querySelector("#relay-listen-disable").disabled')
        page.locator('#relay-listen-disable').click()
        page.wait_for_function('document.querySelector("#relay-listen-badge").textContent === "仅本机"')
        assert state['listen_posts'][-1] == {'address': ''}
        assert page.locator('#relay-listen-url-row').is_hidden()
        assert '回退回环' in page.locator('#relay-listen-status').inner_text()

        # A failed switch must not claim the listener is enabled.
        state['listen_apply_fail'] = True
        page.wait_for_function('!document.querySelector("#relay-listen-address").disabled')
        page.locator('#relay-listen-address').select_option('100.64.0.10')
        page.wait_for_function('!document.querySelector("#relay-listen-save").disabled')
        page.locator('#relay-listen-save').click()
        page.wait_for_function('document.querySelector("#relay-listen-status").textContent.includes("监听切换失败")')
        assert '端口已占用' in page.locator('#relay-listen-status').inner_text()
        assert page.locator('#relay-listen-badge').inner_text() == '仅本机'
        assert page.locator('#relay-listen-url-row').is_hidden()
        state['listen_apply_fail'] = False

        # A lost response is an unconfirmed switch, never an enabled claim; refresh resolves it.
        state['listen_lost'] = True
        page.wait_for_function('!document.querySelector("#relay-listen-address").disabled')
        page.locator('#relay-listen-address').select_option('100.64.0.10')
        page.wait_for_function('!document.querySelector("#relay-listen-save").disabled')
        page.locator('#relay-listen-save').click()
        page.wait_for_function('document.querySelector("#relay-listen-status").textContent.includes("结果未确认")')
        assert page.locator('#relay-listen-badge').inner_text() == '结果未确认'
        assert page.locator('#relay-listen-url-row').is_hidden()
        assert page.locator('#relay-listen-save').is_disabled()
        state['listen_lost'] = False
        page.locator('#relay-listen-refresh').click()
        page.wait_for_function('document.querySelector("#relay-listen-badge").textContent === "仅本机"')
        page.wait_for_function('!document.querySelector("#relay-listen-address").disabled')

        # Listen status read failure is explicit and disables the controls; refresh recovers.
        state['listen_status_fail'] = True
        page.locator('#relay-listen-refresh').click()
        page.wait_for_function('document.querySelector("#relay-listen-summary").textContent.includes("监听配置读取失败")')
        assert page.locator('#relay-listen-save').is_disabled()
        assert page.locator('#relay-listen-address').is_disabled()
        state['listen_status_fail'] = False
        page.locator('#relay-listen-refresh').click()
        page.wait_for_function('document.querySelector("#relay-listen-badge").textContent === "仅本机"')
        page.wait_for_function('!document.querySelector("#relay-listen-address").disabled')

        page.locator('#relay-listen-close').click()
        close_dialog()
        state['nodes_fail'] = True
        open_dialog()
        page.wait_for_function('document.querySelector("#relay-nodes-status").dataset.error === "true"')
        assert page.locator('#relay-add').is_disabled()
        state['nodes_fail'] = False
        state['bindings_fail'] = True
        page.locator('#relay-refresh').click()
        page.wait_for_function('document.querySelector("#relay-node-list").textContent.includes("来源 IP 读取失败")')
        assert page.locator('#relay-node-list button[data-action="add-ip"]').first.is_disabled()
        state['bindings_fail'] = False
        close_dialog()

        # Reopen the dialog: credentials are not recoverable; member-to-IP bindings remain visible.
        posts = state['register_posts']
        open_dialog()
        assert page.locator('#relay-credential').is_hidden()
        assert state['register_posts'] == posts
        remote_card = page.locator('#relay-node-list article[data-relay-member="hermes-remote"]')
        spare_card = page.locator('#relay-node-list article[data-relay-member="hermes-spare"]')
        assert 'Tailscale · 100.64.0.5' in remote_card.locator('.relay-ip-chip').inner_text()
        assert '100.64.0.6' in spare_card.inner_text()
        assert '尚未连接' in remote_card.inner_text()
        root = remote_card.get_by_label('协作根目录')
        save = remote_card.get_by_role('button', name='验证并保存')
        def check_workspace_layout(card):
            field = card.get_by_label('协作根目录')
            control = field.bounding_box()
            button = card.get_by_role('button', name='验证并保存').bounding_box()
            assert abs(control['y'] - button['y']) <= 1 and control['height'] == button['height']
            label = field.evaluate("""el => {
                const range = document.createRange();
                range.selectNodeContents(el.labels[0].firstChild);
                return range.getBoundingClientRect().toJSON();
            }""")
            row = card.locator('form.relay-copy-row').bounding_box()
            assert abs(label['left'] - row['x']) <= 1
            assert abs(label['top'] + label['height'] / 2 - control['y'] - control['height'] / 2) <= 2
            assert abs(control['x'] - label['right'] - 8) <= 1
            assert abs(button['x'] - control['x'] - control['width'] - 8) <= 1
            assert abs(button['x'] + button['width'] - row['x'] - row['width']) <= 1
            return control['width']

        desktop_input_width = check_workspace_layout(remote_card)
        root.fill('relative')
        save.click()
        remote_card.get_by_text('请输入远程主机上的绝对路径', exact=True).wait_for()
        root.fill('/fixture/projects')
        save.click()
        remote_card.get_by_text('远程目录已验证并保存。', exact=True).wait_for()
        assert not spare_card.get_by_label('协作根目录').input_value()
        page.screenshot(path=str(evidence / 'relay-nodes-bindings-1280-dark.png'))

        # Add and remove an extra source IP from the node card; both take effect immediately.
        remote_card.locator('button[data-action="add-ip"]').click()
        form = remote_card.locator('[data-ip-form]')
        assert form.is_visible()
        form.locator('input').fill('100.64.0.7')
        form.locator('button[type="submit"]').click()
        page.wait_for_function('document.querySelector("#relay-nodes-status").textContent.includes("添加来源 IP")')
        assert state['binding_posts'][-1] == {'member': 'hermes-remote', 'ip': '100.64.0.7'}
        remote_card = page.locator('#relay-node-list article[data-relay-member="hermes-remote"]')
        page.wait_for_function('document.querySelector("#relay-node-list").textContent.includes("100.64.0.7")')
        remote_card.locator('button[data-action="remove-ip"][data-ip="100.64.0.7"]').click()
        page.wait_for_function('document.querySelector("#relay-nodes-status").textContent.includes("已移除")')
        assert state['binding_deletes'][-1] == {'member': 'hermes-remote', 'ip': '100.64.0.7'}
        page.wait_for_function('!document.querySelector("#relay-node-list").textContent.includes("100.64.0.7")')
        assert '100.64.0.5' in page.locator('#relay-node-list article[data-relay-member="hermes-remote"]').inner_text()

        # Two independent page snapshots must not overwrite each other's additions.
        # A stale page that still has a removed IP must not restore it when adding another IP.
        close_dialog()
        page2 = context.new_page()
        page2.on('pageerror', lambda error: errors.append(str(error)))
        page2.route('**/*', route)
        page2.goto('http://localhost/app/index.html')
        page2.wait_for_function('document.querySelector("#login").hidden && S.isUserIdentity === true')
        page2.locator('#btn-settings').click()
        page2.locator('#btn-relay-nodes').click()
        page2.wait_for_function('document.querySelector("#relay-nodes-dialog").open')
        page2.wait_for_function('!document.querySelector("#relay-nodes-status").textContent.includes("正在读取")')
        open_dialog()

        def add_via_card(target, ip):
            card = target.locator('#relay-node-list article[data-relay-member="hermes-remote"]')
            card.locator('button[data-action="add-ip"]').click()
            form = card.locator('[data-ip-form]')
            form.locator('input').fill(ip)
            form.locator('button[type="submit"]').click()
            target.wait_for_function(
                'document.querySelector("#relay-nodes-status").textContent.includes("添加来源 IP")')
            target.wait_for_function(
                f'document.querySelector(\'#relay-node-list article[data-relay-member="hermes-remote"]\').textContent.includes("{ip}")')

        add_via_card(page, '100.64.0.8')
        add_via_card(page2, '100.64.0.9')
        remote_bindings = next(item['ips'] for item in state['bindings'] if item['member'] == 'hermes-remote')
        assert set(remote_bindings) == {'100.64.0.5', '100.64.0.8', '100.64.0.9'}, remote_bindings

        page2.locator('#relay-node-list article[data-relay-member="hermes-remote"] '
                     'button[data-action="remove-ip"][data-ip="100.64.0.5"]').click()
        page2.wait_for_function('document.querySelector("#relay-nodes-status").textContent.includes("已移除")')
        page2.wait_for_function(
            '!document.querySelector(\'#relay-node-list article[data-relay-member="hermes-remote"]\').textContent.includes("100.64.0.5")')
        add_via_card(page, '100.64.0.10')
        remote_bindings = next(item['ips'] for item in state['bindings'] if item['member'] == 'hermes-remote')
        assert set(remote_bindings) == {'100.64.0.8', '100.64.0.9', '100.64.0.10'}, remote_bindings
        assert state['binding_posts'][-1] == {'member': 'hermes-remote', 'ip': '100.64.0.10'}
        page.wait_for_function(
            '!document.querySelector(\'#relay-node-list article[data-relay-member="hermes-remote"]\').textContent.includes("100.64.0.5")')
        page2.close()

        # The actual peer IP belongs to the node card; never-connected remains explicit.
        state['nodes'][0].update({'peer_ip': '100.64.0.5', 'state': 'online',
                                  'last_heartbeat': 1760000000.0,
                                  'mcp_fingerprint': 'abcdef0123456789', 'capacity_used': 2,
                                  'code_version': '0.2.1'})
        page.locator('#relay-refresh').click()
        page.wait_for_function('document.querySelector("#relay-node-list").textContent.includes("abcdef0123456789")')
        remote_text = page.locator('#relay-node-list article[data-relay-member="hermes-remote"]').inner_text()
        assert '100.64.0.5' in remote_text and '尚未连接' not in remote_text
        spare_text = page.locator('#relay-node-list article[data-relay-member="hermes-spare"]').inner_text()
        assert '尚未连接' in spare_text

        # Handshake state from the same page after refresh.
        page.wait_for_function('document.querySelector("#relay-node-list").textContent.includes("在线")')
        text = page.locator('#relay-node-list').inner_text()
        assert 'abcdef0123456789' in text
        assert '2' in text and '0.2.1' in text
        assert state['register_posts'] == posts
        assert page.locator('#relay-credential').is_hidden()

        # Revoke needs a separate confirmation; cancel does nothing.
        page.locator('#relay-node-list article[data-relay-member="hermes-remote"] button[data-action="revoke"]').click()
        page.wait_for_function('document.querySelector("#app-alert").open')
        assert '吊销 hermes-remote' in page.locator('#app-alert-message').inner_text()
        assert '当前在线连接会立即断开' in page.locator('#app-alert-message').inner_text()
        page.locator('#app-alert button', has_text='取消').click()
        assert state['revoke_posts'] == 0
        page.locator('#relay-node-list article[data-relay-member="hermes-remote"] button[data-action="revoke"]').click()
        page.wait_for_function('document.querySelector("#app-alert").open')
        page.locator('#app-alert button', has_text='确定').click()
        page.wait_for_function('document.querySelector("#relay-nodes-status").textContent.includes("已吊销")')
        page.wait_for_function('document.querySelector("#relay-node-list").textContent.includes("凭据已吊销")')
        assert state['revoke_posts'] == 1
        assert page.locator('#relay-node-list article[data-relay-member="hermes-remote"] button[data-action="revoke"]').count() == 0

        # The same modal delivers a scoped command and monitors redemption.
        state['listen'].update(remote_enabled=True, remote_address='100.64.0.10', remote_port=18042)
        remote = next(n for n in state['nodes'] if n['member'] == 'hermes-remote')
        remote['host_profile'] = 'default'
        page.locator('#relay-refresh').click()
        page.wait_for_function('document.querySelector("#relay-listen-summary").textContent.includes("100.64.0.10")')
        reissue = page.locator('#relay-node-list article[data-relay-member="hermes-remote"] button[data-action="reissue"]')
        reissue.click()
        page.locator('#relay-join-copy').wait_for()
        assert '发给远程 Hermes' in page.locator('#relay-join-guide').inner_text()
        page.locator('#relay-join-cancel').click()
        page.wait_for_function('!document.querySelector("#relay-join-dialog").open')
        assert state['renewal_state'] == 'cancelled'
        reissue.click()
        page.locator('#relay-join-copy').click()
        new_credential = 'test-renewal-ticket'
        assert page.evaluate('navigator.clipboard.readText()') == (
            'hermes -p "default" bot-group-relay register --engine-url "https://engine.example" '
            f'--fingerprint "{"a" * 64}" --invite "test-renewal-ticket"')
        page.screenshot(path=str(evidence / 'relay-renewal-command.png'))
        remote['credential_revoked'] = False
        state['renewal_state'] = 'confirmed'
        page.wait_for_function('document.querySelector("#relay-nodes-status").textContent.includes("新凭据已在引擎生效")')
        assert page.locator('#relay-join-command').input_value() == ''
        assert state['register_posts'] == posts
        page.wait_for_function('document.querySelector("#relay-node-list").textContent.includes("离线")')

        # Bot identity: read-only explanation, no node requests.
        close_dialog()
        gets_before_bot = state['node_gets']
        binding_gets_before_bot = state['binding_gets']
        listen_gets_before_bot = (state['listen_gets'], state['listen_status_gets'], len(state['listen_posts']))
        page.evaluate('S.isUserIdentity = false; document.dispatchEvent(new Event("service-auth-changed"))')
        open_settings()
        assert page.locator('#btn-relay-nodes').is_visible()
        page.locator('#btn-relay-nodes').click()
        page.wait_for_function('document.querySelector("#relay-nodes-dialog").open')
        assert page.locator('#relay-readonly').is_visible()
        assert '仅可查看说明' in page.locator('#relay-readonly').inner_text()
        assert page.locator('#relay-admin-body').is_hidden()
        assert state['node_gets'] == gets_before_bot
        assert state['binding_gets'] == binding_gets_before_bot
        assert (state['listen_gets'], state['listen_status_gets'], len(state['listen_posts'])) == listen_gets_before_bot
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
        state['forbidden'] = True
        page.locator('#relay-refresh').click()
        page.wait_for_function('document.querySelector("#relay-nodes-status").dataset.error === "true"')
        assert '无权管理远程节点' in page.locator('#relay-nodes-status').inner_text()
        state['forbidden'] = False
        page.locator('#relay-refresh').click()
        page.wait_for_function('document.querySelector("#relay-nodes-status").dataset.error === "false"')
        close_dialog()

        # 390px, both themes, no horizontal overflow.
        page.set_viewport_size({'width': 390, 'height': 844})
        open_dialog()
        page.locator('#relay-nodes-dialog').evaluate('(el) => { el.scrollTop = 0; }')
        box = page.locator('#relay-nodes-dialog').bounding_box()
        assert box['x'] >= 0 and box['x'] + box['width'] <= 390
        overflow = page.locator('#relay-nodes-dialog').evaluate('(el) => el.scrollWidth - el.clientWidth')
        assert overflow <= 0, overflow
        for theme in ('dark', 'light'):
            page.evaluate('(theme) => applyTheme(theme)', theme)
            assert desktop_input_width > check_workspace_layout(spare_card)
            page.screenshot(path=str(evidence / f'relay-nodes-390-{theme}.png'))
        page.evaluate("applyTheme('dark')")
        spare_add = page.locator('#relay-node-list article[data-relay-member="hermes-spare"] button[data-action="add-ip"]')
        spare_add.scroll_into_view_if_needed()
        assert spare_add.is_visible()
        for theme in ('dark', 'light'):
            page.evaluate('(theme) => applyTheme(theme)', theme)
            page.locator('#relay-node-list').evaluate('(el) => { el.scrollTop = el.scrollHeight; }')
            page.locator('#relay-nodes-dialog').evaluate(
                '(el) => { el.scrollTop = Math.max(0, Math.round((el.scrollHeight - el.clientHeight) * 0.55)); }')
            metrics = page.evaluate("""() => {
                const d = document.querySelector('#relay-nodes-dialog');
                const f = d.querySelector(':scope > .dlg-btns');
                return {
                    dialogBottom: d.getBoundingClientRect().bottom,
                    footerBottom: f.getBoundingClientRect().bottom,
                    footerPaddingBottom: getComputedStyle(f).paddingBottom,
                };
            }""")
            assert metrics['footerBottom'] >= metrics['dialogBottom'] - 1, metrics
            assert metrics['footerPaddingBottom'] == '26px', metrics
            page.screenshot(path=str(evidence / f'relay-nodes-390-{theme}-scrolled.png'))
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
        assert new_credential not in stored
        delete = spare_card.locator('[data-action="delete-node"]')
        delete.click()
        page.locator('#app-alert button', has_text='取消').click()
        assert delete.is_visible()
        assert not any(w[:2] == ('DELETE', '/relay/nodes/hermes-spare') for w in writes)
        delete.click()
        assert '聊天和任务历史会保留' in page.locator('#app-alert-message').inner_text()
        page.locator('#app-alert button', has_text='确定').click()
        spare_card.wait_for(state='detached')
        assert sum(w[:2] == ('DELETE', '/relay/nodes/hermes-spare') for w in writes) == 1
        assert not errors, errors
        assert all(path.startswith('/relay/nodes') or path.startswith('/admin/source-ips')
                   or path == '/admin/listen'
                   for method, path, _ in writes if method in ('POST', 'DELETE')), writes
        browser.close()
    print('PASS: node management, listener, independent bindings, rotation, read-only access and 390px themes')


if __name__ == '__main__':
    main()
