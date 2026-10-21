"""Isolated Chrome: project menu remains usable while refresh requests are pending."""
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        def route(request):
            path = urlparse(request.request.url).path
            if path.startswith('/app/'):
                file = ROOT / 'frontend' / (path[5:] or 'index.html')
                request.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
            elif path == '/auth/status':
                # req#123 task#216 node 1104 严格三态后：menu 测试模拟「已进应用」场景，探测须返回免密
                # （configured=false → boot 直进），否则兜底 {} 判探测失败恒弹登录、#layout 永不可见。
                request.fulfill(json={'configured': False})
            else:
                request.fulfill(json={})

        page.route('**/*', route)
        page.goto('https://workspace.test/app/index.html')
        # req#123 task#216：免密（configure 未知，fulfill({})→configured falsy）时前端会自动
        # boot——测试必须先等 boot 完成再布状态，否则 boot 的 loadProjects/restoreCurrentProject
        # 异步回流会覆盖 evaluate 里设置的 S.projects，下拉行被清空导致 inner_text 超时。
        page.wait_for_function("document.querySelector('#layout').style.display !== 'none'", timeout=10000)
        page.evaluate('''() => {
            showLogin(false);
            S.projects = [{name:'active', display_name:'Active project', archived:0}];
            S.currentProject = 'active'; renderProjectBar();
            window.menuRequests = [];
            const original = api;
            api = (path, options) => path.startsWith('/projects')
                ? new Promise((resolve, reject) => menuRequests.push({path, resolve, reject}))
                : original(path, options);
        }''')

        def settle(index, projects=None, fail=False):
            page.evaluate('''async ({index, projects, fail}) => {
                const r = menuRequests[index];
                if (fail) r.reject(new Error('offline'));
                else r.resolve({projects});
                await Promise.resolve();
            }''', {'index': index, 'projects': projects or [], 'fail': fail})

        # The refresh remains unresolved: rows and all controls must already work.
        page.locator('#chip-all').click()
        assert page.locator('.pj-drop-row').inner_text().startswith('Active project')
        page.locator('#pj-drop-toggle-archived').click()
        assert '正在加载' in page.locator('#pj-drop-list').inner_text()
        page.locator('#pj-drop-toggle-archived').click()
        assert page.locator('.pj-drop-row').inner_text().startswith('Active project')
        settle(2, [{'name': 'new', 'display_name': 'Fresh project'}])
        settle(1, [{'name': 'archive', 'archived': 1}])
        settle(0, [{'name': 'stale'}])
        assert page.locator('.pj-drop-row').inner_text().startswith('Fresh project')

        # Closing/reopening during a pending request isolates the new menu.
        page.locator('#chip-all').click()
        page.locator('#chip-all').click()
        page.locator('#pj-drop-add').click()
        assert page.locator('#dlg-project-entry').is_visible()
        page.locator('#project-entry-close').click()
        page.locator('#chip-all').click()
        settle(3, [{'name': 'closed-menu'}])
        assert page.locator('.pj-drop-row').inner_text().startswith('Fresh project')
        settle(4, fail=True)
        assert page.locator('.pj-drop-row').inner_text().startswith('Fresh project')
        page.locator('.pd-retry').click()
        settle(5, [{'name': 'recovered'}])
        assert page.locator('.pj-drop-row').inner_text().startswith('recovered')
        assert page.locator('.pd-retry').count() == 0

        # A first load without a snapshot distinguishes pending, failed and empty.
        page.evaluate('closeProjectDrop(); S.projects = []')
        page.set_viewport_size({'width': 390, 'height': 844})
        page.locator('#chip-all').click()
        assert '正在加载' in page.locator('#pj-drop-list').inner_text()
        settle(6, fail=True)
        assert '加载失败' in page.locator('#pj-drop-list').inner_text()
        assert '暂无项目' not in page.locator('#pj-drop-list').inner_text()
        page.locator('.pd-retry').click()
        settle(7)
        assert '暂无项目' in page.locator('#pj-drop-list').inner_text()
        assert not errors, errors
        browser.close()
    print('PASS: cached rows and controls before response, request ordering, close/reopen, retry, empty, 390px')


if __name__ == '__main__':
    main()
