"""Exercise the real member dialog in Chrome with synthetic APIs only."""
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    calls, errors, pending, joins = [], [], [], []
    state = {'fail': True, 'delay': False, 'empty': False, 'hold': False}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))

        def route(req):
            path = urlparse(req.request.url).path
            if path.startswith('/app/'):
                file = ROOT / 'frontend' / (path[5:] or 'index.html')
                req.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
                return
            data = {}
            if path == '/discovery/scan':
                if state['delay']:
                    pending.append(req)
                    return
                data = {'candidates': [] if state['empty'] else [
                    {'host_type': 'codex', 'profile': 'default', 'config_root': 'E:/test/codex',
                     'display_name': '基础配置', 'already_project_member': True},
                    {'host_type': 'codex', 'profile': 'other', 'config_root': 'E:/test/codex/profiles/other'},
                    {'host_type': 'hermes', 'profile': 'default', 'config_root': 'E:/test/hermes'}]}
            elif path == '/profiles/create':
                calls.append(req.request.post_data_json)
                if state['hold']:
                    pending.append(req)
                    return
                if state['fail']:
                    req.fulfill(status=409, json={'detail': '同名 profile 已存在'})
                    return
                data = {'created': True, 'home': 'E:/test/profiles/new-dev', 'notice': '已创建 profile 并加入项目。',
                        'setup_command': 'hermes -p fresh setup' if state['empty'] else ''}
            elif path == '/bots/register':
                joins.append(req.request.post_data_json)
                data = {'created': False}
            elif path == '/projects':
                data = {'projects': []}
            elif path.endswith('/workspaces'):
                data = {'members': []}
            elif path == '/members':
                data = {'members': []}
            elif path in ('/messages', '/threads', '/tasks'):
                data = {'messages': [], 'threads': [], 'tasks': [], 'requirements': []}
            req.fulfill(json=data)

        page.route('**/*', route)
        page.goto('http://workspace.test/app/index.html')
        page.evaluate("() => { showLogin(false); S.projects=[{name:'p',display_name:'测试项目',root:'E:/test',directory_mode:'shared'}]; openProjectDetail('p', $('#chip-all')); selectProjectTab('members'); }")
        page.locator('#pj-members-add').click()
        page.locator('[name=member-add-host][value=codex]').check()
        page.locator('#member-add-next').click()
        page.wait_for_function("document.querySelectorAll('[name=member-add-profile]').length === 1")
        page.locator('[name=member-add-mode][value=create]').check()
        assert page.locator('#member-create-source option').count() == 3
        assert page.locator('#member-create-source').input_value() == '0'
        assert page.locator('#member-create-auth').count() == 0
        assert '主账户认证' in page.locator('#member-create-auth-help').inner_text()
        assert page.locator('#member-add-join').is_disabled()
        page.locator('#member-create-name').fill('小夏')
        page.locator('#member-create-profile').fill('new-dev')
        page.locator('.member-create-fields summary').click()
        page.locator('#member-create-model').fill('chosen-model')
        page.locator('#member-create-instructions').fill('认真核对证据。')
        page.locator('#member-add-join').click()
        page.wait_for_function("document.querySelector('#member-add-status').textContent.includes('同名')")
        assert page.locator('#member-create-name').input_value() == '小夏'
        assert page.locator('#member-create-instructions').input_value() == '认真核对证据。'
        assert page.locator('#member-create-instructions').is_visible()
        assert not page.locator('#member-add-close').is_disabled()
        out = ROOT / 'reports' / 'tests' / 'profile-create'
        out.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out / 'desktop.png'))
        page.set_viewport_size({'width': 390, 'height': 844})
        page.screenshot(path=str(out / 'mobile.png'))
        page.evaluate("applyTheme('light')")
        page.screenshot(path=str(out / 'mobile-light.png'))
        page.evaluate("applyTheme('dark')")
        assert page.locator('#dlg-member-add').evaluate('el => el.scrollWidth <= el.clientWidth + 1')
        page.locator('#member-add-join').scroll_into_view_if_needed()
        state['fail'] = False
        state['hold'] = True
        page.locator('#member-add-join').click()
        page.wait_for_function("document.querySelector('#member-add-close').disabled")
        page.keyboard.press('Escape')
        assert page.locator('#dlg-member-add').evaluate('el => el.open')
        assert len(calls) == 2
        pending.pop().fulfill(json={'created': True, 'notice': '已创建并加入项目。'})
        page.wait_for_function("!document.querySelector('#dlg-member-add').open")
        assert calls[0] == calls[1]
        assert calls[1]['source']['profile'] == 'default'
        assert calls[1]['project'] == 'p'
        assert calls[1]['model'] == 'chosen-model'
        assert 'reuse_auth' not in calls[1]
        # A late scan from a dismissed dialog cannot mutate the next dialog.
        state['delay'] = True
        page.evaluate('openMemberAdd()')
        page.locator('[name=member-add-host][value=codex]').check()
        page.locator('#member-add-next').click()
        page.locator('#member-add-close').click()
        page.evaluate('openMemberAdd()')
        pending.pop().fulfill(json={'candidates': []})
        assert '1/2' in page.locator('#member-add-title').inner_text()
        state.update(delay=False, empty=True, hold=False)
        page.locator('[name=member-add-host][value=hermes]').check()
        page.locator('#member-add-next').click()
        page.wait_for_function("document.querySelector('#member-add-status').textContent.includes('暂无')")
        page.locator('[name=member-add-mode][value=create]').check()
        assert page.locator('#member-create-auth').is_disabled()
        page.locator('#member-create-profile').fill('fresh')
        page.locator('#member-add-join').click()
        page.locator('#app-alert').wait_for(state='visible')
        assert 'hermes -p fresh setup' in page.locator('#app-alert').inner_text()
        assert calls[-1]['source'] is None
        assert calls[-1]['reuse_auth'] is False
        assert calls[-1]['host_type'] == 'hermes'
        page.locator('#app-alert button.primary').click()
        state['empty'] = False
        page.evaluate('openMemberAdd()')
        page.locator('[name=member-add-host][value=codex]').check()
        page.locator('#member-add-next').click()
        page.locator('[name=member-add-profile]').check()
        page.locator('#member-add-join').click()
        page.wait_for_function("!document.querySelector('#dlg-member-add').open")
        assert joins == [{'project': 'p', 'host_type': 'codex', 'profile': 'other',
                          'config_root': 'E:/test/codex/profiles/other'}]
        page.reload()
        page.evaluate("() => { showLogin(false); S.projects=[{name:'p',display_name:'测试项目',root:'E:/test',directory_mode:'shared'}]; openProjectDetail('p', $('#chip-all')); selectProjectTab('members'); }")
        page.locator('#pj-members-add').click()
        page.locator('[name=member-add-host][value=codex]').check()
        page.locator('#member-add-next').click()
        page.locator('[name=member-add-mode][value=create]').check()
        page.locator('#member-create-source').select_option('')
        assert page.locator('#member-create-auth').count() == 0
        assert '主账户认证' in page.locator('#member-create-auth-help').inner_text()
        assert not errors, errors
        browser.close()
    print('PASS: profile create, retry, source selection, narrow layout, stale scans and login guidance')


if __name__ == '__main__':
    main()
