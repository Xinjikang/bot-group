"""Exercise the real member dialog in Chrome with synthetic APIs only."""
import argparse
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, default=ROOT / 'reports' / 'tests' / 'profile-create')
    out = parser.parse_args().evidence
    out.mkdir(parents=True, exist_ok=True)
    calls, errors, pending, joins = [], [], [], []
    resource_calls = []
    state = {'fail': True, 'delay': False, 'empty': False, 'hold': False, 'extra': False}
    notice = {'code': 'unspecified', 'message': '沙箱配置待确认：该 Bot 未显式指定 Windows 沙箱实现，实际启用情况取决于 Codex 版本和默认设置，加入后可能出现命令执行失败。建议先检查配置，或使用本页的“创建新 profile”准备独立 Bot 并核对配置。你也可以继续加入已有 Bot。'}
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
                    {'host_type': 'codex', 'profile': 'other', 'config_root': 'E:/test/codex/profiles/other', 'sandbox_notice': notice,
                     'approval_notice': {'code': 'not_enabled', 'message': '智能审批提醒：该 Bot 未显式启用可用于终端命令的“帮我审批”，遇到需要审批的操作时可能无法继续。建议在该 Bot 的 Codex 配置中开启“帮我审批”。建议先检查配置，或使用本页的“创建新 profile”准备独立 Bot 并核对配置。你也可以继续加入已有 Bot。'}},
                    {'host_type': 'hermes', 'profile': 'default', 'config_root': 'E:/test/hermes'}]}
                if state['extra']:
                    data['candidates'].append({'host_type': 'codex', 'profile': 'approval-only', 'config_root': 'E:/test/codex/profiles/approval-only',
                                               'approval_notice': {'code': 'not_enabled', 'message': '智能审批提醒：请开启帮我审批。你也可以继续加入已有 Bot。'}})
                    data['candidates'].append({'host_type': 'codex', 'profile': 'ready', 'config_root': 'E:/test/codex/profiles/ready', 'sandbox_notice': None})
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
            elif path == '/projects/import/prepare':
                req.fulfill(status=400, json={'detail': '请使用本地盘普通绝对路径'})
                return
            elif path.endswith('/resources'):
                if req.request.method == 'PUT':
                    resource_calls.append(req.request.post_data_json)
                data = {'path': 'E:/test', 'members': ['bot-7'], 'revision': 'r1', 'config': {
                    'services': {'web': {'port': 18080, 'host': '127.0.0.1'}},
                    'browser': None, 'test_data_directory': 'E:/test/reports/data'}}
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
        profile = page.locator('#member-create-profile')
        for invalid in ('测试22', 'Upper', '_worker', 'default', 'con', 'profiles'):
            profile.fill(invalid)
            assert profile.get_attribute('aria-invalid') == 'true'
            assert page.locator('.dialog-field-error').count() == 1
            assert page.locator('#member-add-join').is_disabled()
        profile.fill('测试22')
        error = page.locator('.dialog-field-error')
        assert '小写英文字母' in error.inner_text()
        assert error.evaluate("el => { const probe=document.createElement('span'); probe.style.color='var(--danger)'; el.append(probe); const same=getComputedStyle(el).color===getComputedStyle(probe).color; probe.remove(); return same; }")
        page.screenshot(path=str(out / 'invalid-profile-desktop.png'))
        page.set_viewport_size({'width': 390, 'height': 844})
        page.evaluate("applyTheme('light')")
        assert error.evaluate("el => { const probe=document.createElement('span'); probe.style.color='var(--danger)'; el.append(probe); const same=getComputedStyle(el).color===getComputedStyle(probe).color; probe.remove(); return same; }")
        page.screenshot(path=str(out / 'invalid-profile-mobile-light.png'))
        page.locator('[name=member-add-mode][value=existing]').check()
        assert page.locator('.dialog-field-error').count() == 0
        page.locator('[name=member-add-mode][value=create]').check()
        assert page.locator('.dialog-field-error').count() == 1
        profile.fill('')
        assert '请填写' in page.locator('.dialog-field-error').inner_text()
        page.evaluate("applyTheme('dark')")
        page.set_viewport_size({'width': 1280, 'height': 900})
        page.locator('#member-create-profile').fill('new-dev')
        assert profile.get_attribute('aria-invalid') is None
        assert profile.get_attribute('aria-describedby') == 'member-create-profile-help'
        assert page.locator('.dialog-field-error').count() == 0
        page.locator('.member-create-fields summary').click()
        page.locator('#member-create-model').fill('chosen-model')
        page.locator('#member-create-instructions').fill('认真核对证据。')
        page.locator('#member-add-join').click()
        page.wait_for_function("document.querySelector('#member-add-status').textContent.includes('同名')")
        assert page.locator('#member-create-name').input_value() == '小夏'
        assert page.locator('#member-create-instructions').input_value() == '认真核对证据。'
        assert page.locator('#member-create-instructions').is_visible()
        assert not page.locator('#member-add-close').is_disabled()
        assert page.locator('#member-add-status').get_attribute('data-error') == 'true'
        assert page.locator('#member-add-status').evaluate("el => { const probe=document.createElement('span'); probe.style.color='var(--danger)'; el.append(probe); const same=getComputedStyle(el).color===getComputedStyle(probe).color; probe.remove(); return same; }")
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
        state.update(empty=False, extra=True)
        page.evaluate('openMemberAdd()')
        page.locator('[name=member-add-host][value=codex]').check()
        page.locator('#member-add-next').click()
        page.locator('[name=member-add-profile]').first.check()
        assert '继续加入' in page.locator('#member-add-sandbox-notice').inner_text()
        assert '帮我审批' in page.locator('#member-add-approval-notice').inner_text()
        assert not page.locator('#member-add-join').is_disabled()
        join_box = page.locator('#member-add-join').bounding_box()
        assert join_box and join_box['y'] >= 0 and join_box['y'] + join_box['height'] <= page.viewport_size['height']
        page.screenshot(path=str(out / 'sandbox-notice-mobile.png'))
        page.locator('[name=member-add-profile]').nth(1).check()
        assert page.locator('#member-add-sandbox-notice').count() == 0
        assert page.locator('#member-add-approval-notice').is_visible()
        assert not page.locator('#member-add-join').is_disabled()
        page.locator('[name=member-add-profile]').last.check()
        assert page.locator('#member-add-sandbox-notice').count() == 0
        assert page.locator('#member-add-approval-notice').count() == 0
        page.locator('[name=member-add-profile]').first.check()
        assert page.locator('#member-add-sandbox-notice').is_visible()
        assert page.locator('#member-add-approval-notice').is_visible()
        page.locator('#member-add-join').click()
        page.wait_for_function("!document.querySelector('#dlg-member-add').open")
        assert joins == [{'project': 'p', 'host_type': 'codex', 'profile': 'other',
                          'config_root': 'E:/test/codex/profiles/other'}]
        page.reload()
        page.evaluate("() => { showLogin(false); S.projects=[{name:'p',display_name:'测试项目',root:'E:/test',directory_mode:'shared'}]; openProjectDetail('p', $('#chip-all')); selectProjectTab('members'); }")
        page.locator('#pj-members-add').click()
        page.locator('[name=member-add-host][value=codex]').check()
        page.locator('#member-add-next').click()
        page.locator('[name=member-add-profile]').first.check()
        assert page.locator('#member-add-sandbox-notice').is_visible()
        assert page.locator('#member-add-approval-notice').is_visible()
        page.locator('[name=member-add-mode][value=create]').check()
        assert page.locator('#member-add-sandbox-notice').count() == 0
        assert page.locator('#member-add-approval-notice').count() == 0
        page.locator('#member-create-source').select_option('')
        assert page.locator('#member-create-auth').count() == 0
        assert '主账户认证' in page.locator('#member-create-auth-help').inner_text()
        assert '智能审批（帮我审批）和沙箱' in page.locator('#member-create-auth-help').inner_text()
        assert page.locator('.dialog-field-error').count() == 0
        page.locator('#member-add-close').click()
        page.evaluate('openProjectImport()')
        page.locator('#import-preview').click()
        assert page.locator('#import-root').get_attribute('aria-invalid') == 'true'
        page.locator('#import-root').fill('relative')
        page.locator('#import-preview').click()
        page.wait_for_function("document.querySelector('#import-status').dataset.error === 'true'")
        assert page.locator('#import-status').evaluate("el => { const probe=document.createElement('span'); probe.style.color='var(--danger)'; el.append(probe); const same=getComputedStyle(el).color===getComputedStyle(probe).color; probe.remove(); return same; }")
        page.locator('#import-close').click()
        page.evaluate("openWorkspaceResources('p', 'bot-7')")
        port = page.get_by_label('服务端口', exact=True)
        port.fill('80')
        assert port.get_attribute('aria-invalid') == 'true'
        assert '1024' in page.locator('.dialog-field-error').inner_text()
        page.get_by_role('button', name='保存资源配置', exact=True).click()
        assert resource_calls == []
        port.fill('18080.5')
        assert '整数' in page.locator('.dialog-field-error').inner_text()
        port.fill('18081')
        assert port.get_attribute('aria-invalid') is None
        page.get_by_role('button', name='保存资源配置', exact=True).click()
        page.wait_for_function("[...document.querySelectorAll('dialog[open] [role=status]')].some(el => el.textContent.includes('已保存'))")
        assert len(resource_calls) == 1
        assert page.locator('.dialog-field-error').count() == 0
        assert not errors, errors
        browser.close()
    print('PASS: profile creation, inline field errors, sandbox advice with unrestricted joining, reload, themes, narrow layout and resource validation')


if __name__ == '__main__':
    main()
