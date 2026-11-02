"""Real Chrome, synthetic HTTP: editable DSH creation and existing member availability."""
import argparse
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DSH_ROOT = 'C:/dsh-home'


def run(evidence):
    state = {'scan': 'ok', 'create_ok': True, 'candidates': []}
    scans, validates, registers, creates = [], [], [], []

    def route(request):
        path = urlparse(request.request.url).path
        if path.startswith('/app/'):
            file = ROOT / 'frontend' / (path[5:] or 'index.html')
            if file.is_file():
                request.fulfill(body=file.read_bytes(),
                                content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
            else:
                request.fulfill(status=404)
            return
        if path == '/auth/status':
            request.fulfill(json={'configured': False}); return
        if path == '/discovery/scan':
            scans.append(path)
            if state['scan'] == 'error':
                request.fulfill(status=503, json={'detail': '模拟扫描失败'}); return
            candidates = [{'host_type': 'hermes', 'profile': 'default', 'config_root': 'C:/hermes'}]
            if state['scan'] == 'ok':
                candidates.extend([{'host_type': 'dsh', 'profile': name, 'config_root': DSH_ROOT}
                                   for name in ('desktop', 'headless')])
            request.fulfill(json={'candidates': candidates}); return
        if path == '/bots/validate-dsh':
            body = request.request.post_data_json
            validates.append(body)
            errors = []
            request.fulfill(json={'ok': not errors, 'errors': errors,
                                  'config_root': body.get('config_root', ''),
                                  'profile': body.get('profile', '')}); return
        if path == '/bots/register':
            body = request.request.post_data_json
            registers.append(body)
            request.fulfill(json={'bot_id': 77, 'created': True, 'profile': body.get('profile'),
                                  'member_profile': None, 'project': None}); return
        if path == '/profiles/create':
            body = request.request.post_data_json
            creates.append(body)
            if not state['create_ok']:
                request.fulfill(status=409, json={'detail': '同名 profile 已存在，请更换标识'}); return
            request.fulfill(json={'bot_id': 77, 'created': True, 'profile': body['profile'],
                                  'host_type': body['host_type'], 'config_root': DSH_ROOT,
                                  'member_profile': None, 'project': None}); return
        if path == '/profiles/source-settings':
            body = request.request.post_data_json
            source = body.get('source') or {}
            if body['host_type'] == 'hermes':
                request.fulfill(json={'model': 'hermes-model', 'provider': 'openrouter'}); return
            request.fulfill(json={'model': 'deepseek-flash', 'provider': 'deepseek-account'}
                            if source.get('profile') == 'desktop' else {'model': '', 'provider': ''}); return
        if path == '/profiles/candidates':
            request.fulfill(json={'candidates': state['candidates']}); return
        if path == '/projects/p/workspaces':
            member = {'profile': 'bot-77', 'display_name': 'DSH Bot', 'host_profile': 'dsh-a',
                      'runtime': 'dsh', 'bot_id': 77, 'active': 1, 'role': '',
                      'workspace': {'valid': True, 'path': 'E:/work/dsh-a'},
                      'directory_mode': 'shared'}
            request.fulfill(json={'project': {'name': 'p', 'directory_mode': 'shared', 'worktree_root': ''},
                                  'members': [member], 'history': [], 'repository': {}}); return
        if path == '/thinking':
            request.fulfill(json={'runs': [], 'thinking': []}); return
        if path == '/events':
            request.fulfill(body=': ok\n\n', content_type='text/event-stream'); return
        if path == '/hud' or path.startswith('/hud/'):
            request.fulfill(json={'id': 'hud-test', 'status': 'idle', 'state': {}, 'entries': [],
                                  'revision': 1, 'project': None}); return
        request.fulfill(json={'projects': [], 'members': [], 'threads': [], 'messages': [], 'tasks': [],
                              'requirements': [], 'pending_approval': [], 'runs': []})

    def open_dialog(page):
        page.evaluate("""() => { window.__created = null;
            openProfileCreate({project: 'p', onCreated: item => { window.__created = item; }}); }""")
        page.wait_for_function("document.querySelector('#dlg-member-add')?.open === true")
        page.wait_for_function('memberAdd && !memberAdd.scanning')

    def select_dsh(page):
        page.locator('[name="member-add-host"][value="dsh"]').check()
        page.wait_for_function("memberAdd.host === 'dsh' && !memberAdd.scanning")

    def status(page):
        return page.locator('#member-add-status').text_content()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', route)
        page.goto('http://dsh.test/app/')
        page.wait_for_function('typeof openProfileCreate === "function"')

        # ---- ① 固定三项宿主 + 扫描成功 ----
        open_dialog(page)
        hosts = page.locator('[name="member-add-host"]')
        assert hosts.count() == 3
        assert [hosts.nth(i).get_attribute('value') for i in range(3)] == ['hermes', 'codex', 'dsh']
        page.locator('#member-create-name').fill('新成员')
        select_dsh(page)
        profile = page.locator('#member-create-profile')
        assert profile.input_value() == ''
        assert profile.is_editable()
        assert page.locator('#member-add-title').inner_text() == '创建 profile'
        assert page.locator('#member-add-join').inner_text() == '创建 profile'
        assert page.locator('#member-add-join').is_disabled()
        assert status(page) == ''
        assert '已发现' not in page.locator('#dlg-member-add').inner_text()
        assert page.locator('#member-create-source option').count() == 3
        assert page.locator('#member-create-source').input_value() == '0'
        assert page.locator('#member-create-model').input_value() == 'deepseek-flash'
        assert page.locator('#member-create-provider').input_value() == 'deepseek-account'
        profile.press_sequentially('new-headless')
        page.locator('#member-create-source').select_option('1')
        page.wait_for_function('!memberAdd.sourceLoading')
        assert page.locator('#member-create-model').input_value() == ''
        assert page.locator('#member-create-provider').input_value() == ''
        assert page.locator('#member-add-join').is_disabled()
        page.locator('#member-create-source').select_option('0')
        page.wait_for_function('!memberAdd.sourceLoading')
        assert profile.input_value() == 'new-headless'
        assert profile.is_editable()
        page.locator('[name="member-add-host"][value="codex"]').check()
        page.wait_for_function('!memberAdd.scanning')
        assert page.locator('#member-create-provider').is_hidden()
        assert profile.input_value() == 'new-headless'
        select_dsh(page)
        assert profile.input_value() == 'new-headless'
        page.locator('#member-add-steps details summary').click()
        page.locator('#member-create-model').fill('chosen-model')
        page.locator('#member-create-provider').fill('')
        assert page.locator('#member-add-join').is_disabled()
        page.locator('#member-create-provider').fill('deepseek-official')
        page.locator('#member-create-source').select_option('1')
        assert page.locator('#member-create-model').input_value() == 'chosen-model'
        assert page.locator('#member-create-provider').input_value() == 'deepseek-official'
        page.locator('#member-create-instructions').fill('独立个人指令')
        state['create_ok'] = False
        page.locator('#member-add-join').click()
        page.wait_for_function('document.querySelector("#member-add-status").textContent.includes("创建失败")')
        assert '同名 profile 已存在' in status(page)
        assert profile.input_value() == 'new-headless'
        assert profile.is_editable()
        assert page.locator('#member-create-instructions').input_value() == '独立个人指令'
        profile.fill('new-agent')
        for theme in ('dark', 'light'):
            page.evaluate('applyTheme', theme)
            for width in (1280, 390):
                page.set_viewport_size({'width': width, 'height': 900})
                assert page.locator('#dlg-member-add').evaluate('e => e.scrollWidth <= e.clientWidth + 1')
                page.locator('#dlg-member-add').screenshot(path=str(evidence / f'dsh-create-{width}-{theme}.png'))
        state['create_ok'] = True
        page.locator('#member-add-join').click()
        page.wait_for_function('window.__created !== null')
        assert creates[-1]['profile'] == 'new-agent'
        assert creates[-1]['host_type'] == 'dsh'
        assert creates[-1]['source'] == {'profile': 'headless', 'config_root': DSH_ROOT}
        assert creates[-1]['model'] == 'chosen-model'
        assert creates[-1]['provider'] == 'deepseek-official'
        assert creates[-1]['instructions'] == '独立个人指令'
        assert not registers and not validates

        for scan in ('empty', 'error'):
            state['scan'] = scan
            open_dialog(page)
            select_dsh(page)
            assert profile.input_value() == '' and profile.is_editable()
            assert page.locator('#member-create-source').input_value() == ''
            assert page.locator('#member-create-dsh-home').count() == 0
            if scan == 'error':
                assert '模拟扫描失败' in status(page)
            profile.fill('default-agent')
            assert page.locator('#member-add-join').is_disabled()
            page.locator('#member-create-model').fill('manual-model')
            page.locator('#member-create-provider').fill('deepseek-account')
            page.locator('#member-add-join').click()
            page.wait_for_function('window.__created !== null')
            assert creates[-1]['source'] is None
        page.set_viewport_size({'width': 1280, 'height': 900})

        # Late source responses cannot overwrite the latest source or a manually edited pair.
        state['scan'] = 'ok'
        open_dialog(page)
        select_dsh(page)
        page.evaluate('''() => {
            window.__sourceApi = api; window.__pendingSources = [];
            api = (path, options) => path === '/profiles/source-settings'
                ? new Promise(resolve => window.__pendingSources.push(resolve))
                : window.__sourceApi(path, options);
        }''')
        page.locator('#member-create-source').select_option('1')
        page.locator('#member-create-source').select_option('0')
        assert page.evaluate('window.__pendingSources.length') == 2
        page.evaluate('window.__pendingSources[1]({model:"latest-model", provider:"latest-provider"})')
        page.wait_for_function('!memberAdd.sourceLoading')
        page.evaluate('window.__pendingSources[0]({model:"stale-model", provider:"stale-provider"})')
        assert page.locator('#member-create-model').input_value() == 'latest-model'
        assert page.locator('#member-create-provider').input_value() == 'latest-provider'
        page.locator('#member-create-source').select_option('1')
        profile.fill('race-agent')
        page.locator('#member-create-model').fill('manual-model')
        page.locator('#member-create-provider').fill('manual-provider')
        page.evaluate('window.__pendingSources[2]({model:"late-model", provider:"late-provider"})')
        page.wait_for_function('!memberAdd.sourceLoading')
        assert page.locator('#member-create-model').input_value() == 'manual-model'
        assert page.locator('#member-create-provider').input_value() == 'manual-provider'
        assert page.locator('#member-add-join').is_enabled()
        page.evaluate('() => { api = window.__sourceApi; }')
        page.locator('[name="member-add-host"][value="hermes"]').check()
        page.wait_for_function('!memberAdd.scanning')
        assert page.locator('#member-create-model').input_value() == 'hermes-model'
        assert page.locator('#member-create-provider').input_value() == 'openrouter'
        select_dsh(page)
        assert page.locator('#member-create-model').input_value() == 'manual-model'
        assert page.locator('#member-create-provider').input_value() == 'manual-provider'
        page.locator('#member-add-close').click()

        # ---- ④ 已创建 dsh Bot 的绑定资源失效 ----
        state['candidates'] = []
        page.evaluate("""async () => {
            projectMembersCache.clear(); projectMembersVersions.clear();
            document.querySelector('#pj-detail-form').dataset.origName = 'p';
            document.querySelector('#dlg-project-detail').showModal();
            document.querySelector('#pj-tab-members').hidden = false;
            selectProjectTab('members');
            await loadProjectMembers();
        }""")
        marker = page.locator('[data-binding-invalid="true"]')
        marker.wait_for()
        assert '宿主绑定失效' in marker.inner_text(), marker.inner_text()
        assert '成员与历史保留' in marker.inner_text(), marker.inner_text()
        page.locator('#pj-member-list').screenshot(path=str(evidence / 'dsh-binding-invalid.png'))
        state['candidates'] = [{'host_type': 'dsh', 'profile': 'dsh-a', 'config_root': DSH_ROOT,
                                'bot_id': 77, 'bound': True, 'already_project_member': True}]
        page.evaluate("""async () => {
            projectMembersCache.clear(); projectMembersVersions.clear();
            await loadProjectMembers();
        }""")
        page.wait_for_function('!document.querySelector("[data-binding-invalid=\\"true\\"]")')
        assert page.locator('#pj-member-list').inner_text().find('宿主绑定失效') == -1
        page.locator('#pj-member-list').screenshot(path=str(evidence / 'dsh-binding-invalid-and-recovered.png'))

        assert not errors, errors
        browser.close()

    print('PASS: editable unique DSH profile, common form, source and host switch preservation, conflict retry, defaults, payload, binding invalidation, desktop/390px themes')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    run(args.evidence)
