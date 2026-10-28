"""Real Chrome UI, synthetic HTTP only: ordering, receipt polling and reload."""
import json
import argparse
import mimetypes
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def run(evidence):
    state = {'posts': 0, 'phase': 'draining', 'health_id': 'old', 'fail': False, 'loads': 0}
    receipt = {'id': 'operation', 'instance_id': 'old', 'program_root': 'isolated', 'db_path': 'isolated.db'}
    def route(request):
        path = urlparse(request.request.url).path
        if path.startswith('/app/'):
            if path == '/app/':
                state['loads'] += 1
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
        page.locator('#btn-settings').click()
        button.wait_for(state='visible')
        order = page.locator('#app-settings-menu').evaluate('(e) => [...e.children].map(c => c.id)')
        assert order == ['btn-user-name', 'btn-background', 'btn-theme-top', 'btn-restart', 'btn-about'], order
        button.click()
        page.get_by_role('button', name='取消', exact=True).click()
        page.wait_for_function('!document.querySelector("#btn-restart").disabled')
        assert state['posts'] == 0
        page.locator('#btn-settings').click()
        button.click()
        assert '120 秒' in page.locator('#app-alert-message').inner_text()
        assert 'Hermes 网关中的执行会继续运行' in page.locator('#app-alert-message').inner_text()
        assert 'Codex 等本地执行' in page.locator('#app-alert-message').inner_text()
        assert '服务启动后接回原执行' in page.locator('#app-alert-message').inner_text()
        page.get_by_role('button', name='确定', exact=True).click()
        page.wait_for_function('S.restarting && document.querySelector("#btn-restart").disabled')
        page.wait_for_function('document.querySelector("#conn-text").textContent.includes("收尾本地执行")')
        assert state['posts'] == 1
        state['phase'] = 'terminating'
        page.wait_for_function('document.querySelector("#conn-text").textContent.includes("正在停止本地执行")')
        page.reload()
        page.wait_for_function('S.restarting && document.querySelector("#btn-restart").disabled')
        assert state['posts'] == 1  # Reload only resumes GET polling.
        state['phase'] = 'done'
        page.wait_for_timeout(1200)
        assert page.evaluate('S.restarting')  # Same old health instance cannot pass.
        loads = state['loads']
        state['health_id'] = 'new'
        page.wait_for_event('load')
        page.wait_for_function('S.isUserIdentity && !document.querySelector("#btn-restart").disabled')
        assert state['loads'] == loads + 1
        assert page.evaluate('sessionStorage.getItem("ge_service_restart")') is None
        assert button.is_enabled()
        state['fail'] = True
        page.locator('#btn-settings').click()
        button.click()
        page.get_by_role('button', name='确定', exact=True).click()
        page.wait_for_function('document.querySelector("#app-alert-message")?.textContent.includes("启动失败")')
        assert '重启失败' in page.locator('#app-alert-message').inner_text()
        assert 'service-restart.log' in page.locator('#app-alert-message').inner_text()
        assert state['posts'] == 2
        assert state['loads'] == loads + 1
        page.get_by_role('button', name='确定', exact=True).click()
        screenshot = evidence / 'service-restart-topbar.png'
        page.locator('#topbar').screenshot(path=str(screenshot))
        # 超时：沿用回执恢复监控（不重复 POST），到点给出明确的超时结果并保留回执。
        # 用可控时钟把 5 分钟窗口跳到过期，避免真等 300 秒。
        page.add_init_script("""(() => {
            const real = Date.now.bind(Date);
            window.__clockSkew = 0;
            Date.now = () => real() + window.__clockSkew;
        })();""")
        state['fail'] = False
        state['health_id'] = 'old'
        page.evaluate("""() => sessionStorage.setItem('ge_service_restart',
            JSON.stringify({id: 'operation', instance_id: 'old'}))""")
        page.reload()
        page.wait_for_function('S.restarting === true')
        page.evaluate('window.__clockSkew = 400000')
        page.wait_for_function('document.querySelector("#app-alert")?.open === true')
        timeout_text = page.locator('#app-alert-message').inner_text()
        assert '等待重启结果超时' in timeout_text and '5 分钟' in timeout_text, timeout_text
        assert '刷新页面可继续核对回执' in timeout_text, timeout_text
        assert state['posts'] == 2
        page.get_by_role('button', name='确定', exact=True).click()
        assert page.evaluate("sessionStorage.getItem('ge_service_restart')") is not None
        page.evaluate("sessionStorage.removeItem('ge_service_restart')")
        page.evaluate('logout()')
        assert button.is_hidden()
        browser.close()
    print('PASS: button order, confirmation/cancel, stop progress, reload recovery, new-instance gate, '
          'automatic reload once, failure without reload, wait timeout with receipt kept, login visibility')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='restart-browser-') as temp, \
         patch.dict(os.environ, {'TEMP': temp, 'TMP': temp}):
        run(args.evidence)
