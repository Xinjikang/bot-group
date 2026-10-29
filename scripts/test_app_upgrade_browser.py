"""Real browser interactions with isolated upgrade API responses."""
import argparse
import mimetypes
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def run(evidence):
    state = {'checks': 0, 'posts': 0, 'receipt': {}, 'fail_check': False, 'lost_response': False,
             'loads': 0, 'health_id': 'old', 'hold_post': False, 'held_post': None, 'poll_error': False}
    update = {'status': 'ok', 'current_version': 'v1.0.0', 'version_label': 'v1.0.0',
              'latest_version': 'v1.1.0', 'update_available': True, 'can_upgrade': True}
    errors = []
    def route(req):
        path = urlparse(req.request.url).path
        if path.startswith('/app/'):
            if path == '/app/':
                state['loads'] += 1
            file = ROOT / 'frontend' / (path[5:] or 'index.html')
            req.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
            return
        data = {}
        if path == '/auth/status':
            data = {'configured': False}
        elif path == '/system/updates':
            state['checks'] += 1
            data = {'status': 'unavailable'} if state['fail_check'] else update
        elif path == '/system/version':
            data = {'version_label': update['version_label']}
        elif path == '/system/upgrade':
            if req.request.method == 'POST':
                state['posts'] += 1
                payload = req.request.post_data_json
                assert payload['version'] == 'v1.1.0'
                if state['hold_post']:
                    state['held_post'] = req
                    return
                state['receipt'] = {'id': payload['request_id'], 'state': 'running', 'phase': 'downloading',
                    'target_version': 'v1.1.0', 'new_instance_id': 'new', 'program_root': 'isolated', 'db_path': 'isolated.db'}
                if state['lost_response']:
                    req.abort()
                    return
            elif state['poll_error']:
                req.fulfill(status=500, body='Internal Server Error', content_type='text/plain')
                return
            data = state['receipt']
        elif path == '/health':
            data = {'status': 'ok', 'instance_id': state['health_id'], 'program_root': 'isolated', 'db_path': 'isolated.db'}
        elif path == '/appearance/background':
            req.fulfill(status=204)
            return
        elif path in ('/projects', '/members', '/messages', '/threads'):
            data = {path[1:]: []}
        elif path == '/tasks':
            data = {'tasks': [], 'requirements': [], 'pending_approval': []}
        req.fulfill(json=data)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', route)
        page.goto('http://upgrade.test/app/')
        page.locator('#app-update-notice').wait_for(state='visible')
        page.locator('#btn-settings').click()
        page.locator('#btn-about').click()
        before = state['checks']
        page.locator('#app-check-update').click()
        page.wait_for_function('!document.querySelector("#app-check-update").disabled')
        assert state['checks'] == before + 1
        state['fail_check'] = True
        page.locator('#app-check-update').click()
        page.wait_for_function('document.querySelector("#app-about-detail").textContent.includes("失败")')
        state['fail_check'] = False
        update['update_available'] = False
        page.locator('#app-check-update').click()
        page.wait_for_function('document.querySelector("#app-about-detail").textContent.includes("已是最新")')
        assert page.locator('#app-run-upgrade').is_hidden()
        update['update_available'] = True
        page.locator('#app-check-update').click()
        page.locator('#app-run-upgrade').wait_for(state='visible')
        for theme in ('light', 'dark'):
            page.evaluate('applyTheme', theme)
            page.set_viewport_size({'width': 390, 'height': 844})
            for selector in ('#app-about', '#app-check-update', '#app-run-upgrade', '#app-about-close'):
                rect = page.locator(selector).bounding_box()
                assert rect['x'] >= 0 and rect['x'] + rect['width'] <= 391, (selector, rect)
            page.screenshot(path=str(evidence / f'about-upgrade-390-{theme}.png'))
        page.locator('#app-run-upgrade').click()
        assert '备份' in page.locator('#app-alert-message').inner_text()
        page.get_by_role('button', name='取消', exact=True).click()
        assert state['posts'] == 0
        # Exercise both entry points while the server has not responded.
        state['hold_post'] = True
        for index, status in enumerate((500, 503)):
            if index:
                page.locator('#app-about-close').click()
                page.locator('#app-update-notice').click()
            else:
                page.set_viewport_size({'width': 1280, 'height': 900})
                page.locator('#app-run-upgrade').click()
            page.get_by_role('button', name='确定', exact=True).click()
            page.wait_for_function('document.querySelector("#app-upgrade-progress").textContent.includes("正在提交")')
            page.wait_for_timeout(300)
            assert state['held_post'] is not None
            assert page.locator('#app-about').is_visible()
            assert page.locator('#app-run-upgrade').is_disabled()
            page.screenshot(path=str(evidence / f'upgrade-pending-{status}.png'))
            if status == 500:
                state['held_post'].fulfill(status=500, body='Internal Server Error', content_type='text/plain')
            else:
                state['held_post'].fulfill(status=503, json={'detail': '无法启动升级进程：访问被拒绝'})
            state['held_post'] = None
            page.locator('#app-alert').wait_for(state='visible')
            message = page.locator('#app-alert-message').inner_text()
            assert f'HTTP {status}' in message and 'POST /system/upgrade' in message, message
            assert ('服务端处理请求失败' if status == 500 else '访问被拒绝') in message
            page.get_by_role('button', name='确定', exact=True).click()
            assert page.evaluate('sessionStorage.getItem("ge_app_upgrade")') is None
            page.wait_for_function('!document.querySelector("#app-run-upgrade").disabled')
            assert page.locator('#app-upgrade-progress').get_attribute('data-error') == 'true'
            assert f'HTTP {status}' in page.locator('#app-upgrade-progress').inner_text()
            for width, theme in ((1280, 'dark'), (390, 'light')):
                page.set_viewport_size({'width': width, 'height': 900})
                page.evaluate('applyTheme', theme)
                rect = page.locator('#app-upgrade-progress').bounding_box()
                assert rect['x'] >= 0 and rect['x'] + rect['width'] <= width
                page.screenshot(path=str(evidence / f'upgrade-error-{status}-{width}.png'))
        state['hold_post'] = False
        page.locator('#app-about-close').click()
        page.locator('#app-update-notice').click()
        assert '升级到 v1.1.0' in page.locator('#app-alert-message').inner_text()
        page.screenshot(path=str(evidence / 'upgrade-confirm-390.png'))
        state['lost_response'] = True
        page.get_by_role('button', name='确定', exact=True).click()
        page.wait_for_function('document.querySelector("#app-upgrade-progress").textContent.includes("下载")')
        assert state['posts'] == 3
        assert page.locator('#app-run-upgrade').is_disabled()
        assert page.locator('#btn-restart').is_disabled()
        state['poll_error'] = True
        page.wait_for_function('document.querySelector("#app-upgrade-progress").textContent.includes("HTTP 500")')
        assert 'GET /system/upgrade' in page.locator('#app-upgrade-progress').inner_text()
        assert '自动重试' in page.locator('#app-upgrade-progress').inner_text()
        assert page.evaluate('sessionStorage.getItem("ge_app_upgrade")') is not None
        state['poll_error'] = False
        page.wait_for_function('document.querySelector("#app-upgrade-progress").textContent.includes("下载")')
        assert page.locator('#app-upgrade-progress').get_attribute('data-error') == 'false'
        page.reload()
        page.wait_for_function('document.querySelector("#app-upgrade-progress").textContent.includes("下载")')
        assert state['posts'] == 3
        state['receipt'].update(state='failed', error='升级失败，已恢复原版本并启动服务。')
        page.wait_for_function('document.querySelector("#app-upgrade-progress").textContent.includes("已恢复原版本")')
        assert page.evaluate('sessionStorage.getItem("ge_app_upgrade")') is None
        state['lost_response'] = False
        page.locator('#app-run-upgrade').click()
        page.get_by_role('button', name='确定', exact=True).click()
        page.wait_for_function('document.querySelector("#app-upgrade-progress").textContent.includes("下载")')
        state['receipt']['state'] = 'succeeded'
        loads = state['loads']
        page.wait_for_timeout(1700)
        assert state['loads'] == loads
        assert page.evaluate('S.upgrading')
        update.update(version_label='v1.1.0', current_version='v1.1.0', update_available=False)
        state['health_id'] = 'new'
        page.wait_for_event('load')
        page.wait_for_function('S.isUserIdentity && !document.querySelector("#btn-restart").disabled')
        assert state['loads'] == loads + 1
        assert page.evaluate('sessionStorage.getItem("ge_app_upgrade")') is None
        assert state['posts'] == 4
        page.locator('#btn-settings').click()
        page.locator('#btn-about').click()
        page.wait_for_function('document.querySelector("#app-about-version").textContent.includes("v1.1.0")')
        page.screenshot(path=str(evidence / 'upgrade-complete-390.png'))
        assert not errors, errors
        browser.close()
    print('PASS: manual check, latest/error states, both confirmation entries, cancellation, pending POST, HTTP 500/503, polling failure/recovery, lost POST, reload recovery, failure, success, narrow themes')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    run(args.evidence)
