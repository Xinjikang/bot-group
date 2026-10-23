"""Isolated Chrome checks for settings, update timing and resume behavior."""
import argparse
import mimetypes
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def run(evidence):
    state = {'checks': 0, 'failure': False, 'hold': False, 'held': [], 'version': 'v1.0.0'}
    errors, writes = [], []
    update = {'status': 'ok', 'current_version': 'v1.0.0', 'latest_version': 'v1.1.0', 'update_available': True}
    def route(req):
        path = urlparse(req.request.url).path
        if path.startswith('/app/'):
            file = ROOT / 'frontend' / (path[5:] or 'index.html')
            req.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
            return
        if req.request.method not in ('GET', 'HEAD'):
            writes.append(path)
        data = {}
        if path == '/auth/status':
            data = {'configured': False}
        elif path == '/system/updates':
            state['checks'] += 1
            if state['hold']:
                state['held'].append(req)
                return
            data = {'status': 'unavailable', 'update_available': False} if state['failure'] else update
        elif path == '/system/version':
            data = {'current_version': state['version'], 'version_label': state.get('label') or state['version'] or '源码副本 · 缺少版本信息'}
        elif path == '/user/display-name':
            data = {'profile': 'user', 'display_name': '很长的用户称呼，用于窄屏验证'}
        elif path == '/appearance/background':
            req.fulfill(status=204, headers={'X-Background-Configured': 'true'})
            return
        elif path == '/projects':
            data = {'projects': []}
        elif path == '/members':
            data = {'members': []}
        elif path == '/messages':
            data = {'messages': []}
        elif path == '/threads':
            data = {'threads': []}
        elif path == '/tasks':
            data = {'tasks': [], 'requirements': [], 'pending_approval': []}
        req.fulfill(json=data)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', route)
        page.clock.install()
        page.goto('http://settings.test/app/')
        notice = page.locator('#app-update-notice')
        notice.wait_for(state='visible')
        assert state['checks'] == 1
        assert page.locator('#app-settings-menu').is_hidden()
        assert page.locator('#topbar-right').evaluate('e => e.children[0].id === "app-update-notice" && e.children[1].id === "topbar-user-name"')
        page.locator('#btn-settings').click()
        assert page.locator('#btn-settings').get_attribute('aria-expanded') == 'true'
        page.locator('#btn-theme-top').click()
        assert page.evaluate('document.documentElement.dataset.theme') == 'light'
        assert page.locator('#app-settings-menu').is_hidden()
        page.locator('#btn-settings').click()
        page.locator('#btn-background').click()
        assert page.locator('#dlg-background').is_visible()
        page.evaluate('closeDialogAnchored(document.querySelector("#dlg-background"))')
        page.locator('#btn-settings').click()
        page.locator('#btn-about').click()
        page.wait_for_function('document.querySelector("#app-about-version").textContent.includes("v1.0.0")')
        assert page.locator('#app-check-update').is_enabled()
        assert page.locator('#app-run-upgrade').is_visible()
        page.locator('#app-about-close').click()
        notice.click()
        assert '升级到 v1.1.0' in page.locator('#app-alert-message').inner_text()
        page.get_by_role('button', name='取消', exact=True).click()
        page.locator('#btn-settings').click()
        page.keyboard.press('Escape')
        assert page.locator('#btn-settings').evaluate('e => document.activeElement === e')
        page.locator('#btn-settings').click()
        page.locator('.logo').click()
        assert page.locator('#app-settings-menu').is_hidden()

        page.clock.fast_forward(12 * 60 * 60 * 1000 + 60000)
        page.wait_for_function('!document.querySelector("#app-update-notice").hidden')
        page.wait_for_timeout(100)
        assert state['checks'] == 2, state
        # Advance wall time without running intervals to model a suspended page.
        page.clock.set_system_time(page.evaluate('Date.now()') + 13 * 60 * 60 * 1000)
        state['hold'] = True
        page.evaluate('document.dispatchEvent(new Event("visibilitychange"))')
        page.wait_for_timeout(100)
        assert state['checks'] == 3
        page.evaluate('window.dispatchEvent(new Event("focus")); window.dispatchEvent(new Event("pageshow"))')
        assert state['checks'] == 3
        state['held'].pop().fulfill(json=update)
        state['hold'] = False
        page.wait_for_timeout(100)
        state['failure'] = True
        page.clock.fast_forward(12 * 60 * 60 * 1000 + 60000)
        page.wait_for_timeout(100)
        assert state['checks'] == 4
        assert notice.is_visible()
        assert page.locator('#app-alert').is_hidden()
        state['failure'] = False
        page.clock.fast_forward(6 * 60 * 1000)
        page.wait_for_timeout(100)
        assert state['checks'] == 5
        page.reload()
        notice.wait_for(state='visible')
        assert state['checks'] == 6
        assert page.evaluate('document.documentElement.dataset.theme') == 'light'

        for theme in ('light', 'dark'):
            page.evaluate('applyTheme', theme)
            page.set_viewport_size({'width': 390, 'height': 844})
            page.locator('#btn-settings').click()
            for selector in ('#app-settings-menu', '#btn-settings', '#topbar-user-name', '#app-update-notice', '#conn'):
                rect = page.locator(selector).bounding_box()
                assert rect['x'] >= 0 and rect['x'] + rect['width'] <= 391, (selector, rect)
            page.screenshot(path=str(evidence / f'settings-390-{theme}.png'))
            page.keyboard.press('Escape')
        state['version'] = None
        state['label'] = '开发版 · abc123456789 · 本地有修改'
        update.update(status='development', update_available=False)
        page.reload()
        page.wait_for_timeout(100)
        page.locator('#btn-settings').click()
        page.locator('#btn-about').click()
        page.wait_for_function('document.querySelector("#app-about-version").textContent.includes("abc123456789")')
        assert '按提交记录核对' in page.locator('#app-about-detail').inner_text()
        page.screenshot(path=str(evidence / 'about-git-development.png'))
        page.locator('#app-about-close').click()
        state['label'] = None
        update.update(status='unknown', update_available=False)
        page.reload()
        page.wait_for_timeout(100)
        assert notice.is_hidden()
        page.locator('#btn-settings').click()
        page.locator('#btn-about').click()
        page.wait_for_function('document.querySelector("#app-about-version").textContent.includes("源码副本 · 缺少版本信息")')
        assert '暂时无法比较' in page.locator('#app-about-detail').inner_text()
        page.locator('#app-about-close').click()
        page.evaluate('logout()')
        checks = state['checks']
        page.clock.fast_forward(13 * 60 * 60 * 1000)
        assert state['checks'] == checks
        assert not errors, errors
        assert all(path == '/auth/events' for path in writes), writes
        browser.close()
    print('PASS: initial/reload check, 12-hour timer, overdue resume, in-flight deduplication, quiet retry, unknown version, auth boundary, settings/background/theme, keyboard/outside close, 390px themes')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    run(args.evidence)
