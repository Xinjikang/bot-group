"""Chromium UI with an isolated real display-name API and SQLite database."""
import mimetypes
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from scripts.test_user_display_name import UserNameAPI

ROOT = Path(__file__).resolve().parents[1]


def main():
    errors, writes = [], []
    state = {'fail_save': False, 'fail_read': False}
    with UserNameAPI() as fixture, sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)

        def route(req):
            path = urlparse(req.request.url).path
            if path.startswith('/app/'):
                file = ROOT / 'frontend' / (path[5:] or 'index.html')
                req.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
                return
            if path in ('/user/display-name', '/msg'):
                if req.request.method == 'PATCH':
                    writes.append(req.request.post_data_json)
                    if state['fail_save']:
                        req.fulfill(status=503, json={'detail': 'Save unavailable'})
                        return
                elif state['fail_read'] and path == '/user/display-name':
                    req.fulfill(status=503, json={'detail': 'Read unavailable'})
                    return
                response = fixture.client.request(req.request.method, path,
                    content=req.request.post_data_buffer, headers={'content-type': 'application/json'})
                req.fulfill(status=response.status_code, body=response.content, headers=dict(response.headers))
                return
            data = {}
            if path == '/auth/status':
                data = {'configured': False}
            elif path == '/projects':
                data = {'projects': [{'name': p, 'display_name': p, 'root': 'E:/sample'} for p in ('p', 'q')]}
            elif path == '/members':
                data = {'members': [{'profile': 'bot-a', 'display_name': '用户', 'active': 1, 'role': 'developer'}]}
            elif path == '/messages':
                data = {'messages': [dict(id=1, project='p', sender='user', sender_type='user',
                    content='Sample @user', mentions='["user"]', ts='2026-10-20T10:00:00', thread_id=None)]}
            elif path == '/threads':
                data = {'threads': []}
            elif path == '/tasks':
                data = {'tasks': [], 'requirements': [], 'pending_approval': []}
            elif path == '/appearance/background':
                req.fulfill(status=204, headers={'X-Background-Configured': 'true'})
                return
            req.fulfill(json=data)

        def open_page(context):
            context.route('**/*', route)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto('http://user-name.test/app/index.html')
            page.locator('#topbar-user-name').wait_for()
            page.locator('#settings-menu summary').click()
            page.locator('.sender').first.wait_for()
            return page

        context = browser.new_context(viewport={'width': 1280, 'height': 800})
        page = open_page(context)
        assert page.locator('.sender').first.inner_text() == '用户'
        assert page.locator('#topbar-right #btn-user-name').count() == 1
        page.locator('#btn-user-name').click()
        field = page.locator('#user-name-input')
        page.wait_for_function('!document.querySelector("#user-name-input").disabled')
        assert field.input_value() == '用户'
        field.fill('   ')
        page.locator('#user-name-save').click()
        assert not writes
        name = '🌟 <b>Shared $&</b>'
        field.fill(name)
        field.dispatch_event('keydown', {'key': 'Enter', 'isComposing': True})
        assert not writes
        page.locator('#user-name-save').click()
        page.locator('#dlg-user-name').wait_for(state='hidden')
        assert page.locator('.sender').first.inner_text() == name
        assert page.locator('.sender b').count() == 0
        assert page.locator('.avatar.av-user').first.inner_text() == '🌟'
        assert page.locator('.mention').first.inner_text() == '@' + name
        assert page.evaluate('NAME_OF("user")') == name
        assert page.evaluate('AV_CLASS("user")') == 'av-user'
        assert page.evaluate('NAME_OF("bot-a")') == '用户'
        assert not page.evaluate('n => [...Object.values(localStorage), ...Object.values(sessionStorage)].some(v => v.includes(n))', name)
        page.reload()
        page.wait_for_function('name => S.userDisplayName === name', arg=name)
        assert page.locator('.sender').first.inner_text() == name
        page.evaluate('selectProject("q")')
        assert page.evaluate('NAME_OF("user")') == name

        fresh_context = browser.new_context(viewport={'width': 390, 'height': 844})
        fresh_page = open_page(fresh_context)
        assert fresh_page.locator('.sender').first.inner_text() == name
        button_rect = fresh_page.locator('#btn-user-name').bounding_box()
        assert button_rect['x'] >= 0 and button_rect['x'] + button_rect['width'] <= 390
        fresh_page.locator('#btn-user-name').click()
        fresh_field = fresh_page.locator('#user-name-input')
        fresh_page.wait_for_function('!document.querySelector("#user-name-input").disabled')
        rect = fresh_page.locator('#dlg-user-name').bounding_box()
        assert rect['x'] >= 0 and rect['x'] + rect['width'] <= 390
        fresh_field.fill('Cancelled')
        fresh_field.press('Escape')
        assert len(writes) == 1
        fresh_page.locator('#settings-menu summary').click()
        fresh_page.locator('#btn-user-name').click()
        fresh_page.wait_for_function('!document.querySelector("#user-name-input").disabled')
        fresh_field.fill('Retry name')
        state['fail_save'] = True
        fresh_page.locator('#user-name-save').click()
        fresh_page.wait_for_function('document.querySelector("#user-name-status").dataset.error === "true"')
        assert fresh_field.input_value() == 'Retry name'
        assert fixture.client.get('/user/display-name').json()['display_name'] == name
        state['fail_save'] = False
        fresh_page.locator('#user-name-save').click()
        fresh_page.locator('#dlg-user-name').wait_for(state='hidden')
        assert fixture.client.get('/user/display-name').json()['display_name'] == 'Retry name'
        page.evaluate('handleEvent({type:"user.display_name.changed"})')
        page.wait_for_function('S.userDisplayName === "Retry name"')

        state['fail_read'] = True
        fresh_page.locator('#settings-menu summary').click()
        fresh_page.locator('#btn-user-name').click()
        fresh_page.wait_for_function('document.querySelector("#user-name-status").dataset.error === "true"')
        assert fresh_page.locator('#user-name-save').is_disabled()
        fresh_page.locator('#user-name-cancel').click()
        state['fail_read'] = False
        fresh_page.reload()
        fresh_page.wait_for_function('S.userDisplayName === "Retry name"')
        assert len(writes) == 3
        fresh_page.evaluate('logout()')
        assert fresh_page.locator('#btn-user-name').is_hidden()
        assert not errors, errors
        browser.close()
    print('User display name browser checks passed: persistence, fresh context, identity, validation, failure recovery, mobile layout')


if __name__ == '__main__':
    main()
