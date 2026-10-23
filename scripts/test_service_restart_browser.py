"""Real Chrome UI, synthetic HTTP only: ordering, receipt polling and reload."""
import json
import mimetypes
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def run():
    state = {'posts': 0, 'phase': 'draining', 'health_id': 'old', 'fail': False}
    receipt = {'id': 'operation', 'instance_id': 'old', 'program_root': 'isolated', 'db_path': 'isolated.db'}
    def route(request):
        path = urlparse(request.request.url).path
        if path.startswith('/app/'):
            file = ROOT / 'frontend' / (path[5:] or 'index.html')
            if file.is_file():
                request.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
            else:
                request.fulfill(status=404)
        elif path == '/admin/restart':
            if request.request.method == 'POST':
                state['posts'] += 1
                request.fulfill(status=202, json={**receipt, 'state': 'starting'})
            else:
                request.fulfill(json={**receipt, 'state': 'failed' if state['fail'] else
                    'succeeded' if state['phase'] == 'done' else 'restarting',
                    'new_instance_id': 'new', 'shutdown': {'state': state['phase']}, 'error': '隔离测试：启动失败'})
        elif path == '/health':
            request.fulfill(json={**receipt, 'status': 'ok', 'instance_id': state['health_id']})
        elif path == '/auth/status':
            request.fulfill(json={'configured': False})
        elif path == '/appearance/background':
            request.fulfill(status=404)
        elif path == '/events':
            request.fulfill(body=': ok\n\n', content_type='text/event-stream')
        else:
            request.fulfill(json={'projects': [], 'members': [], 'threads': [], 'messages': [],
                                  'tasks': [], 'requirements': [], 'runs': []})

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1400, 'height': 900})
        page.route('**/*', route)
        page.goto('http://restart.test/app/')
        button = page.locator('#btn-restart')
        page.locator('#settings-menu summary').click()
        button.wait_for(state='visible')
        order = page.locator('#topbar-right').evaluate('(e) => [...e.children].map(c => c.id)')
        assert order == ['btn-update', 'topbar-user-name', 'settings-menu', 'conn'], order
        assert page.locator('#settings-menu #btn-restart').count() == 1
        button.click()
        page.get_by_role('button', name='取消', exact=True).click()
        page.wait_for_function('!document.querySelector("#btn-restart").disabled')
        assert state['posts'] == 0
        page.locator('#settings-menu summary').click()
        button.click()
        assert '120 秒' in page.locator('#app-alert-message').inner_text()
        page.get_by_role('button', name='确定', exact=True).click()
        page.wait_for_function('S.restarting && document.querySelector("#btn-restart").disabled')
        page.wait_for_function('document.querySelector("#conn-text").textContent.includes("等待 Bot")')
        assert state['posts'] == 1
        state['phase'] = 'terminating'
        page.wait_for_function('document.querySelector("#conn-text").textContent.includes("正在停止 Bot")')
        page.reload()
        page.wait_for_function('S.restarting && document.querySelector("#btn-restart").disabled')
        assert state['posts'] == 1  # Reload only resumes GET polling.
        state['phase'] = 'done'
        page.wait_for_timeout(1200)
        assert page.evaluate('S.restarting')  # Same old health instance cannot pass.
        state['health_id'] = 'new'
        page.wait_for_function('document.querySelector("#app-alert-message")?.textContent === "服务已重启"')
        assert page.evaluate('sessionStorage.getItem("ge_service_restart")') is None
        page.get_by_role('button', name='确定', exact=True).click()
        assert button.is_enabled()
        state['fail'] = True
        page.locator('#settings-menu summary').click()
        button.click()
        page.get_by_role('button', name='确定', exact=True).click()
        page.wait_for_function('document.querySelector("#app-alert-message")?.textContent.includes("启动失败")')
        assert state['posts'] == 2
        page.get_by_role('button', name='确定', exact=True).click()
        screenshot = ROOT / 'cache' / 'service-restart-topbar.png'
        screenshot.parent.mkdir(exist_ok=True)
        page.locator('#topbar').screenshot(path=str(screenshot))
        page.evaluate('logout()')
        assert button.is_hidden()
        browser.close()
    print('PASS: button order, confirmation/cancel, stop progress, reload without duplicate POST, new-instance gate, failure, login visibility')


if __name__ == '__main__':
    cache = ROOT / 'cache'
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='restart-browser-', dir=cache) as temp, \
         patch.dict(os.environ, {'TEMP': temp, 'TMP': temp}):
        run()
