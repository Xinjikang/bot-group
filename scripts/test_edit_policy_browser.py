"""Isolated Chrome coverage for global edit-policy settings and asynchronous states."""
import argparse
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


def run(evidence):
    state = dict(policy='workspace_session', writes=[], fail_read=False, fail_save=False, hold=False, held=None)
    errors = []

    def route(req):
        path = urlparse(req.request.url).path
        if path.startswith('/app/'):
            file = ROOT / 'frontend' / (path[5:] or 'index.html')
            req.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
            return
        if path == '/system/edit-approval-policy':
            if req.request.method == 'PATCH':
                state['writes'].append(req.request.post_data_json)
                if state['fail_save']:
                    req.fulfill(status=503, json={'detail': '测试保存失败'})
                    return
                if state['hold']:
                    state['held'] = req
                    return
                state['policy'] = req.request.post_data_json['policy']
            elif state['fail_read']:
                req.fulfill(status=503, json={'detail': '测试读取失败'})
                return
            req.fulfill(json={'policy': state['policy']})
            return
        if path == '/appearance/background':
            req.fulfill(status=204)
            return
        if path.startswith('/hud'):
            req.fulfill(status=503, json={'detail': '隔离测试未启用项目助手'})
            return
        data = {}
        if path == '/auth/status': data = {'configured': False}
        if path == '/user/display-name': data = {'profile': 'user', 'display_name': '用户'}
        if path in ('/projects', '/members', '/messages', '/threads'): data = {path[1:]: []}
        if path == '/tasks': data = {'tasks': [], 'requirements': [], 'pending_approval': []}
        req.fulfill(json=data)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        context.route('**/*', route)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://edit-policy.test/app/')

        def open_dialog(target=page):
            target.locator('#btn-settings').click()
            target.locator('#btn-edit-policy').click()
            expect(target.locator('#dlg-edit-policy')).to_be_visible()

        def selected(policy, target=page):
            expect(target.locator(f'input[name=policy][value="{policy}"]')).to_be_checked()

        open_dialog()
        selected('workspace_session')
        assert 'Codex' in page.locator('#edit-policy-scope').inner_text()
        expect(page.locator('#app-settings-menu')).to_be_hidden()
        page.locator('input[value=session]').check()
        page.locator('#edit-policy-cancel').click()
        assert state['writes'] == []
        open_dialog()
        selected('workspace_session')
        for policy in ('ask', 'session', 'workspace_session'):
            page.locator(f'input[value="{policy}"]').check()
            page.locator('#edit-policy-save').click()
            expect(page.locator('#edit-policy-status')).to_contain_text('已保存')
            assert state['policy'] == policy
        page.locator('#edit-policy-cancel').click()
        page.reload()
        open_dialog()
        selected('workspace_session')

        state['policy'] = 'ask'
        other = context.new_page()
        other.goto('http://edit-policy.test/app/')
        open_dialog(other)
        selected('ask', other)
        other.close()
        page.locator('#edit-policy-cancel').click()
        page.evaluate("S.currentProject='another-project'")
        open_dialog()
        selected('ask')

        state['fail_save'] = True
        page.locator('input[value=session]').check()
        page.locator('#edit-policy-save').click()
        expect(page.locator('#edit-policy-status')).to_contain_text('测试保存失败')
        selected('session')
        expect(page.locator('#edit-policy-save')).to_be_enabled()
        assert state['policy'] == 'ask'
        state['fail_save'] = False
        state['hold'] = True
        before = len(state['writes'])
        page.locator('#edit-policy-save').click()
        expect(page.locator('#edit-policy-save')).to_be_disabled()
        expect(page.locator('input[value=session]')).to_be_disabled()
        page.locator('#edit-policy-form').evaluate('form => form.requestSubmit()')
        page.wait_for_timeout(100)
        assert len(state['writes']) == before + 1
        state['policy'] = 'session'
        state['held'].fulfill(json={'policy': 'session'})
        state['hold'] = False
        expect(page.locator('#edit-policy-status')).to_contain_text('已保存')

        for width, theme in ((1440, 'dark'), (1440, 'light'), (390, 'dark'), (390, 'light')):
            page.locator('#edit-policy-cancel').click()
            page.set_viewport_size({'width': width, 'height': 844})
            page.evaluate('applyTheme', theme)
            open_dialog()
            selected('session')
            for selector in ('#dlg-edit-policy', '#edit-policy-save', '#edit-policy-cancel'):
                rect = page.locator(selector).bounding_box()
                assert 0 <= rect['x'] and rect['x'] + rect['width'] <= width + 1, (selector, rect)
                assert 0 <= rect['y'] and rect['y'] + rect['height'] <= 845, (selector, rect)
            page.screenshot(path=str(evidence / f'edit-policy-{width}-{theme}.png'))
        page.keyboard.press('Escape')
        expect(page.locator('#dlg-edit-policy')).to_be_hidden()
        expect(page.locator('#btn-settings')).to_be_focused()
        state['fail_read'] = True
        open_dialog()
        expect(page.locator('#edit-policy-status')).to_contain_text('测试读取失败')
        expect(page.locator('#edit-policy-save')).to_be_disabled()
        page.locator('#edit-policy-cancel').click()
        state['fail_read'] = False
        open_dialog()
        selected('session')
        page.evaluate("S.isUserIdentity=false; document.dispatchEvent(new Event('service-auth-changed'))")
        expect(page.locator('#dlg-edit-policy')).to_be_hidden()
        expect(page.locator('#btn-edit-policy')).to_be_hidden()
        assert not errors, errors
        browser.close()
    (evidence / 'result.json').write_text(json.dumps({'passed': True, 'writes': state['writes']}, ensure_ascii=False), encoding='utf-8')
    print('PASS policy selection, global refresh, failure/retry, duplicate guard, auth, keyboard and desktop/mobile themes')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    args.evidence.mkdir(parents=True, exist_ok=True)
    run(args.evidence)
