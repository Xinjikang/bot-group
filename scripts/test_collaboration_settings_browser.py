"""Chrome coverage for settings, timed updates and the isolated project wizard."""
import argparse
from datetime import datetime, timedelta
import json
import mimetypes
import os
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright, expect
from scripts.test_project_import import ImportTests

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence', type=Path, required=True)
    output = parser.parse_args().evidence.resolve()
    output.mkdir(parents=True, exist_ok=True)
    fixture = ImportTests()
    fixture.setUp()
    errors, calls = [], []
    state = {'update': True, 'offline': False}
    try:
        with patch.dict(os.environ, {'CODEX_HOME': str(fixture.base / 'new-codex-home')}), sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            page = browser.new_page(viewport={'width': 1280, 'height': 900})
            page.clock.install()
            page.on('pageerror', lambda error: errors.append(str(error)))

            def route(request):
                req = request.request
                path = urlparse(req.url).path
                if path.startswith('/app/'):
                    file = ROOT / 'frontend' / (path[5:] or 'index.html')
                    request.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'text/plain')
                    return
                calls.append(path)
                if path == '/system/update-check':
                    request.fulfill(json={'current_version': 'v1.9.0', 'latest_version': 'v1.10.0',
                                          'update_available': state['update'], 'status': 'unavailable' if state['offline'] else 'ok'})
                elif path == '/system/version':
                    request.fulfill(json={'current_version': 'v1.9.0', 'version_known': True})
                elif path == '/auth/status':
                    request.fulfill(json={'configured': False})
                elif path == '/events':
                    request.fulfill(body='', content_type='text/event-stream')
                elif (path in ('/projects', '/members', '/profiles/create', '/discovery/scan', '/user/display-name')
                      or path.startswith(('/projects/', '/members/'))):
                    response = fixture.client.request(req.method, req.url,
                        json=req.post_data_json if req.method in ('POST', 'PUT', 'PATCH') else None)
                    request.fulfill(status=response.status_code, json=response.json())
                else:
                    request.fulfill(json={'messages': [], 'threads': [], 'tasks': [], 'requirements': [], 'pending_approval': []})

            page.route('**/*', route)
            page.goto('http://workspace.test/app/index.html')
            expect(page.locator('#btn-update')).to_be_visible()
            assert calls.count('/system/update-check') == 1
            assert page.locator('#topbar-right').evaluate("n => n.children[0].id === 'btn-update' && n.children[1].id === 'topbar-user-name'")
            expect(page.locator('#btn-restart')).not_to_be_visible()
            page.locator('#settings-menu summary').click()
            for selector in ('#btn-user-name', '#btn-background', '#btn-theme-top', '#btn-restart', '#btn-about'):
                expect(page.locator('#settings-menu ' + selector)).to_be_visible()
            page.locator('#btn-about').click()
            expect(page.locator('#about-version')).to_contain_text('v1.9.0')
            expect(page.locator('#about-update')).to_contain_text('v1.10.0')
            page.locator('#about-close').click()
            page.screenshot(path=str(output / 'header.png'))
            page.reload()
            expect(page.locator('#btn-update')).to_be_visible()
            assert calls.count('/system/update-check') == 2
            page.clock.fast_forward(12 * 60 * 60 * 1000 + 100)
            page.wait_for_function("document.querySelector('#btn-update').textContent.includes('v1.10.0')")
            page.wait_for_timeout(100)
            assert calls.count('/system/update-check') == 3, calls.count('/system/update-check')
            page.clock.set_system_time(datetime.now() + timedelta(days=2))
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            page.wait_for_timeout(100)
            assert calls.count('/system/update-check') == 4
            state['offline'] = True
            page.locator('#btn-update').click()
            page.locator('#about-check').click()
            expect(page.locator('#about-update')).to_contain_text('暂时无法')
            page.locator('#about-close').click()
            state.update(offline=False, update=False)
            page.locator('#settings-menu summary').click()
            page.locator('#btn-about').click()
            page.locator('#about-check').click()
            expect(page.locator('#about-update')).to_contain_text('已是最新')
            expect(page.locator('#btn-update')).not_to_be_visible()
            page.locator('#about-close').click()

            page.evaluate("openProjectImport({mode:'new'})")
            fresh = fixture.base / 'prototype-project'
            page.locator('#import-root').fill(str(fresh))
            page.locator('#import-template').select_option('product-prototype')
            page.locator('#import-preview').click()
            expect(page.locator('#import-config')).to_be_visible()
            page.locator('#import-next').click()
            removed = page.locator('#import-roles .import-card').filter(has_text='原型评审员')
            removed.get_by_role('button', name='移除角色').click()
            expect(page.locator('#app-alert')).to_be_visible()
            expect(page.locator('#app-alert-message')).to_contain_text('AGENTS.md')
            page.locator('#app-alert .primary').click()
            expect(removed).to_have_count(0)
            retained = page.locator('#import-roles .import-card').filter(has_text='产品经理')
            retained.locator('summary').click()
            retained.locator('input[type=checkbox]').first.click()
            expect(page.locator('#app-alert-message')).to_contain_text('权限变化')
            page.locator('#app-alert .primary').click()
            page.locator('#import-next').click()
            expect(page.locator('#import-agents-step')).to_be_visible()
            rules = page.locator('#import-agents-content').input_value()
            assert '原型评审员' in rules
            page.locator('#import-agents-reviewed').check()
            page.locator('#import-next').click()
            expect(page.locator('#import-member-step')).to_be_visible()
            page.locator('#import-create-profile').click()
            page.locator('[name=member-add-host][value=codex]').check()
            page.locator('#member-add-next').click()
            expect(page.locator('#member-create-profile')).to_be_visible()
            page.locator('#member-create-profile').fill('wizard-dev')
            page.locator('#member-create-name').fill('向导新成员')
            page.locator('#member-add-join').click()
            expect(page.locator('#dlg-member-add')).not_to_be_visible(timeout=20000)
            if page.locator('#app-alert').is_visible():
                page.locator('#app-alert .primary').click()
            row = page.locator('#import-members .import-member').filter(has_text='向导新成员')
            expect(row.locator('input[data-bot-id]')).to_be_checked()
            row.locator('select').select_option(label='产品经理')
            page.locator('#import-next').click()
            expect(page.locator('#import-resource-step')).to_be_visible()
            page.locator('#import-next').click()
            expect(page.locator('#import-review')).to_be_visible()
            page.locator('#import-submit').click()
            expect(page.locator('#dlg-project-import')).not_to_be_visible(timeout=30000)
            if page.locator('#app-alert').is_visible():
                page.locator('#app-alert .primary').click()
            assert fresh.joinpath('AGENTS.md').read_text(encoding='utf-8') == rules
            page.evaluate("openProjectDetail(S.currentProject, document.querySelector('#chip-all')); selectProjectTab('members')")
            switch = page.locator('#pj-member-list [role=switch]').first
            expect(switch).to_have_text('启用')
            switch.click()
            expect(page.locator('#pj-member-list [role=switch]').first).to_have_text('停用')
            project = page.evaluate("document.querySelector('#pj-detail-form').dataset.origName")
            page.reload()
            page.wait_for_function('S.projects.length > 0')
            page.evaluate("p => {openProjectDetail(p, document.querySelector('#chip-all')); selectProjectTab('members');}", project)
            expect(page.locator('#pj-member-list [role=switch]').first).to_have_attribute('aria-checked', 'false')
            page.locator('#pj-member-list [role=switch]').first.click()
            expect(page.locator('#pj-member-list [role=switch]').first).to_have_text('启用')
            for theme in ('dark', 'light'):
                page.evaluate("theme => { document.documentElement.dataset.theme = theme; }", theme)
                page.set_viewport_size({'width': 390, 'height': 844})
                page.screenshot(path=str(output / ('members-' + theme + '.png')))
                rect = page.locator('#dlg-project-detail').bounding_box()
                assert 0 <= rect['x'] and rect['x'] + rect['width'] <= 390
            assert not errors, errors
            browser.close()
            (output / 'result.json').write_text(json.dumps({'passed': True, 'update_checks': calls.count('/system/update-check'),
                'errors': errors, 'project': project}, ensure_ascii=False, indent=2), encoding='utf-8')
            print('PASS: settings, updates, reload, timer/resume, role warnings without save blocking, profile creation and member switches')
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
