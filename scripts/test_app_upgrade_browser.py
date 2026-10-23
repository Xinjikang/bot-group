"""Real browser interactions with isolated upgrade API responses."""
import argparse
import mimetypes
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def run(evidence):
    state = {'checks': 0, 'posts': 0, 'receipt': {}, 'fail_check': False, 'lost_response': False}
    update = {'status': 'ok', 'current_version': 'v1.0.0', 'version_label': 'v1.0.0',
              'latest_version': 'v1.1.0', 'update_available': True, 'can_upgrade': True}
    errors = []
    def route(req):
        path = urlparse(req.request.url).path
        if path.startswith('/app/'):
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
            data = {'version_label': 'v1.0.0'}
        elif path == '/system/upgrade':
            if req.request.method == 'POST':
                state['posts'] += 1
                payload = req.request.post_data_json
                assert payload['version'] == 'v1.1.0'
                state['receipt'] = {'id': payload['request_id'], 'state': 'running', 'phase': 'downloading',
                    'target_version': 'v1.1.0', 'new_instance_id': 'new', 'program_root': 'isolated', 'db_path': 'isolated.db'}
                if state['lost_response']:
                    req.abort()
                    return
            data = state['receipt']
        elif path == '/health':
            data = {'status': 'ok', 'instance_id': 'new', 'program_root': 'isolated', 'db_path': 'isolated.db'}
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
        page.locator('#app-about-close').click()
        page.locator('#app-update-notice').click()
        assert '升级到 v1.1.0' in page.locator('#app-alert-message').inner_text()
        page.screenshot(path=str(evidence / 'upgrade-confirm-390.png'))
        state['lost_response'] = True
        page.get_by_role('button', name='确定', exact=True).click()
        page.wait_for_function('document.querySelector("#app-upgrade-progress").textContent.includes("下载")')
        assert state['posts'] == 1
        assert page.locator('#app-run-upgrade').is_disabled()
        assert page.locator('#btn-restart').is_disabled()
        page.reload()
        page.wait_for_function('document.querySelector("#app-upgrade-progress").textContent.includes("下载")')
        assert state['posts'] == 1
        state['receipt'].update(state='failed', error='升级失败，已恢复原版本并启动服务。')
        page.wait_for_function('document.querySelector("#app-upgrade-progress").textContent.includes("已恢复原版本")')
        assert page.evaluate('sessionStorage.getItem("ge_app_upgrade")') is None
        state['lost_response'] = False
        page.locator('#app-run-upgrade').click()
        page.get_by_role('button', name='确定', exact=True).click()
        page.wait_for_function('document.querySelector("#app-upgrade-progress").textContent.includes("下载")')
        state['receipt']['state'] = 'succeeded'
        page.locator('#app-upgrade-refresh').wait_for(state='visible')
        assert '已升级到 v1.1.0' in page.locator('#app-upgrade-progress').inner_text()
        assert 'v1.1.0' in page.locator('#app-about-version').inner_text()
        assert state['posts'] == 2
        page.screenshot(path=str(evidence / 'upgrade-complete-390.png'))
        assert not errors, errors
        browser.close()
    print('PASS: manual check, latest/error states, both confirmation entries, cancellation, lost POST, reload recovery, failure, success, narrow themes')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    run(args.evidence)
