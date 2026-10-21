"""req#101 project entry, Chromium + synthetic API, no production writes."""
import mimetypes
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    project = dict(name='stable-id', display_name='Existing <project>', root='E:/existing', readiness_status='ready')
    state = {'projects': [project], 'fail': False, 'posts': [], 'auth_mode': 'free', 'logins': [], 'status_calls': 0}
    with sync_playwright() as pw:
        # req#123 task#216 node 1104：Chrome 启动偶发 EPERM——重试两次再放弃，每态独立 browser。
        def launch_browser(attempts=2):
            for i in range(attempts):
                try:
                    return pw.chromium.launch(channel='chrome', headless=True)
                except Exception:
                    if i == attempts - 1:
                        raise
                    import time
                    time.sleep(2)
        browser = launch_browser()
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))

        # req#123 task#216 验收①：未设密码时初始入口探测 /auth/status 免密直进，不先弹登录。
        # 此段在 ① 独立 page 上完成（auth 三态各用新 page），随后 ② 段原有入口链在新 page 重放。

        def route(request):
            path = urlparse(request.request.url).path
            if path.startswith('/app/'):
                file = ROOT / 'frontend' / (path[5:] or 'index.html')
                request.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
                return
            data = {}
            if path == '/projects':
                if request.request.method == 'POST':
                    body = request.request.post_data_json
                    state['posts'].append(body)
                    state['projects'].append(dict(body, name='new-stable-id', readiness_status='ready'))
                    data = {'project': 'new-stable-id', 'root': body['root']}
                elif state['fail']:
                    request.fulfill(status=503, json={'detail': 'offline'})
                    return
                else:
                    data = {'projects': state['projects']}
            elif path == '/threads':
                data = {'threads': []}
            elif path == '/messages':
                data = {'messages': []}
            elif path == '/members':
                data = {'members': []}
            elif path == '/tasks':
                data = {'tasks': [], 'requirements': [], 'pending_approval': []}
            elif path == '/auth/status':
                # req#123 task#216 node 1104：status_calls 路由内递增——有 token 断言「不探测」
                # 才有证明力（非仅手工置 0）。
                state['status_calls'] += 1
                if state['auth_mode'] == 'netfail':
                    request.fulfill(status=503, json={'detail': 'engine offline'}); return
                if state['auth_mode'] == 'bad':
                    request.fulfill(json=state.get('bad_body') or {}); return
                request.fulfill(json={'configured': state['auth_mode'] == 'set'}); return
            elif path == '/auth/login':
                body = request.request.post_data_json or {}
                state['logins'].append(body.get('password'))
                if state['auth_mode'] == 'set' and body.get('password') == 'secret123':
                    request.fulfill(json={'token': 'tok-1'})
                else:
                    request.fulfill(status=401, json={'detail': '密码错误'})
                return
            request.fulfill(json=data)

        page.route('**/*', route)
        # req#123 task#216 验收①④：无 token+免密（configured=false）→ 探测后直接进应用，无登录阻挡；
        # 弹窗断言留给原链（pj-drop-add 打开后断言），此处只锁「免密直进+身份口径+零登录提交」。
        page.goto('http://workspace.test/app/index.html')
        page.wait_for_function("document.querySelector('#layout').style.display !== 'none'", timeout=10000)
        assert state['logins'] == [], '免密不应提交登录'
        assert page.evaluate('S.isUserIdentity') is True, 'dev 免密进应用按 _admin_only 口径算管理员身份'
        assert state['status_calls'] == 1, f"免密应先探测一次: {state['status_calls']}"
        # 回归载入点：原链在此之后显式渲染项目条，继续既有断言
        page.evaluate('p => { showLogin(false); S.projects=[p]; S.members=[]; renderProjectBar(); }', project)
        page.locator('#chip-all').click()
        page.locator('#pj-drop-add').click()
        page.locator('#dlg-project-entry').wait_for(state='visible')
        # req#108 #186：接入弹窗收窄为两入口，冗余「打开已登记项目」入口（按钮+展开列表）已移除
        assert page.locator('#project-entry-open').count() == 0, '「打开已登记项目」按钮应已移除'
        assert page.locator('#project-entry-projects').count() == 0, '展开快捷列表容器应已移除'
        assert page.locator('#project-entry-import').inner_text() == '首次导入已有项目'
        assert page.locator('#project-entry-new').is_enabled()
        assert page.locator('#project-entry-new').inner_text() == '新建项目（空白或场景模板）'
        # ②打开已登记项目改走「全部项目」chip-all 下拉：点行可切换
        page.locator('#dlg-project-entry').evaluate('d => d.close()')
        page.locator('#chip-all').click()
        page.locator('#pj-dropdown').wait_for(state='visible')
        rows = page.locator('.pj-drop-row')
        assert rows.count() == 1
        page.locator('.pj-drop-row .pd-name').click()
        page.wait_for_function("S.currentProject === 'stable-id'")
        assert state['posts'] == []

        page.evaluate('openProjectNew()')
        state['projects'] = []
        # req#108 #186：原「打开已登记项目」空/失败/重试断言随入口移除；导入/新建入口保留可用
        page.locator('#project-entry-import').click()
        assert page.locator('#dlg-project-import').is_visible()
        page.locator('#import-close').click()
        # Legacy pure-registration form remains compatible for existing callers.
        page.evaluate('openProjectDetail(null)')
        assert page.locator('#pj-detail-title').inner_text() == '登记已有项目'
        assert page.locator('#pj-registration-note').is_visible()
        page.locator('#pj-d-name').fill('Imported name')
        page.locator('#pj-d-root').fill('E:/existing/second')
        page.locator('#pj-detail-form button[type=submit]').click()
        page.wait_for_function("S.currentProject === 'new-stable-id'")
        assert len(state['posts']) == 1
        assert state['posts'][0]['directory_mode'] == 'shared'
        assert state['posts'][0]['root'] == 'E:/existing/second'

        page.set_viewport_size({'width': 390, 'height': 844})
        page.evaluate('openProjectNew()')
        assert page.locator('#dlg-project-entry').is_visible()
        assert page.evaluate("document.querySelector('#dlg-project-entry').scrollWidth <= document.querySelector('#dlg-project-entry').clientWidth")
        page.locator('#project-entry-close').click()
        page.evaluate("openProjectDetail('new-stable-id')")
        assert page.locator('#pj-readiness').inner_text() == '项目状态：已就绪'
        page.locator('#dlg-pj-detail-close').click()
        assert not errors, errors
        browser.close()

    # req#123 task#216 验收②③ + ④有 token 不探测：auth 三态各用独立 page
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        # ②已设密码：弹登录；错密码报错不进入；对密码登录成功进应用
        state['auth_mode'] = 'set'; state['logins'] = []; errors.clear()
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', route)
        page.goto('http://workspace.test/app/index.html')
        page.wait_for_function('!document.querySelector("#login").hidden', timeout=10000)
        assert page.evaluate("document.querySelector('#layout').style.display") == 'none'
        page.locator('#login-pwd').fill('wrong')
        page.locator('#login-form button[type=submit]').click()
        page.wait_for_timeout(300)
        assert '密码错误' in page.locator('#login-err').inner_text()
        assert page.evaluate("document.querySelector('#layout').style.display") == 'none'
        page.locator('#login-pwd').fill('secret123')
        page.locator('#login-form button[type=submit]').click()
        page.wait_for_function("S.token === 'tok-1'", timeout=10000)
        assert page.evaluate("document.querySelector('#layout').style.display != 'none'")
        page.close()
        # ③网络/认证错误：不误判免密——保持登录页并显示探测错误
        state['auth_mode'] = 'netfail'; state['logins'] = []; errors.clear()
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/*', route)
        page.goto('http://workspace.test/app/index.html')
        page.wait_for_function('!document.querySelector("#login").hidden', timeout=10000)
        txt = page.locator('#login-err').inner_text()
        assert '鉴权探测失败' in txt and 'engine offline' in txt, txt
        assert page.evaluate("document.querySelector('#layout').style.display") == 'none'
        assert state['logins'] == [], '网络错不应发起登录'
        page.close()
        # ④有 token：直接进应用，不探测不弹登录
        state['auth_mode'] = 'set'; state['logins'] = []; state['status_calls'] = 0; errors.clear()
        page = browser.new_page(viewport={'width': 1280, 'height': 900})
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.add_init_script("sessionStorage.setItem('ge_token', 'tok-pre')")
        page.route('**/*', route)
        page.goto('http://workspace.test/app/index.html')
        page.wait_for_function("document.querySelector('#layout').style.display !== 'none'", timeout=10000)
        page.wait_for_timeout(300)
        assert state['status_calls'] == 0, f"有 token 不应探测: {state['status_calls']}"
        assert state['logins'] == []
        page.close()
        assert not errors, errors
        browser.close()

    # node 1104 ⑤畸形响应段：configured 非 bool（undefined/null/字符串）→ 弹登录报探测失败、不误判免密
    with sync_playwright() as pw:
        for bad in ({}, {'configured': None}, {'configured': 'no'}, {'configured': 0}):
            state['auth_mode'] = 'bad'; state['logins'] = []; state['status_calls'] = 0
            state['bad_body'] = bad
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page(viewport={'width': 1280, 'height': 900})
            errors.clear()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.route('**/*', route)
            page.goto('http://workspace.test/app/index.html')
            page.wait_for_function('!document.querySelector("#login").hidden', timeout=10000)
            txt = page.locator('#login-err').inner_text()
            assert '鉴权探测失败' in txt, (bad, txt)
            assert page.evaluate("document.querySelector('#layout').style.display") == 'none', bad
            assert page.evaluate('S.isUserIdentity') is False, f'畸形响应不得设管理员标记: {bad}'
            assert state['logins'] == [], bad
            page.close()
            browser.close()
        print('PASS: entry paths (req#108 #186 two entries), stable ID opening, registration, readiness, 390px, req#123 #216 auth status probe (free/login/netfail/token/malformed/strict)')


if __name__ == '__main__':
    main()
